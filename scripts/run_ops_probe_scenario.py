"""一键把运维塔台大屏灌上**真实**数据（探针端到端演示场景）。

跟 `services/ops_probe_demo/` 配套：起一个模拟探针接进平台，然后把整条链路
真跑一遍——查询 → 分析 → 提议 → 审批 → 执行 → 复盘，跑完总览的 KPI、
告警关联时间线、审批卡片、事后复盘、四个效果指标全部有真实数据。

**为什么必须走对话（而不是脚本直接调工具）**：`analyze_ops_incident` 只注册给了
LLM，没有 HTTP 触发入口；而且探针的 WebSocket 连接活在**后端进程**里
（`app.py` 的 `active_ops_connector_ws`），脚本本地构造 `OpsToolset` 根本够不到它。
所以要产生分析摘要，只有让模型在对话里真的调用那个工具这一条路。
代价是这一步依赖 7B 模型愿意调工具，不是 100% 稳定——脚本会如实报告它调没调，
不会在没调用时假装成功。

⚠️ **默认不清理数据**（CLAUDE.md §7.2 多会话协作纪律）：跑完打印出连接器编号，
留给另一个会话复核，确认后再用 `--cleanup` 删。

用法：
    # 起一个后端（端口自选，别撞别人的）
    RAGENT_PORT=8044 .venv/bin/python -m src.ragent_backend.app

    # 跑场景
    .venv/bin/python scripts/run_ops_probe_scenario.py --api http://localhost:8044
    .venv/bin/python scripts/run_ops_probe_scenario.py --api http://localhost:8044 --cleanup
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.ops_probe_demo.fake_ops_data import points_for  # noqa: E402
from services.ops_probe_demo.probe import OpsProbe  # noqa: E402
from src.ops.analysis import Alert, correlate_alerts, detect_anomalies  # noqa: E402
from src.ops.types import DataPoint  # noqa: E402

TARGET_SERVICE = "order-service"
CONNECTOR_NAME = "演示探针（模拟客户环境）"


def _call(api: str, method: str, path: str, token: str, body: Optional[dict] = None) -> Any:
    """⚠️ **阻塞调用。在 async 上下文里必须 `asyncio.to_thread` 包一层。**

    这是真跑时踩到的坑：本脚本在**同一个事件循环**里既跑着探针的 WebSocket、
    又直接调这个阻塞函数发对话请求。对话动辄几十秒，期间事件循环被占死，
    探针回不了服务端的 keepalive ping，连接被以
    `1011 keepalive ping timeout` 断开——而这个报错**一个字都没提到真正的原因**，
    看起来像网络问题或者服务端 bug。
    """
    req = urllib.request.Request(
        f"{api.rstrip('/')}{path}", method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as resp:
        raw = resp.read().decode()
    return json.loads(raw) if raw else None


def _self_check() -> None:
    """**先验数据、再跑链路。**

    这一步是刻意放在最前面的：如果模拟数据本身触发不了检测器，后面整条链路会
    "全绿但大屏是空的"，而且很容易被误判成平台侧的 bug。先在本地用真实的检测
    函数验一遍，不通过就直接退出——用验证代替调参（见 fake_ops_data.py 顶部）。
    """
    end = time.time()
    start = end - 3600
    metric = points_for("metric", target=TARGET_SERVICE, start_ts=start, end_ts=end)
    report = detect_anomalies(
        [DataPoint(ts=p["ts"], value=p["value"]) for p in metric],
        target=TARGET_SERVICE, metric="error_rate",
    )
    assert report.has_anomaly, "模拟指标触发不了异常检测——数据不够典型，先修 fake_ops_data"

    healthy = points_for("metric", target=TARGET_SERVICE, start_ts=start, end_ts=end, healthy=True)
    calm = detect_anomalies([DataPoint(ts=p["ts"], value=p["value"]) for p in healthy], target=TARGET_SERVICE)
    assert not calm.has_anomaly, "健康序列被误报成异常——检测器或数据有问题，先查清再往下跑"

    raw_alerts = points_for("alert", target=TARGET_SERVICE, start_ts=start, end_ts=end)
    corr = correlate_alerts([
        Alert(f"a{i}", ts=p["ts"], target=p["labels"]["target"], labels=p["labels"],
              text=p["text"], severity=p["labels"]["severity"])
        for i, p in enumerate(raw_alerts)
    ])
    assert len(corr.incidents) == 1, f"{corr.original_count} 条同源告警没能合并成 1 个事件"
    print(f"✅ 数据自检：{len(report.anomalies)} 个异常点、健康序列不误报、"
          f"{corr.original_count} 条告警 → {len(corr.incidents)} 个事件（降噪 {corr.noise_reduction:.0%}）")


async def _wait_online(api: str, token: str, connection_id: str, timeout_s: float = 30.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        for c in _call(api, "GET", "/api/v1/admin/ops/connectors", token):
            if c["connection_id"] == connection_id and c["connector_status"] == "online":
                return True
        await asyncio.sleep(1.0)
    return False


def _chat(api: str, token: str, query: str, timeout_s: float = 180.0) -> str:
    """走真实对话链路。返回拼起来的响应文本（SSE 流）。"""
    req = urllib.request.Request(
        f"{api.rstrip('/')}/api/v1/chat/stream", method="POST",
        data=json.dumps({"query": query}).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    chunks: List[str] = []
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        for line in resp:
            text = line.decode("utf-8", "replace").strip()
            if text.startswith("data:"):
                chunks.append(text[5:].strip())
    return "\n".join(chunks)


async def run(api: str, ws_url: str, username: str, cleanup: bool, exec_fails: bool,
              keep_alive: bool) -> int:
    from src.ragent_backend import auth
    from src.ragent_backend.org_store import OrgStore
    from src.ragent_backend.user_store import UserStore

    _self_check()

    users = await UserStore().list_users()
    user = next((u for u in users if u.username == username), None)
    if user is None:
        print(f"❌ 找不到账号 {username}")
        return 1
    token = auth.create_access_token(user.user_id, user.username)
    org = await OrgStore().get_org_for_user(user.user_id)
    if org is None:
        print(f"❌ {username} 不属于任何企业")
        return 1

    admin = next(u for u in users if u.username == "admin")
    admin_token = auth.create_access_token(admin.user_id, admin.username)
    await asyncio.to_thread(_call, api, "PUT", f"/api/v1/admin/organizations/{org.org_id}/aiops-module-enabled",
          admin_token, {"enabled": True})
    print(f"✅ {org.name} 的智能运维模块已开通")

    if cleanup:
        for c in await asyncio.to_thread(_call, api, "GET", "/api/v1/admin/ops/connectors", token):
            await asyncio.to_thread(_call, api, "DELETE", f"/api/v1/admin/ops/connectors/{c['connection_id']}", token)
            print(f"🧹 已删除连接器 {c['name']}")
        return 0

    # ⚠️ **先清掉上一次跑剩下的同名连接器。**
    # 第一版每跑一次就新建一个，跑三次库里就有三个同名连接器、两个是离线僵尸。
    # 后果不只是脏数据：总览的"部分数据不可用"会把这些僵尸如实报出来，
    # 而它们跟真探针**同名**，看起来像"我的探针一会儿在线一会儿掉线"，
    # 排查方向会被带到完全错误的地方（我自己就被带偏过一次）。
    for old_conn in await asyncio.to_thread(_call, api, "GET", "/api/v1/admin/ops/connectors", token):
        if old_conn["name"] == CONNECTOR_NAME:
            await asyncio.to_thread(_call, api, "DELETE",
                                    f"/api/v1/admin/ops/connectors/{old_conn['connection_id']}", token)
            print(f"🧹 清掉上次残留的连接器 {old_conn['connection_id']}")

    conn = await asyncio.to_thread(_call, api, "POST", "/api/v1/admin/ops/connectors", token, {
        "name": CONNECTOR_NAME, "system_type": "prometheus",
        "approval_timeout_minutes": 30,
    })
    cid = conn["connection_id"]
    print(f"✅ 连接器已登记 {cid}")

    reg = await asyncio.to_thread(_call, api, "POST", f"/api/v1/admin/ops/connectors/{cid}/register-token", token)
    probe = OpsProbe(ws_url, cid, reg["register_token"], exec_fails=exec_fails)
    probe_task = asyncio.create_task(probe.run())
    try:
        if not await _wait_online(api, token, cid):
            # ⚠️ **必须把探针自己的异常翻出来。** 第一版这里只打印"没上线，看后端
            # 日志"，而真实原因是探针连都没连上（握手 URL 少传一个查询参数）——
            # 后端日志里当然什么都没有，排查方向被这句话带偏了。
            if probe_task.done() and probe_task.exception() is not None:
                print(f"❌ 探针自身报错：{probe_task.exception()!r}")
            else:
                print("❌ 探针 30 秒内没有上线（探针进程没报错，看后端日志）")
            return 1
        print("✅ 探针已上线（心跳可见）")

        await asyncio.to_thread(_call, api, "PUT", f"/api/v1/admin/ops/connectors/{cid}/remediation-scopes/restart_service",
              token, {"scope_config": {"allowed_targets": [TARGET_SERVICE, "payment-gateway"]}})
        print("✅ 修复范围白名单已登记")

        # —— 走真实对话，让模型自己去调 analyze_ops_incident ——
        print("⏳ 正在通过对话触发分析（依赖模型愿意调工具，可能要一两分钟）…")
        # 提示词写得很直白，是刻意的：**这一步的目的是造演示数据，不是考模型
        # 会不会自己想到调工具**。想验"模型自主性"要单独跑一次自然措辞的对话，
        # 那是另一件事，不该跟"把大屏灌满"混在一起（混在一起的后果是数据没造成，
        # 还搞不清是探针的问题还是模型的问题）。
        answer = await asyncio.to_thread(_chat, api, token,
                       f"请调用 analyze_ops_incident 工具，参数 target 设为 {TARGET_SERVICE}，"
                       "分析它最近一小时的运行情况")
        summaries = await asyncio.to_thread(_call, api, "GET", "/api/v1/admin/ops/analysis-summaries", token)
        if summaries:
            print(f"✅ 模型调用了分析工具，产出 {len(summaries)} 条分析摘要")
        else:
            # **不假装成功**：没调就是没调，如实报出来，后面的步骤照常跑
            print("⚠️ 模型这次没有调用 analyze_ops_incident，时间线会是空的。"
                  "这不是探针或平台的问题——是 7B 模型的工具选择不稳定。"
                  f"模型的回答片段：{answer[:120]}")

        action = await asyncio.to_thread(_call, api, "POST", f"/api/v1/admin/ops/connectors/{cid}/remediation-actions", token, {
            "action_type": "restart_service",
            "intent": f"重启 {TARGET_SERVICE} 以缓解错误率突增",
            "plan": {"action_type": "restart_service", "target": TARGET_SERVICE},
            "impact_radius": f"{TARGET_SERVICE} 单实例，下游 payment-gateway 可能短暂受影响",
        })
        print(f"✅ 已生成待审批动作 {action['action_id']}")

        await asyncio.to_thread(_call, api, "POST", f"/api/v1/admin/ops/remediation-actions/{action['action_id']}/approve", token)
        print("✅ 已批准（注意：批准不会自动执行，下面这一步才是真正下发）")

        # —— 第一次由真连接器跑通 exec 链路 ——
        print("⏳ 通过对话触发执行…")
        await asyncio.to_thread(_chat, api, token,
              f"请调用 execute_approved_remediation 工具，参数 action_id 设为 {action['action_id']}，"
              "action_type 设为 restart_service")
        final = next((a for a in await asyncio.to_thread(_call, api, "GET", "/api/v1/admin/ops/remediation-actions", token)
                      if a["action_id"] == action["action_id"]), None)
        print(f"   动作最终状态：{final['status'] if final else '?'}"
              + (f"（结果：{(final.get('result') or {}).get('detail')}）" if final and final.get("result") else ""))

        print("\n" + "=" * 62)
        print("场景跑完。现在去 运维塔台 → 总览 看数据。")
        print(f"连接器编号（留给另一个会话复核，别急着删）：{cid}")
        print(f"清理：.venv/bin/python scripts/{Path(__file__).name} --api {api} --cleanup")
        print("=" * 62)

        if keep_alive:
            # ⚠️ **探针必须活着，大屏才有活数据。**
            # 第一版这里直接 return，探针任务随之被取消——结果连接器立刻转成离线，
            # 之后任何查询都会拿到"连接器当前离线"，看起来像平台坏了。
            # 演示环境要的是一个**常驻**的客户侧进程，不是跑完就消失的一次性脚本。
            print("\n探针保持在线中（Ctrl-C 停止）。停掉之后连接器会转为离线，"
                  "大屏上的实时查询就查不到数据了。")
            await probe_task
        return 0
    finally:
        if not probe_task.done():
            probe_task.cancel()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--api", default="http://localhost:8010")
    ap.add_argument("--ws", default=None, help="平台 WS 地址，默认由 --api 推导")
    ap.add_argument("--user", default="alice_acme", help="用哪个企业管理员账号跑")
    ap.add_argument("--cleanup", action="store_true", help="删掉本企业全部连接器（级联清数据）后退出")
    ap.add_argument("--exec-fails", action="store_true", help="让探针的执行一律失败，验证失败态展示")
    ap.add_argument("--once", action="store_true",
                    help="灌完数据就退出（探针随之下线，连接器转离线）。默认保持探针常驻，"
                         "因为大屏的实时查询需要一个活着的客户侧进程")
    args = ap.parse_args()
    ws = args.ws or args.api.replace("http://", "ws://").replace("https://", "wss://")
    return asyncio.run(run(args.api, ws, args.user, args.cleanup, args.exec_fails, not args.once))


if __name__ == "__main__":
    raise SystemExit(main())
