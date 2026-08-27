"""联邦查询层（`docs/aiops_module_design.md` §3.5）。

平台是**查询层**，不是数据仓库——客户的原始运维数据始终留在客户自己的系统里，
这里只做"抽象查询 → 多连接器 fan-out → 结果合并"，参照 Grafana 的定位。

这一层要守住的四条（前三条来自 §3.5，第四条来自本项目的隔离基线）：

1. **抽象查询不暴露底层**：调用方说"过去 1 小时 order-service 的错误率"，
   翻译成 PromQL / Datadog API 是连接器在客户环境内做的事。
2. **多连接器并行 fan-out**，表现得像单一数据源。
3. **部分失败必须显式呈现**：一个连接器超时/离线，既不能让整个请求失败，
   也不能静默丢掉那部分数据——失败进 `FederatedResult.errors`，调用方负责展示。
4. **跨 org 绝不放行**：请求里指定的连接器如果不属于这个 org，
   记为 `ERROR_UNAUTHORIZED` 并**不发出任何查询**。这是安全边界不是错误处理，
   所以单独一类原因，方便审计时把它跟"超时"这种普通故障区分开。

**热路径不做在线预检查**（不调 `online_status`）：直接发查询，失败进 errors。
省一轮 DB 往返，语义也更准——"心跳说在线"和"这次查得到"本来就不是一回事。
`online_status` 只留给展示/诊断路径。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Protocol, Sequence

from src.ops.federation.cache import FederatedQueryCache
from src.ops.types import (
    ERROR_OFFLINE,
    ERROR_TIMEOUT,
    ERROR_TRANSPORT,
    ERROR_UNAUTHORIZED,
    ConnectorError,
    ConnectorTransport,
    ConnectorUnavailable,
    FederatedResult,
    QueryRequest,
    QueryResult,
)

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 8.0
"""跟知识库委托检索的 `REMOTE_SEARCH_TIMEOUT_SECONDS` 取同一个数值，
保持"企业系统响应多久算超时"的产品预期一致（见 query_knowledge_hub.py 顶部）。
调用方可以按场景覆盖：大屏交互该更短，后台批量分析可以更长。"""


@dataclass(frozen=True)
class ConnectionRef:
    """联邦查询需要知道的连接器最小信息。

    刻意不复用 `ops_store.OpsSystemConnection`：那是存储层的完整记录（含心跳时间、
    创建人、审批超时配置等），联邦查询层只需要 id 和给用户看的名字。
    依赖最小集，将来存储层加字段不会波及这里。
    """

    connection_id: str
    system_name: str


class ConnectionDirectory(Protocol):
    """"这个 org 名下有哪些连接器"——实现由调用方注入（生产环境接 `ops_store`）。"""

    async def list_for_org(self, org_id: str) -> Sequence[ConnectionRef]:
        ...


class FederatedQueryEngine:
    """把一个抽象查询扇出到多个连接器，合并结果。

    构造时注入 transport / directory / cache，**没有任何全局状态**——
    测试用假件就能跑完整链路，不需要起 WebSocket 也不需要连 DB。
    """

    def __init__(
        self,
        transport: ConnectorTransport,
        directory: ConnectionDirectory,
        cache: Optional[FederatedQueryCache] = None,
        default_timeout_s: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._transport = transport
        self._directory = directory
        self._cache = cache if cache is not None else FederatedQueryCache()
        self._default_timeout_s = default_timeout_s

    async def query(
        self,
        org_id: str,
        request: QueryRequest,
        connection_ids: Optional[Sequence[str]] = None,
        timeout_s: Optional[float] = None,
        use_cache: bool = True,
    ) -> FederatedResult:
        """执行一次联邦查询。

        Args:
            org_id: 发起方所属企业。**所有连接器都会按它校验归属**。
            request: 抽象查询（构造时已自校验，非法请求在构造处就抛了）。
            connection_ids: 限定只查这几个连接器；`None` = 这个 org 名下全部。
            timeout_s: 单个连接器的超时。**由调用方决定**，不从连接器配置读。
            use_cache: 关掉它用于"用户点了刷新"这种明确要最新数据的场景。

        Returns:
            `FederatedResult`。**不会因为某个连接器失败而抛异常**——
            失败在 `errors` 里，调用方必须处理（§3.5 第 4 条）。
        """
        timeout = timeout_s if timeout_s is not None else self._default_timeout_s
        owned = {c.connection_id: c for c in await self._directory.list_for_org(org_id)}

        targets, errors = self._resolve_targets(owned, connection_ids)
        if not targets:
            # 一个可查的连接器都没有。可能是这个 org 还没接系统（errors 为空），
            # 也可能是指定的连接器全部越权（errors 非空）——两者对调用方是不同的事，
            # 靠 errors 区分，这里都不抛异常。
            return FederatedResult(request=request, results=[], errors=errors)

        target_ids = [c.connection_id for c in targets]
        if use_cache:
            cached = self._cache.get(org_id, request, target_ids)
            if cached is not None:
                # 越权错误不进缓存（缓存只存成功结果），所以命中缓存时要把本次
                # 算出来的 errors 再拼回去，否则"你无权访问 X"会在缓存期内消失。
                if errors:
                    return FederatedResult(
                        request=cached.request, results=cached.results,
                        errors=[*cached.errors, *errors], from_cache=True,
                    )
                return cached

        fanned = await asyncio.gather(
            *[self._query_one(org_id, ref, request, timeout) for ref in targets]
        )

        results: List[QueryResult] = []
        for ref, outcome in zip(targets, fanned):
            if isinstance(outcome, ConnectorError):
                errors.append(outcome)
            else:
                results.append(outcome)

        result = FederatedResult(request=request, results=results, errors=errors)
        if use_cache:
            # put() 自己会拒绝带 errors 的结果，这里不用再判一次——
            # 判两次的坏处是两处逻辑将来会漂移。
            self._cache.put(org_id, request, target_ids, result)
        return result

    def _resolve_targets(
        self, owned: Dict[str, ConnectionRef], connection_ids: Optional[Sequence[str]]
    ) -> tuple[List[ConnectionRef], List[ConnectorError]]:
        """定出这次要查哪些连接器，并把越权的挑出来。

        ⚠️ **越权的连接器不会被查询，一次请求都不会发出去**——不是"查了但丢弃结果"。
        """
        if connection_ids is None:
            return list(owned.values()), []

        targets: List[ConnectionRef] = []
        errors: List[ConnectorError] = []
        for cid in connection_ids:
            ref = owned.get(cid)
            if ref is None:
                logger.warning(
                    "Blocked cross-org ops query: connection_id=%s not owned by requesting org", cid
                )
                errors.append(ConnectorError(
                    connection_id=cid,
                    system_name="(未知)",
                    reason=ERROR_UNAUTHORIZED,
                    detail="这个运维系统不属于你所在的企业，或者已经被吊销",
                ))
            else:
                targets.append(ref)
        return targets, errors

    async def _query_one(
        self, org_id: str, ref: ConnectionRef, request: QueryRequest, timeout_s: float
    ):
        """查单个连接器，把任何失败翻译成 `ConnectorError`。

        **这里必须吞掉所有异常**：fan-out 用的是 `asyncio.gather`，
        任何一个协程抛出去都会污染整批结果，那正是 §3.5 第 4 条禁止的
        "一个连接器出问题让整个分析请求失败"。
        """
        try:
            return await asyncio.wait_for(
                self._transport.query(ref.connection_id, org_id, request, timeout_s),
                timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            return ConnectorError(
                connection_id=ref.connection_id, system_name=ref.system_name,
                reason=ERROR_TIMEOUT,
                detail=f"{timeout_s:.0f} 秒内没有返回，可以试试缩短查询的时间范围",
            )
        except ConnectorUnavailable as e:
            return ConnectorError(
                connection_id=ref.connection_id, system_name=ref.system_name,
                reason=e.reason, detail=e.detail,
            )
        except asyncio.CancelledError:
            # 取消是调用方的意图，不是连接器故障——不要吞，原样往上抛。
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "Ops federated query failed: connection_id=%s err=%s", ref.connection_id, e,
                exc_info=True,
            )
            return ConnectorError(
                connection_id=ref.connection_id, system_name=ref.system_name,
                reason=ERROR_TRANSPORT, detail=f"连接器通信失败: {e}",
            )

    async def connector_health(self, org_id: str) -> List[tuple[ConnectionRef, bool]]:
        """展示/诊断路径：这个 org 的连接器各自在不在线。

        **批量查一次**，不按 id 循环——见 `types.py` 顶部第 3 条。
        """
        refs = list(await self._directory.list_for_org(org_id))
        if not refs:
            return []
        status = await self._transport.online_status([r.connection_id for r in refs])
        return [(r, bool(status.get(r.connection_id, False))) for r in refs]


def describe_unavailable(result: FederatedResult) -> List[str]:
    """把 `errors` 渲染成给用户看的降级提示。

    §3.5 第 4 条要求 UI 上明确标注"来自 XX 系统的数据不可用"。
    放在这里而不是各个调用方各写一遍，是为了让措辞只有一处。
    """
    wording = {
        ERROR_OFFLINE: "连接器当前离线",
        ERROR_TIMEOUT: "查询超时",
        ERROR_UNAUTHORIZED: "无权访问",
        ERROR_TRANSPORT: "与连接器的通信失败",
    }
    return [
        f"来自「{e.system_name}」的数据不可用：{wording.get(e.reason, e.reason)}（{e.detail}）"
        for e in result.errors
    ]
