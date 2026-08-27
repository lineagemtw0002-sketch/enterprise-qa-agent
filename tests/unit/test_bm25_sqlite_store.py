"""BM25 SQLite 后端（方案 C）的等价性与正确性测试。

对应 `docs/bm25_storage_design.md` §7 的 T-1 / T-3 / T-4 / T-5，
以及 `CLAUDE.md` §4 第 2e 条 P0（GIL convoy）的修复验证。

## T-1 判据在 2026-08-27 发生过一次刻意的放宽，原因写在这里

`query()` 原来是"Python 侧逐行取数、按 `for term in query_terms` 原序在
内存里累加"，这个实现能做到与 JSON 侧**完整分数映射逐 bit 相同**（不是
"接近"，是 `==`）——本文件下方大段的手写累加顺序分析（ord 列、交错
postings 等）都是那个实现留下的证据。

但那个实现有一个致命问题：热词查询要跨 `sqlite3_step()` 边界（= GIL
释放/重取一次）与命中 postings 条数同阶，`CLAUDE.md` §4 第 2e 条实测
6 线程并发下会触发 GIL convoy——比单线程还慢 100 倍量级。修法是把
`GROUP BY chunk_id ... ORDER BY ... LIMIT top_k` 整段打分逻辑下推进 SQL，
让跨边界行数从「postings 总数」降到「top_k」。

**下推之后，逐 bit 相同不再成立，且是刻意接受的代价**：单条 posting 的
打分表达式仍与 JSON 侧逐项对应（因此单个词、单个 chunk 的贡献值本身仍是
位级相同），但**跨词条/跨 posting 的最终求和顺序改由 SQLite 的查询计划
决定**，不再是 Python `for term in terms` 的确定原序。浮点加法不满足
结合律，因此 `SUM()` 的聚合结果与 JSON 侧朴素累加会在最低几位上出现 ULP
级差异。实测（本文件跑一遍，751 次比对）**最大相对误差 2.02e-16、最大
绝对误差 8.88e-16**——量级正好卡在 IEEE754 double 的机器精度（约
2.22e-16）附近，这正是"纯浮点舍入、不是公式算错"的证据：如果哪天这个
数字涨到远高于 1e-9（比如百分之几），说明是打分逻辑本身出了问题，
不能靠调大容差糊弄过去。

**因此判据改为**：分数用 `math.isclose(rel_tol=1e-9, abs_tol=1e-12)`
逐项比较（本文件 `_assert_score_maps_close`），**但候选集合（哪些
chunk_id 命中）与 top-k 排序不放宽**——容差只用于比较同一个 chunk_id 的
分数数值，命中集合不同、顺序不同一律判失败。

保留原有关于"公式括号位置""avg_doc_length 存取无损""重复查询词计数"
三条钉子，因为它们仍然决定单条 posting 的贡献值是否位级正确：
1. **公式的括号位置**要一致（`idf * (num / den)` ≠ `idf * num / den`）；
2. **avg_doc_length 存取要无损**（用 `repr` 不用 `str`，否则打分整片偏移）；
3. **重复查询词**要一样处理（JSON 侧不去重，同词出现两次就算两次分——
   下推后改用 `WITH q(term, mult) AS (VALUES ...)` 的乘数语义还原，
   不是诊断脚本 `_sql_side_query`（`scripts/benchmark_bm25_backends.py`）
   那种 `WHERE term IN (...)` 的去重语义，两者不等价，这是本次改动要
   解决的核心问题之一）。

以下是原实现（Python 侧逐行累加）留下的累加顺序分析，**结论在下推之后
不再是"逐 bit 相同"，但分析本身仍解释了为什么容差可以给得这么紧**：
浮点加法确实不满足结合律 —— 实测 20 万组真实 BM25 分数，用生产代码那种朴素
累加（`acc = acc + v`）逐一重排，62.8% 的组合会差 1 ULP。所以本实现一度
给 `chunks` 表加了一个 `ord` 列去复刻 postings 的原始顺序。那是多余的：
单个 chunk 的分数是「各查询词贡献之和」，累加顺序只由外层
`for term in query_terms` 决定，词条内 postings 的先后只影响"哪个 chunk 先
拿到这一项"，不改变任何单个 chunk 自身的累加序列。`ord` 列已删。
下推之后这条分析的价值变成了"解释误差为什么恰好卡在机器精度量级"，
而不是"解释为什么能做到逐 bit 相同"。

〔顺带：验证这件事时若用 `sum()` 会得出"顺序不敏感"的错误结论 ——
Python 3.12 起 `sum()` 改用 Neumaier 补偿求和，而生产代码是朴素累加。〕
"""

from __future__ import annotations

import asyncio
import math
import random
from typing import Any, Dict, List

import pytest

