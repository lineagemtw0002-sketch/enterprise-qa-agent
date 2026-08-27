"""模拟客户运维系统的数据（指标 / 日志 / 告警）。

**这个文件唯一的难点是：生成的数据必须真的能触发平台侧的检测器。**
随手编几个数的话，探针接上了、查询也通了，但大屏上依然什么都没有——
异常检测判定"无异常"、告警关联合并出 N 个只有 1 条的事件，
看起来像功能坏了，实际是数据不够典型。

所以这里的取值是有依据的，但**刻意不 import 平台侧的阈值常量**：
真实的客户系统不可能知道平台用的是 MAD 还是标准差、阈值是 3.5 还是 3.0。
探针要是照着阈值生成数据，就等于把"检测器能不能发现问题"这件事变成了自证。
取而代之的做法是**把基线和异常拉开一个任何合理阈值都能发现的量级**
（错误率 0.5% → 8%，16 倍），再由场景脚本**实际断言检测确实触发了**
（见 `scripts/run_ops_probe_scenario.py`）——用验证代替调参。
"""

from __future__ import annotations

import math
import random
import time
from typing import Any, Dict, List

# 基线与异常的量级刻意拉开：不针对任何具体阈值调参，见模块顶部说明。
BASELINE_ERROR_RATE = 0.5      # %
SPIKE_ERROR_RATE = 8.0         # %
BASELINE_JITTER = 0.08         # 基线上的正常抖动，远小于两者之差

SAMPLE_INTERVAL_SECONDS = 60.0
"""采样间隔。1 小时窗口 → 60 个点，远超检测器要求的最小样本数。"""


def metric_points(
    *, start_ts: float, end_ts: float, baseline: float = BASELINE_ERROR_RATE,
    inject_anomaly: bool = True, anomaly_value: float = SPIKE_ERROR_RATE,
    anomaly_points: int = 4, seed: int = 20260827,
) -> List[Dict[str, Any]]:
    """一条指标序列。`inject_anomaly=False` 时是纯正常序列——
    **这个分支同样重要**：要能验证"没有异常时检测器确实不报"，
    否则一个永远报异常的检测器也会通过演示。"""
    rng = random.Random(seed)
    points: List[Dict[str, Any]] = []
    ts = start_ts
    while ts < end_ts:
        points.append({"ts": ts, "value": round(baseline + rng.uniform(-BASELINE_JITTER, BASELINE_JITTER), 4)})
        ts += SAMPLE_INTERVAL_SECONDS

    if inject_anomaly and len(points) > anomaly_points:
        # 异常放在序列尾部——真实故障就是"刚刚开始出问题"，放中间会让
        # 时间线上的"最近一次分析"看起来跟当前状态脱节。
        for p in points[-anomaly_points:]:
            p["value"] = round(anomaly_value + rng.uniform(-0.3, 0.3), 4)
    return points


def alert_points(
    *, target: str, start_ts: float, end_ts: float, count: int = 19,
    severity: str = "error", cluster: str = "prod-cluster-1",
) -> List[Dict[str, Any]]:
    """同源告警串——同一个 target、同一组 labels、时间彼此相邻。

    默认 19 条是照着设计稿里那个"19 条 → 1 个事件"的例子来的，
    只是为了让演示画面跟当初的设计意图对得上；数量本身不影响能不能合并。

    ⚠️ **时间必须挤在一起**：告警关联是按"跟上一条的时间差"分组的，
    散在一小时里的 19 条会被切成 19 个事件——那正是"数据不典型导致
    看起来像功能坏了"的典型案例。
    """
    span = min(end_ts - start_ts, 240.0)   # 挤在 4 分钟内
    step = span / max(count - 1, 1)
    base = end_ts - span
    return [
        {
            "ts": base + i * step,
            "text": f"{target} 5xx 比例超过阈值（第 {i + 1} 次触发）",
            "labels": {
                "target": target, "severity": severity, "env": "prod",
                "cluster": cluster, "rule": "http_5xx_ratio",
            },
        }
        for i in range(count)
    ]


def daily_alert_points(*, target: str, start_ts: float, end_ts: float,
                       clusters: int = 4) -> List[Dict[str, Any]]:
    """一整天的告警：几簇彼此隔开的同源告警串，用于「今日告警合并」这个 KPI。

    每簇内部挤在几分钟里（能被合并），簇与簇之间隔几小时（不该被合并）。
    这样合并率是一个**有意义**的数字：多条重复告警收敛成少数几个事件。
    如果所有告警都挤在一起，合并率会趋近 100%，好看但毫无信息量。
    """
    span = max(end_ts - start_ts, 1.0)
    gap = span / (clusters + 1)
    out: List[Dict[str, Any]] = []
    for i in range(clusters):
        cluster_end = start_ts + gap * (i + 1)
        out.extend(alert_points(
            target=target, start_ts=cluster_end - 300.0, end_ts=cluster_end,
            count=19 if i == 0 else 12 + i,
            cluster=f"prod-cluster-{i + 1}",
        ))
    return out


