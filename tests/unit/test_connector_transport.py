"""`ConnectorTransport`/`RemediationDispatcher` 的 WebSocket 实现回归保护。

用假件模拟"WebSocket 连接"（只需要 `send_json`）和"ops_store 查询"
（`get_connector`/`online_status`），不连真实 WebSocket/DB——跟联邦查询层
（`src/ops/federation/`）的测试同一个风格：契约两边各自用假件验证，
真正的集成靠 `scripts/verify_aiops_endpoints.py` 里过一遍真实的
FastAPI TestClient WebSocket。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Dict, Optional
from unittest.mock import AsyncMock

import pytest

from src.ops.connector_transport import (
    WebSocketConnectorTransport,
    WebSocketRemediationDispatcher,
)
from src.ops.types import (
    ERROR_OFFLINE,
    ERROR_TIMEOUT,
    ERROR_UNAUTHORIZED,
    ERROR_UPSTREAM,
    ConnectorUnavailable,
    QueryRequest,
    QUERY_KIND_METRIC,
    TimeRange,
)


@dataclass
class _FakeConnector:
    connection_id: str
    org_id: str
    name: str


class _FakeOpsStore:
    def __init__(self, connector: Optional[_FakeConnector], online: Optional[Dict[str, bool]] = None):
        self._connector = connector
        self._online = online or {}

    async def get_connector(self, connection_id: str):
        if self._connector is not None and self._connector.connection_id == connection_id:
            return self._connector
        return None

    async def online_status(self, connection_ids):
        return {cid: self._online.get(cid, False) for cid in connection_ids}


class _FakeWebSocket:
    """记录发出去的帧，不真的联网。响应通过测试代码手动 resolve `pending`
    里对应的 future 来模拟"连接器回了一帧"。"""

    def __init__(self):
        self.sent: list = []

    async def send_json(self, data: Dict[str, Any]) -> None:
        self.sent.append(data)


def _make_request() -> QueryRequest:
    return QueryRequest(
        kind=QUERY_KIND_METRIC, target="order-service",
        time_range=TimeRange(start_ts=0, end_ts=3600), metric="error_rate",
    )


class TestQuerySuccess:
    @pytest.mark.asyncio
    async def test_query_resolves_from_matching_response_frame(self):
        conn = _FakeConnector("opsconn_1", "org-1", "prom-1")
        ws = _FakeWebSocket()
        registry = {"opsconn_1": ws}
        pending: Dict[str, "asyncio.Future"] = {}
        transport = WebSocketConnectorTransport(registry, pending, _FakeOpsStore(conn))

        async def responder():
            # 等 query() 把请求塞进 pending 之后，找到那个 id，模拟连接器回包。
            while not pending:
                await asyncio.sleep(0)
            msg_id = next(iter(pending))
            pending[msg_id].set_result({
                "type": "query_result", "id": msg_id, "payload": {
                    "points": [{"ts": 100, "value": 0.5, "labels": {}}], "truncated": False,
                },
            })

        result, _ = await asyncio.gather(
            transport.query("opsconn_1", "org-1", _make_request(), timeout_s=1.0),
            responder(),
        )
        assert result.connection_id == "opsconn_1"
        assert result.system_name == "prom-1"
        assert len(result.points) == 1
        assert result.points[0].value == 0.5

    @pytest.mark.asyncio
    async def test_query_sends_expected_frame_shape(self):
        conn = _FakeConnector("opsconn_1", "org-1", "prom-1")
        ws = _FakeWebSocket()
        registry = {"opsconn_1": ws}
        pending: Dict[str, "asyncio.Future"] = {}
        transport = WebSocketConnectorTransport(registry, pending, _FakeOpsStore(conn))

        async def responder():
            while not pending:
                await asyncio.sleep(0)
            msg_id = next(iter(pending))
            pending[msg_id].set_result({"type": "query_result", "id": msg_id, "payload": {"points": []}})

        await asyncio.gather(
            transport.query("opsconn_1", "org-1", _make_request(), timeout_s=1.0),
            responder(),
        )
        assert len(ws.sent) == 1
        frame = ws.sent[0]
        assert frame["type"] == "query_request"
        assert frame["connector_id"] == "opsconn_1"
        assert frame["payload"]["target"] == "order-service"
        assert frame["payload"]["kind"] == QUERY_KIND_METRIC


class TestQueryFailureModes:
    @pytest.mark.asyncio
    async def test_offline_connector_raises_offline(self):
        conn = _FakeConnector("opsconn_1", "org-1", "prom-1")
        transport = WebSocketConnectorTransport({}, {}, _FakeOpsStore(conn))
        with pytest.raises(ConnectorUnavailable) as exc_info:
            await transport.query("opsconn_1", "org-1", _make_request(), timeout_s=1.0)
        assert exc_info.value.reason == ERROR_OFFLINE

    @pytest.mark.asyncio
    async def test_wrong_org_raises_unauthorized_without_sending_anything(self):
        """§3.5：越权的连接器一次请求都不发——断言 ws.sent 是空的，不只是
        断言抛了异常，这才是"一次都不发"这句话真正要守的东西。"""
        conn = _FakeConnector("opsconn_1", "org-owner", "prom-1")
        ws = _FakeWebSocket()
        transport = WebSocketConnectorTransport({"opsconn_1": ws}, {}, _FakeOpsStore(conn))
        with pytest.raises(ConnectorUnavailable) as exc_info:
            await transport.query("opsconn_1", "org-attacker", _make_request(), timeout_s=1.0)
        assert exc_info.value.reason == ERROR_UNAUTHORIZED
        assert ws.sent == []

    @pytest.mark.asyncio
    async def test_nonexistent_connector_raises_unauthorized(self):
        transport = WebSocketConnectorTransport({}, {}, _FakeOpsStore(None))
        with pytest.raises(ConnectorUnavailable) as exc_info:
            await transport.query("opsconn_missing", "org-1", _make_request(), timeout_s=1.0)
        assert exc_info.value.reason == ERROR_UNAUTHORIZED

    @pytest.mark.asyncio
    async def test_timeout_raises_connector_unavailable_with_timeout_reason(self):
        conn = _FakeConnector("opsconn_1", "org-1", "prom-1")
        ws = _FakeWebSocket()
        transport = WebSocketConnectorTransport({"opsconn_1": ws}, {}, _FakeOpsStore(conn))
        with pytest.raises(ConnectorUnavailable) as exc_info:
            # 没有 responder 去 resolve future，必然超时。
            await transport.query("opsconn_1", "org-1", _make_request(), timeout_s=0.05)
        assert exc_info.value.reason == ERROR_TIMEOUT

    @pytest.mark.asyncio
    async def test_error_frame_raises_upstream(self):
        conn = _FakeConnector("opsconn_1", "org-1", "prom-1")
        ws = _FakeWebSocket()
        registry = {"opsconn_1": ws}
        pending: Dict[str, "asyncio.Future"] = {}
        transport = WebSocketConnectorTransport(registry, pending, _FakeOpsStore(conn))

        async def responder():
            while not pending:
                await asyncio.sleep(0)
            msg_id = next(iter(pending))
            pending[msg_id].set_result({
                "type": "error", "id": msg_id,
                "payload": {"reason": "upstream_error", "detail": "Prometheus 连不上"},
            })

        with pytest.raises(ConnectorUnavailable) as exc_info:
            await asyncio.gather(
                transport.query("opsconn_1", "org-1", _make_request(), timeout_s=1.0),
                responder(),
            )
        assert exc_info.value.reason == ERROR_UPSTREAM

    @pytest.mark.asyncio
    async def test_pending_entry_cleaned_up_after_timeout(self):
        """超时之后 `pending` 字典里不应该留下这条 future——不清理的话，
        长时间运行的进程会积累永远不会被 resolve 的 future，是内存泄漏。"""
        conn = _FakeConnector("opsconn_1", "org-1", "prom-1")
        ws = _FakeWebSocket()
        pending: Dict[str, "asyncio.Future"] = {}
        transport = WebSocketConnectorTransport({"opsconn_1": ws}, pending, _FakeOpsStore(conn))
        with pytest.raises(ConnectorUnavailable):
            await transport.query("opsconn_1", "org-1", _make_request(), timeout_s=0.05)
        assert pending == {}


class TestOnlineStatus:
    @pytest.mark.asyncio
    async def test_delegates_to_ops_store_batch_method(self):
        """联邦查询层的展示路径要用批量接口，不是逐个查——这里只验证
        transport 确实把调用原样转发给 ops_store 的批量方法，不是自己
        在循环里逐个调。"""
        store = AsyncMock()
        store.online_status = AsyncMock(return_value={"a": True, "b": False})
        transport = WebSocketConnectorTransport({}, {}, store)
        result = await transport.online_status(["a", "b"])
        assert result == {"a": True, "b": False}
        store.online_status.assert_awaited_once_with(["a", "b"])


class TestRemediationDispatcher:
    @pytest.mark.asyncio
    async def test_execute_success(self):
        conn = _FakeConnector("opsconn_1", "org-1", "prom-1")
        ws = _FakeWebSocket()
        registry = {"opsconn_1": ws}
        pending: Dict[str, "asyncio.Future"] = {}
        dispatcher = WebSocketRemediationDispatcher(registry, pending, _FakeOpsStore(conn))

        async def responder():
            while not pending:
                await asyncio.sleep(0)
            msg_id = next(iter(pending))
            pending[msg_id].set_result({
                "type": "exec_result", "id": msg_id,
                "payload": {"succeeded": True, "detail": "重启成功"},
            })

        outcome, _ = await asyncio.gather(
            dispatcher.execute("opsconn_1", "org-1", "remact_1", {"target": "order-service"}, timeout_s=1.0),
            responder(),
        )
        assert outcome.succeeded is True
        assert outcome.detail == "重启成功"

    @pytest.mark.asyncio
    async def test_execute_wrong_org_raises_unauthorized_without_sending(self):
        conn = _FakeConnector("opsconn_1", "org-owner", "prom-1")
        ws = _FakeWebSocket()
        dispatcher = WebSocketRemediationDispatcher({"opsconn_1": ws}, {}, _FakeOpsStore(conn))
        with pytest.raises(ConnectorUnavailable) as exc_info:
            await dispatcher.execute("opsconn_1", "org-attacker", "remact_1", {}, timeout_s=1.0)
        assert exc_info.value.reason == ERROR_UNAUTHORIZED
        assert ws.sent == []

    @pytest.mark.asyncio
    async def test_execute_error_frame_returns_failed_outcome_not_exception(self):
        """执行失败**不抛异常**，返回 `ExecutionOutcome(succeeded=False, ...)`
        ——跟 `query()` 不一样：查询失败对联邦查询层是"这个连接器没数据"，
        执行失败对调用方（工具层）是"这次修复没做成"，两者都需要被明确
        地看到结果，而不是被异常路径吞掉，执行结果本身就要落库
        （`remediation_actions.result`）。"""
        conn = _FakeConnector("opsconn_1", "org-1", "prom-1")
        ws = _FakeWebSocket()
        registry = {"opsconn_1": ws}
        pending: Dict[str, "asyncio.Future"] = {}
        dispatcher = WebSocketRemediationDispatcher(registry, pending, _FakeOpsStore(conn))

        async def responder():
            while not pending:
                await asyncio.sleep(0)
            msg_id = next(iter(pending))
            pending[msg_id].set_result({
                "type": "error", "id": msg_id,
                "payload": {"detail": "服务不存在，重启失败"},
            })

        outcome, _ = await asyncio.gather(
            dispatcher.execute("opsconn_1", "org-1", "remact_1", {}, timeout_s=1.0),
            responder(),
        )
        assert outcome.succeeded is False
        assert "重启失败" in outcome.detail
