"""层次化检索粗筛层的预算分配（`src/core/query_engine/narrow_plan.py`）。

这一层此前**零测试覆盖**，而它决定"用户这次能不能查到东西"：
`tests/e2e/test_recall.py` 直接构造 HybridSearch、`benchmark_rag.py` 走
`tool.execute(collection=...)` 的单库路径，两条评估基线都不经过粗筛，
所以 2026-08-26 那个"6 个候选库只查了 2 个、还都是不相关的"的缺陷
能一路上线。完整证据见 `docs/hierarchical_narrowing_redesign.md`。

**判别力说明**（CLAUDE.md §7.2「它在旧实现下会失败吗」）：

本文件测的 `narrow_plan.py` 是新拆出来的模块，旧实现里没有对应的可测单元
（预算分配焊在一个 async + 现建 client + 查询的方法里，这正是它零覆盖的原因）。
所以**这里的判别力是"按新不变量构造"，不是实测跑红**——
真正在旧实现下跑红的判别式在 `tests/integration/test_hierarchy_narrowing_recall.py`，
那 4 条已实测确认：回退到旧实现后 4 条全部失败（2026-08-26 实跑）。

下表说的是"这条用例编码的规则，旧实现违反了没有"：

| 用例 | 旧实现下会失败吗 |
|---|---|
| test_every_candidate_collection_gets_a_decision | ✅ 旧实现只产出 top-N 落在的 1~2 个库 |
| test_decision_is_independent_of_other_collections | ✅ 旧实现下会被别的库挤掉 |
| test_budget_scales_with_collection_size | ✅ 旧实现恒为常数 5 |
| test_low_confidence_collection_is_searched_whole | ✅ 旧实现没有置信门控 |
| test_disabled_narrows_nothing | ✅ 旧实现没有开关 |
| test_absent_collection_means_search_whole_not_skip | ✅ **这条就是那个 bug 本身** |
| test_no_summary_signal_falls_back_to_flat | ❌ 旧实现已正确——**回归保护，不是新判别力** |
"""

from __future__ import annotations

import pytest

from src.core.query_engine.narrow_plan import (
    NarrowConfig,
    budget_for,
    decisions_to_filters,
    plan_narrowing,
)

SIX_KBS = [
    "acme_hr_admin_kb", "acme_finance_kb", "acme_it_support_kb",
    "acme_rd_product_kb", "acme_sales_marketing_kb", "acme_customer_success_kb",
]


def _hit(doc_id: str, score: float) -> dict:
    return {"id": doc_id, "score": score, "metadata": {"doc_id": doc_id}}


def _cfg(**kw) -> NarrowConfig:
    base = dict(enabled=True, ratio=0.4, min_docs=20, max_docs=100, min_top_score=0.85)
    base.update(kw)
    return NarrowConfig(**base)


def _hits_for(collections, top_score: float, n: int = 30) -> dict:
    """每个库 n 条命中，分数从 top_score 递减。"""
    return {
        c: [_hit(f"{c}_doc{i}", top_score - i * 0.001) for i in range(n)]
        for c in collections
    }


class TestInvariantNeverDropsACollection:
    """核心不变量：粗筛只在库内收窄，永远不改变"要检索哪几个库"。"""

    def test_every_candidate_collection_gets_a_decision(self):
        # 一个库分数明显更高，其余五个库都很低——旧实现下全局 top-N 会全部落在
        # 那一个库里，另外五个库直接从检索里消失。
        hits = _hits_for(SIX_KBS[1:], top_score=0.86)
        hits[SIX_KBS[0]] = [_hit("winner", 0.99)]

        decisions = plan_narrowing(hits, {c: 121 for c in SIX_KBS}, _cfg())

        assert [d.collection for d in decisions] == list(hits.keys())
        assert len(decisions) == 6

    def test_absent_collection_means_search_whole_not_skip(self):
        """这条用例就是 2026-08-26 那个 bug 本身。

        `decisions_to_filters` 的 keys 是"要加文档过滤的库"，
        **不是"要检索的库"**——调用方拿 keys 当 search_collections 用，
        就等于让一层弱 embedding 把整个知识库从检索里删掉。
        """
        hits = {
            "kb_high": [_hit("d1", 0.95)],
            "kb_low": [_hit("d2", 0.10)],  # 低于 min_top_score
        }
        decisions = plan_narrowing(hits, {"kb_high": 121, "kb_low": 121}, _cfg())
        filters = decisions_to_filters(decisions)

        assert set(filters) == {"kb_high"}, "只有有把握的库才加文档过滤"
        # 但 kb_low 必须仍然有一条"整库参检"的决定
        low = next(d for d in decisions if d.collection == "kb_low")
        assert low.doc_ids is None
        assert low.reason == "low_confidence"

    def test_decision_is_independent_of_other_collections(self):
        """同一个库的收窄结果不受"候选库有几个"影响——
        旧实现下 `bob_acme`（1 个库）正常而 `alice_acme`（6 个库）全错，
        根因就是这个独立性不成立。"""
        target = {"acme_it_support_kb": [_hit(f"it_{i}", 0.95 - i * 0.001) for i in range(30)]}
        counts = {c: 121 for c in SIX_KBS}

        alone = plan_narrowing(dict(target), {"acme_it_support_kb": 121}, _cfg())
        crowded_hits = dict(target)
        crowded_hits.update(_hits_for([c for c in SIX_KBS if c != "acme_it_support_kb"], 0.98))
        crowded = plan_narrowing(crowded_hits, counts, _cfg())

        pick = lambda ds: next(d for d in ds if d.collection == "acme_it_support_kb")  # noqa: E731
        assert pick(alone).doc_ids == pick(crowded).doc_ids


