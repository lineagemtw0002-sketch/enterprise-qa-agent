"""`RequestContextMiddleware` 测试 —— T-3（`docs/observability_design.md` §5.3）。

刻意不经过 `create_app()`（会连 Postgres）——中间件被设计成纯 ASGI 类，
这里用一个 3 行的假下游 app 单测它，见该设计文档「中间件必须抽成独立的
ASGI 中间件类」一节。
"""

from __future__ import annotations

import pytest

from src.observability.context import clear_request_context, get_request_context
from src.observability.middleware import RequestContextMiddleware


async def _echo_app(scope, receive, send):
    """3 行假下游 app：把当前 request_id 塞进响应体，验证中间件绑定生效。"""
    ctx = get_request_context()
    await send({"type": "http.response.start", "status": 200, "headers": []})
    body = (ctx.request_id if ctx else "NONE").encode()
    await send({"type": "http.response.body", "body": body})


def _http_scope(headers: list[tuple[bytes, bytes]] | None = None) -> dict:
    return {"type": "http", "method": "GET", "path": "/", "headers": headers or []}


async def _run(scope, app=_echo_app):
    sent_messages: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent_messages.append(message)

    middleware = RequestContextMiddleware(app)
    await middleware(scope, receive, send)
    return sent_messages


def _response_headers(messages: list[dict]) -> dict[bytes, bytes]:
    start = next(m for m in messages if m["type"] == "http.response.start")
    return dict(start["headers"])


def _response_body(messages: list[dict]) -> bytes:
    return next(m for m in messages if m["type"] == "http.response.body")["body"]


@pytest.fixture(autouse=True)
def _clean_context():
    clear_request_context()
    yield
    clear_request_context()


class TestNoIncomingHeader:
    @pytest.mark.asyncio
    async def test_generates_16_char_hex_id(self):
        messages = await _run(_http_scope())
        headers = _response_headers(messages)
        assert b"x-request-id" in headers
        request_id = headers[b"x-request-id"].decode()
        assert len(request_id) == 16
        int(request_id, 16)  # must be valid hex

    @pytest.mark.asyncio
    async def test_bound_id_matches_response_header(self):
        """中间件绑的 id 必须和响应头回写的是同一个——不能各生成一次。"""
        messages = await _run(_http_scope())
        header_id = _response_headers(messages)[b"x-request-id"].decode()
        body_id = _response_body(messages).decode()
        assert header_id == body_id


class TestValidIncomingHeader:
    @pytest.mark.asyncio
    async def test_reuses_valid_incoming_id(self):
        incoming = "client-supplied-id-123"
        messages = await _run(_http_scope([(b"x-request-id", incoming.encode())]))
        assert _response_headers(messages)[b"x-request-id"].decode() == incoming
        assert _response_body(messages).decode() == incoming


class TestInvalidIncomingHeader:
    """第三条断言拦的是日志注入——非法字符必须被拒绝并重新生成，不能原样透传。"""

    @pytest.mark.asyncio
    async def test_rejects_id_with_newline(self):
        malicious = "abc\r\nX-Injected: evil"
        messages = await _run(_http_scope([(b"x-request-id", malicious.encode())]))
        result = _response_headers(messages)[b"x-request-id"].decode()
        assert result != malicious
        assert "\r" not in result and "\n" not in result
        assert len(result) == 16

    @pytest.mark.asyncio
    async def test_rejects_overlong_id(self):
        overlong = "a" * 500
        messages = await _run(_http_scope([(b"x-request-id", overlong.encode())]))
        result = _response_headers(messages)[b"x-request-id"].decode()
        assert result != overlong
        assert len(result) == 16

    @pytest.mark.asyncio
    async def test_rejects_id_with_disallowed_characters(self):
        malicious = "id-with-<script>tag"
        messages = await _run(_http_scope([(b"x-request-id", malicious.encode())]))
        result = _response_headers(messages)[b"x-request-id"].decode()
        assert result != malicious
        assert len(result) == 16


class TestContextCleanup:
    @pytest.mark.asyncio
    async def test_context_cleared_after_request(self):
        assert get_request_context() is None
        await _run(_http_scope())
        assert get_request_context() is None

    @pytest.mark.asyncio
    async def test_context_cleared_even_on_downstream_exception(self):
        async def _boom(scope, receive, send):
            raise RuntimeError("downstream blew up")

        with pytest.raises(RuntimeError):
            await _run(_http_scope(), app=_boom)
        assert get_request_context() is None


class TestNonHttpScopePassesThrough:
    @pytest.mark.asyncio
    async def test_lifespan_scope_untouched(self):
        """非 HTTP scope（如 lifespan/websocket）不应该被这个中间件处理——
        它只负责 HTTP 请求头，WebSocket 有自己独立的鉴权/上下文路径。"""
        calls = []

        async def fake_app(scope, receive, send):
            calls.append(scope["type"])

        middleware = RequestContextMiddleware(fake_app)
        await middleware({"type": "lifespan"}, lambda: None, lambda m: None)
        assert calls == ["lifespan"]
        assert get_request_context() is None
