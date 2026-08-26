"""读后端灰度开关的语义 —— 迁移设计阶段 3。

**这组是纯单测，不需要 OpenSearch 在跑** —— 开关判定是纯函数。
切读是本次迁移唯一改变生产检索行为的步骤，开关判错的后果是
"一批本不该切的库悄悄换了检索引擎"，而症状（结果变了）不会立刻被发现。
所以它值得被单独钉死，而不是混在需要外部服务的集成测试里。
"""

from __future__ import annotations

import pytest

from src.libs.search.opensearch_store import opensearch_read_enabled


class TestReadSwitchDefaultsToLegacy:
    """默认必须走旧链路 —— 迁移开关的默认值出错是最危险的一类。"""

    def test_unset_means_legacy(self, monkeypatch):
        monkeypatch.delenv("RAGENT_OPENSEARCH_READ", raising=False)
        assert opensearch_read_enabled("any_kb") is False

    def test_empty_means_legacy(self, monkeypatch):
        monkeypatch.setenv("RAGENT_OPENSEARCH_READ", "")
        assert opensearch_read_enabled("any_kb") is False

    @pytest.mark.parametrize("value", ["off", "OFF", " off ", "Off"])
    def test_off_is_case_and_space_insensitive(self, monkeypatch, value):
        """回滚开关必须好用。`OFF` / 带空格都要认 —— 紧急回滚时没人有心思
        纠结大小写，认不出来就等于回滚失败。"""
        monkeypatch.setenv("RAGENT_OPENSEARCH_READ", value)
        assert opensearch_read_enabled("any_kb") is False


class TestWhitelist:
    def test_star_enables_everything(self, monkeypatch):
        monkeypatch.setenv("RAGENT_OPENSEARCH_READ", "*")
        assert opensearch_read_enabled("whatever") is True

    def test_only_listed_collections(self, monkeypatch):
        monkeypatch.setenv("RAGENT_OPENSEARCH_READ", "mmarco,product_req_kb")
        assert opensearch_read_enabled("mmarco") is True
        assert opensearch_read_enabled("product_req_kb") is True
        assert opensearch_read_enabled("acme_hr_admin_kb") is False

    def test_whitespace_around_names_is_tolerated(self, monkeypatch):
        monkeypatch.setenv("RAGENT_OPENSEARCH_READ", " mmarco , product_req_kb ")
        assert opensearch_read_enabled("mmarco") is True
        assert opensearch_read_enabled("product_req_kb") is True

    def test_matching_is_exact_not_prefix(self, monkeypatch):
        """**必须是精确匹配。**

        前缀/通配匹配在这种开关上是危险的：写错一个字符就可能把一批本不该切
        的库带进去，而症状（检索结果变了）不会立刻被发现。
        """
        monkeypatch.setenv("RAGENT_OPENSEARCH_READ", "mmarc")
        assert opensearch_read_enabled("mmarco") is False

        monkeypatch.setenv("RAGENT_OPENSEARCH_READ", "acme")
        assert opensearch_read_enabled("acme_hr_admin_kb") is False

    def test_a_collection_named_off_is_not_a_whitelist(self, monkeypatch):
        """边界：`off` 是关闭指令，不是"名叫 off 的库"。

        真有库叫 off 的话它切不了 —— 这是刻意取舍：回滚开关的可靠性
        比支持一个古怪的库名重要。
        """
        monkeypatch.setenv("RAGENT_OPENSEARCH_READ", "off")
        assert opensearch_read_enabled("off") is False


class TestRetrieverSignaturesMatchLegacy:
    """OpenSearch 版 retriever 的签名必须与旧实现**逐字对齐**。

    ⚠️ 这条不是风格检查，是防一类**静默降级**。
    `HybridSearch._dense_search` 捕获所有异常并打一行
    "Dense retrieval failed, using sparse only"，然后**退化成只有稀疏检索**。
    所以 dense retriever 少一个参数不会报错到用户面前 —— 检索照常返回结果，
    只是少了一半召回，谁也不会发现。

    实施阶段 3 时第一版就漏了 `filters`，是端到端对照发现结果对不上才查出来的。
    单靠"跑起来没报错"抓不到它。
    """

    @staticmethod
    def _params(fn):
        import inspect

        return list(inspect.signature(fn).parameters)

    def test_dense_signature_matches(self):
        from src.core.query_engine.dense_retriever import DenseRetriever
        from src.core.query_engine.opensearch_retrievers import OpenSearchDenseRetriever

        assert self._params(OpenSearchDenseRetriever.retrieve) == self._params(
            DenseRetriever.retrieve
        ), "dense retriever 签名不一致 —— HybridSearch 会静默退化成 sparse-only"

    def test_sparse_signature_matches(self):
        from src.core.query_engine.opensearch_retrievers import (
            OpenSearchSparseRetriever,
        )
        from src.core.query_engine.sparse_retriever import SparseRetriever

        assert self._params(OpenSearchSparseRetriever.retrieve) == self._params(
            SparseRetriever.retrieve
        ), "sparse retriever 签名不一致"

    def test_dense_actually_pushes_filters_down(self):
        """`filters` 不能只接受不实现 —— 那样元数据过滤会静默失效，
        调用方以为过滤生效了，实际拿到全库结果。"""
        captured = {}

        class FakeStore:
            def knn_kb(self, collection, vector, top_k=10, filters=None):
                captured["filters"] = filters
                return []

        class FakeEmbedding:
            def embed(self, texts):
                return [[0.0] * 8 for _ in texts]

        from src.core.query_engine.opensearch_retrievers import OpenSearchDenseRetriever

        r = OpenSearchDenseRetriever("kb", FakeStore(), FakeEmbedding())
        r.retrieve("查询", filters={"source_path": "/a.pdf"})
        assert captured["filters"] == {"source_path": "/a.pdf"}, "filters 被吞掉了"
