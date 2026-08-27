"""探针的故障注入控制口。**演示件，不是产品交付件。**

存在的理由：静态的假数据只能演示"一个已经坏了的系统长什么样"，演示不了
"我现在把它弄坏 → 大屏亮红 → AI 分析 → 人工审批 → 执行修复 → 恢复绿色"
这个**过程**，而后者才是这个模块真正要证明的东西。

⚠️ **只监听 127.0.0.1，且不做鉴权。** 这个口的能力是"让被监控系统随时坏掉"，
在真实环境里它本身就是攻击面。演示件绑本地回环、不加鉴权是可以接受的取舍；
**但也正因如此，这个模块永远不该被搬进产品代码**——真要做混沌工程能力，
那是另一件事，要走自己的设计和权限模型。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, Optional

from services.ops_probe_demo.environments import FAULT_KINDS, Environment


class FaultState:
    """当前注入的故障（服务名 → 故障类型）。

    ⚠️ **跨线程共享**：控制口跑在一个后台线程里，探针跑在 asyncio 事件循环里。
    读的时候返回**快照副本**而不是内部字典——否则事件循环正在遍历它时，
    控制口线程插进来改一下，就是一个 `RuntimeError: dictionary changed size
    during iteration`，而且只在"演示时恰好同时操作"的时候才出现。
    """

    def __init__(self) -> None:
        self._faults: Dict[str, str] = {}
        self._lock = threading.Lock()

    def snapshot(self) -> Dict[str, str]:
        with self._lock:
            return dict(self._faults)

    def inject(self, service: str, kind: str) -> None:
        if kind not in FAULT_KINDS:
            raise KeyError(f"未知故障类型 {kind!r}，可选：{sorted(FAULT_KINDS)}")
        with self._lock:
            self._faults[service] = kind

    def heal(self, service: Optional[str] = None) -> None:
        with self._lock:
            if service is None:
                self._faults.clear()
            else:
                self._faults.pop(service, None)


def _make_handler(state: FaultState, env: Environment):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):     # 别把每个请求都打到探针日志里
            pass

        def _send(self, code: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):                  # noqa: N802
            if self.path.rstrip("/") != "/state":
                self._send(404, {"error": "只有 GET /state"})
                return
            faults = state.snapshot()
            self._send(200, {
                "environment": env.key, "label": env.label,
                "services": sorted(env.services),
                "faults": faults,
                "fault_kinds": {k: v.label for k, v in FAULT_KINDS.items()},
            })

        def do_POST(self):                 # noqa: N802
            path = self.path.rstrip("/")
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or "{}")
            except json.JSONDecodeError:
                self._send(400, {"error": "请求体不是合法 JSON"})
                return

            if path == "/inject":
                service, kind = body.get("service"), body.get("kind")
                # **服务名必须在这套环境里真实存在**，不接受任意字符串——
                # 拼错一个字母就注入了一个谁也看不到的"故障"，然后花十分钟
                # 怀疑是平台没检测出来。
                if service not in env.services:
                    self._send(400, {"error": f"环境 {env.key} 里没有服务 {service!r}",
                                     "services": sorted(env.services)})
                    return
                try:
                    state.inject(service, kind)
                except KeyError as e:
                    self._send(400, {"error": str(e)})
                    return
                self._send(200, {"ok": True, "faults": state.snapshot()})
                return

            if path == "/heal":
                state.heal(body.get("service"))
                self._send(200, {"ok": True, "faults": state.snapshot()})
                return

            self._send(404, {"error": "只有 POST /inject 和 POST /heal"})

    return Handler


def serve_control(state: FaultState, env: Environment, *, port: int = 9330) -> ThreadingHTTPServer:
    """在后台线程里起控制口，返回 server（调用方一般不用管它）。"""
    server = ThreadingHTTPServer(("127.0.0.1", port), _make_handler(state, env))
    threading.Thread(target=server.serve_forever, daemon=True, name="ops-probe-control").start()
    return server
