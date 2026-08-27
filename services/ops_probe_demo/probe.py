"""运维探针（BYOC 连接器的客户环境那一端）——**演示件，不是产品交付件**。

见本包 `__init__.py`：真实连接器不在项目范围内，这个进程存在的目的是给平台侧
协议做端到端验证，并让运维塔台大屏上有真实数据。

跑起来做四件事：
1. 用一次性 `register_token` 握手，拿回 `session_token` + `refresh_token`
2. 每 10 秒发一次心跳（平台据此判定在线/离线）
3. 应答 `query_request` → 回 `query_result`（数据来自 fake_ops_data）
4. 应答 `exec_request` → 回 `exec_result`

⚠️ **真实连接器还需要、而这里刻意不做的**：断线重连与退避、refresh_token 到期
前的主动轮换、凭证落盘加密、多运维系统适配层、并发查询限流。做了会让这个演示件
看起来像可以拿去部署的东西——它不是。

用法：
    .venv/bin/python -m services.ops_probe_demo.probe \
        --platform ws://localhost:8033 --token <register_token>
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid
from typing import Any, Dict, Optional

import websockets

from services.ops_probe_demo.fake_ops_data import points_for

HEARTBEAT_INTERVAL_SECONDS = 10.0
"""跟设计文档 §10.1 给的心跳周期一致。平台侧的"多久没心跳算离线"是另一个数，
两者不必相同，但心跳必须明显快于那个判定窗口。"""


class OpsProbe:
    def __init__(self, platform_ws: str, connection_id: str, register_token: str, *,
                 healthy: bool = False, exec_fails: bool = False, verbose: bool = True) -> None:
        # ⚠️ `connection_id` 和 `token` 两个查询参数**都是必需的**（见 app.py
        # 那个端点的签名：`connection_id: str = Query(...)`）。少传一个的表现是
        # 握手直接被拒成 HTTP 403，而**服务端日志里连一条连接记录都不会有**——
        # 排查时很容易误判成"平台侧没起来"或"token 不对"。
        self._url = (f"{platform_ws.rstrip('/')}/ws/ops/connector/register"
                     f"?connection_id={connection_id}&token={register_token}")
        self.connection_id = connection_id
        self._healthy = healthy
        # 让执行故意失败，用来验证"执行失败"这条路径在 UI 上长什么样——
        # 一个只会成功的演示件，验不出失败态的展示对不对。
        self._exec_fails = exec_fails
        self._verbose = verbose
        self._session_token: Optional[str] = None
        self._refresh_token: Optional[str] = None

    def _log(self, msg: str) -> None:
        if self._verbose:
            print(f"[probe] {msg}", flush=True)

    async def run(self) -> None:
        async with websockets.connect(self._url) as ws:
            registered = json.loads(await ws.recv())
            if registered.get("type") != "registered":
                raise RuntimeError(f"握手失败，收到 {registered.get('type')}: {registered}")
            payload = registered.get("payload") or {}
            self._session_token = payload.get("session_token")
            self._refresh_token = payload.get("refresh_token")
            self._log(f"已注册 connection_id={self.connection_id}")

            heartbeat = asyncio.create_task(self._heartbeat_loop(ws))
            try:
                await self._receive_loop(ws)
            finally:
                heartbeat.cancel()

    async def _heartbeat_loop(self, ws) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
            await self._send(ws, "heartbeat", {})

    async def _send(self, ws, frame_type: str, payload: Dict[str, Any],
                    msg_id: Optional[str] = None) -> None:
        await ws.send(json.dumps({
            "type": frame_type,
            "id": msg_id or str(uuid.uuid4()),
            "connector_id": self.connection_id,
            "ts": time.time(),
            "payload": payload,
        }))

    async def _receive_loop(self, ws) -> None:
        async for raw in ws:
            frame = json.loads(raw)
            kind = frame.get("type")
            if kind == "heartbeat":
                continue                      # 平台的心跳回执，不用处理
            if kind == "query_request":
                await self._on_query(ws, frame)
            elif kind == "exec_request":
                await self._on_exec(ws, frame)
            else:
                self._log(f"忽略未知帧: {kind}")

    async def _on_query(self, ws, frame: Dict[str, Any]) -> None:
        """只读查询。真实连接器在这里把抽象查询翻译成 PromQL / Datadog API 调用，
        演示件直接从 fake_ops_data 取。"""
        p = frame.get("payload") or {}
        kind, target = p.get("kind"), p.get("target", "")
        points = points_for(
            kind, target=target,
            start_ts=float(p.get("start_ts", 0)), end_ts=float(p.get("end_ts", 0)),
            healthy=self._healthy,
        )
        limit = int(p.get("limit") or 0)
        truncated = bool(limit and len(points) > limit)
        # limit 要真的生效并如实标 truncated——平台侧会把 truncated 显示给用户，
        # 谎报"没截断"会让人以为看到的是全部数据。
        await self._send(ws, "query_result",
                         {"points": points[:limit] if limit else points, "truncated": truncated},
                         msg_id=frame.get("id"))
        self._log(f"query[{kind}] {target} -> {len(points[:limit] if limit else points)} 点"
                  + ("（已截断）" if truncated else ""))

    async def _on_exec(self, ws, frame: Dict[str, Any]) -> None:
        """执行已批准的修复动作。演示件不真的重启任何东西——只回一个像样的结果。

        ⚠️ **这里不检查审批状态**，跟真实连接器的职责划分一致：审批是平台工具层
        强制的（见 src/ops/tools.py 顶部那张表），连接器收到什么就执行什么。
        连接器自己再查一遍审批状态是 V2 的纵深防御候选（设计文档 §10.1 记了），
        V1 不做——做了反而会掩盖"平台侧这道闸如果漏了会怎样"。
        """
        p = frame.get("payload") or {}
        plan = p.get("plan") or {}
        target = plan.get("target", "(未指定)")
        action = plan.get("action_type", "unknown")
        await asyncio.sleep(0.4)              # 装作真在做事，让 executing 状态在 UI 上可见
        if self._exec_fails:
            await self._send(ws, "exec_result",
                             {"succeeded": False, "detail": f"{target}: 目标进程无响应，操作超时"},
                             msg_id=frame.get("id"))
            self._log(f"exec[{action}] {target} -> 失败（--exec-fails）")
            return
        detail = {
            "restart_service": f"{target} 已重启，新进程 PID 48127，健康检查通过",
            "scale_instances": f"{target} 实例数已调整为 {plan.get('target_instances', '?')}",
            "clean_disk": f"{plan.get('path', target)} 已清理，释放 12.4 GB",
            "rollback_deployment": f"{target} 已回滚 {plan.get('versions_back', 1)} 个版本",
        }.get(action, f"{target} 动作已执行")
        await self._send(ws, "exec_result", {"succeeded": True, "detail": detail},
                         msg_id=frame.get("id"))
        self._log(f"exec[{action}] {target} -> 成功")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--platform", default="ws://localhost:8010", help="平台 WS 地址，如 ws://localhost:8033")
    ap.add_argument("--connection-id", required=True, help="连接器 id（登记连接器时拿）")
    ap.add_argument("--token", required=True, help="一次性 register_token（管理面「生成握手凭证」拿）")
    ap.add_argument("--healthy", action="store_true", help="返回「一切正常」的数据，用于验证检测器不会无中生有")
    ap.add_argument("--exec-fails", action="store_true", help="执行一律失败，用于验证失败态的展示")
    args = ap.parse_args()
    probe = OpsProbe(args.platform, args.connection_id, args.token,
                     healthy=args.healthy, exec_fails=args.exec_fails)
    try:
        asyncio.run(probe.run())
    except KeyboardInterrupt:
        print("\n[probe] 已停止")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
