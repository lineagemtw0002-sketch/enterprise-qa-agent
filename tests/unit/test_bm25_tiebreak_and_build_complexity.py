"""BM25 索引两条独立缺陷的回归保护（2026-08-25，原型实测后修复）。

两条缺陷来自 `scripts/prototype_bm25_sqlite.py` 的实测，互相独立：

1. **`query` 没有确定性 tie-break** —— 同分候选的顺序 = `scores` 字典插入顺序
   = postings 物理顺序。Python 的 `sorted` 是稳定排序，`reverse=True` 也不会
   打乱同分组，所以旧实现的 top-k 在"分数完全相同"时取决于摄入顺序。
   实测 50K 块的截断线上有 14–19 个同分候选，换任何存储后端 top-10 都会变
   （全量分数映射逐 bit 相同，但 top-10 只对上 4/12）。
   **这是 BM25 存储层改造能否验收的前提**：没有它，"新旧结果一致"根本无法判定。

2. **`build` 是 `O(词表 × 文档数)`** —— 旧实现为每个词条把 `term_stats`
   整个重扫一遍找 `tf > 0` 的文档。实测 1K/16K/50K 块 = 0.35 / 31.08 / 393.97 s，
   16K→50K 段 α=2.23；外推 143K 块约 1.1 小时、716K 约 41 小时。

第 2 条的测试**刻意不用墙钟时间**（会随机器负载抖动，原型实测同一档三轮
相差 1.7 倍），改成数"访问了多少次 term_frequencies"——确定性、与机器无关。
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from src.ingestion.storage.bm25_indexer import BM25Indexer


def _stat(chunk_id: str, terms: Dict[str, int]) -> Dict[str, Any]:
    return {
        "chunk_id": chunk_id,
        "term_frequencies": dict(terms),
        "doc_length": sum(terms.values()),
    }


def _reference_build_index(
    term_stats: List[Dict[str, Any]], indexer: BM25Indexer
) -> Dict[str, Any]:
    """修复前那段嵌套循环的逐行复刻，用作等价性判定的 oracle。

    单遍改写只有在"输出与这段完全一致"时才算纯优化。特意保留了旧实现里
    `df` 统计所有 key、而 postings 只收 `tf > 0` 的那处不一致 —— 修复不该
    顺手改变它的语义（要改是另一个决策，得单独提）。
    """
    num_docs = len(term_stats)

    doc_freq: Dict[str, int] = {}
    for stat in term_stats:
        for term in stat["term_frequencies"].keys():
            doc_freq[term] = doc_freq.get(term, 0) + 1

    index: Dict[str, Dict[str, Any]] = {}
    for term, df in doc_freq.items():
        postings = []
        for stat in term_stats:
            tf = stat["term_frequencies"].get(term, 0)
            if tf > 0:
                postings.append(
                    {
                        "chunk_id": stat["chunk_id"],
                        "tf": tf,
                        "doc_length": stat["doc_length"],
                    }
                )
        index[term] = {
            "idf": indexer._calculate_idf(num_docs, df),
            "df": df,
            "postings": postings,
        }
    return index


class _CountingFreqs(dict):
    """记录被访问次数的 dict —— 用来把"复杂度"变成确定性断言。

    旧实现每个 (词条, 文档) 组合调一次 `.get()`，即 词表×文档数 次；
    单遍实现对每篇文档的词频表只遍历一次。两者差着一个数量级以上，
    不需要卡精确值，只要把阈值放在中间就能稳定区分。
    """

    def __init__(self, *args, counter: List[int], **kwargs):
        super().__init__(*args, **kwargs)
        self._counter = counter

    def get(self, *args, **kwargs):  # noqa: D102
        self._counter[0] += 1
        return super().get(*args, **kwargs)

    def __getitem__(self, key):
        self._counter[0] += 1
        return super().__getitem__(key)


@pytest.fixture()
def indexer(tmp_path) -> BM25Indexer:
    return BM25Indexer(index_dir=str(tmp_path / "bm25"))


# ─────────────────────── 缺陷 1：query 的确定性 tie-break ───────────────────────


class TestQueryTieBreak:
    """同分候选必须按 chunk_id 定序，且定序要发生在截断之前。"""

    @staticmethod
    def _uniform_corpus(chunk_ids: List[str]) -> List[Dict[str, Any]]:
        """所有块对查询词的 tf 和 doc_length 都相同 → BM25 分数必然完全相等。"""
        return [_stat(cid, {"年假": 2, "申请": 1}) for cid in chunk_ids]

    def test_equal_scores_are_ordered_by_chunk_id(self, indexer):
        # 摄入顺序刻意打乱，且与字典序相反
        ids = ["c_e", "c_c", "c_a", "c_d", "c_b"]
        indexer.build(self._uniform_corpus(ids), collection="tie")

        results = indexer.query(["年假"], top_k=5)

        scores = {round(r["score"], 12) for r in results}
        assert len(scores) == 1, "前提失效：这些块的分数本应完全相同"
        assert [r["chunk_id"] for r in results] == sorted(ids)

    def test_tiebreak_is_applied_before_truncation(self, indexer):
        """截断线上有同分候选时，留下的必须是 chunk_id 最小的那几个。

        这条专门抓"先截断再排序"的写法 —— 那种写法下返回的是摄入顺序里
        靠前的几个，看起来也"有序"，但换个摄入顺序结果就变了。
        """
        ids = [f"c_{i:02d}" for i in range(20)]
        indexer.build(self._uniform_corpus(list(reversed(ids))), collection="trunc")

        results = indexer.query(["年假"], top_k=3)

        assert [r["chunk_id"] for r in results] == ["c_00", "c_01", "c_02"]

    def test_rebuild_with_permuted_ingestion_order_gives_same_topk(self, indexer):
        """真实世界的不变量：重建索引不该改变检索结果。

        旧实现下这条必失败 —— 同分组的顺序直接跟着摄入顺序走。
        """
        ids = [f"c_{i:02d}" for i in range(12)]

        indexer.build(self._uniform_corpus(ids), collection="order_a")
        first = [r["chunk_id"] for r in indexer.query(["年假"], top_k=5)]

        indexer.build(self._uniform_corpus(list(reversed(ids))), collection="order_b")
        second = [r["chunk_id"] for r in indexer.query(["年假"], top_k=5)]

        assert first == second

    def test_score_ordering_still_dominates_tiebreak(self, indexer):
        """防止 key 写反：分数高的必须排在前面，哪怕它的 chunk_id 更大。

        ⚠️ 语料必须让"年假"只出现在少数文档里。这个 BM25 用的是经典 IDF
        `log((N-df+0.5)/(df+0.5))`，词条出现在超过半数文档时 **idf 为负**
        （df=N=2 时 idf=-1.61），届时 tf 越高分数反而越低 —— 只放两篇都含该词
        的文档会让这条测试测反了方向。这里补 8 篇不含该词的文档，把 idf 拉正。
        """
        stats = [
            _stat("z_high", {"年假": 9}),   # chunk_id 最大，但分数最高
            _stat("a_low", {"年假": 1, "填充": 40}),
        ]
        stats += [_stat(f"pad_{i}", {"无关": 5}) for i in range(8)]
        indexer.build(stats, collection="dominate")

        results = indexer.query(["年假"], top_k=2)

        assert results[0]["chunk_id"] == "z_high"
        assert results[0]["score"] > results[1]["score"]

    def test_distinct_scores_are_unaffected(self, indexer):
        """分数各不相同时，tie-break 不该改变任何东西。"""
        stats = [
            _stat("c_a", {"年假": 1, "填充": 30}),
            _stat("c_b", {"年假": 5}),
            _stat("c_c", {"年假": 3}),
        ]
        indexer.build(stats, collection="distinct")

        results = indexer.query(["年假"], top_k=3)
        scores = [r["score"] for r in results]

        assert scores == sorted(scores, reverse=True)
        assert len(set(scores)) == 3, "前提失效：这三个分数本应互不相同"


# ─────────────────── 缺陷 2：build 的 O(词表 × 文档数) ───────────────────


class TestBuildIsSinglePass:
    def test_term_frequency_lookups_scale_with_postings_not_vocab_times_docs(
        self, indexer
    ):
        """确定性的复杂度断言，不依赖墙钟时间。

        构造 200 篇文档 × 每篇 20 个互不重叠的词条 → 词表 4000、文档 200。
        旧实现：4000 × 200 = 800,000 次词频查找。
        单遍实现：只遍历每篇文档自己的词频表，共 4000 次。
        阈值放在 50,000 —— 离两边都足够远，不会因实现细节微调而误报。
        """
        counter = [0]
        term_stats = []
        for doc_i in range(200):
            freqs = {f"t_{doc_i}_{k}": 1 for k in range(20)}
            term_stats.append(
                {
                    "chunk_id": f"c_{doc_i}",
                    "term_frequencies": _CountingFreqs(freqs, counter=counter),
                    "doc_length": 20,
                }
            )

        vocab_size = 200 * 20
        indexer.build(term_stats, collection="complexity")

        assert len(indexer._index) == vocab_size, "前提失效：词表应完全不重叠"
        assert counter[0] < 50_000, (
            f"词频查找 {counter[0]} 次，量级接近 词表×文档数 "
            f"({vocab_size * 200})，说明仍是嵌套扫描"
        )

    def test_index_is_byte_identical_to_reference_implementation(self, indexer):
        """等价性 oracle：单遍改写的输出必须和旧嵌套循环完全一致。

        包括词条的键顺序和每个 postings 列表内部的顺序 —— 这两样都会影响
        序列化结果，而 tie-break 之外的任何顺序变化都属于回归。
        """
        term_stats = [
            _stat("c_0", {"年假": 3, "申请": 1, "流程": 2}),
            _stat("c_1", {"报销": 2, "申请": 4}),
            _stat("c_2", {"年假": 1, "考勤": 5}),
            _stat("c_3", {"流程": 1, "报销": 1, "年假": 2}),
        ]
        expected = _reference_build_index(term_stats, indexer)

        indexer.build(term_stats, collection="equiv")

        assert list(indexer._index.keys()) == list(expected.keys()), "词条键顺序变了"
        assert indexer._index == expected

    def test_zero_tf_edge_case_semantics_are_preserved(self, indexer):
        """旧实现里 df 统计所有 key、postings 只收 tf>0，两者会对不上。

        这是既有行为，单遍改写不该顺手"修正"它 —— 那是另一个决策。
        真实的 SparseEncoder 不产生 tf==0，所以这里只锁语义不做主张。
        """
        term_stats = [
            _stat("c_0", {"年假": 2}),
            {"chunk_id": "c_1", "term_frequencies": {"年假": 0}, "doc_length": 5},
        ]
        expected = _reference_build_index(term_stats, indexer)

        indexer.build(term_stats, collection="zero_tf")

        assert indexer._index["年假"]["df"] == expected["年假"]["df"] == 2
        assert len(indexer._index["年假"]["postings"]) == 1
        assert indexer._index == expected

    def test_metadata_is_unchanged(self, indexer):
        term_stats = [
            _stat("c_0", {"年假": 3, "申请": 1}),
            _stat("c_1", {"报销": 2}),
        ]
        indexer.build(term_stats, collection="meta")

        assert indexer._metadata["num_docs"] == 2
        assert indexer._metadata["total_terms"] == 3
        assert indexer._metadata["avg_doc_length"] == pytest.approx((4 + 2) / 2)
        assert indexer._metadata["collection"] == "meta"

    def test_round_trip_through_disk_preserves_index(self, indexer, tmp_path):
        """单遍改写不能破坏落盘格式 —— 存回来的必须和内存里一样。"""
        term_stats = [
            _stat("c_0", {"年假": 3, "申请": 1}),
            _stat("c_1", {"报销": 2, "申请": 1}),
        ]
        indexer.build(term_stats, collection="disk")
        in_memory = indexer._index

        reloaded = BM25Indexer(index_dir=str(tmp_path / "bm25"))
        assert reloaded.load(collection="disk") is True
        assert reloaded._index == in_memory
