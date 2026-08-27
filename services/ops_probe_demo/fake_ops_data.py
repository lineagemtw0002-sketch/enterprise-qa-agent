"""模拟运维数据。**演示件，不是产品交付件**（见本包 `__init__.py`）。

数据由 `environments.py` 里的环境基线 + **当前注入的故障**共同决定：
不注入故障时一切正常，注入之后对应服务的指标才劣化、才产生告警。

⚠️ **刻意不 import 平台侧的检测阈值。** 真实客户系统不可能知道平台用 MAD
还是标准差、阈值是 3.5 还是 3.0；照着阈值造数据等于把"检测器能不能发现问题"
变成自证。这里只把正常态和故障态的量级拉开一个数量级以上，能不能检出交给
检测器自己回答——`scripts/run_ops_probe_scenario.py` 会实际断言它确实检出了。
"""

from __future__ import annotations

import math
import random
import time
from typing import Any, Dict, List, Optional

from services.ops_probe_demo.environments import (
    FAULT_KINDS, Environment, ServiceSpec, effective_spec, resolve_environment,
)

METRIC_INTERVAL_SECONDS = 60.0


def metric_points(*, start_ts: float, end_ts: float, baseline: float,
                  spike: Optional[float] = None, spike_points: int = 4) -> List[Dict[str, Any]]:
    """一条指标时间序列。`spike` 非空时在**尾部**注入几个尖峰。

    ⚠️ **尖峰要多根、要在尾部**：
    - 多根——一根尖峰抬不动标准差，但也正因如此，用一根来演示"稳健统计比
      标准差强"是立不住的；多根才真正复现出遮蔽效应（这是 `detection.py`
      的单测踩过的坑，数据这边要配合）。
    - 尾部——运维界面看的是"最近出了什么事"，尖峰埋在中间不符合真实故障形态。
    """
    span = max(end_ts - start_ts, METRIC_INTERVAL_SECONDS)
    count = max(int(span / METRIC_INTERVAL_SECONDS), 12)
    rng = random.Random(1729)                      # 固定种子：演示要可复现
    points: List[Dict[str, Any]] = []
    for i in range(count):
        ts = start_ts + i * (span / count)
        # 正常波动用一个平缓的正弦 + 小抖动，看起来像真实指标而不是直线。
        wobble = 1.0 + 0.16 * math.sin(i / 3.0) + rng.uniform(-0.08, 0.08)
        value = max(baseline * wobble, 0.0)
        if spike is not None and i >= count - spike_points:
            value = spike * (1.0 + rng.uniform(-0.05, 0.05))
        points.append({"ts": ts, "value": value, "text": "", "labels": {}})
    return points


def alert_points(*, service: str, fault: str, env: Environment,
                 start_ts: float, end_ts: float, count: int = 19) -> List[Dict[str, Any]]:
    """同源告警串——同一个服务、同一组标签、时间彼此相邻。

    ⚠️ **时间必须挤在一起**：告警关联是按"跟上一条的时间差"分组的，散在一小时
    里的 19 条会被切成 19 个事件——那正是"数据不典型导致看起来像功能坏了"的
    典型案例。
    """
    kind = FAULT_KINDS[fault]
    span = min(end_ts - start_ts, 240.0)
    step = span / max(count - 1, 1)
    base = end_ts - span
    return [
        {
            "ts": base + i * step,
            "value": None,
            "text": kind.alert_text.format(service=service, n=i + 1),
            "labels": {
                "target": service, "severity": "error", "env": "prod",
                "cluster": env.cluster, "rule": kind.key,
            },
        }
        for i in range(count)
    ]


def daily_alert_points(*, service: str, fault: str, env: Environment,
                       start_ts: float, end_ts: float, clusters: int = 4) -> List[Dict[str, Any]]:
    """一整天的告警：几簇彼此隔开的同源告警串，用于「今日告警合并」这个 KPI。

    每簇内部挤在几分钟里（能被合并），簇与簇之间隔几小时（不该被合并）。
    这样合并率是一个**有意义**的数字：多条重复告警收敛成少数几个事件。
    全都挤在一起的话合并率会趋近 100%，好看但毫无信息量。
    """
    span = max(end_ts - start_ts, 1.0)
    gap = span / (clusters + 1)
    out: List[Dict[str, Any]] = []
    for i in range(clusters):
        cluster_end = start_ts + gap * (i + 1)
        out.extend(alert_points(service=service, fault=fault, env=env,
                                start_ts=cluster_end - 300.0, end_ts=cluster_end,
                                count=19 if i == 0 else 12 + i))
    return out