def log_points(*, target: str, start_ts: float, end_ts: float, count: int = 12) -> List[Dict[str, Any]]:
    """日志样本。RCA 那一层会把它们当上下文喂给模型，所以内容要像真的
    ——全是"log line 1/2/3"的话，模型只能编。"""
    templates = [
        "upstream payment-gateway timeout after 3000ms, retrying (attempt {i})",
        "connection pool exhausted: active=64 idle=0 waiting={i}",
        "circuit breaker half-open for payment-gateway",
        "request rejected: too many concurrent connections",
    ]
    step = (end_ts - start_ts) / max(count, 1)
    return [
        {
            "ts": start_ts + i * step,
            "text": templates[i % len(templates)].format(i=i + 1),
            "labels": {"target": target, "level": "ERROR"},
        }
        for i in range(count)
    ]


def points_for(kind: str, *, target: str, start_ts: float, end_ts: float,
               healthy: bool = False) -> List[Dict[str, Any]]:
    """按查询类别产出数据。`healthy=True` 时给"一切正常"的版本，
    用于验证检测器不会无中生有。"""
    if kind == "metric":
        return metric_points(start_ts=start_ts, end_ts=end_ts, inject_anomaly=not healthy)
    if kind == "alert":
        if healthy:
            return []
        # 查询窗口超过 6 小时 = 调用方在问"今天的告警"，给多簇；
        # 短窗口 = 在查某一次故障，给单簇。**簇之间必须隔开**，否则
        # 关联层会把一整天的告警合成一个事件，合并率虚高到没有意义。
        if end_ts - start_ts > 6 * 3600:
            return daily_alert_points(target=target, start_ts=start_ts, end_ts=end_ts)
        return alert_points(target=target, start_ts=start_ts, end_ts=end_ts)
    if kind == "service_health":
        return service_health_points(healthy=healthy)
    if kind == "log":
        return [] if healthy else log_points(target=target, start_ts=start_ts, end_ts=end_ts)
    return []


# 设计稿（`docs/design_reference/aiops_console_mockup.html`）里那六个服务。
# **数字刻意跟设计稿对齐**，这样演示环境长出来的样子跟设计稿一致；
# 数据本身是假的没关系——这个包整体就是模拟的客户环境，不假装是真实测量。
#
# ⚠️ `search-index` 刻意**不返回任何指标点**：设计稿上它那格是"—/同步中"，
# 而 `service_health.classify()` 在两个指标都缺时判 `warning` 而不是 `ok`
# ——"查不到"和"很健康"是两件不同的事。留着这个服务是为了让这条边界
# 在演示环境里真的被走到，而不是只存在于单测里。
_SERVICES = {
    "order-service":       {"error_rate": 0.081,  "p95_latency_ms": 2400.0},
    "payment-gateway":     {"error_rate": 0.012,  "p95_latency_ms": 640.0},
    "auth-service":        {"error_rate": 0.0002, "p95_latency_ms": 88.0},
    "inventory-api":       {"error_rate": 0.0005, "p95_latency_ms": 121.0},
    # 队列延迟走独立的指标名和独立的阈值——异步 worker 排队 3 秒是正常的，
    # 同一个数字放在 HTTP 延迟上就是严重故障。
    "notification-worker": {"error_rate": 0.001,  "queue_latency_ms": 3000.0},
    # 只报"我发现了这个服务"，不报任何指标值 → 网格上是「数据中断」，
    # 跟设计稿上那一格的"—/同步中"一致。
    "search-index":        {"discovered": 1.0},
}


def service_health_points(healthy: bool = False):
    """服务健康网格的数据源：每个服务回两个点，靠 `labels.service` + `labels.metric` 区分。

    **刻意复用现有的数据点形状，没有给连接器协议加新帧型**——新增的只是一个
    `kind` 取值（`service_health`）。加新帧型意味着所有已部署的连接器都得跟着
    升级；多一个 kind 取值是向后兼容的，老连接器不认识就报错，走既有的
    部分失败路径。

    真实连接器在这里做的是 `label_values(up, job)` 之类的服务发现 + 逐个查指标；
    服务清单**不是**企业在平台上手工配的（见 `src/ops/service_health.py` 顶部）。
    """
    now = time.time()
    points = []
    for name, metrics in _SERVICES.items():
        for metric, value in metrics.items():
            if healthy and metric == "error_rate":
                value = 0.0003
            if healthy and metric == "p95_latency_ms":
                value = min(value, 150.0)
            points.append({"ts": now, "value": value, "text": "",
                           "labels": {"service": name, "metric": metric}})
    return points
