"""告警关联降噪（`docs/aiops_module_design.md` §2 V1 能力之一）。

把"一个故障炸出几十条告警"合并成一个事件。跟异常检测一样**不用 LLM**——
这是分组问题，规则 + 相似度就能做，而且必须可解释：审批人看到"这 17 条告警是
同一件事"时，要能追问"凭什么"，答案得是"时间相邻 + 标签重合度 0.8"这种能核对的
东西，不能是"模型觉得像"。

分组规则（单趟扫描，按时间排序）：一条告警加入某个已开事件，当且仅当
① 它跟该事件**最后一条**告警的时间差 ≤ `window_s`，且
② 它跟该事件的标签相似度（Jaccard）≥ `min_similarity`。

⚠️ **窗口是"跟最后一条比"而不是"跟事件起点比"**，这是刻意的：一次持续
20 分钟的故障会不断产生新告警，按起点算会在 5 分钟处硬切成好几个事件，
而它们明明是同一件事。代价是理论上可以无限延长（一条一条接龙），
所以 `max_span_s` 兜一个总长度上限——**这两个参数是一对，改一个必须想另一个**。

⚠️ 降噪率（`noise_reduction`）如实算、不修饰。行业宣传里 90–95% 的数字很常见，
但设计 §10.5 定的 V1 目标是"能测量"而不是"达到某个数字"——这里只负责把它算出来。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

DEFAULT_WINDOW_SECONDS = 300.0
DEFAULT_MIN_SIMILARITY = 0.4
DEFAULT_MAX_SPAN_SECONDS = 3600.0

SEVERITY_ORDER = {"critical": 3, "error": 2, "warning": 1, "info": 0}


@dataclass(frozen=True)
class Alert:
    """一条原始告警。字段刻意保持贫瘠——不同客户系统的告警结构差别很大，
    联邦查询层已经把它们归一成这几个共同字段了。"""

    alert_id: str
    ts: float
    target: str
    labels: Dict[str, str] = field(default_factory=dict)
    text: str = ""
    severity: str = "warning"


@dataclass(frozen=True)
class Incident:
    incident_id: str
    alerts: List[Alert]
    started_at: float
    ended_at: float
    targets: List[str]
    shared_labels: Dict[str, str]
    severity: str

    @property
    def alert_count(self) -> int:
        return len(self.alerts)

    def describe(self) -> str:
        span = self.ended_at - self.started_at
        label_str = "、".join(f"{k}={v}" for k, v in sorted(self.shared_labels.items())) or "无共同标签"
        return (
            f"事件 {self.incident_id}：{self.alert_count} 条告警合并，"
            f"影响 {'、'.join(self.targets)}，持续 {span:.0f} 秒，"
            f"最高级别 {self.severity}，共同特征：{label_str}。"
        )


@dataclass(frozen=True)
class CorrelationResult:
    incidents: List[Incident]
    original_count: int

    @property
    def noise_reduction(self) -> float:
        """(1 - 事件数/告警数)。告警为空时返回 0.0——**不是 1.0**：
        没有告警不等于降噪 100%，那会让空数据在报表上看起来像最好的成绩。"""
        if self.original_count <= 0:
            return 0.0
        return 1.0 - len(self.incidents) / self.original_count


def _fingerprint(alert: Alert) -> set:
    """一条告警的特征集合：目标 + 每个标签键值对。用于算 Jaccard 相似度。"""
    return {f"target={alert.target}"} | {f"{k}={v}" for k, v in alert.labels.items()}


def _similarity(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _worst_severity(alerts: Sequence[Alert]) -> str:
    return max((a.severity for a in alerts), key=lambda s: SEVERITY_ORDER.get(s, 0), default="info")


def correlate_alerts(
    alerts: Sequence[Alert],
    *,
    window_s: float = DEFAULT_WINDOW_SECONDS,
    min_similarity: float = DEFAULT_MIN_SIMILARITY,
    max_span_s: float = DEFAULT_MAX_SPAN_SECONDS,
) -> CorrelationResult:
    """把告警列表合并成事件列表。纯函数，不看时钟、不查库。"""
    if not alerts:
        return CorrelationResult(incidents=[], original_count=0)

    ordered = sorted(alerts, key=lambda a: a.ts)
    open_groups: List[List[Alert]] = []

    for alert in ordered:
        fp = _fingerprint(alert)
        placed = False
        # 从最近的组往前找——同一时刻的故障告警在时间上是聚在一起的，
        # 倒序能让绝大多数告警在第一两次比较就命中。
        for group in reversed(open_groups):
            last = group[-1]
            if alert.ts - last.ts > window_s:
                continue
            if alert.ts - group[0].ts > max_span_s:
                continue
            group_fp = set().union(*(_fingerprint(a) for a in group))
            if _similarity(fp, group_fp) >= min_similarity:
                group.append(alert)
                placed = True
                break
        if not placed:
            open_groups.append([alert])

    incidents = []
    for group in open_groups:
        # 共同标签 = 组内**每一条**告警都带的键值对。用交集而不是并集：
        # 并集会把"某一条告警独有的标签"也说成事件特征，审批人照着它去查会扑空。
        shared = set.intersection(*({f"{k}={v}" for k, v in a.labels.items()} or set() for a in group)) \
            if all(a.labels for a in group) else set()
        incidents.append(Incident(
            incident_id=f"opsinc_{uuid.uuid4().hex[:10]}",
            alerts=group,
            started_at=group[0].ts,
            ended_at=group[-1].ts,
            targets=sorted({a.target for a in group}),
            shared_labels=dict(p.split("=", 1) for p in sorted(shared)),
            severity=_worst_severity(group),
        ))
    return CorrelationResult(incidents=incidents, original_count=len(alerts))
