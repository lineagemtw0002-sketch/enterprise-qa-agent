"""智能运维的 AI 分析层（`docs/aiops_module_design.md` §2）。

三块能力，**分工是刻意的**：异常检测和告警关联是统计/规则（可解释、快、稳），
LLM 只用在根因分析那一层（叙述性推理），且产出必须带由代码推导出来的依据引用。
理由分别写在 detection.py 和 rca.py 顶部。
"""

from src.ops.analysis.correlation import (
    Alert,
    CorrelationResult,
    Incident,
    correlate_alerts,
)
from src.ops.analysis.detection import AnomalyPoint, AnomalyReport, detect_anomalies
from src.ops.analysis.rca import ChatModel, EvidenceRef, RcaResult, analyze_root_cause, build_evidence

__all__ = [
    "Alert", "Incident", "CorrelationResult", "correlate_alerts",
    "AnomalyPoint", "AnomalyReport", "detect_anomalies",
    "ChatModel", "EvidenceRef", "RcaResult", "analyze_root_cause", "build_evidence",
]
