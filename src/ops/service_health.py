"""服务健康网格的判定逻辑（§9.2 大屏「服务健康」）——**纯函数，不碰数据库、不发查询**。

跟 `aiops_scope.py` / `activation.py` 同一个模式：判定写成纯函数，才测得动。

## 服务清单从哪来（2026-08-27 用户拍板）

**连接器自动发现**，不是企业在平台上手工配置一份清单。理由是业界共识：
Datadog Service Catalog 从 APM 遥测里自动长出服务、Grafana 从
`label_values(up, job)` 发现——**手工维护的清单必然腐烂**，新服务上线没人
去平台补一条，网格上就永远看不到它。

这条也正好跟本模块的 BYOC 架构对上：探针就在客户环境里，客户的监控系统
本来就知道有哪些服务，让企业再手抄一遍既多余又必然过期。

⚠️ **平台侧不存服务清单**，每次打开总览现查现用——跟 §3.1「不落库原始运维
数据」是同一条原则。清单是运维现状的投影，存下来就会跟现实脱节。

## 阈值：平台默认值 + 两级覆盖（2026-08-27）

不同服务的可接受错误率差别很大——支付网关和内部报表不是一回事，用一套固定
阈值判所有服务，要么把正常服务染红、要么把真故障判成正常。两者都会让人很快
学会忽略这个网格。

解析顺序（**逐字段**，不是整块替换）：

    某个服务的配置 → 该连接器的默认配置（`service="*"`）→ 平台内置默认值

**逐字段回退是刻意的**：管理员只想改一个错误率阈值时，不该被迫把六个数字
全填一遍——填了就等于把其余五个也冻结在填写那天的值上，以后平台默认值改了
它们也不会跟着动。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

STATUS_CRITICAL = "critical"
STATUS_WARNING = "warning"
STATUS_OK = "ok"
STATUS_STALE = "stale"
"""**「数据中断」，跟"健康"和"有问题"都不是一回事。**

服务被发现了、但一个指标都查不回来（连接器同步中、查询超时、该服务暂时
没有遥测）。设计稿上 `search-index` 那一格就是这个状态。

⚠️ 早前这里返回的是 `warning`，那是错的——它会让一个**根本没被观测到**的
服务混进"观察中"，看起来像"我们看过了，有点小问题"。真实情况是我们什么
都不知道。这是运维界面上最不能糊弄的一类区分。"""

ERROR_RATE_CRITICAL = 0.05
ERROR_RATE_WARNING = 0.01
P95_CRITICAL_MS = 2000.0
P95_WARNING_MS = 500.0

QUEUE_LATENCY_CRITICAL_MS = 30000.0
QUEUE_LATENCY_WARNING_MS = 10000.0
"""⚠️ **队列延迟的容忍度比 HTTP 延迟高一个量级，必须分开判。**