from src.ingestion.storage.bm25_indexer import BM25Indexer
from src.ingestion.storage.bm25_sqlite_store import BM25SQLiteStore

_HUGE_TOP_K = 10_000  # 取全量分数映射，而不是只比 top-k
_REL_TOL = 1e-9  # 实测最大相对误差 2.02e-16，留了近 7 个数量级的余量
_ABS_TOL = 1e-12


def _assert_score_maps_close(
    json_scores: Dict[str, float], sqlite_scores: Dict[str, float], context: str = ""
) -> None:
    """比较两种后端的完整分数映射：**候选集合与顺序不放宽，分数值带容差**。

    候选集合不同 = 结果集本身不一样，不是浮点误差能解释的，必须原样报错。
    """
    assert json_scores.keys() == sqlite_scores.keys(), f"候选集不同 {context}"
    diffs = []
    for cid, jv in json_scores.items():
        sv = sqlite_scores[cid]
        if not math.isclose(jv, sv, rel_tol=_REL_TOL, abs_tol=_ABS_TOL):
            diffs.append((cid, jv, sv, abs(jv - sv)))
    assert not diffs, (
        f"{len(diffs)} 个 chunk 的分数超出容差（rel_tol={_REL_TOL}）{context}，"
        f"首例：{diffs[0]}"
    )


def _stat(chunk_id: str, terms: Dict[str, int]) -> Dict[str, Any]:
    return {
        "chunk_id": chunk_id,
        "term_frequencies": dict(terms),
        "doc_length": sum(terms.values()),
    }


def _random_corpus(rng: random.Random, n_docs: int, vocab: List[str]) -> List[Dict]:
    corpus = []
    for i in range(n_docs):
        freqs: Dict[str, int] = {}
        for _ in range(rng.randint(3, 25)):
            t = rng.choice(vocab)
            freqs[t] = freqs.get(t, 0) + 1
        corpus.append(_stat(f"c_{i:04d}", freqs))
    return corpus


def _mirror(indexer: BM25Indexer, store: BM25SQLiteStore, term_stats: List[Dict],
            doc_hashes: Dict[str, str] | None = None) -> None:
    """按阶段 1 的双写语义把内存索引镜像进 SQLite。"""
    store.replace_all(
        index=indexer._index,
        metadata=indexer._metadata,
        doc_hash_by_chunk=doc_hashes,
    )


@pytest.fixture()
def indexer(tmp_path) -> BM25Indexer:
    return BM25Indexer(index_dir=str(tmp_path / "bm25"))


@pytest.fixture()
def store(tmp_path) -> BM25SQLiteStore:
    with BM25SQLiteStore(tmp_path / "bm25" / "test_bm25.sqlite") as s:
        yield s


# ───────────────────── T-1：打分逐 bit 等价 ─────────────────────


