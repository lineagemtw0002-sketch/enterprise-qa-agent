"""P1-2 回归保护：14 个 Store 共享一个按 DSN 缓存的连接池
（`src/ragent_backend/db_pool.py`）。

判别力自查（`CLAUDE.md` §7.2）：把 `get_shared_pool` 换回"每次调用都
`asyncpg.create_pool`"（旧实现），`TestSameDsnSharesOnePool` 与
`TestConcurrentCallsCreateOnlyOnePool` 会红——前者因为两次调用拿到的不再是
同一个对象，后者因为并发调用会触发多次 `asyncpg.create_pool`。
"""

from __future__ import annotations

import asyncio

import pytest

from src.ragent_backend import db_pool


class _FakePool:
    """轻量假 asyncpg.Pool，只用来验证身份（is）与 close() 是否被调用过。"""

    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _reset_pool_cache():
    """每条测试前后都清空全局缓存，测试之间不应互相污染。"""
    db_pool._POOL_CACHE.clear()
    yield
    db_pool._POOL_CACHE.clear()


class TestSameDsnSharesOnePool:
    @pytest.mark.asyncio
    async def test_two_calls_same_dsn_return_same_object(self, monkeypatch):
        created = []

        async def fake_create_pool(dsn, *, min_size, max_size):
            pool = _FakePool()
            created.append((dsn, min_size, max_size))
            return pool

        monkeypatch.setattr(db_pool.asyncpg, "create_pool", fake_create_pool)

        pool1 = await db_pool.get_shared_pool("postgresql://a")
        pool2 = await db_pool.get_shared_pool("postgresql://a")

        assert pool1 is pool2
        assert len(created) == 1

    @pytest.mark.asyncio
    async def test_different_dsn_get_different_pools(self, monkeypatch):
        async def fake_create_pool(dsn, *, min_size, max_size):
            return _FakePool()

        monkeypatch.setattr(db_pool.asyncpg, "create_pool", fake_create_pool)

        pool_a = await db_pool.get_shared_pool("postgresql://a")
        pool_b = await db_pool.get_shared_pool("postgresql://b")

        assert pool_a is not pool_b


class TestConcurrentCallsCreateOnlyOnePool:
    @pytest.mark.asyncio
    async def test_concurrent_get_shared_pool_same_dsn(self, monkeypatch):
        """多个 Store 在应用启动阶段几乎同时第一次调用 get_shared_pool
        （同一个 dsn）时，必须只真正建一次池——用真并发（asyncio.gather）
        而不是串行调用两次，且在 fake create_pool 里人为加一点延迟撑开
        竞争窗口，逼出没加锁保护时才会暴露的重复创建。"""
        created = []

        async def fake_create_pool(dsn, *, min_size, max_size):
            await asyncio.sleep(0.05)
            created.append(dsn)
            return _FakePool()

        monkeypatch.setattr(db_pool.asyncpg, "create_pool", fake_create_pool)

        pools = await asyncio.gather(
            *[db_pool.get_shared_pool("postgresql://concurrent") for _ in range(8)]
        )

        assert len(created) == 1, f"应该只真正建一次池，实际建了 {len(created)} 次"
        assert len({id(p) for p in pools}) == 1, "并发调用应该都拿到同一个池对象"


class TestDefaultMaxSize:
    @pytest.mark.asyncio
    async def test_uses_default_max_size_when_not_specified(self, monkeypatch):
        captured = {}

        async def fake_create_pool(dsn, *, min_size, max_size):
            captured["min_size"] = min_size
            captured["max_size"] = max_size
            return _FakePool()

        monkeypatch.setattr(db_pool.asyncpg, "create_pool", fake_create_pool)

        await db_pool.get_shared_pool("postgresql://default-size")

        assert captured["min_size"] == 1
        assert captured["max_size"] == db_pool.DEFAULT_MAX_SIZE

    @pytest.mark.asyncio
    async def test_explicit_max_size_only_applies_on_first_creation(self, monkeypatch):
        """池一旦建好就不会因为后来者传了不同的 max_size 而重建——这是合并成
        共享池后的既有代价（db_pool.py 模块 docstring 已经写明），这里钉死
        行为，防止以后有人"顺手"改成每次都按调用方参数重建。"""
        captured = {}

        async def fake_create_pool(dsn, *, min_size, max_size):
            captured["max_size"] = max_size
            return _FakePool()

        monkeypatch.setattr(db_pool.asyncpg, "create_pool", fake_create_pool)

        await db_pool.get_shared_pool("postgresql://x", max_size=7)
        await db_pool.get_shared_pool("postgresql://x", max_size=99)

        assert captured["max_size"] == 7


class TestCloseSharedPools:
    @pytest.mark.asyncio
    async def test_closes_all_cached_pools_and_clears_cache(self, monkeypatch):
        async def fake_create_pool(dsn, *, min_size, max_size):
            return _FakePool()

        monkeypatch.setattr(db_pool.asyncpg, "create_pool", fake_create_pool)

        pool_a = await db_pool.get_shared_pool("postgresql://a")
        pool_b = await db_pool.get_shared_pool("postgresql://b")

        await db_pool.close_shared_pools()

        assert pool_a.closed is True
        assert pool_b.closed is True
        assert db_pool._POOL_CACHE == {}

    @pytest.mark.asyncio
    async def test_safe_to_call_with_empty_cache(self):
        # 不应该抛异常——app 从未真正建过任何池（比如启动即失败）时，
        # shutdown 钩子仍然会调用一次。
        await db_pool.close_shared_pools()
