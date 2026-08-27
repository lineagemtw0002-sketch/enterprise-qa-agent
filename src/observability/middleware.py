"""`RequestContextMiddleware` —— 阶段二落地（`docs/observability_design.md` §5）。

纯 ASGI 中间件（不是 ``@app.middleware("http")`` 装饰器形态），原因见该设计文档
「中间件必须抽成独立的 ASGI 中间件类，能用 3 行的假 app 测它，不测真
``create_app()``（那会连 Postgres）」——这正是 `CLAUDE.md` §7.1 点名的
``create_app()`` 反面教材的具体规避方式。

职责：
1. 读入站 ``X-Request-Id``；不存在或不合法（见 `context.sanitize_request_id`
   的字符白名单，防日志注入）就用 `context.new_request_id()` 生成一个新的。
2. 用 `bind_request_context(request_id=...)` 绑到当前 asyncio 上下文。
3. 响应头回写同一个 ``X-Request-Id``，方便前端/客户端关联。
4. 请求结束（含异常）后 `clear_request_context()`。

⚠️ **不覆盖 SSE 生成器体的场景**：设计文档 R1 明确标注"中间件设置的 contextvar
能否透传进 `StreamingResponse` 的生成器体，未实测，属于设计推断"。因此这里
**不依赖**这条透传假设——`chat_stream` 端点在函数体第一行显式再 `bind_request_context`
一次（与 `workflow.py` 现有的 `_CURRENT_TOKEN_QUEUE` 是同一个「不信任透传，显式
再绑一次」的保守做法）。中间件本身覆盖的是其余 71 个非流式端点，改动为零、
覆盖面最广。

中间件顺序：应该在 `app.add_middleware(CORSMiddleware, ...)` **之后**注册——
Starlette 中间件是洋葱模型，后注册的在更外层，这样连 CORS 预检失败的请求
也能带上 request_id。
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, MutableMapping

from src.observability.context import (
    bind_request_context,
    clear_request_context,
    new_request_id,
    sanitize_request_id,
)

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]

_HEADER_NAME = b"x-request-id"


class RequestContextMiddleware:
    """纯 ASGI 中间件：绑定/回写 ``X-Request-Id``，非 HTTP 请求（如 WebSocket 的
    lifespan 事件）原样透传，不做任何处理——WebSocket 有自己独立的鉴权/上下文
    需求（见 trace WebSocket 的鉴权修复），这里不越界处理。"""

    def __init__(self, app: Callable[[Scope, Receive, Send], Awaitable[None]]) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        incoming = self._read_header(scope)
        request_id = sanitize_request_id(incoming) or new_request_id()
        bind_request_context(request_id=request_id)

        async def send_with_request_id(message: MutableMapping[str, Any]) -> None:
            if message.get("type") == "http.response.start":
                headers = list(message.get("headers") or [])
                headers.append((_HEADER_NAME, request_id.encode("latin-1")))
                message = dict(message)
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            # 中间件的 __call__ 会一直挂到响应（含流式响应）完全发送完/客户端
            # 断开才返回——此时请求确实结束了，清理是安全的。用 set(None) 而不是
            # reset(token)，跟项目里所有 contextvars 清理点保持同一条纪律。
            clear_request_context()

    @staticmethod
    def _read_header(scope: Scope) -> str | None:
        for key, value in scope.get("headers") or []:
            if key.lower() == _HEADER_NAME:
                try:
                    return value.decode("latin-1")
                except UnicodeDecodeError:
                    return None
        return None