class TestScoreParity:
    def test_full_score_map_is_close_on_random_corpora(
        self, indexer, store
    ):
        """随机语料压测 —— 手写的几个例子碰不到累加顺序问题。

        固定种子保证可复现；每轮换一批语料和查询，累计比对上千个分数。
        分数值带容差比较（见模块 docstring），候选集合不放宽。
        """
        rng = random.Random(20260825)
        vocab = [f"w{i}" for i in range(60)]
        compared = 0

        for round_i in range(12):
            term_stats = _random_corpus(rng, rng.randint(5, 40), vocab)
            indexer.build(term_stats, collection=f"parity_{round_i}")
            _mirror(indexer, store, term_stats)

            for _ in range(6):
                q = rng.sample(vocab, rng.randint(1, 5))
                json_side = {
                    r["chunk_id"]: r["score"]
                    for r in indexer.query(q, top_k=_HUGE_TOP_K)
                }
                sqlite_side = {
                    r["chunk_id"]: r["score"]
                    for r in store.query(q, top_k=_HUGE_TOP_K)
                }

                _assert_score_maps_close(json_side, sqlite_side, context=f"查询={q}")
                compared += len(json_side)

        assert compared > 500, f"样本量太小（{compared}），压不出顺序问题"

    def test_accumulation_order_parity_when_chunk_id_order_differs(
        self, indexer, store
    ):
        """最容易出问题的形态：chunk_id 的字典序与插入序**相反**。

        SQLite 按聚簇主键 (term, chunk_id) 返回行，顺序是 chunk_id 字典序；
        JSON 侧的 postings 是 term_stats 顺序。语料里 chunk_id 若形如
        `c_0000, c_0001, …`，两者碰巧一致，这条差异就被掩盖了 ——
        所以这里刻意造成相反。词表也刻意小（每个 chunk 命中全部查询词），
        让单个 chunk 累加 6 项，把浮点末位差异的机会放到最大。

        下推之后不再要求逐 bit 相同（见模块 docstring），这条用例改为验证
        "即使在这种最容易暴露累加顺序差异的构造下，误差仍在容差内"。
        """
        rng = random.Random(31337)
        # 词表小 => 每个 chunk 命中大部分查询词 => 单个 chunk 累加 4~6 项
        vocab = [f"t{i}" for i in range(6)]
        n = 40
        term_stats = []
        for i in range(n):
            freqs = {t: rng.randint(1, 30) for t in vocab}
            # chunk_id 倒序：字典序与插入序完全相反
            term_stats.append(_stat(f"c_{n - 1 - i:04d}", freqs))

        indexer.build(term_stats, collection="accum_adversarial")
        _mirror(indexer, store, term_stats)

        q = list(vocab)
        j = {r["chunk_id"]: r["score"] for r in indexer.query(q, top_k=_HUGE_TOP_K)}
        s = {r["chunk_id"]: r["score"] for r in store.query(q, top_k=_HUGE_TOP_K)}

        _assert_score_maps_close(j, s, context="chunk_id 倒序构造")

    def test_multi_term_accumulation_order_matters(self, indexer, store):
        """专门构造"一个 chunk 命中多个查询词"的场景。

        这是浮点累加顺序唯一会显形的地方 —— 单词条查询无论怎么排都一样。
        用刻意难加的小数放大末位差异。
        """
        term_stats = [
            _stat("c_x", {"a": 3, "b": 7, "c": 11, "d": 13}),
            _stat("c_y", {"a": 1, "b": 1, "c": 1, "d": 1}),
            _stat("c_z", {"a": 17, "b": 2, "c": 5, "d": 3, "pad": 40}),
        ]
        indexer.build(term_stats, collection="accum")
        _mirror(indexer, store, term_stats)

        for q in (["a", "b", "c", "d"], ["d", "c", "b", "a"], ["c", "a", "d", "b"]):
            j = {r["chunk_id"]: r["score"] for r in indexer.query(q, top_k=_HUGE_TOP_K)}
            s = {r["chunk_id"]: r["score"] for r in store.query(q, top_k=_HUGE_TOP_K)}
            _assert_score_maps_close(j, s, context=f"查询词顺序 {q}")

    def test_partially_overlapping_postings_lists(self, indexer, store):
        """各词条的 postings 覆盖不同 chunk 子集、且相对顺序不一致的形态。

        词条 A 覆盖 [c1, c3]、B 覆盖 [c2, c3]。这类交错曾被怀疑会破坏累加
        顺序等价（见模块 docstring 里那段弯路），实测在容差内成立。
        """
        term_stats = [
            _stat("c1", {"A": 5}),
            _stat("c2", {"B": 3}),
            _stat("c3", {"A": 2, "B": 9}),
        ]
        indexer.build(term_stats, collection="ord")
        _mirror(indexer, store, term_stats)

        j = {r["chunk_id"]: r["score"] for r in indexer.query(["A", "B"], top_k=99)}
        s = {r["chunk_id"]: r["score"] for r in store.query(["A", "B"], top_k=99)}
        _assert_score_maps_close(j, s)

    def test_duplicate_query_terms_are_counted_twice_on_both_sides(
        self, indexer, store
    ):
        """JSON 侧 `for term in query_terms` 不去重 —— SQLite 侧不能擅自去重。

        这条是"重复计数"语义的判别力核心：诊断脚本 `_sql_side_query`
        用 `WHERE term IN (...)`，去重语义下 `twice == once`（不会翻倍），
        如果生产实现退化成那种写法，下面第一条断言会失败。
        """
        term_stats = [_stat("c_0", {"年假": 4}), _stat("c_1", {"年假": 1, "填充": 20})]
        indexer.build(term_stats, collection="dup")
        _mirror(indexer, store, term_stats)

        once = store.query(["年假"], top_k=9)
        twice = store.query(["年假", "年假"], top_k=9)

        assert twice[0]["score"] == pytest.approx(once[0]["score"] * 2)
        j = {r["chunk_id"]: r["score"] for r in indexer.query(["年假", "年假"], top_k=9)}
        s = {r["chunk_id"]: r["score"] for r in twice}
        _assert_score_maps_close(j, s)

    def test_duplicate_query_terms_three_times_still_matches_json(
        self, indexer, store
    ):
        """词条重复三次（奇数次，`2*x` 那种"乘法天然精确"的巧合不适用）。

        `x+x` 与 `2*x` 在 IEEE754 下总是位级相同（乘 2 只移指数不进位），
        但 `x+x+x` 与 `3*x` 不保证相同 —— SQL 侧用的是 `mult * 单行贡献`
        （乘法），JSON 侧是逐次相加。这条用例专门盯这个奇数次场景，
        确保就算不是位级相同，也仍在容差内。
        """
        term_stats = [_stat("c_0", {"年假": 4}), _stat("c_1", {"年假": 1, "填充": 20})]
        indexer.build(term_stats, collection="dup3")
        _mirror(indexer, store, term_stats)

        j = {
            r["chunk_id"]: r["score"]
            for r in indexer.query(["年假", "年假", "年假"], top_k=9)
        }
        s = {
            r["chunk_id"]: r["score"]
            for r in store.query(["年假", "年假", "年假"], top_k=9)
        }
        _assert_score_maps_close(j, s)

    def test_topk_ordering_is_identical(self, indexer, store):
        """含大量同分候选时，两边的 top-k 必须逐位相同（依赖两侧同样的 tie-break）。"""
        term_stats = [_stat(f"c_{i:03d}", {"年假": 2, "申请": 1}) for i in range(30)]
        indexer.build(term_stats, collection="topk")
        _mirror(indexer, store, term_stats)

        j = [r["chunk_id"] for r in indexer.query(["年假"], top_k=7)]
        s = [r["chunk_id"] for r in store.query(["年假"], top_k=7)]
        assert j == s == sorted(j)

    def test_unknown_terms_and_empty_query(self, indexer, store):
        term_stats = [_stat("c_0", {"年假": 1})]
        indexer.build(term_stats, collection="edge")
        _mirror(indexer, store, term_stats)

        assert store.query([], top_k=5) == []
        assert store.query(["不存在的词"], top_k=5) == indexer.query(
            ["不存在的词"], top_k=5
        )

    def test_query_on_empty_database_returns_empty(self, tmp_path):
        empty = BM25SQLiteStore(tmp_path / "never_written.sqlite")
        assert empty.query(["年假"], top_k=5) == []
        empty.close()

    def test_avg_doc_length_zero_falls_back_to_one(self, store):
        """`avg_doc_length == 0` 时代入 1.0 继续算，不是短路成别的式子。

        真实生产路径里这个分支理论上不可达（`num_docs==0` 时也不会有
        postings），但 `replace_all` 接受任意 metadata，直接构造这个
        不常见的库状态来钉住这条边界处理——SQL 侧的
        `if avg_doc_length == 0: avg_doc_length = 1.0` 必须和 Python 侧的
        `_calculate_bm25_score` 逐字一致。
        """
        index = {
            "年假": {
                "idf": 1.2345,
                "df": 2,
                "postings": [
                    {"chunk_id": "c_0", "tf": 3, "doc_length": 5},
                    {"chunk_id": "c_1", "tf": 1, "doc_length": 9},
                ],
            }
        }
        metadata = {"num_docs": 2, "avg_doc_length": 0.0, "total_terms": 1,
                    "collection": "zero_avgdl"}
        store.replace_all(index=index, metadata=metadata)

        results = store.query(["年假"], top_k=9)
        by_cid = {r["chunk_id"]: r["score"] for r in results}

        k1, b, avg_doc_length = 1.5, 0.75, 1.0  # 代入的是 1.0，不是 0
        expected = {}
        for cid, tf, dl in (("c_0", 3, 5), ("c_1", 1, 9)):
            numerator = tf * (k1 + 1)
            denominator = tf + k1 * (1 - b + b * (dl / avg_doc_length))
            expected[cid] = 1.2345 * (numerator / denominator)

        _assert_score_maps_close(expected, by_cid)

    def test_topk_truncation_boundary_with_many_ties_at_scale(self, indexer, store):
        """比 `test_topk_ordering_is_identical` 更大规模的截断边界场景。

        200 个候选、"年假"命中全部 200 个且分数完全相同（tf/doc_length
        一致），"填充"只命中前 100 个把它们的分数拉开，制造一批"刚好卡在
        top_k 附近"的同分/近分候选，并且候选总数远超一次 SQL 结果窗口，
        逼 `ORDER BY ... LIMIT` 在真正的候选池里做截断，不是在一个碰巧
        已经很小的结果集里。
        """
        term_stats = []
        for i in range(200):
            freqs = {"年假": 2}
            if i < 100:
                freqs["填充"] = 3
            term_stats.append(_stat(f"c_{i:04d}", freqs))
        indexer.build(term_stats, collection="topk_scale")
        _mirror(indexer, store, term_stats)

        for k in (1, 5, 37, 100, 150):
            j = [r["chunk_id"] for r in indexer.query(["年假", "填充"], top_k=k)]
            s = [r["chunk_id"] for r in store.query(["年假", "填充"], top_k=k)]
            assert j == s, f"top_k={k} 时结果集/顺序不一致"


