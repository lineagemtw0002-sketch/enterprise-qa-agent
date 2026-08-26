"""异常检测（`docs/aiops_module_design.md` §2 V1 能力之一）。

**这一层刻意不用 LLM。** 异常检测是数值问题：LLM 在上面既慢又不稳定，更要命的是
它说不清"为什么这算异常"——而审批人需要看数据血缘（§3.3），一个解释不了自己的
判断在这条链路上没有价值。行业里也是这么分工的（Datadog/AWS 的异常检测走统计方法，
LLM 集中在根因分析那一层）。

**用中位数 + MAD（绝对中位差），不用均值 + 标准差**，理由是"遮蔽效应"
（masking）：一根足够高的尖峰会把标准差本身抬上去，于是它自己落在
"均值 ± 3σ"里面，检测不出来——而尖峰恰恰是最该被抓到的那种异常。中位数和 MAD
都是稳健统计量，少数极端值改不动它们。这是这个模块唯一一处"看起来可以随便换个
公式"但其实不能换的地方。
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from src.ops.types import DataPoint

DEFAULT_SENSITIVITY = 3.5
"""稳健 z 分数的阈值。3.5 是 Iglewicz & Hoaglin 给的经验值（配 0.6745 的换算
常数），比"3σ"更保守一点——这条链路的下游是**在生产环境执行修复动作**，
宁可漏报一个也不要多报一个，多报的代价是有人被叫起来看一个不存在的问题。"""

MIN_SAMPLES = 8
"""少于这么多点就不判断。**返回"没评估"而不是"没异常"**——两者对调用方是
完全不同的信息：前者该去把时间窗拉长，后者可以放心。把样本不足伪装成"一切正常"
是这类系统最容易骗人的地方。"""

_MAD_TO_SIGMA = 0.6745  # MAD 换算成正态分布标准差的常数

DIRECTION_SPIKE = "spike"
DIRECTION_DIP = "dip"


@dataclass(frozen=True)
class AnomalyPoint:
    ts: float
    value: float
    score: float          # 稳健 z 分数（绝对值）
    direction: str        # spike（高于基线）/ dip（低于基线）


@dataclass(frozen=True)
class AnomalyReport:
    """检测结果。

    `evaluated=False` 表示**没有做出判断**（样本不足/没有数值点），
    这时 `anomalies` 为空**不代表正常**。调用方必须区分对待，
    所以这里用一个显式字段而不是让调用方去猜空列表的含义。
    """

    target: str
    metric: Optional[str]
    evaluated: bool
    reason: str = ""
    baseline: Optional[float] = None
    spread: Optional[float] = None          # 换算成 σ 量级的离散度
    threshold: float = DEFAULT_SENSITIVITY
    anomalies: List[AnomalyPoint] = field(default_factory=list)
    sample_count: int = 0

    @property
    def has_anomaly(self) -> bool:
        return self.evaluated and bool(self.anomalies)

    def describe(self) -> str:
        """给人看的一句话——同时也是喂给 RCA 那一层的输入之一。"""
        if not self.evaluated:
            return f"{self.target} 的 {self.metric or '指标'}：{self.reason}，本次未做异常判断。"
        if not self.anomalies:
            return (f"{self.target} 的 {self.metric or '指标'}：{self.sample_count} 个采样点内未发现偏离基线的异常"
                    f"（基线 {self.baseline:.4g}）。")
        worst = max(self.anomalies, key=lambda a: a.score)
        word = "高于" if worst.direction == DIRECTION_SPIKE else "低于"
        return (
            f"{self.target} 的 {self.metric or '指标'}：{len(self.anomalies)} 个点偏离基线，"
            f"最显著的一个 {worst.value:.4g} 明显{word}基线 {self.baseline:.4g}"
            f"（稳健 z={worst.score:.1f}，阈值 {self.threshold}）。"
        )


def detect_anomalies(
    points: Sequence[DataPoint],
    *,
    target: str,
    metric: Optional[str] = None,
    sensitivity: float = DEFAULT_SENSITIVITY,
    min_samples: int = MIN_SAMPLES,
) -> AnomalyReport:
    """对一条指标序列做基线偏离检测。

    纯函数：进去是数据点，出来是结论，不查库、不调模型、不看时钟。
    """
    values = [p.value for p in points if p.value is not None]
    if len(values) < min_samples:
        return AnomalyReport(
            target=target, metric=metric, evaluated=False, sample_count=len(values),
            reason=f"只有 {len(values)} 个有效数值点，少于所需的 {min_samples} 个",
        )

    baseline = statistics.median(values)
    deviations = [abs(v - baseline) for v in values]
    mad = statistics.median(deviations)

    if mad == 0:
        # 一半以上的点完全相同（常见于恒定值指标，或者采样精度太粗）。
        # 这时 MAD 退化成 0，稳健 z 分数会除零。用"平均绝对偏差"兜底；
        # 如果连它也是 0，说明整条序列是常数——**常数序列没有异常可言**，
        # 不能因为除零就把每个点都报成异常。
        mean_abs = sum(deviations) / len(deviations)
        if mean_abs == 0:
            return AnomalyReport(
                target=target, metric=metric, evaluated=True, baseline=baseline,
                spread=0.0, threshold=sensitivity, sample_count=len(values),
                reason="序列为常数，无离散度可言",
            )
        spread = mean_abs * 1.253314  # 平均绝对偏差 → σ 的换算常数
    else:
        spread = mad / _MAD_TO_SIGMA

    anomalies = [
        AnomalyPoint(
            ts=p.ts, value=p.value, score=abs(p.value - baseline) / spread,
            direction=DIRECTION_SPIKE if p.value > baseline else DIRECTION_DIP,
        )
        for p in points
        if p.value is not None and abs(p.value - baseline) / spread >= sensitivity
    ]
    return AnomalyReport(
        target=target, metric=metric, evaluated=True, baseline=baseline, spread=spread,
        threshold=sensitivity, anomalies=anomalies, sample_count=len(values),
    )
