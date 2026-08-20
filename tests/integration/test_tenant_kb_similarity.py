"""校验 Acme / Globex 两家企业「同类型知识库」的内容相似度低于 10%。

背景见 `scripts/compare_tenant_kb_similarity.py` 顶部的说明：两家企业各有
11 个知识库分类，其中 hr/meeting/troubleshoot/onboarding 4 个是两家都有的
同名分类（同一种部门/流程主题，但各自内容独立编写），这几个类目是本次
"相似度低于 10%" 的核对对象——如果这几个类目实际相似度很高，说明两家企业
的知识库其实在灌水同一批内容，检索时会互相串号，达不到"独立企业各自
知识域"的联邦查询设计目标（knowledge-base-tenant-federation.md）。

相似度用词法（TF-IDF 余弦）指标衡量，不用语义 embedding 余弦——后者对同
语言同领域的中文商务文本天然有 60%+ 的基线（不是内容重叠的信号，见
compare_tenant_kb_similarity.py 里跟"企业内部同类文档自相似度"的对比），
用它卡 10% 既不现实也不是这个要求真正想测的东西；词法相似度不需要外部
embedding 服务，也不需要网络，跑得快、结果稳定可重复。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.compare_tenant_kb_similarity import (
    SHARED_CATEGORIES,
    TENANT_DEMO_DIR,
    _compute_category,
    _load_category_texts,
)

LEXICAL_SIMILARITY_THRESHOLD = 0.10
MIN_ENTRIES_PER_TENANT = 200  # "几百条"


class TestTenantKbCorpusExists:
    """先确认语料本身真的在，且量级达到"几百条"，相似度数字才有意义。"""

    @pytest.mark.parametrize("tenant", ["acme", "globex"])
    def test_corpus_dir_exists(self, tenant: str) -> None:
        corpus_dir = TENANT_DEMO_DIR / tenant / "kb_corpus"
        assert corpus_dir.is_dir(), f"{corpus_dir} 不存在，请先跑 scripts/generate_tenant_kb_corpus.py"

    @pytest.mark.parametrize("tenant", ["acme", "globex"])
    def test_total_entries_at_least_a_few_hundred(self, tenant: str) -> None:
        corpus_dir = TENANT_DEMO_DIR / tenant / "kb_corpus"
        total = len(list(corpus_dir.rglob("*.txt")))
        assert total >= MIN_ENTRIES_PER_TENANT, (
            f"{tenant} 知识库语料只有 {total} 条，未达到\"几百条\"的量级"
        )

    @pytest.mark.parametrize("category", sorted(SHARED_CATEGORIES))
    def test_shared_category_has_entries_for_both_tenants(self, category: str) -> None:
        acme_texts = _load_category_texts("acme", category)
        globex_texts = _load_category_texts("globex", category)
        assert len(acme_texts) > 0, f"acme 的 {category} 分类没有语料"
        assert len(globex_texts) > 0, f"globex 的 {category} 分类没有语料"


class TestSharedCategoryLexicalSimilarity:
    """核心要求：同类型知识库的跨企业词法相似度均值 < 10%。"""

    @pytest.mark.parametrize("category", sorted(SHARED_CATEGORIES))
    def test_cross_tenant_similarity_below_threshold(self, category: str) -> None:
        result = _compute_category(category)
        assert result["lexical_mean"] < LEXICAL_SIMILARITY_THRESHOLD, (
            f"{SHARED_CATEGORIES[category]}（{category}）Acme/Globex 词法相似度均值 "
            f"{result['lexical_mean']:.2%}，超过 {LEXICAL_SIMILARITY_THRESHOLD:.0%} 的上限"
        )


class TestSharedCategorySemanticSanity:
    """辅助信号（需要本地 Ollama embedding 服务，不可用时跳过）：跨企业语义相似度
    不应该高于各自企业内部同类文档的自相似度——如果跨企业反而更像，说明大概率
    是互相抄了同一批内容，而不是"恰好都在写 HR 制度但各写各的"。"""

    @pytest.fixture(scope="class")
    @classmethod
    def embedder(cls):
        try:
            from src.core.settings import load_settings
            from src.libs.embedding.embedding_factory import EmbeddingFactory

            settings = load_settings("config/settings.yaml")
            instance = EmbeddingFactory.create(settings)
            instance.embed(["连通性探测"])
        except Exception as e:  # noqa: BLE001 - 探测失败就跳过，不让 embedding 服务的可用性挡住主测试
            pytest.skip(f"本地 embedding 服务不可用，跳过语义相似度校验: {e}")
        return instance

    @pytest.mark.parametrize("category", sorted(SHARED_CATEGORIES))
    def test_cross_tenant_semantic_similarity_not_above_self_baseline(self, category: str, embedder) -> None:
        result = _compute_category(category, embedder=embedder)
        self_baseline = min(result["semantic_acme_self_mean"], result["semantic_globex_self_mean"])
        assert result["semantic_cross_mean"] <= self_baseline, (
            f"{SHARED_CATEGORIES[category]}（{category}）跨企业语义相似度 "
            f"{result['semantic_cross_mean']:.2%} 高于企业内部自相似度基线 {self_baseline:.2%}，"
            f"疑似两家企业的内容互相抄了同一批素材"
        )