# ───────────────── T-3：删除真的删得掉（那条 P0 的正解）─────────────────


class TestDeletionActuallyWorks:
    def test_delete_by_doc_hash_removes_postings(self, indexer, store):
        """对照组是 JSON 侧 `remove_document` —— 它恒返回 False。"""
        doc_hash = "a" * 64  # 真实 doc_id 是 64 字符的文件内容 SHA256
        term_stats = [
            _stat("65046ad1_0000_2a3ac7ab", {"年假": 3}),
            _stat("65046ad1_0001_9f2b1c04", {"年假": 1, "申请": 2}),
            _stat("ffffffff_0000_00000000", {"报销": 5}),
        ]
        indexer.build(term_stats, collection="del")
        _mirror(
            indexer,
            store,
            term_stats,
            doc_hashes={
                "65046ad1_0000_2a3ac7ab": doc_hash,
                "65046ad1_0001_9f2b1c04": doc_hash,
            },
        )

        before = store.count_postings()
        assert store.count_chunks_for_doc(doc_hash) == 2

        deleted = store.delete_by_doc_hash(doc_hash)

        assert deleted == 3, "该文档两个 chunk 共 3 条 postings"
        assert store.count_postings() == before - 3
        assert store.count_chunks_for_doc(doc_hash) == 0
        assert store.query(["年假"], top_k=9) == [], "该文档的词条应已查不到"
        assert store.query(["报销"], top_k=9), "别的文档不能被误删"

    def test_json_backend_still_cannot_delete_this(self, indexer):
        """把那条 P0 钉死成回归测试：JSON 侧至今删不掉。

        这条**故意断言现状是坏的**。等阶段 2 切读、JSON 侧下线时它会失败，
        那正是提醒去删掉它的信号 —— 不要在那之前"修绿"它。
        """
        doc_hash = "a" * 64
        indexer.build(
            [_stat("65046ad1_0000_2a3ac7ab", {"年假": 3})], collection="p0"
        )
        assert indexer.remove_document(doc_hash, collection="p0") is False
        assert indexer.query(["年假"], top_k=9), "postings 仍在（这就是那条 P0）"

    def test_delete_unknown_doc_hash_is_a_noop(self, indexer, store):
        term_stats = [_stat("c_0", {"年假": 1})]
        indexer.build(term_stats, collection="noop")
        _mirror(indexer, store, term_stats, doc_hashes={"c_0": "h1"})

        assert store.delete_by_doc_hash("不存在") == 0
        assert store.count_postings() == 1