def log_points(*, service: str, start_ts: float, end_ts: float,
               count: int = 12) -> List[Dict[str, Any]]:
    span = max(end_ts - start_ts, 1.0)
    step = span / max(count, 1)
    lines = [
        "upstream timed out (110: Connection timed out) while reading response header",
        "connection pool exhausted, waited 5000ms for a free slot",
        "circuit breaker open for downstream dependency",
        "retry budget exhausted, dropping request",
    ]
    return [
        {"ts": start_ts + i * step, "value": None,
         "text": f"[error] {service}: {lines[i % len(lines)]}", "labels": {"level": "error"}}
        for i in range(count)
    ]


def _metric_series_for(spec: ServiceSpec, faulted: bool, *, start_ts: float,
                       end_ts: float) -> List[Dict[str, Any]]:
    baseline = spec.error_rate
    return metric_points(start_ts=start_ts, end_ts=end_ts, baseline=baseline,
                         spike=0.081 if faulted else None)


def points_for(kind: str, *, target: str, start_ts: float, end_ts: float,
               environment: Optional[str] = None,
               faults: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
    """按查询类别产出数据。

    `faults` 是"服务名 → 故障类型"的当前注入状态；空表示一切正常。
    **没有注入故障时不返回任何告警**——一个永远有告警的演示环境，
    没法证明"没问题时不会误报"。
    """
    env = resolve_environment(environment)
    faults = faults or {}

    if kind == "metric":
        spec = env.services.get(target)
        if spec is None:
            return []
        return _metric_series_for(spec, target in faults, start_ts=start_ts, end_ts=end_ts)

    if kind in ("alert", "log"):
        # target 为空 = 问整个企业；否则只问这一个服务。
        targets = [target] if target else sorted(env.services)
        out: List[Dict[str, Any]] = []
        for name in targets:
            fault = faults.get(name)
            if not fault:
                continue
            if kind == "log":
                out.extend(log_points(service=name, start_ts=start_ts, end_ts=end_ts))
            elif end_ts - start_ts > 6 * 3600:
                # 窗口超过 6 小时 = 在问"今天的告警"，给多簇；短窗口 = 在查
                # 某一次故障，给单簇。**簇之间必须隔开**，否则关联层会把一整天
                # 合成一个事件，合并率虚高到没有意义。
                out.extend(daily_alert_points(service=name, fault=fault, env=env,
                                              start_ts=start_ts, end_ts=end_ts))
            else:
                out.extend(alert_points(service=name, fault=fault, env=env,
                                        start_ts=start_ts, end_ts=end_ts))
        return out

    if kind == "service_health":
        return service_health_points(environment=environment, faults=faults)
    return []


def service_health_points(*, environment: Optional[str] = None,
                          faults: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
    """服务健康网格的数据源：每个服务回两个点，靠 `labels.service` +
    `labels.metric` 区分。

    **刻意复用现有的数据点形状，没有给连接器协议加新帧型**——新增的只是一个
    `kind` 取值。加新帧型意味着所有已部署的连接器都要跟着升级；多一个 kind
    取值是向后兼容的，老连接器不认识就报错，走既有的部分失败路径。

    真实连接器在这里做的是 `label_values(up, job)` 之类的服务发现 + 逐个查
    指标；服务清单**不是**企业在平台上手工配的（见 `src/ops/service_health.py`）。
    """
    env = resolve_environment(environment)
    faults = faults or {}
    now = time.time()
    points: List[Dict[str, Any]] = []
    for name, base in env.services.items():
        spec = effective_spec(base, faults.get(name))
        if not spec.reports_metrics:
            # 只报"我发现了这个服务"，不报指标值 → 网格上是「数据中断」。
            # 这跟"服务很健康"是两件完全不同的事，平台侧靠这个区分。
            points.append({"ts": now, "value": 1.0, "text": "",
                           "labels": {"service": name, "metric": "discovered"}})
            continue
        for metric, value in (("error_rate", spec.error_rate),
                              ("p95_latency_ms", spec.p95_latency_ms),
                              ("queue_latency_ms", spec.queue_latency_ms),
                              # 实例数：扩缩容越界判定的唯一可信基线来源。
                              # 真实连接器在这里回 `kube_deployment_status_replicas`
                              # 之类的实测值。
                              ("instances", float(spec.instances))):
            if value is not None:
                points.append({"ts": now, "value": value, "text": "",
                               "labels": {"service": name, "metric": metric}})
    return points
