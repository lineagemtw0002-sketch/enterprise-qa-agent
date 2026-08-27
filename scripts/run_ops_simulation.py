"""把 2–3 套**仿真运维环境**接进平台，长期驻留。

跟 `run_ops_probe_scenario.py` 的分工：
- 那个是**一次性演示脚本**——建连接器、跑一遍完整链路（分析→提议→审批→执行）
  把大屏灌满数据，跑完就是一个结果快照。
- 这个是**常驻仿真环境**——把几套环境接上去、保持在线、每套带自己的故障注入
  控制台，供日常测试和演示反复使用。不主动制造任何动作，一切从人点开始。

## 每套环境是什么

一套环境 = 一个连接器（挂在指定企业名下）+ 一个探针进程内的会话 + 一个故障
注入控制口。三套环境的服务名、基线指标、集群名各不相同，见
`services/ops_probe_demo/environments.py`。

## 用法

    .venv/bin/python scripts/run_ops_simulation.py --api http://localhost:8010

    # 只起其中两套 / 换企业
    .venv/bin/python scripts/run_ops_simulation.py --envs ecommerce payments
    .venv/bin/python scripts/run_ops_simulation.py --user carol_globex

    # 清掉之前留下的仿真连接器（级联删除其动作/摘要/配置）
    .venv/bin/python scripts/run_ops_simulation.py --cleanup

⚠️ **同一个 Postgres 被多个后端共用**（本机开发常态）。连接器的「在线」是按
库里的心跳算的，而能不能真正查询取决于 WebSocket 握在哪个后端进程手里——
所以在 A 后端上起的仿真环境，会在 B 后端的界面上显示在线却查不到数据。
本脚本把"接的是哪个平台/哪个企业/哪个连接器"直接印在每个控制台页面上。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.ops_probe_demo.control_server import FaultState, serve_control  # noqa: E402
from services.ops_probe_demo.environments import ENVIRONMENTS, resolve_environment  # noqa: E402
from services.ops_probe_demo.probe import OpsProbe  # noqa: E402

# 连接器名字带这个前缀，`--cleanup` 才知道哪些是本脚本建的、可以安全删除。
# **不按"名字里有'演示'"这类模糊判据删**——别的会话也可能建过测试连接器，
# 误删别人的验证数据是这个项目已经踩过的协作问题（CLAUDE.md §7.2）。
NAME_PREFIX = "仿真环境"
BASE_CONTROL_PORT = 9330


def _call(api: str, method: str, path: str, token: str, body: Optional[dict] = None) -> Any:
    req = urllib.request.Request(
        f"{api.rstrip('/')}{path}", method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read().decode()
    return json.loads(raw) if raw else None


def _scope_configs(services: List[str]) -> Dict[str, Dict[str, Any]]:
    """四类修复动作的允许范围。

    **四类都配**：每一类都有对应的可触发故障（见 `environments.py` 里那条
    断言），没配的那一类在演示里会被"默认拒绝"挡下——那是正确行为，但会让人
    以为功能坏了。
    """
    return {
        "restart_service": {"allowed_targets": services},
        "scale_instances": {"min_instances": 1, "max_multiplier_of_baseline": 3},
        "clean_disk": {
            "allowed_path_patterns": ["/var/log/*", "/tmp/*", "/var/cache/*"],
            # 排除规则优先于允许规则（§10.3）。数据目录永远不许清，
            # 这也是演示"越界提议会被拦下"最好用的一条。
            "excluded_path_patterns": ["/var/lib/*", "/data/*"],
        },
        "rollback_deployment": {"max_versions_back": 2},
    }


async def _bring_up(api: str, ws_url: str, token: str, org_name: str, env_key: str,
                    port: int) -> Dict[str, Any]:
    env = resolve_environment(env_key)
    name = f"{NAME_PREFIX}·{env.label}"

    for old in await asyncio.to_thread(_call, api, "GET", "/api/v1/admin/ops/connectors", token):
        if old["name"] == name:
            await asyncio.to_thread(_call, api, "DELETE",
                                    f"/api/v1/admin/ops/connectors/{old['connection_id']}", token)

    conn = await asyncio.to_thread(_call, api, "POST", "/api/v1/admin/ops/connectors", token,
                                   {"name": name, "system_type": "prometheus",
                                    "approval_timeout_minutes": 30})
    cid = conn["connection_id"]
    for action_type, cfg in _scope_configs(sorted(env.services)).items():
        await asyncio.to_thread(
            _call, api, "PUT",
            f"/api/v1/admin/ops/connectors/{cid}/remediation-scopes/{action_type}",
            token, {"scope_config": cfg})

    reg = await asyncio.to_thread(_call, api, "POST",
                                  f"/api/v1/admin/ops/connectors/{cid}/register-token", token)
    state = FaultState()
    probe = OpsProbe(ws_url, cid, reg["register_token"], environment=env.key,
                     state=state, verbose=False)
    task = asyncio.create_task(probe.run())
    try:
        serve_control(state, env, port=port, platform=api, connection_id=cid, org_label=org_name)
    except OSError as e:
        # 本机同时跑两套仿真（比如一套接 8010、一套接自己的测试后端）时必然撞口。
        # 原来这里漏出一串 socket 的 traceback，看不出是端口问题、更看不出该怎么办。
        raise SystemExit(
            f"❌ 故障注入控制口 {port} 已被占用（多半是另一套仿真环境在用）。\n"
            f"   换一个起点：--control-port-base {port + 10}") from e
    return {"env": env, "cid": cid, "port": port, "task": task}


async def run(api: str, ws_url: str, username: str, env_keys: List[str], cleanup: bool,
              control_port_base: int) -> int:
    from src.ragent_backend import auth
    from src.ragent_backend.org_store import OrgStore
    from src.ragent_backend.user_store import UserStore

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
    await asyncio.to_thread(
        _call, api, "PUT", f"/api/v1/admin/organizations/{org.org_id}/aiops-module-enabled",
        auth.create_access_token(admin.user_id, admin.username), {"enabled": True})

    if cleanup:
        removed = 0
        for c in await asyncio.to_thread(_call, api, "GET", "/api/v1/admin/ops/connectors", token):
            if c["name"].startswith(NAME_PREFIX):
                await asyncio.to_thread(_call, api, "DELETE",
                                        f"/api/v1/admin/ops/connectors/{c['connection_id']}", token)
                print(f"🧹 已删除 {c['name']}（{c['connection_id']}）")
                removed += 1
        print(f"共清理 {removed} 个仿真连接器" if removed else "没有需要清理的仿真连接器")
        return 0

    running = []
    for i, key in enumerate(env_keys):
        running.append(await _bring_up(api, ws_url, token, org.name, key, control_port_base + i))

    await asyncio.sleep(2.0)
    status = {c["connection_id"]: c["connector_status"]
              for c in await asyncio.to_thread(_call, api, "GET",
                                               "/api/v1/admin/ops/connectors", token)}
    print("\n" + "=" * 78)
    print(f"仿真运维环境已就绪　企业：{org.name}　平台：{api}")
    print(f"登录账号：{username}（在运维塔台里看这些数据，得用这个企业的账号）")
    print("-" * 78)
    for r in running:
        print(f"  {r['env'].label:8} {r['cid']}  {status.get(r['cid'], '?'):8} "
              f"故障注入 → http://127.0.0.1:{r['port']}")
        print(f"           服务：{'、'.join(sorted(r['env'].services))}")
    print("-" * 78)
    print("Ctrl-C 停止全部。停掉之后连接器转为离线，大屏上的实时查询就查不到数据了。")
    print("=" * 78)

    await asyncio.gather(*[r["task"] for r in running])
    return 0


def main() -> int:
    # ⚠️ **强制行缓冲。** 这个脚本几乎总是被 nohup/重定向着跑，而 Python 在
    # 非 tty 下会整块缓冲 stdout——表现是"控制口明明已经能响应了，日志却始终
    # 是空的"，看起来像脚本卡死。实际就被这个骗过一次。
    sys.stdout.reconfigure(line_buffering=True)

    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--api", default="http://localhost:8010")
    ap.add_argument("--ws", default=None)
    ap.add_argument("--user", default="alice_acme", help="用哪个企业管理员的身份登记连接器")
    ap.add_argument("--envs", nargs="*", default=list(ENVIRONMENTS),
                    choices=list(ENVIRONMENTS), help="要起哪几套环境")
    ap.add_argument("--control-port-base", type=int, default=BASE_CONTROL_PORT,
                    help="故障注入控制口的起点（每套环境依次 +1）。本机同时跑两套仿真时要错开")
    ap.add_argument("--cleanup", action="store_true", help="删掉本脚本建的仿真连接器后退出")
    args = ap.parse_args()
    ws = args.ws or args.api.replace("http://", "ws://").replace("https://", "wss://")
    try:
        return asyncio.run(run(args.api, ws, args.user, args.envs, args.cleanup,
                              args.control_port_base))
    except KeyboardInterrupt:
        print("\n已停止")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