# ─────────────── T-4：重摄入不产生重复 postings ───────────────


class TestReingestIdempotency:
    def test_rebuilding_same_corpus_does_not_duplicate(self, indexer, store):
        term_stats = [_stat("c_0", {"年假": 2}), _stat("c_1", {"申请": 3})]
        indexer.build(term_stats, collection="idem")
        _mirror(indexer, store, term_stats)
        first = store.count_postings()

        for _ in range(3):
            indexer.build(term_stats, collection="idem")
            _mirror(indexer, store, term_stats)

        assert store.count_postings() == first

    def test_shrinking_corpus_drops_stale_chunks(self, indexer, store):
        """chunk 从语料里消失后，chunks 表不能留下孤儿行。"""
        big = [_stat(f"c_{i}", {"年假": 1}) for i in range(5)]
        indexer.build(big, collection="shrink")
        _mirror(indexer, store, big, doc_hashes={f"c_{i}": "h" for i in range(5)})
        assert store.count_chunks_for_doc("h") == 5

        small = big[:2]
        indexer.build(small, collection="shrink")
        _mirror(indexer, store, small, doc_hashes={f"c_{i}": "h" for i in range(2)})

        assert store.count_chunks_for_doc("h") == 2
        assert store.count_postings() == 2

    def test_doc_hash_survives_a_full_rebuild(self, indexer, store):
        """上层 build() 是全量重建、会丢出处信息，chunks 表必须自己记住。

        这条抓的是"重建时把 chunks 清空再写"的实现 —— 那样 doc_hash 会变
        NULL，删除功能随之失效，而且只在"先摄入 A 再摄入 B"的真实流程里才暴露。
        """
        stats_a = [_stat("c_a", {"年假": 1})]
        indexer.build(stats_a, collection="survive")
        _mirror(indexer, store, stats_a, doc_hashes={"c_a": "hash_a"})

        combined = stats_a + [_stat("c_b", {"报销": 1})]
        indexer.build(combined, collection="survive")
        _mirror(indexer, store, combined, doc_hashes={"c_b": "hash_b"})

        assert store.count_chunks_for_doc("hash_a") == 1, "老 chunk 的出处被抹掉了"
        assert store.count_chunks_for_doc("hash_b") == 1


# ───────────────── T-5：并发查询（硬性规则：必须真并发）─────────────────