异步 worker 排队 3 秒是正常的，同一个数字放在 HTTP 请求上就是严重故障。
早前用同一套 P95 阈值判所有延迟，把设计稿里标着"正常"的
`notification-worker`（队列延迟 3s）判成了 critical——**一个把正常服务染红的
健康网格，比没有这个网格更糟**，看的人会很快学会忽略红色。"""

METRIC_ERROR_RATE = "error_rate"
METRIC_P95_MS = "p95_latency_ms"
METRIC_QUEUE_LATENCY_MS = "queue_latency_ms"
METRIC_DISCOVERED = "discovered"
"""服务存在性标记：连接器发现了这个服务，但这一轮没有指标值。
有了它，网格才能把"服务不存在"和"服务在但查不到数据"区分开。"""

_KNOWN_METRICS = (METRIC_ERROR_RATE, METRIC_P95_MS, METRIC_QUEUE_LATENCY_MS, METRIC_DISCOVERED)


@dataclass(frozen=True)
class Thresholds:
    """一套完整的判定阈值。**每个字段都必须有值**——`resolve_thresholds()`
    负责把不完整的覆盖补齐，到了这里就不该再有 None，否则判定逻辑里到处都要
    判空，而"这个阈值没配"和"这个指标没有值"会混成一团。"""

    error_rate_warning: float = ERROR_RATE_WARNING
    error_rate_critical: float = ERROR_RATE_CRITICAL
    p95_warning_ms: float = P95_WARNING_MS
    p95_critical_ms: float = P95_CRITICAL_MS
    queue_warning_ms: float = QUEUE_LATENCY_WARNING_MS
    queue_critical_ms: float = QUEUE_LATENCY_CRITICAL_MS

    def to_dict(self) -> Dict[str, float]:
        return {
            "error_rate_warning": self.error_rate_warning,
            "error_rate_critical": self.error_rate_critical,
            "p95_warning_ms": self.p95_warning_ms,
            "p95_critical_ms": self.p95_critical_ms,
            "queue_warning_ms": self.queue_warning_ms,
            "queue_critical_ms": self.queue_critical_ms,
        }


DEFAULT_THRESHOLDS = Thresholds()
THRESHOLD_FIELDS = tuple(DEFAULT_THRESHOLDS.to_dict().keys())
WILDCARD_SERVICE = "*"


class InvalidThresholds(ValueError):
    """阈值配置非法。跟 `aiops_scope.InvalidScopeConfig` 同一个档位：
    管理员填错了要当场报错，不能夹紧成一个看起来正常的值悄悄用下去。"""


def validate_thresholds(raw: Any) -> Dict[str, float]:
    """校验一份（可能不完整的）阈值覆盖，返回只含合法字段的字典。

    三条硬规则：
    1. **只认已知字段**——写错字段名必须报错，不能静默忽略。静默忽略的后果是
       管理员以为自己改了阈值、界面也保存成功，实际一点没生效。
    2. **必须是正数**（`bool` 显式排除，它是 `int` 的子类）。
    3. **同一指标的 warning 必须 ≤ critical**，且**要一起判**：只改 critical
       把它压到已有的 warning 之下，那个指标就永远跳不到 warning 档——一个
       永远不会出现的状态比配错更难被发现。
    """
    if not isinstance(raw, dict):
        raise InvalidThresholds("阈值配置必须是一个对象")

    unknown = sorted(set(raw) - set(THRESHOLD_FIELDS))
    if unknown:
        raise InvalidThresholds(f"未知的阈值字段：{unknown}（可选：{list(THRESHOLD_FIELDS)}）")

    out: Dict[str, float] = {}
    for key, value in raw.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise InvalidThresholds(f"{key} 必须是数字，收到 {value!r}")
        if value <= 0:
            raise InvalidThresholds(f"{key} 必须大于 0，收到 {value}")
        out[key] = float(value)

    merged = {**DEFAULT_THRESHOLDS.to_dict(), **out}
    for warn_key, crit_key in (("error_rate_warning", "error_rate_critical"),
                               ("p95_warning_ms", "p95_critical_ms"),
                               ("queue_warning_ms", "queue_critical_ms")):
        if merged[warn_key] > merged[crit_key]:
            raise InvalidThresholds(
                f"{warn_key}({merged[warn_key]}) 不能大于 {crit_key}({merged[crit_key]})"
                "——那会让这个指标永远跳不到「观察中」这一档")
    return out


def resolve_thresholds(*overrides: Optional[Dict[str, Any]]) -> Thresholds:
    """按传入顺序**逐字段**叠加覆盖，后面的优先级更高。

    调用方按 `(连接器默认, 该服务专属)` 的顺序传即可；`None` 和空字典都表示
    "这一层没有配置"，直接跳过。
    """
    merged = DEFAULT_THRESHOLDS.to_dict()
    for override in overrides:
        if override:
            merged.update({k: float(v) for k, v in override.items() if k in THRESHOLD_FIELDS})
    return Thresholds(**merged)


@dataclass
class ServiceHealth:
    service: str
    status: str
    error_rate: Optional[float] = None
    p95_latency_ms: Optional[float] = None
    queue_latency_ms: Optional[float] = None
    connection_id: Optional[str] = None
    connector_name: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "service": self.service, "status": self.status,
            "error_rate": self.error_rate, "p95_latency_ms": self.p95_latency_ms,
            "queue_latency_ms": self.queue_latency_ms,
            "connection_id": self.connection_id, "connector_name": self.connector_name,
        }


def classify(error_rate: Optional[float], p95_latency_ms: Optional[float],
             queue_latency_ms: Optional[float] = None,
             thresholds: Optional[Thresholds] = None) -> str:
    """两个指标各判一次，**取较严重的那个**。

    ⚠️ 取严重侧而不是取平均，是刻意的：一个服务延迟正常但错误率 8%，它就是
    坏的——平均一下变成"观察中"会把真实故障降级成一个不那么扎眼的颜色。

    ⚠️ **两个指标都缺时返回 `ok` 是错的，返回的是 `warning`**：
    "查不到这个服务的数据"和"这个服务很健康"是两件完全不同的事，
    渲染成绿色会让人以为已经确认过它没问题。
    """
    if error_rate is None and p95_latency_ms is None and queue_latency_ms is None:
        return STATUS_STALE

    th = thresholds or DEFAULT_THRESHOLDS
    levels = []
    if error_rate is not None:
        levels.append(STATUS_CRITICAL if error_rate >= th.error_rate_critical
                      else STATUS_WARNING if error_rate >= th.error_rate_warning else STATUS_OK)
    if p95_latency_ms is not None:
        levels.append(STATUS_CRITICAL if p95_latency_ms >= th.p95_critical_ms
                      else STATUS_WARNING if p95_latency_ms >= th.p95_warning_ms else STATUS_OK)
    if queue_latency_ms is not None:
        levels.append(STATUS_CRITICAL if queue_latency_ms >= th.queue_critical_ms
                      else STATUS_WARNING if queue_latency_ms >= th.queue_warning_ms else STATUS_OK)
    for worst in (STATUS_CRITICAL, STATUS_WARNING):
        if worst in levels:
            return worst
    return STATUS_OK


def points_to_services(points: Iterable[Dict[str, Any]], *, connection_id: Optional[str] = None,
                       connector_name: Optional[str] = None,
                       overrides: Optional[Dict[str, Dict[str, Any]]] = None) -> List[ServiceHealth]:
    """把连接器回的扁平数据点聚成一个个服务。

    **刻意复用现有的 `DataPoint` 形状（ts/value/labels），没有给连接器协议加新帧型**
    ——每个服务回两个点（`labels.metric` 分别是 `error_rate` 和 `p95_latency_ms`），
    新增的只是一个 `kind` 取值。加新帧型意味着所有已部署的连接器都要跟着升级，
    而多一个 kind 取值是向后兼容的：老连接器不认识就报错，走既有的部分失败路径。

    没有 `labels.service` 的点直接跳过——**不猜**。真实连接器什么都可能回，
    在这里靠位置或顺序去猜哪个点属于哪个服务，会在数据稍有变化时静默错配。
    """
    bucket: Dict[str, Dict[str, float]] = {}
    for p in points:
        labels = (p.get("labels") or {}) if isinstance(p, dict) else {}
        service, metric = labels.get("service"), labels.get("metric")
        if not service or metric not in _KNOWN_METRICS:
            continue
        slot = bucket.setdefault(service, {})
        if metric == METRIC_DISCOVERED:
            continue                      # 只用来建桶，本身不是一个可判定的指标
        value = p.get("value")
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        slot[metric] = float(value)

    out = [
        ServiceHealth(
            service=name,
            status=classify(m.get(METRIC_ERROR_RATE), m.get(METRIC_P95_MS),
                            m.get(METRIC_QUEUE_LATENCY_MS),
                            thresholds=resolve_thresholds(
                                (overrides or {}).get(WILDCARD_SERVICE),
                                (overrides or {}).get(name))),
            error_rate=m.get(METRIC_ERROR_RATE), p95_latency_ms=m.get(METRIC_P95_MS),
            queue_latency_ms=m.get(METRIC_QUEUE_LATENCY_MS),
            connection_id=connection_id, connector_name=connector_name,
        )
        for name, m in bucket.items()
    ]
    # 坏的排在前面——大屏上最该先被看见的是出问题的服务，不是字母序靠前的。
    order = {STATUS_CRITICAL: 0, STATUS_WARNING: 1, STATUS_STALE: 2, STATUS_OK: 3}
    return sorted(out, key=lambda s: (order.get(s.status, 3), s.service))


def median_seconds(values: List[float]) -> Optional[float]:
    """中位数。**空列表返回 `None` 而不是 0**——"还没有样本"和"耗时是 0"
    是两件不同的事，糊在一起会让刚开始用的企业看到一个漂亮但假的 MTTR。"""
    vals = sorted(v for v in values if isinstance(v, (int, float)) and v >= 0)
    if not vals:
        return None
    mid = len(vals) // 2
    return float(vals[mid]) if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2.0