class TestBudget:
    @pytest.mark.parametrize("doc_count,expected", [
        (0, 20),      # 空库：给下限，不给 0
        (10, 20),     # 小库：ratio 算出来 4，被 min_docs 抬到 20
        (121, 49),    # 演示库：ceil(0.4 × 121)
        (1000, 100),  # 大库：被 max_docs 压住，避免收窄退化成全表扫
    ])
    def test_budget_scales_with_collection_size(self, doc_count, expected):
        assert budget_for(doc_count, _cfg()) == expected

    def test_budget_never_depends_on_candidate_count(self):
        """预算只看库内文档数——旧实现把预算和候选库数量耦合在一起，
        于是权限越大的用户被误伤越重。"""
        assert budget_for(121, _cfg()) == budget_for(121, _cfg())

    def test_budget_respects_ratio(self):
        assert budget_for(500, _cfg(ratio=0.1, min_docs=1, max_docs=1000)) == 50


class TestConfidenceGate:
    def test_low_confidence_collection_is_searched_whole(self):
        hits = {"kb": [_hit("d1", 0.84)]}
        d = plan_narrowing(hits, {"kb": 121}, _cfg(min_top_score=0.85))[0]
        assert d.doc_ids is None and d.reason == "low_confidence"
        assert d.top_score == pytest.approx(0.84)

    def test_confident_collection_is_narrowed_to_budget(self):
        hits = {"kb": [_hit(f"d{i}", 0.99 - i * 0.001) for i in range(80)]}
        d = plan_narrowing(hits, {"kb": 121}, _cfg())[0]
        assert d.reason == "narrowed"
        assert len(d.doc_ids) == budget_for(121, _cfg()) == 49
        assert d.doc_ids[0] == "d0", "必须按分数降序取"

    def test_hits_are_sorted_before_truncation(self):
        """向量库返回顺序不可依赖——收窄前必须自己排一次。"""
        hits = {"kb": [_hit("low", 0.86), _hit("high", 0.99), _hit("mid", 0.90)]}
        d = plan_narrowing(hits, {"kb": 5}, _cfg(min_docs=2, max_docs=2))[0]
        assert d.doc_ids == ["high", "mid"]


class TestDisabledAndFallbacks:
    def test_disabled_narrows_nothing(self):
        hits = _hits_for(SIX_KBS, top_score=0.99)
        decisions = plan_narrowing(hits, {c: 121 for c in SIX_KBS}, _cfg(enabled=False))
        assert all(d.doc_ids is None and d.reason == "disabled" for d in decisions)
        assert decisions_to_filters(decisions) == {}
        assert len(decisions) == 6, "关掉粗筛也不能让任何库从候选里消失"

    def test_no_summary_signal_falls_back_to_flat(self):
        """回归保护：摘要层没有数据时退回平铺——旧实现这一条已经是对的。"""
        decisions = plan_narrowing({c: [] for c in SIX_KBS}, {c: 0 for c in SIX_KBS}, _cfg())
        assert all(d.reason == "no_summary_signal" for d in decisions)
        assert decisions_to_filters(decisions) == {}

    def test_hits_without_doc_id_are_not_narrowed(self):
        hits = {"kb": [{"score": 0.99}, {"score": 0.95}]}
        d = plan_narrowing(hits, {"kb": 121}, _cfg())[0]
        assert d.doc_ids is None and d.reason == "no_doc_id"

    def test_filters_never_contain_empty_lists(self):
        hits = {"a": [_hit("d1", 0.99)], "b": []}
        filters = decisions_to_filters(plan_narrowing(hits, {"a": 121, "b": 121}, _cfg()))
        assert all(v for v in filters.values())
        assert "b" not in filters


class TestConfigParsing:
    class _Settings:
        def __init__(self, doc_summary):
            self.ingestion = type("I", (), {"doc_summary": doc_summary})()

    def test_missing_narrow_section_defaults_to_disabled(self):
        """没有 narrow 段时默认关闭——宁可多查几个库慢一点，也不要静默丢答案。"""
        cfg = NarrowConfig.from_settings(self._Settings({"use_llm": False}))
        assert cfg.enabled is False

    def test_missing_ingestion_section_defaults_to_disabled(self):
        cfg = NarrowConfig.from_settings(object())
        assert cfg.enabled is False

    def test_reads_all_fields(self):
        cfg = NarrowConfig.from_settings(self._Settings({"narrow": {
            "enabled": True, "ratio": 0.25, "min_docs": 5,
            "max_docs": 50, "min_top_score": 0.9,
        }}))
        assert (cfg.enabled, cfg.ratio, cfg.min_docs, cfg.max_docs, cfg.min_top_score) == \
            (True, 0.25, 5, 50, 0.9)

    def test_partial_section_keeps_defaults(self):
        cfg = NarrowConfig.from_settings(self._Settings({"narrow": {"enabled": True}}))
        assert cfg.enabled is True and cfg.ratio == 0.4 and cfg.min_docs == 20

    def test_shipped_settings_yaml_has_narrowing_disabled(self):
        """配置正本里这一项必须是关的——见 config/settings.yaml 旁的理由。
        有人把它打开时，这条会当场失败并逼他读那段注释和设计文档。"""
        from src.core.settings import load_settings
        assert NarrowConfig.from_settings(load_settings()).enabled is False