class TestConcurrentReads:
    def test_concurrent_queries_do_not_interfere(self, indexer, tmp_path):
        """`CLAUDE.md` §7.2：并发缺陷必须用并发方式验证，串行跑 N 次不算。

        每个协程用**自己的** store 实例（sqlite3 连接不跨线程共享），
        断言各自拿到的结果与串行基线一致。
        """
        db = tmp_path / "bm25" / "conc_bm25.sqlite"
        term_stats = [
            _stat(f"c_{i:03d}", {"年假": i % 5 + 1, "申请": i % 3 + 1})
            for i in range(50)
        ]
        indexer.build(term_stats, collection="conc")
        with BM25SQLiteStore(db) as writer:
            writer.replace_all(
                index=indexer._index,
                metadata=indexer._metadata,
            )

        with BM25SQLiteStore(db) as baseline_store:
            baseline = baseline_store.query(["年假", "申请"], top_k=10)

        started = asyncio.Event()

        async def one(idx: int):
            def run():
                with BM25SQLiteStore(db) as s:
                    return s.query(["年假", "申请"], top_k=10)

            if idx == 0:
                started.set()
            else:
                await started.wait()
            await asyncio.sleep(0)  # 强制让出，制造真实交错
            return await asyncio.to_thread(run)

        async def main():
            return await asyncio.gather(*(one(i) for i in range(10)))

        results = asyncio.run(main())

        assert len(results) == 10
        for i, r in enumerate(results):
            assert r == baseline, f"并发第 {i} 个协程的结果与串行基线不同"


class TestMetadata:
    def test_metadata_round_trips(self, indexer, store):
        term_stats = [_stat("c_0", {"年假": 3, "申请": 1}), _stat("c_1", {"报销": 2})]
        indexer.build(term_stats, collection="meta_rt")
        _mirror(indexer, store, term_stats)

        meta = store.load_metadata()
        assert meta["num_docs"] == indexer._metadata["num_docs"]
        assert meta["total_terms"] == indexer._metadata["total_terms"]
        assert meta["collection"] == "meta_rt"
        assert meta["schema_version"] == "1"

    def test_avg_doc_length_survives_float_round_trip(self, indexer, store):
        """avg_doc_length 参与打分，存成字符串再读回来必须**逐 bit 还原**。

        用 `repr()` 而不是 `str()` 就是为了这个；换成 str 会丢精度，
        打分随之偏移，T-1 的逐 bit 判据会整片失守。
        """
        term_stats = [_stat("c_0", {"a": 1}), _stat("c_1", {"b": 1}), _stat("c_2", {"c": 1})]
        indexer.build(term_stats, collection="float_rt")  # avg = 1.0
        term_stats2 = [_stat(f"c_{i}", {"a": i + 1}) for i in range(7)]
        indexer.build(term_stats2, collection="float_rt2")  # avg 除不尽
        _mirror(indexer, store, term_stats2)

        assert store.load_metadata()["avg_doc_length"] == indexer._metadata[
            "avg_doc_length"
        ]


# ─────────── 阶段 1 双写：走 BM25Indexer 公开 API 的端到端验证 ───────────


