"""
租户外部身份映射存储 (Tenant Identity Store) — PostgreSQL 版

职责：记录"我们系统里的这个用户，在企业自己的系统里对应哪个工号"——只有委托
考勤查询这类场景需要（我们要拿企业自己系统认得的员工 ID 去查），知识库检索不需要
（按组织路由即可，见 `knowledge-base-tenant-federation.md` 第 1.4 节对比表）。

表结构原样落地 `attendance-tenant-federation.md` 第 3 节的 `tenant_external_identities`。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List, Optional

import asyncpg


@dataclass(frozen=True)
class TenantExternalIdentity:
    user_id: str
    org_id: str
    capability: str
    external_id: str


class TenantIdentityStore:
    """租户外部身份映射存储 (PostgreSQL)。"""

    def __init__(self) -> None:
        self._pool: Optional[asyncpg.Pool] = None
        self._dsn = os.getenv("RAGENT_POSTGRES_URL", "postgresql://postgres:postgres@localhost:5432/ragent")

    async def _get_pool(self) -> asyncpg.Pool:
        if self._pool is not None:
            return self._pool
        self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=5)
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
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
