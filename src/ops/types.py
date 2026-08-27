"""联邦查询层与 BYOC 连接器之间的共享契约（`docs/aiops_module_design.md` §3.5 / §10.1）。

**这个文件是两个并行会话之间唯一的接缝**：联邦查询层（`src/ops/federation/`）
只依赖这里的类型和 `ConnectorTransport` 协议；连接器会话实现那个协议。
两边都不 import 对方，改这个文件必须两边一起确认。

三条已经谈定、不要单方面推翻的约定：

1. **超时由调用方传**（`timeout_s` 参数），不从连接器配置里读——同一个连接器
   在"用户点开大屏"和"后台批量分析"两种场景下的耐心程度不一样，配置读死做不到。
2. **部分失败走返回值，不用异常**。一个连接器超时/离线不能让整个 fan-out 失败，
   也不能被静默丢掉——这是 §3.5 第 4 条的硬性要求。所以
   `FederatedResult` 同时带 `results` 和 `errors`，调用方必须处理 `errors`。
3. **在线状态用批量接口**（`online_status(ids)`），不是逐个 `is_online(id)`。
   真实来源是 `ops_store` 里的 `connector_status` 字段（DB），逐个查会在 fan-out
   里造出一个 N+1——这个项目刚清掉一批同类问题（`CLAUDE.md` §4 P1-14），
   不要在新代码里重新造一个。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol, Sequence, runtime_checkable

# 查询类别。V1 范围见设计文档 §2：指标 / 日志 / 告警三类，不含"预测性容量检测"。
QUERY_KIND_METRIC = "metric"
QUERY_KIND_LOG = "log"
QUERY_KIND_ALERT = "alert"
QUERY_KIND_SERVICE_HEALTH = "service_health"
"""服务发现 + 每个服务的当前健康指标（总览大屏的服务健康网格）。

**没有为它新增帧型**，仍然回 `DataPoint`：每个服务两个点，靠
`labels.service` + `labels.metric` 区分（见 `src/ops/service_health.py`）。
新增帧型意味着所有已部署的连接器都要跟着升级；多一个 kind 取值是向后兼容的
——老连接器不认识它就报错，走既有的部分失败路径，界面上照常标注"数据不可用"。"""

QUERY_KINDS = frozenset({QUERY_KIND_METRIC, QUERY_KIND_LOG, QUERY_KIND_ALERT,
                         QUERY_KIND_SERVICE_HEALTH})

TARGETLESS_QUERY_KINDS = frozenset({QUERY_KIND_SERVICE_HEALTH, QUERY_KIND_ALERT})
"""这两类允许 `target` 为空，含义是**整个企业范围**，不是"忘了填"。

- `service_health`：服务清单本来就是这次查询要发现的东西，事先没有可填的目标。
- `alert`：「今天一共有多少告警」是一个合法的全局问题；指定 target 时仍然照旧
  只查那一个目标。

