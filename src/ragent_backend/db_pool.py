"""P1-2：14 个 Store 类原来各自独立 `asyncpg.create_pool`，但全部指向同一个
`RAGENT_POSTGRES_URL`（已核对：14 个文件读的是同一个环境变量、同一个默认值），
累计连接数上限约 68（13 个 `max_size=5` + `ltm_store` 的 `max_size=3`），而
`docs/scale_slo_and_priorities.md` 重估的并发在飞请求数只有 4（合成 P50）～
19（合成 P95）。68 这个上限本身不是问题，问题是分散在 14 处既浪费连接配额，
也没法统一调参/观测。

这里提供一个按 DSN 缓存的共享连接池注册表，14 个 Store 的 `_get_pool()` 都改成
从这里取——跟 `chroma_store.py::_get_or_create_client`（P1-8）是同一个模式。

⚠️ **池的生命周期是全局共享的**：任何单个 Store 都不应该自己 `pool.close()`，
那会把其它 13 个 Store 正在用的连接一起关掉。真正的关闭只应该在 app 关闭时调用
一次 `close_shared_pools()`（见 `app.py` 的 shutdown 路径）；各 Store 自己的
`close()` 方法只清掉自己持有的引用，不触发真实关闭。
"""

from __future__ import annotations

import asyncio
import os
from typing import Dict, Optional

import asyncpg

# 按重估的 P95 并发在飞请求数（19）留了余量，远低于原来 14 个池累加的 68。
DEFAULT_MAX_SIZE = int(os.getenv("RAGENT_DB_POOL_MAX_SIZE", "20"))

_POOL_CACHE: Dict[str, asyncpg.Pool] = {}
_POOL_CACHE_LOCK = asyncio.Lock()


async def get_shared_pool(
    dsn: str, *, min_size: int = 1, max_size: Optional[int] = None
) -> asyncpg.Pool:
    """返回按 *dsn* 缓存的共享连接池，不存在则创建一次。

    多个 Store 用不同的 min_size/max_size 调用同一个 dsn 时，只有第一次调用的
    参数会生效（池一旦建好不会重建）——这跟原来"每个 Store 各自的 max_size
    互不影响"不同，是合并成一个池的必然代价，用 `RAGENT_DB_POOL_MAX_SIZE`
    统一控制上限即可，不需要每个 Store 再各自传一次。
    """
    pool = _POOL_CACHE.get(dsn)
    if pool is not None:
        return pool
    async with _POOL_CACHE_LOCK:
        pool = _POOL_CACHE.get(dsn)
        if pool is not None:
            return pool
        pool = await asyncpg.create_pool(
            dsn, min_size=min_size, max_size=max_size or DEFAULT_MAX_SIZE
        )
        _POOL_CACHE[dsn] = pool
        return pool


async def close_shared_pools() -> None:
    """关闭全部缓存的共享池。只应该在 app 真正关闭时调用一次。"""
    async with _POOL_CACHE_LOCK:
        pools = list(_POOL_CACHE.values())
        _POOL_CACHE.clear()
    for pool in pools:
        await pool.close()
