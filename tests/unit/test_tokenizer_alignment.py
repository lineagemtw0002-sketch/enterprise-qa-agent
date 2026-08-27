"""索引侧 / 查询侧分词对齐的回归保护（`CLAUDE.md` §4 第 9b 条，2026-08-26 修）。

## 这组测试守的是一条契约，不是一段代码

    对任意文本 t:  set(match_terms(query_tokens(t))) ⊆ set(index_tokens(t))

即**索引侧产出的词条必须是查询侧的超集**。BM25 与 OpenSearch 的 `whitespace`
分析器都是词条级精确匹配，查询词条不在索引里就是永远命中不了。

**判据刻意不是"两侧输出相同"。** 相同是更强也更坏的要求：查询侧要去停用词、
要去重、要截断，索引侧不该跟着做这些。真正需要成立的只有 ⊆。

## 修复前会失败吗（`CLAUDE.md` §7.2 的判别力自查）

会，而且三类各有一条独立的失败原因：

- `TestSingleCharTermsAreIndexed`：旧 `SparseEncoder(min_term_length=2)`
  把中文单字全丢了，而 jieba 词典里没有「年假」——「年」「假」在查询侧留着、
  在索引侧没了。旧实现下 `年 not in index_tokens(...)`，直接红。
- `TestCompoundGranularity`：旧索引侧用 `jieba.lcut`（精确模式），
  文档里是 `顺延到`、查询 `顺延` 得到 `顺延`，旧实现下 `顺延` 不在索引词条里。
- `TestMatchLayerLowercases` / `TestOpenSearchQueryTextIsLowercased`：
  旧 `_as_query_text` 直接 `" ".join(tokens)`，`HR` 原样送进 `whitespace`
  分析器，匹配不上索引里的 `hr`。旧实现下断言 `"hr" in text` 红。

`TestSupersetInvariantHolds` 在旧实现下同样红（前两类的直接后果）。
"""

from __future__ import annotations

import pytest

from src.core.query_engine.query_processor import QueryProcessor
from src.core.tokenization import index_tokens, match_terms, query_tokens
from src.ingestion.embedding.sparse_encoder import SparseEncoder


# 语料刻意混合四种形态，因为四种的失败原因不同：
#   中文双字未登录词（年假）      → 单字被索引侧丢掉
#   中文长词（顺延到 / 试用期）    → 精确模式粒度不一致
#   英文缩写与混排（HR / iTunes）  → 大小写
#   数字与量词（3 天 / 100 毫升）  → 单字符数字被丢掉
_TEXTS = [
    "年假可以顺延到次年三月，逾期作废",
    "员工需要提前3天申请远程办公",
    "HR主管审批流程 B类岗位适用",
    "试用期6个月，转正后享受全额年假",
    "报销单据由财务部门在 5 个工作日内完成审核",
    "Python 3.11 和 GPT-4 的推理成本对比",
    "100 毫升可乐中的咖啡因含量是多少",
    "远程办公申请需经直属主管与部门总监双重审批",
]


class TestSupersetInvariantHolds:
    """契约本身。**改分词时先看这一组。**"""

    @pytest.mark.parametrize("text", _TEXTS)
    def test_query_terms_are_a_subset_of_index_terms(self, text: str):
        idx = set(index_tokens(text))
        qry = set(match_terms(query_tokens(text)))
        missing = qry - idx
        assert not missing, (
            f"查询侧切出来的这些词在索引里根本不存在，稀疏检索永远命中不了：{sorted(missing)}\n"
            f"  文本 = {text}\n"
            f"  索引侧 = {sorted(idx)}\n"
            f"  查询侧 = {sorted(qry)}"
        )

    @pytest.mark.parametrize("text", _TEXTS)
    def test_real_pipeline_classes_honour_the_same_invariant(self, text: str):
        """走真正被生产调用的两个类，而不是只走底层函数。

        分开测是因为两个类各自还有自己的一层处理（`SparseEncoder` 的
        min_term_length、`QueryProcessor` 的停用词/去重/截断），
        契约必须在**它们的输出**上成立才算数。
        """
        idx = set(SparseEncoder()._tokenize(text))
        qry = set(match_terms(QueryProcessor().process(text).keywords))
        assert not (qry - idx), f"{sorted(qry - idx)} 在索引侧缺失（{text}）"

    def test_encoder_and_module_do_not_drift(self):
        """`SparseEncoder._tokenize` 必须只是转发。

        它自己再写一份 jieba 调用正是这次要修的根因 —— 两份实现没有任何
        机制强制同步，分叉了几个月都没人发现。
        """
        for text in _TEXTS:
            assert SparseEncoder()._tokenize(text) == index_tokens(text)

    def test_query_processor_tokenize_does_not_drift(self):
        qp = QueryProcessor()
        for text in _TEXTS:
            assert qp._tokenize(text) == query_tokens(text)


