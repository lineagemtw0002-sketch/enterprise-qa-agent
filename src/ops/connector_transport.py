"""
`ConnectorTransport` / `RemediationDispatcher` 的 WebSocket 实现
——联邦查询层（`src/ops/federation/`）与连接器会话之间的接缝。

契约定义在 `src/ops/types.py`（另一个会话建的，不是这个文件的一部分，两边
都不改对方的文件）。这里只实现协议，不定义协议。

## 怎么把"请求"和"响应"对上号

一个 WebSocket 连接同时可能有多个查询在飞（联邦查询层会并发 fan-out 到
多个连接器，但**同一个**连接器上也可能同时收到分析请求 + 展示页面的请求）。
消息信封（`docs/aiops_module_design.md` §10.1）里的 `id` 字段就是关联 id：
发送时生成一个，注册一个等待中的 `asyncio.Future`；收到帧时按 `id` 找到
对应的 Future 并 resolve 它。真正处理"收到什么类型的帧该怎么办"的分发逻辑
在 `app.py::ops_connector_register_ws` 的接收循环里，这里只负责"发送 + 等待"
这一半。

## 为什么 `query_request` 用 asyncio.wait_for 超时而不是让 WebSocket 自己超时

WebSocket 连接本身可能是好的（心跳正常），但客户环境里连接器进程转发给
Prometheus/Datadog 的那次调用可能挂住——连接活着不代表这次查询会有响应。
`ConnectorTransport.query` 的契约要求超时用调用方传入的 `timeout_s`，
不能依赖传输层的默认超时（那个可能比业务期望的耐心程度长得多）。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any, Dict, Optional, Protocol, Sequence

from src.ops.types import (
    ERROR_OFFLINE,
    ERROR_TIMEOUT,
    ERROR_UNAUTHORIZED,
    ERROR_UPSTREAM,
    ConnectorUnavailable,
    DataPoint,
    ExecutionOutcome,
    QueryRequest,
    QueryResult,
)


class _SendsJson(Protocol):
    async def send_json(self, data: Dict[str, Any]) -> None: ...


class _ConnectorLookup(Protocol):
    """只声明用得到的两个方法——跟 `OpsStoreDirectory` 用窄 Protocol 是
    同一个理由，不要求传一个完整的 `OpsStore` 进来，降低测试时的假件成本。
    """

    async def get_connector(self, connection_id: str) -> Optional[Any]: ...
    async def online_status(self, connection_ids: list) -> Dict[str, bool]: ...


async def _send_and_await_response(
    ws_registry: Dict[str, _SendsJson],
    pending: Dict[str, "asyncio.Future[Dict[str, Any]]"],
    connection_id: str,
    frame_type: str,
    payload: Dict[str, Any],
    timeout_s: float,
) -> Dict[str, Any]:
    """跟连接器发一帧、按 id 等对应的响应帧。找不到活连接直接抛
    `ConnectorUnavailable(ERROR_OFFLINE, ...)`——这是"离线"这个原因分类
    在传输层的唯一来源，不是心跳新鲜度判断出来的猜测。
    """
    ws = ws_registry.get(connection_id)
    if ws is None:
        raise ConnectorUnavailable(ERROR_OFFLINE, f"连接器 '{connection_id}' 当前没有活跃连接")

    msg_id = uuid.uuid4().hex
    loop = asyncio.get_event_loop()
    fut: "asyncio.Future[Dict[str, Any]]" = loop.create_future()
    pending[msg_id] = fut
    try:
        await ws.send_json({
            "type": frame_type, "id": msg_id, "connector_id": connection_id,
            "ts": time.time(), "payload": payload,
        })
        try:
            return await asyncio.wait_for(fut, timeout=timeout_s)
        except asyncio.TimeoutError:
            raise ConnectorUnavailable(
                ERROR_TIMEOUT, f"连接器 '{connection_id}' 在 {timeout_s}s 内没有响应",
            ) from None
    finally:
        pending.pop(msg_id, None)


def _query_request_payload(request: QueryRequest) -> Dict[str, Any]:
    return {
        "kind": request.kind,
        "target": request.target,
        "metric": request.metric,
        "start_ts": request.time_range.start_ts,
        "end_ts": request.time_range.end_ts,
        "filters": request.filters,
        "limit": request.limit,
    }


class WebSocketConnectorTransport:
    """`ConnectorTransport` 的实现——通过活跃的 WebSocket 连接向连接器
    下发只读查询。构造时注入连接注册表和待响应表，不直接依赖 `app.py`
    的模块级变量（那两个字典由 `app.py` 在创建实例时传进来），保持这个
    类本身可以脱离真实 app 单测。
    """

    def __init__(
        self,
        ws_registry: Dict[str, _SendsJson],
        pending: Dict[str, "asyncio.Future[Dict[str, Any]]"],
        ops_store: _ConnectorLookup,
    ) -> None:
        self._ws_registry = ws_registry
        self._pending = pending
        self._ops_store = ops_store

    async def query(
        self, connection_id: str, org_id: str, request: QueryRequest, timeout_s: float,
    ) -> QueryResult:
        conn = await self._ops_store.get_connector(connection_id)
        if conn is None or conn.org_id != org_id:
            # §3.5：越权的连接器一次请求都不发，不是查了再丢结果——这是安全
            # 事件，要跟"连接器碰巧离线"这种普通故障分开记。
            raise ConnectorUnavailable(
                ERROR_UNAUTHORIZED, f"连接器 '{connection_id}' 不属于组织 '{org_id}'",
            )

        frame = await _send_and_await_response(
            self._ws_registry, self._pending, connection_id,
            "query_request", _query_request_payload(request), timeout_s,
        )

        if frame.get("type") == "error":
            detail = (frame.get("payload") or {}).get("detail", "连接器返回错误")
            raise ConnectorUnavailable(ERROR_UPSTREAM, detail)

        payload = frame.get("payload") or {}
        points = [
            DataPoint(
                ts=p["ts"], value=p.get("value"), text=p.get("text"), labels=p.get("labels", {}),
            )
            for p in payload.get("points", [])
        ]
        return QueryResult(
            connection_id=connection_id, system_name=conn.name,
            points=points, truncated=bool(payload.get("truncated", False)),
        )

    async def online_status(self, connection_ids: Sequence[str]) -> Dict[str, bool]:
        return await self._ops_store.online_status(list(connection_ids))


class WebSocketRemediationDispatcher:
    """`RemediationDispatcher` 的实现。**不检查审批状态**——那是工具层
    （`src/ops/tools.py`）的职责，见 `types.py::RemediationDispatcher`
    的说明。这里只管把已经批准的执行计划送到活连接、等结果。
    """

    def __init__(
        self,
        ws_registry: Dict[str, _SendsJson],
        pending: Dict[str, "asyncio.Future[Dict[str, Any]]"],
        ops_store: _ConnectorLookup,
    ) -> None:
        self._ws_registry = ws_registry
        self._pending = pending
        self._ops_store = ops_store

    async def execute(
        self, connection_id: str, org_id: str, action_id: str,
        plan: Dict[str, Any], timeout_s: float,
    ) -> ExecutionOutcome:
        conn = await self._ops_store.get_connector(connection_id)
        if conn is None or conn.org_id != org_id:
            raise ConnectorUnavailable(
                ERROR_UNAUTHORIZED, f"连接器 '{connection_id}' 不属于组织 '{org_id}'",
            )

        frame = await _send_and_await_response(
            self._ws_registry, self._pending, connection_id,
            "exec_request", {"action_id": action_id, "plan": plan}, timeout_s,
        )

        if frame.get("type") == "error":
            detail = (frame.get("payload") or {}).get("detail", "连接器执行失败")
            return ExecutionOutcome(succeeded=False, detail=detail, raw=frame.get("payload") or {})

        payload = frame.get("payload") or {}
        return ExecutionOutcome(
            succeeded=bool(payload.get("succeeded", False)),
            detail=payload.get("detail", ""),
            raw=payload,
        )
