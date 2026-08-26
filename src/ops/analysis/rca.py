"""根因分析辅助（`docs/aiops_module_design.md` §2 V1 能力之一）。

**这是分析层里唯一用 LLM 的一块**，前两块（异常检测、告警关联）是统计和规则。
分工的理由见 detection.py 顶部：数值判断要可解释，叙述性推理才是模型的强项。

三条硬约束，都不是风格问题：

1. **依据引用由代码从输入里推出来，绝不采信模型输出的引用。**
   设计 §3.1 要求落库的是"分析结论摘要 + 依据引用"，因为审批人要看数据血缘。
   如果让模型自己写引用，它会编出看起来很像真的 PromQL 和时间窗——
   而审批人恰恰会因为"有引用"就更容易相信结论。**模型只负责叙述，引用是算出来的。**

2. **产出必须是"可能原因 + 排查建议"，不是结论。**
   §2 的 V1 范围表里写明 RCA "不代表最终结论，仍需人工判断"。
   措辞上不给模型下定论的机会——提示词里明说，兜底文案里也守着。

3. **LLM 不可用/出错时降级，不抛异常。**
   跟 `doc_summary.py` 的"LLM 未启用或调用失败都退回规则版本"同一个模式：
   分析是辅助能力，模型挂了应该少给一点信息，而不是让整条链路失败。
   降级时 `degraded=True`，**调用方和 UI 必须能看出这次没有模型参与**——
   把降级结果伪装成正常结果是这类系统最不该做的事。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol, Sequence

from src.ops.analysis.correlation import Incident
from src.ops.analysis.detection import AnomalyReport

logger = logging.getLogger(__name__)

MAX_ALERT_SAMPLES = 8
"""喂给模型的告警样本上限。一个事件可能有上百条告警，全塞进去既慢又会把
真正有用的信息淹掉；取最早几条（故障最初的表现通常最接近根因）。"""


class ChatModel(Protocol):
    """项目主链路用的那个生成模型的最小接口（`app.py::_build_llm` 建的
    `ChatOpenAI`，指向 Ollama 的 OpenAI 兼容端点）。

    只声明 `ainvoke` 一个方法：**这一层不需要别的能力，声明多了测试就得多造假件**。
    """

    async def ainvoke(self, prompt: Any) -> Any:
        ...


@dataclass(frozen=True)
class EvidenceRef:
    """一条依据。`detail` 会原样进 `ops_analysis_summaries.evidence_refs`
    （Store 层不解析它的内部结构，格式由这一层定）。"""

    source: str          # anomaly_detection / alert_correlation
    description: str     # 给人看的一句话
    detail: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"source": self.source, "description": self.description, "detail": self.detail}


@dataclass(frozen=True)
class RcaResult:
    summary: str
    likely_causes: List[str] = field(default_factory=list)
    next_steps: List[str] = field(default_factory=list)
    evidence: List[EvidenceRef] = field(default_factory=list)
    degraded: bool = False
    degraded_reason: str = ""

    def to_text(self) -> str:
        """给 LLM/用户看的完整文本。降级时**开头就说明**，不放在末尾——
        放末尾的话，模型转述时经常把它丢掉。"""
        lines: List[str] = []
        if self.degraded:
            lines.append(f"⚠️ 本次分析未经模型推理（{self.degraded_reason}），以下只是数据摘要，不构成根因判断。")
        lines.append(self.summary)
        if self.likely_causes:
            lines.append("可能原因（需人工确认，不是结论）：")
            lines += [f"  {i}. {c}" for i, c in enumerate(self.likely_causes, 1)]
        if self.next_steps:
            lines.append("建议的排查步骤：")
            lines += [f"  {i}. {s}" for i, s in enumerate(self.next_steps, 1)]
        if self.evidence:
            lines.append("依据：")
            lines += [f"  - {e.description}" for e in self.evidence]
        return "\n".join(lines)


def build_evidence(
    incident: Optional[Incident], anomaly_reports: Sequence[AnomalyReport],
) -> List[EvidenceRef]:
    """**从输入数据推导依据，不经过模型。** 见模块顶部第 1 条。"""
    evidence: List[EvidenceRef] = []
    if incident is not None:
        evidence.append(EvidenceRef(
            source="alert_correlation",
            description=incident.describe(),
            detail={
                "incident_id": incident.incident_id,
                "alert_count": incident.alert_count,
                "targets": incident.targets,
                "started_at": incident.started_at,
                "ended_at": incident.ended_at,
                "shared_labels": incident.shared_labels,
                "severity": incident.severity,
                "alert_ids": [a.alert_id for a in incident.alerts[:MAX_ALERT_SAMPLES]],
            },
        ))
    for report in anomaly_reports:
        # 没评估的报告也要留证：审批人需要知道"这个指标我们其实没看"，
        # 而不是默认它正常（见 detection.py 里 evaluated 字段的说明）。
        evidence.append(EvidenceRef(
            source="anomaly_detection",
            description=report.describe(),
            detail={
                "target": report.target,
                "metric": report.metric,
                "evaluated": report.evaluated,
                "baseline": report.baseline,
                "threshold": report.threshold,
                "sample_count": report.sample_count,
                "anomaly_count": len(report.anomalies),
                "worst": (
                    {"ts": max(report.anomalies, key=lambda a: a.score).ts,
                     "value": max(report.anomalies, key=lambda a: a.score).value,
                     "score": max(report.anomalies, key=lambda a: a.score).score}
                    if report.anomalies else None
                ),
            },
        ))
    return evidence


def _rule_based(incident: Optional[Incident], anomaly_reports: Sequence[AnomalyReport]) -> str:
    """不带模型时的摘要：只复述观测到的事实，**不做任何因果推断**。"""
    parts: List[str] = []
    if incident is not None:
        parts.append(incident.describe())
    for r in anomaly_reports:
        parts.append(r.describe())
    return "\n".join(parts) if parts else "没有可用于分析的告警或指标数据。"


def _build_prompt(incident: Optional[Incident], anomaly_reports: Sequence[AnomalyReport]) -> str:
    facts = _rule_based(incident, anomaly_reports)
    samples = ""
    if incident is not None and incident.alerts:
        lines = [f"- [{a.severity}] {a.target}: {a.text or '(无正文)'}"
                 for a in incident.alerts[:MAX_ALERT_SAMPLES]]
        samples = "\n原始告警样本（最多 %d 条）：\n%s" % (MAX_ALERT_SAMPLES, "\n".join(lines))
    return (
        "你是一名运维根因分析助手。下面是从客户运维系统查到的观测事实。\n"
        "请基于这些事实给出**可能的**根因方向和排查步骤。\n\n"
        "硬性要求：\n"
        "1. 你的产出是排查线索，不是结论。不要用「确认」「就是」「根因是」这类断言措辞。\n"
        "2. 只能基于下面给出的事实推理。不要编造任何未出现在事实里的指标名、"
        "服务名、日志内容或时间点。\n"
        "3. 如果事实不足以支持任何推断，就直接说不足以判断，并说明还需要看什么数据。\n"
        "4. 严格按下面的 JSON 格式回复，不要有额外文字：\n"
        '{"summary": "一段话概括观测到了什么", '
        '"likely_causes": ["可能原因1", "可能原因2"], '
        '"next_steps": ["排查步骤1", "排查步骤2"]}\n\n'
        f"观测事实：\n{facts}{samples}\n"
    )


def _extract_text(response: Any) -> str:
    content = getattr(response, "content", response)
    return content if isinstance(content, str) else str(content)


def _parse(raw: str) -> Optional[Dict[str, Any]]:
    """宽松解析模型输出。模型经常在 JSON 前后加解释文字或 ```json 围栏，
    这里截取第一个 `{` 到最后一个 `}`——解析不出来就返回 None，
    由调用方退回"把原文当摘要"，**不抛异常**。"""
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(raw[start:end + 1])
        return parsed if isinstance(parsed, dict) else None
    except (json.JSONDecodeError, ValueError):
        return None


async def analyze_root_cause(
    *,
    incident: Optional[Incident] = None,
    anomaly_reports: Sequence[AnomalyReport] = (),
    llm: Optional[ChatModel] = None,
) -> RcaResult:
    """产出根因分析辅助结论。**任何情况下都不抛异常。**"""
    evidence = build_evidence(incident, anomaly_reports)

    if llm is None:
        return RcaResult(
            summary=_rule_based(incident, anomaly_reports), evidence=evidence,
            degraded=True, degraded_reason="未配置分析模型",
        )

    try:
        raw = _extract_text(await llm.ainvoke(_build_prompt(incident, anomaly_reports)))
    except Exception as e:  # noqa: BLE001
        logger.warning("Ops RCA LLM call failed, falling back to rule-based summary: %s", e, exc_info=True)
        return RcaResult(
            summary=_rule_based(incident, anomaly_reports), evidence=evidence,
            degraded=True, degraded_reason=f"模型调用失败：{e}",
        )

    parsed = _parse(raw)
    if parsed is None:
        # 模型给了自由文本而不是 JSON。原文仍然有价值，当摘要用；
        # 但**不标 degraded**——模型确实推理了，只是格式没听话。
        return RcaResult(summary=raw.strip() or _rule_based(incident, anomaly_reports), evidence=evidence)

    def _strlist(key: str) -> List[str]:
        value = parsed.get(key) or []
        return [str(x) for x in value if str(x).strip()] if isinstance(value, list) else []

    return RcaResult(
        summary=str(parsed.get("summary") or "").strip() or _rule_based(incident, anomaly_reports),
        likely_causes=_strlist("likely_causes"),
        next_steps=_strlist("next_steps"),
        evidence=evidence,
    )