class TestSingleCharTermsAreIndexed:
    """旧 `min_term_length=2` 造成的缺陷：中文单字进不了索引。"""

    def test_default_min_term_length_is_one(self):
        assert SparseEncoder().min_term_length == 1, (
            "改回 2 会让中文单字重新落不进索引 —— 「年假」这类 jieba 词典里"
            "没有的词会被切成两个单字，查询侧留着、索引侧丢掉。"
        )

    def test_nianjia_is_findable(self):
        """具体后果：一个专门讲年假的库，搜「年假」在稀疏侧零命中。"""
        doc = index_tokens("年假可以顺延到次年三月")
        asked = match_terms(query_tokens("年假"))
        assert set(asked) <= set(doc), f"查询 {asked} 在文档词条 {doc} 里缺失"

    def test_standalone_digit_is_indexed(self):
        assert "3" in index_tokens("提前3天申请")

    def test_min_len_is_still_configurable(self):
        assert "3" not in index_tokens("提前3天申请", min_len=2)

    def test_min_len_zero_rejected(self):
        with pytest.raises(ValueError, match="min_len must be >= 1"):
            index_tokens("x", min_len=0)


class TestCompoundGranularity:
    """jieba 精确模式对同一串字在不同上下文切法不同 —— 这条与"两侧配置是否
    一致"无关，两侧都调 `lcut` 也照样中招，所以"统一分词"修不掉它。
    修法是索引侧改用搜索引擎模式（`cut_for_search`）产出子词。
    """

    def test_shunyan_matches_shunyandao(self):
        doc = set(index_tokens("年假可以顺延到次年三月"))
        assert "顺延" in doc, "索引里只有 `顺延到` 的话，用户搜 `顺延` 命中不了"
        assert "顺延到" in doc, "搜索模式必须同时保留整词，否则长查询反而退化"

    def test_shiyongqi_subword(self):
        doc = set(index_tokens("试用期6个月转正"))
        assert "试用期" in doc
        assert "试用" in doc


class TestMatchLayerLowercases:
    """大小写归一化属于**匹配**，不属于分词 —— 它是两个后端唯一分歧过的一步。"""

    def test_match_terms_lowercases(self):
        assert match_terms(["HR", "iTunes", "B"]) == ["hr", "itunes", "b"]

    def test_query_tokens_preserve_case(self):
        """查询侧保留原始大小写：`ProcessedQuery.keywords` 要进 trace 与前端。"""
        assert "iTunes" in query_tokens("如何在 iTunes 中移动歌曲")

    def test_index_tokens_lowercase(self):
        assert "hr" in index_tokens("HR主管审批")
        assert "HR" not in index_tokens("HR主管审批")


class TestOpenSearchQueryTextIsLowercased:
    """OpenSearch 侧的 `whitespace` 分析器**不做小写化**，所以查询串必须自己
    先归一化。这个缺陷只在 OpenSearch 后端出现（BM25 侧 `query()` 自己
    `lower()` 了），单测和"跑起来没报错"都抓不到，只能这样钉住。

    这条不连 OpenSearch，只测查询串怎么拼 —— 所以放在 unit 而不是 integration。
    """

    def test_caller_supplied_tokens_are_lowercased(self):
        from src.libs.search.opensearch_store import _as_query_text

        # 生产调用方（opensearch_retrievers）传的就是 QueryProcessor 的
        # keywords，保留原始大小写
        text = _as_query_text("", ["HR", "主管", "iTunes"])
        assert text == "hr 主管 itunes"

    def test_fallback_path_is_lowercased_too(self):
        from src.libs.search.opensearch_store import _as_query_text

        text = _as_query_text("HR主管审批流程", None)
        assert "hr" in text.split()
        assert "HR" not in text.split()
