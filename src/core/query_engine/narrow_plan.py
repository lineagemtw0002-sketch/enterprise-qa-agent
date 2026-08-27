"""层次化检索粗筛层的**预算分配**——从 IO 里拆出来的纯逻辑。

背景见 `docs/hierarchical_narrowing_redesign.md`。原来这段逻辑焊在
`query_knowledge_hub.py::_narrow_by_document_summary` 里（async + 现建
向量库 client + 查询 + 排序全在一个方法里），有两个后果：

1. **测不了**——要验证"预算怎么分"就得起真库、连 Ollama，于是它上线至今零测试覆盖，
   而它恰恰是决定"用户这次能不能查到东西"的一段代码；
2. **它悄悄多了一个不该有的权力**：原实现把全部候选库的摘要命中合并、取
   **跨库全局前 N**，于是"某个库一篇都没进前 N" == **这个库整个被从检索里删掉**。
   实测（30 条正向问题）11 条空结果全部是"问题所属的那个库压根没被检索"。

所以这里立一条不变量，并且用类型把它表达出来：

    **粗筛只能在单个 collection 内部收窄文档范围，永远不能改变被检索的
    collection 集合。** `plan_narrowing()` 返回的 dict 里，
    **某个 collection 缺席的含义是"这个库整库参与检索"，绝不是"跳过这个库"。**

调用方（`_execute_local_multi`）据此把 `search_collections` 恒定为
`candidate_collections`，只把 `narrowed` 当作各库自己的 `source_ref` 过滤条件。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence


@dataclass(frozen=True)
class NarrowConfig:
    """`config/settings.yaml` 的 `ingestion.doc_summary.narrow` 段。

    `enabled` 默认 **False**：在当前全部语料上，文档级硬过滤是净负收益
    （见设计文档 §3），关掉即退回层次化上线前的平铺检索。
    配置里没有 `narrow` 段时也按 False 处理——这是刻意的：
    宁可多查几个库慢一点，也不要静默地丢答案。
    """

    enabled: bool = False
    ratio: float = 0.4          # 每库预算 = clamp(ceil(ratio × 库内文档数), min_docs, max_docs)
    min_docs: int = 20
    max_docs: int = 100
    min_top_score: float = 0.85  # 置信门控：某库摘要 top1 低于它 → 该库整库参检

    @classmethod
    def from_settings(cls, settings: Any) -> "NarrowConfig":
        """从 Settings 读取；缺字段一律用上面的默认值（`_Section` 是 frozen 的，
        `doc_summary` 本身是 `Optional[Dict[str, Any]]` 直接透传，见 core/settings.py）。"""
        doc_summary = getattr(getattr(settings, "ingestion", None), "doc_summary", None) or {}
        raw = doc_summary.get("narrow") or {}
        default = cls()
        return cls(
            enabled=bool(raw.get("enabled", default.enabled)),
            ratio=float(raw.get("ratio", default.ratio)),
            min_docs=int(raw.get("min_docs", default.min_docs)),
            max_docs=int(raw.get("max_docs", default.max_docs)),
            min_top_score=float(raw.get("min_top_score", default.min_top_score)),
        )


def budget_for(doc_count: int, cfg: NarrowConfig) -> int:
    """单个 collection 的文档预算。

    **随库内文档数走，不随候选库数量走**——这是本次修复的核心：原实现的
    全局 top-N 让预算和"用户能看几个库"耦合在一起，同一段代码对只有 1 个库的
    员工完全正常、对有 6 个库的企业管理员全错（**bug 的严重程度与用户权限范围成正比**）。
    """
    if doc_count <= 0:
        return cfg.min_docs
    scaled = math.ceil(cfg.ratio * doc_count)
    return max(cfg.min_docs, min(cfg.max_docs, scaled))


def _score(hit: Dict[str, Any]) -> float:
    return float(hit.get("score") or 0.0)


def _doc_id(hit: Dict[str, Any]) -> Optional[str]:
    return (hit.get("metadata") or {}).get("doc_id") or hit.get("id")


@dataclass(frozen=True)
class NarrowDecision:
    """一个 collection 的粗筛决定，用于 trace/排查。

    `doc_ids` 为 None 表示"不收窄，整库参检"；`reason` 说明为什么。
    """

    collection: str
    doc_ids: Optional[List[str]]
    reason: str
    budget: int
    top_score: float


def plan_narrowing(
    per_collection_hits: Dict[str, Sequence[Dict[str, Any]]],
    doc_counts: Dict[str, int],
    cfg: NarrowConfig,
) -> List[NarrowDecision]:
    """按库各自决定收窄到哪几篇文档。

    Args:
        per_collection_hits: {collection: 该库摘要层的命中列表}。命中是向量库
            `query()` 的原始结构（`score` 越大越相关，`metadata.doc_id` 或 `id`
            是文档标识）。**这里不做跨库比较**——那正是原实现的错误所在。
        doc_counts: {collection: 该库摘要层的文档总数}，用于算自适应预算。
        cfg: 见 NarrowConfig。

    Returns:
        每个候选 collection 一条决定。`doc_ids is None` = 整库参检。
        **任何情况下都不会有 collection 从结果里消失**——调用方据此保证
        "被检索的库集合" 与粗筛无关。
    """
    decisions: List[NarrowDecision] = []
    for collection, hits in per_collection_hits.items():
        budget = budget_for(doc_counts.get(collection, 0), cfg)
        ordered = sorted(hits, key=_score, reverse=True)
        top_score = _score(ordered[0]) if ordered else 0.0

        if not cfg.enabled:
            reason = "disabled"
        elif not ordered:
            # 摘要层没有这个库的任何数据（老数据没补摘要，或摄入时摘要那步降级了）。
            reason = "no_summary_signal"
        elif top_score < cfg.min_top_score:
            # 置信门控：摘要层对这个库没把握就别替检索器做决定。摘要用的是弱中文
            # embedding，实测金标文档与榜首的分差中位只有 0.0495（设计文档 §3.1），
            # 大多数情况下都会走到这一条——这本身就是"当前不该开硬过滤"的证据。
            reason = "low_confidence"
        else:
            doc_ids = [d for d in (_doc_id(h) for h in ordered[:budget]) if d]
            if doc_ids:
                decisions.append(NarrowDecision(collection, doc_ids, "narrowed", budget, top_score))
                continue
            # 命中里一个可用的 doc_id 都没有（元数据缺失），当作没有信号处理。
            reason = "no_doc_id"

        decisions.append(NarrowDecision(collection, None, reason, budget, top_score))
    return decisions


def decisions_to_filters(decisions: Sequence[NarrowDecision]) -> Dict[str, List[str]]:
    """把决定转成 `{collection: [doc_id, ...]}`，只含真正收窄的那些库。

    调用方拿它当 `source_ref` 过滤条件用；**不得**拿它的 keys 当"要查哪几个库"
    ——那是本次修复要消灭的用法，见模块顶部的不变量。
    """
    return {d.collection: list(d.doc_ids) for d in decisions if d.doc_ids}