class TestDualWriteThroughPublicApi:
    """前面的用例都是手工调 `replace_all` 镜像。这一组走真实调用路径 ——
    `add_documents` / `remove_document`，验证接线本身没接错。

    没有这一组的话，`replace_all` 再正确也可能根本没被调用到。
    """

    @staticmethod
    def _sqlite_of(indexer: BM25Indexer, collection: str) -> BM25SQLiteStore:
        return BM25SQLiteStore(indexer._sqlite_path(collection))

    def test_add_documents_populates_sqlite_with_matching_scores(self, indexer):
        doc_hash = "b" * 64
        stats = [
            _stat("aa11bb22_0000_ffff", {"年假": 3, "申请": 1}),
            _stat("aa11bb22_0001_eeee", {"年假": 1, "流程": 2}),
        ]
        indexer.add_documents(stats, collection="dw", doc_id=doc_hash)

        assert indexer._sqlite_path("dw").exists(), "影子库没被创建"
        with self._sqlite_of(indexer, "dw") as s:
            assert s.count_chunks_for_doc(doc_hash) == 2, "doc_hash 没被记进 chunks 表"
            j = {r["chunk_id"]: r["score"]
                 for r in indexer.query(["年假", "申请"], top_k=_HUGE_TOP_K)}
            q = {r["chunk_id"]: r["score"]
                 for r in s.query(["年假", "申请"], top_k=_HUGE_TOP_K)}
            assert j == q, "双写后两边打分不一致"

    def test_remove_document_deletes_on_sqlite_side(self, indexer):
        """**这是那条 P0 在阶段 1 的实际闭环点。**

        JSON 侧依旧删不掉（`chunk_id.startswith(doc_id)` 恒为假），
        但 SQLite 侧按 doc_hash 精确删除 —— 所以 `remove_document` 现在
        返回 True，而在此改动之前它恒返回 False。
        """
        doc_hash = "c" * 64
        keep_hash = "d" * 64
        indexer.add_documents(
            [_stat("11112222_0000_aaaa", {"年假": 3})],
            collection="dwdel", doc_id=doc_hash,
        )
        indexer.add_documents(
            [_stat("33334444_0000_bbbb", {"报销": 5})],
            collection="dwdel", doc_id=keep_hash,
        )

        with self._sqlite_of(indexer, "dwdel") as s:
            assert s.count_chunks_for_doc(doc_hash) == 1

        assert indexer.remove_document(doc_hash, collection="dwdel") is True

        with self._sqlite_of(indexer, "dwdel") as s:
            assert s.count_chunks_for_doc(doc_hash) == 0
            assert s.query(["年假"], top_k=9) == [], "被删文档的词条仍可检索"
            assert s.query(["报销"], top_k=9), "误删了别的文档"

    def test_reingest_same_doc_does_not_duplicate_in_sqlite(self, indexer):
        """T-4：同一文档重复摄入，SQLite 侧 postings 不能翻倍。"""
        doc_hash = "e" * 64
        stats = [_stat("55556666_0000_cccc", {"年假": 2, "申请": 1})]

        indexer.add_documents(stats, collection="dwidem", doc_id=doc_hash)
        with self._sqlite_of(indexer, "dwidem") as s:
            first = s.count_postings()

        for _ in range(3):
            indexer.add_documents(stats, collection="dwidem", doc_id=doc_hash)

        with self._sqlite_of(indexer, "dwidem") as s:
            assert s.count_postings() == first
            assert s.count_chunks_for_doc(doc_hash) == 1

    def test_dual_write_can_be_disabled(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RAGENT_BM25_SQLITE_DUAL_WRITE", "false")
        ix = BM25Indexer(index_dir=str(tmp_path / "off"))
        ix.add_documents([_stat("c_0", {"年假": 1})], collection="off", doc_id="h")
        assert not ix._sqlite_path("off").exists()

    def test_sqlite_failure_does_not_break_ingestion(self, indexer, monkeypatch, caplog):
        """影子写失败绝不能让摄入失败 —— 但必须留下 ERROR 痕迹。

        阶段 1 里 SQLite 没有生产读者，拿它去阻断正在用的老链路是本末倒置；
        可一旦静默，阶段 2 切读时就无从知道哪个 collection 的副本掉过队。
        """
        import logging

        import src.ingestion.storage.bm25_sqlite_store as store_mod

        def boom(*a, **kw):
            raise RuntimeError("磁盘满了")

        monkeypatch.setattr(store_mod.BM25SQLiteStore, "replace_all", boom)

        with caplog.at_level(logging.ERROR):
            indexer.add_documents(
                [_stat("c_0", {"年假": 1})], collection="fail", doc_id="h"
            )

        assert indexer.query(["年假"], top_k=5), "JSON 侧读路径必须照常工作"
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert any("影子写失败" in r.getMessage() for r in errors), "失败被静默了"


# ─────────── 阶段 2：读路径切换（含回退与阈值）───────────


class TestReadBackendSwitch:
    """`load()` 是读路径的分流点，`query()` 据此走 SQLite 或 JSON。

    这组测试的重点不是"能切"，而是**每一条不该切的情况都真的没切** ——
    切错的后果是检索静默返回旧数据或空结果，比报错难查得多。
    """

    def _big_corpus(self, n: int = 400) -> List[Dict[str, Any]]:
        """造一个大到能越过 auto 阈值的语料（阈值默认 256KB）。"""
        rng = random.Random(4242)
        vocab = [f"w{i}" for i in range(500)]
        return [
            _stat(f"c_{i:05d}", {t: rng.randint(1, 5) for t in rng.sample(vocab, 40)})
            for i in range(n)
        ]

    def test_big_index_reads_from_sqlite_and_leaves_index_empty(self, tmp_path):
        """走 SQLite 时 `_index` 必须保持为空 —— 那正是这次改动的收益。"""
        ix = BM25Indexer(index_dir=str(tmp_path / "b"))
        ix.build(self._big_corpus(), collection="big")

        fresh = BM25Indexer(index_dir=str(tmp_path / "b"))
        assert fresh.load("big") is True
        assert fresh._sqlite_read_collection == "big"
        assert fresh._index == {}, "把 JSON 也载进来了，等于没省"
        assert fresh._metadata["num_docs"] == 400, "metadata 应照常填充"
        assert fresh.query(["w1"], top_k=5) is not None

    def test_small_index_still_reads_json(self, tmp_path):
        """小索引上 SQLite 更慢（实测交叉点约 290KB），auto 必须留在 JSON。"""
        ix = BM25Indexer(index_dir=str(tmp_path / "s"))
        ix.build([_stat("c_0", {"年假": 1})], collection="small")
        assert ix._sqlite_path("small").exists(), "前提：影子副本确实写了"

        fresh = BM25Indexer(index_dir=str(tmp_path / "s"))
        assert fresh.load("small") is True
        assert fresh._sqlite_read_collection is None
        assert fresh._index, "小索引应走 JSON，_index 必须被填充"

    def test_results_identical_across_backends(self, tmp_path):
        """同一个库、两种后端，完整分数映射带容差一致（下推后不再逐 bit 相同，
        见 `tests/unit/test_bm25_sqlite_store.py` 模块 docstring）。"""
        corpus = self._big_corpus()
        ix = BM25Indexer(index_dir=str(tmp_path / "p"))
        ix.build(corpus, collection="par")
        q = ["w1", "w7", "w13", "w21"]

        j = BM25Indexer(index_dir=str(tmp_path / "p")); j.read_backend = "json"
        j.load("par")
        s = BM25Indexer(index_dir=str(tmp_path / "p")); s.read_backend = "sqlite"
        s.load("par")

        _assert_score_maps_close(
            {r["chunk_id"]: r["score"] for r in j.query(q, top_k=10**6)},
            {r["chunk_id"]: r["score"] for r in s.query(q, top_k=10**6)},
        )

    def test_stale_sqlite_falls_back_to_json(self, tmp_path, caplog):
        """影子写失败过 => SQLite 比 JSON 旧 => 必须回退，且要留告警。

        没有这道检查，一次无人注意的影子写失败会**静默地把该库的检索结果
        退回到过去某个时刻**。
        """
        import logging
        import os

        ix = BM25Indexer(index_dir=str(tmp_path / "st"))
        ix.build(self._big_corpus(), collection="stale")

        # 把 JSON 的 mtime 推到 SQLite 之后，模拟"JSON 更新了但影子写失败"
        json_p = ix._get_index_path("stale")
        sqlite_p = ix._sqlite_path("stale")
        newer = sqlite_p.stat().st_mtime + 60
        os.utime(json_p, (newer, newer))

        fresh = BM25Indexer(index_dir=str(tmp_path / "st"))
        with caplog.at_level(logging.WARNING):
            assert fresh.load("stale") is True

        assert fresh._sqlite_read_collection is None, "读到了过期副本"
        assert fresh._index, "应已回退到 JSON"
        assert any("比 JSON 旧" in r.getMessage() for r in caplog.records), "回退没留痕"

    def test_backend_json_forces_json_even_when_sqlite_exists(self, tmp_path):
        """回滚开关：出问题时不用改代码就能退回老路径。"""
        ix = BM25Indexer(index_dir=str(tmp_path / "f"))
        ix.build(self._big_corpus(), collection="forced")

        fresh = BM25Indexer(index_dir=str(tmp_path / "f"))
        fresh.read_backend = "json"
        assert fresh.load("forced") is True
        assert fresh._sqlite_read_collection is None
        assert fresh._index

    def test_backend_sqlite_raises_when_copy_missing(self, tmp_path):
        """强制模式缺副本要显式失败，不能悄悄回退。

        它的用途是验收迁移；一旦回退，"读的到底是哪个后端"就说不清了。
        """
        ix = BM25Indexer(index_dir=str(tmp_path / "m"))
        ix.dual_write_sqlite = False           # 不写影子副本
        ix.build([_stat("c_0", {"年假": 1})], collection="nocopy")

        fresh = BM25Indexer(index_dir=str(tmp_path / "m"))
        fresh.read_backend = "sqlite"
        with pytest.raises(FileNotFoundError, match="不存在"):
            fresh.load("nocopy")

    def test_write_path_still_uses_json_after_switch(self, tmp_path):
        """**最危险的回归**：写路径若误用 `load()`，既有 postings 会被整份覆盖。

        `add_documents` 靠 `self._index` 做合并重建，而切读后 `load()` 刻意
        不填充它。用错的话，新增一份文档会把整个索引换成只有这一份。
        """
        ix = BM25Indexer(index_dir=str(tmp_path / "w"))
        ix.build(self._big_corpus(), collection="wr")
        before_chunks = {
            p["chunk_id"]
            for td in ix._index.values() for p in td["postings"]
        }
        assert len(before_chunks) == 400

        fresh = BM25Indexer(index_dir=str(tmp_path / "w"))
        fresh.add_documents(
            [_stat("newcomer", {"年假": 3})], collection="wr", doc_id="h" * 64
        )

        after_chunks = {
            p["chunk_id"]
            for td in fresh._index.values() for p in td["postings"]
        }
        assert "newcomer" in after_chunks
        assert before_chunks <= after_chunks, (
            f"既有 chunk 丢失了 {len(before_chunks - after_chunks)} 个 —— "
            f"写路径误用了读路径的 load()"
        )
