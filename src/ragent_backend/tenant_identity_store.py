"""
租户外部身份映射存储 (Tenant Identity Store) — PostgreSQL 版

职责：记录"我们系统里的这个用户，在企业自己的系统里对应哪个工号"——只有委托
考勤查询这类场景需要（我们要拿企业自己系统认得的员工 ID 去查），知识库检索不需要
（按组织路由即可，见 `knowledge-base-tenant-federation.md` 第 1.4 节对比表）。

表结构原样落地 `attendance-tenant-federation.md` 第 3 节的 `tenant_external_identities`。
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import List, Optional

import asyncpg

from src.ragent_backend.db_pool import get_shared_pool


@dataclass(frozen=True)
class TenantExternalIdentity:
    user_id: str
    org_id: str
    capability: str
    external_id: str


class TenantIdentityStore:
    """租户外部身份映射存储 (PostgreSQL)。"""

    # 类级别共享连接池，见 store.py 同名字段的注释——调用方经常每次都 new 一个
    # 新实例，池必须挂在类属性上才不会被重复创建、打满 Postgres 连接数。
    _pool: Optional[asyncpg.Pool] = None
    _pool_lock = asyncio.Lock()

    def __init__(self) -> None:
        self._dsn = os.getenv("RAGENT_POSTGRES_URL", "postgresql://postgres:postgres@localhost:5432/ragent")

    async def _get_pool(self) -> asyncpg.Pool:
        if self._pool is not None:
            return self._pool
        async with self._pool_lock:
            if self._pool is not None:
                return self._pool
            type(self)._pool = await get_shared_pool(self._dsn)
            await self._ensure_schema()
        return self._pool

    async def _ensure_schema(self) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tenant_external_identities (
                    user_id      TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    org_id       TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
                    capability   VARCHAR(32) NOT NULL,
                    external_id  TEXT NOT NULL,
                    PRIMARY KEY (user_id, capability)
                )
                """
            )

    async def get_external_id(self, user_id: str, capability: str) -> Optional[str]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT external_id FROM tenant_external_identities WHERE user_id = $1 AND capability = $2",
                user_id, capability,
            )
        return row["external_id"] if row else None

    async def upsert(self, user_id: str, org_id: str, capability: str, external_id: str) -> TenantExternalIdentity:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO tenant_external_identities (user_id, org_id, capability, external_id)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (user_id, capability) DO UPDATE SET
                    org_id = EXCLUDED.org_id, external_id = EXCLUDED.external_id
                """,
                user_id, org_id, capability, external_id,
            )
        return TenantExternalIdentity(user_id=user_id, org_id=org_id, capability=capability, external_id=external_id)

    async def list_for_org(self, org_id: str, capability: str) -> List[TenantExternalIdentity]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT user_id, org_id, capability, external_id FROM tenant_external_identities "
                "WHERE org_id = $1 AND capability = $2",
                org_id, capability,
            )
        return [TenantExternalIdentity(**dict(r)) for r in rows]

    async def close(self) -> None:
        # 池现在是跨 14 个 Store 共享的（db_pool.py，P1-2），这里只清掉
        # 本 Store 持有的引用，不触发真实关闭——那会把其它 Store 正在用的
        # 连接一起关掉。真正关闭见 db_pool.close_shared_pools()，只在 app
        # 关闭时调一次。
        type(self)._pool = None