⚠️ `metric` / `log` **不在这里面**，且刻意不放进来：一个不指定目标的指标查询
没有意义，允许它只会让"忘了传 target"变成一次昂贵的全量扫描，而不是一个当场
就能看见的报错。"""

# 连接器失败的原因分类——**给人看的**，UI 上要能明确告诉用户"来自 XX 系统的数据
# 为什么不可用"（§3.5 第 4 条），所以不能只有一个笼统的 "error"。
ERROR_OFFLINE = "offline"        # 连接器不在线（心跳超时/未注册）
ERROR_TIMEOUT = "timeout"        # 在线但这次查询超时
ERROR_UNAUTHORIZED = "unauthorized"  # 连接器不属于这个 org —— 安全事件，不只是失败
ERROR_TRANSPORT = "transport"    # 连接断了、协议错等传输层问题
ERROR_UPSTREAM = "upstream"      # 连接器连上了，但客户自己的运维系统报错


class InvalidQueryRequest(ValueError):
    """查询请求本身不合法（类别不认识、时间范围颠倒等）。

    这类错误在**发出去之前**就该抛——它不是"某个连接器失败"，
    不该混进 `FederatedResult.errors` 里，那会让调用方以为"换个连接器就好了"。
    """


@dataclass(frozen=True)
class TimeRange:
    """查询的时间窗口，UNIX 秒。"""

    start_ts: float
    end_ts: float

    def __post_init__(self) -> None:
        if self.end_ts <= self.start_ts:
            raise InvalidQueryRequest(
                f"时间范围非法: start_ts={self.start_ts} 必须早于 end_ts={self.end_ts}"
            )

    @property
    def duration_s(self) -> float:
        return self.end_ts - self.start_ts


@dataclass(frozen=True)
class QueryRequest:
    """**抽象查询**——不暴露底层是 Prometheus 还是 Datadog（§3.5 第 1 条）。

    翻译成 PromQL / Datadog API 调用是**连接器在客户环境内**做的事，平台侧
    只表达"要看什么"。这一层刻意保持贫瘠：一旦这里出现 `promql` 这种字段，
    就等于把底层实现漏进了平台侧，联邦查询层也就名存实亡了。
    """

    kind: str
    target: str                      # 服务/实例名，如 "order-service"
    time_range: TimeRange
    metric: Optional[str] = None     # kind=metric 时的指标名，如 "error_rate"
    filters: Dict[str, str] = field(default_factory=dict)
    limit: int = 100

    def __post_init__(self) -> None:
        if self.kind not in QUERY_KINDS:
            raise InvalidQueryRequest(f"未知查询类别: {self.kind!r}（可选：{sorted(QUERY_KINDS)}）")
        if (not self.target or not self.target.strip()) and self.kind not in TARGETLESS_QUERY_KINDS:
            raise InvalidQueryRequest(
                f"target 不能为空（kind={self.kind!r}；只有 {sorted(TARGETLESS_QUERY_KINDS)} "
                "允许留空表示企业全范围）")
        if self.limit <= 0:
            raise InvalidQueryRequest(f"limit 必须为正数，收到 {self.limit}")
        if self.kind == QUERY_KIND_METRIC and not self.metric:
            raise InvalidQueryRequest("kind=metric 时必须指定 metric")

    def fingerprint(self) -> str:
        """缓存键用的稳定指纹。

        用排序后的 JSON 而不是 `hash()`：`hash()` 在不同进程里不稳定
        （PYTHONHASHSEED），将来如果缓存要跨进程共享会静默失效。
        """
        payload = {
            "kind": self.kind,
            "target": self.target,
            "metric": self.metric,
            "start": self.time_range.start_ts,
            "end": self.time_range.end_ts,
            "filters": dict(sorted(self.filters.items())),
            "limit": self.limit,
        }
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class DataPoint:
    """一条数据点。指标是 (时间, 值)，日志/告警把原文放 `text`、结构化字段放 `labels`。"""

    ts: float
    value: Optional[float] = None
    text: Optional[str] = None
    labels: Dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class QueryResult:
    """**单个连接器**的查询结果。多连接器合并后的结构是 `FederatedResult`。"""

    connection_id: str
    system_name: str                 # 客户给这个系统起的名字，UI 上要显示
    points: List[DataPoint] = field(default_factory=list)
    truncated: bool = False          # 命中了 limit，还有更多数据没返回


@dataclass(frozen=True)
class ConnectorError:
    """某个连接器这次为什么没给出数据——**必须能呈现给用户**，不是内部日志。

    `reason` 是给程序判断的分类，`detail` 是给人看的说明。两个都要有：
    只有 detail 就没法在 UI 上按类型做不同处理（离线该提示"去检查连接器"，
    超时该提示"换个更短的时间范围"），只有 reason 则用户看不懂发生了什么。
    """

    connection_id: str
    system_name: str
    reason: str
    detail: str


@dataclass(frozen=True)
class FederatedResult:
    """多连接器 fan-out 合并后的结果。

    ⚠️ **`errors` 非空不等于失败**——它就是"部分失败"的正常表达方式。
    调用方（UI / AI 分析层）必须显式处理它：§3.5 第 4 条要求界面上明确标注
    "来自 XX 系统的数据不可用"，而不是当作没这回事。
    """

    request: QueryRequest
    results: List[QueryResult] = field(default_factory=list)
    errors: List[ConnectorError] = field(default_factory=list)
    from_cache: bool = False

    @property
    def is_partial(self) -> bool:
        """有数据但也有失败的连接器——UI 必须给出降级提示。"""
        return bool(self.errors) and bool(self.results)

    @property
    def is_empty(self) -> bool:
        """一条数据都没拿到。可能是全部连接器都失败，也可能是真的没数据——
        两者的区别看 `errors` 是否为空，调用方不要把它们混为一谈。"""
        return not self.results

    @property
    def all_failed(self) -> bool:
        return bool(self.errors) and not self.results

    def point_count(self) -> int:
        return sum(len(r.points) for r in self.results)


@runtime_checkable
class ConnectorTransport(Protocol):
    """联邦查询层看到的连接器抽象。实现方是 BYOC 连接器会话（另一个会话在写）。

    **联邦查询层只依赖这个协议**，用假件就能完整测试，不需要真的起 WebSocket。
    """

    async def query(
        self,
        connection_id: str,
        org_id: str,
        request: QueryRequest,
        timeout_s: float,
    ) -> QueryResult:
        """向单个连接器下发一次只读查询。

        约定：**超时/离线/上游报错都用抛异常表达**（`asyncio.TimeoutError`
        或下面的 `ConnectorUnavailable`），由联邦查询层统一翻译成
        `ConnectorError` 放进 `FederatedResult.errors`。
        单连接器这一层用异常是合适的——它只有"成功"和"没成功"两种结局；
        "部分失败"是多连接器合并那一层才有的概念。
        """
        ...

    async def online_status(self, connection_ids: Sequence[str]) -> Dict[str, bool]:
        """**批量**查在线状态，一次往返拿全部，不要让调用方按 id 循环调。

        只用于展示/诊断路径。热路径（fan-out）不做在线预检查——
        直接发查询、失败进 errors，省掉一轮往返，语义也更准
        （"心跳说在线"和"这次查得到"本来就不是一回事）。
        """
        ...


class ConnectorUnavailable(RuntimeError):
    """连接器这次用不了。`reason` 用上面的 `ERROR_*` 常量。"""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail or reason


# ---------------------------------------------------------------------------
# 执行侧契约（§3.6 / §10.1 的 exec_request / exec_result 帧）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ExecutionOutcome:
    """一次修复动作在客户环境里的执行结果。"""

    succeeded: bool
    detail: str
    raw: Dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class RemediationDispatcher(Protocol):
    """把**已批准**的执行计划下发到客户环境的连接器。

    ⚠️ **刻意跟 `ConnectorTransport` 分开，不合并成一个协议。**
    查询是只读的、可以随便重试；执行是有副作用的、错一次就是生产事故。
    两者放在同一个协议里，任何"拿到 transport 就什么都能干"的调用点都会
    同时获得执行能力——设计文档 §3.3 把审批列为"不可分割的前置依赖"，
    在类型上就把这两种能力分开，是同一个态度的落实。

    实现方是连接器会话；本协议的实现**不负责检查审批状态**，
    那是工具层的责任（§3.6：执行类工具必须在工具层强制检查，
    不能只依赖上游"应该已经检查过"这种隐式假设）。
    """

    async def execute(
        self,
        connection_id: str,
        org_id: str,
        action_id: str,
        plan: Dict[str, Any],
        timeout_s: float,
    ) -> ExecutionOutcome:
        ...
