"""进 cross-encoder 重排的候选池上限（`src/core/query_engine/rerank_pool.py`）。

背景：全库并行召回里"多查几个库变慢"的真实机制是**合并后的候选池变大**，
不是查得慢——6 个库的 dense+sparse 是并发的，墙钟只有 ~32ms，而 cross-encoder
要给池子里每个 (query, doc) 对逐一打分，占整个检索段 87%（931.7ms / 1072ms）。

**它在没有这个模块时会失败吗**（CLAUDE.md §7.2）：这是新增能力，旧实现里
"全部候选都进重排"是无条件的，没有可对照的单元。所以本文件的判别力是
**按新规则构造**；真正拿真实数据守着的是
`tests/integration/test_hierarchy_narrowing_recall.py::TestRerankPoolCap`
那条"截断后每个候选库仍有代表"——它在把上限写成常数时会跑红。

最容易被后人改错的一条：**上限必须随候选库数增长**。写成常数就会退化成
粗筛层那个"全局预算饿死库"的老毛病（实测截到 20 条时只剩 4~5 个库有代表，
金标召回从 25/30 掉到 19/30）。`test_cap_grows_with_collection_count` 守这条。
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from src.core.query_engine.rerank_pool import RerankPoolConfig, pool_cap, trim_pool


@dataclass
class _Result:
    chunk_id: str
    score: float


def _cfg(**kw) -> RerankPoolConfig:
    base = dict(per_collection=5, min_size=20)
    base.update(kw)
    return RerankPoolConfig(**base)


class TestPoolCap:
    @pytest.mark.parametrize("collections,expected", [
        (1, 20),    # 单库：召回池本来就只有 top_k×2=10 条，下限 20 保证碰不到它
        (2, 20),
        (6, 30),    # 实测这一档零召回代价
        (12, 60),   # 库翻倍，上限跟着翻倍
        (24, 120),
    ])
    def test_cap_grows_with_collection_count(self, collections, expected):
        """**这条是本模块的命脉**：上限写成常数就会饿死库。"""
        assert pool_cap(5, collections, _cfg()) == expected

    def test_single_collection_user_is_untouched(self):
        """单库用户的池子只有 10 条，上限 20 意味着永远不截——
        这笔优化只该向多库用户收取，不该改变单库行为。"""
        cap = pool_cap(5, 1, _cfg())
        pool = [_Result(f"c{i}", 1.0 - i * 0.01) for i in range(10)]
        assert trim_pool(pool, cap) == pool

    def test_large_top_k_is_not_starved_by_the_cap(self):
        """请求 top_k=20 时，上限不能把候选压到比要返回的还少。"""
        assert pool_cap(20, 2, _cfg()) == 40

    def test_zero_per_collection_disables_trimming(self):
        assert pool_cap(5, 6, _cfg(per_collection=0)) == 0

    def test_zero_collections_does_not_crash(self):
        assert pool_cap(5, 0, _cfg()) == 20


class TestTrimPool:
    def test_keeps_highest_scores(self):
        pool = [_Result("low", 0.1), _Result("high", 0.9), _Result("mid", 0.5)]
        assert [r.chunk_id for r in trim_pool(pool, 2)] == ["high", "mid"]

    def test_sorts_before_trimming(self):
        """merged 是多个库按 collection 顺序拼起来的，跨库之间没有顺序保证——
        不能假设传进来就是有序的。"""
        pool = [_Result(f"a{i}", 0.1 * i) for i in range(5)] + \
               [_Result(f"b{i}", 0.1 * i + 0.05) for i in range(5)]
        kept = {r.chunk_id for r in trim_pool(pool, 3)}
        assert kept == {"a4", "b4", "b3"}

    def test_cap_zero_returns_everything(self):
        pool = [_Result(f"c{i}", 0.5) for i in range(50)]
        assert len(trim_pool(pool, 0)) == 50

    def test_pool_smaller_than_cap_is_returned_as_is(self):
        pool = [_Result(f"c{i}", 0.5) for i in range(5)]
        assert trim_pool(pool, 30) == pool

    def test_returns_a_new_list_not_the_input(self):
        pool = [_Result("a", 0.5)]
        assert trim_pool(pool, 30) is not pool


class TestConfigParsing:
    def test_reads_from_settings(self):
        from src.core.settings import load_settings
        cfg = RerankPoolConfig.from_settings(load_settings())
        assert cfg.per_collection > 0, "配置正本里截断不该是关的"
        assert pool_cap(5, 6, cfg) == 30, "6 库应当截到 30 条（实测零召回代价的那一档）"

    def test_missing_section_uses_defaults(self):
        cfg = RerankPoolConfig.from_settings(object())
        assert (cfg.per_collection, cfg.min_size) == (5, 20)
