"""
组织（企业/租户）存储 (Org Store) — PostgreSQL 版

职责：
1. 组织的增/查——一个组织大致对应"一家企业"。
2. 用户所属哪个组织（`users.org_id`），以及据此判断"这个人是不是平台管理员"
   （`organizations.is_platform`）——平台管理员能看到/管理所有企业的用户，
   普通企业的管理员只能看到/管理自己企业的（`auth.require_same_org_or_platform`
   用到这个判断）。

不负责：
- 角色/知识库权限（那是 role_store.py 的事，这次没有按组织隔离，见
  attendance-tenant-federation.md 第 8.7 节的记录）
- 组织专属的外部数据源接入（attendance-tenant-federation.md 第 3-5 节的
  tenant_connectors/tenant_external_identities，是另一块设计，这里不涉及）
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from dataclasses import dataclass
from typing import List, Optional

import asyncpg

# 种子平台组织：现有唯一部署里的所有历史用户都会被回填到这个组织，
# is_platform=TRUE 意味着"能看到/管理所有企业的用户"——迁移前后行为完全
# 一致（人人都能管所有用户），符合零回归要求。
ORG_PLATFORM_ID = "org_platform"
ORG_PLATFORM_NAME = "平台运营方"


@dataclass(frozen=True)
class Organization:
    org_id: str
    name: str
    is_platform: bool
    created_at: float


class OrgStore:
    """组织存储 (PostgreSQL)。"""

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
            type(self)._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=5)
            await self._ensure_schema()
        return self._pool

    async def _ensure_schema(self) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS organizations (
                    id           TEXT PRIMARY KEY,
                    name         VARCHAR(128) NOT NULL,
                    is_platform  BOOLEAN NOT NULL DEFAULT FALSE,
                    created_at   DOUBLE PRECISION NOT NULL
                )
                """
            )
            # users 表由 user_store.py 建，这里只加一列；REFERENCES 能正常建成的
            # 前提是 organizations 表已经在上面这条 CREATE 里建好了——两条语句
            # 在同一个连接里顺序执行，不存在跨 store 的建表时序问题。org_store
            # 本身第一次被用到必然晚于登录（users 表早已存在），所以这里不用
            # 担心 users 表还不存在。
            await conn.execute(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS org_id TEXT REFERENCES organizations(id)"
            )
            await conn.execute(
                """
                INSERT INTO organizations (id, name, is_platform, created_at)
                VALUES ($1, $2, TRUE, $3)
                ON CONFLICT (id) DO NOTHING
                """,
                ORG_PLATFORM_ID, ORG_PLATFORM_NAME, time.time(),
            )
            # 历史账号（迁移前创建的、org_id 还是 NULL 的）一律回填到平台组织；
            # 已经有 org_id 的行不受影响，这条语句每次启动重跑都是安全的空操作。
            await conn.execute(
                "UPDATE users SET org_id = $1 WHERE org_id IS NULL", ORG_PLATFORM_ID,
            )

    @staticmethod
    def _row_to_org(row: asyncpg.Record) -> Organization:
        return Organization(
            org_id=row["id"],
            name=row["name"],
            is_platform=row["is_platform"],
            created_at=row["created_at"],
        )

    async def list_organizations(self) -> List[Organization]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, name, is_platform, created_at FROM organizations ORDER BY created_at ASC"
            )
        return [self._row_to_org(r) for r in rows]

    async def get_organization(self, org_id: str) -> Optional[Organization]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, name, is_platform, created_at FROM organizations WHERE id = $1", org_id,
            )
        return self._row_to_org(row) if row else None

    async def create_organization(self, name: str) -> Organization:
        pool = await self._get_pool()
        org_id = str(uuid.uuid4())
        now = time.time()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO organizations (id, name, is_platform, created_at) VALUES ($1, $2, FALSE, $3)",
                org_id, name, now,
            )
        return Organization(org_id=org_id, name=name, is_platform=False, created_at=now)

    async def get_org_for_user(self, user_id: str) -> Optional[Organization]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT o.id, o.name, o.is_platform, o.created_at
                FROM users u JOIN organizations o ON o.id = u.org_id
                WHERE u.id = $1
                """,
                user_id,
            )
        return self._row_to_org(row) if row else None

    async def get_orgs_for_users_batch(self, user_ids: List[str]) -> "dict[str, Organization]":
        """`get_org_for_user` 的批量版——1 次查询覆盖任意多用户，不是 N 次。

        2026-08-26 P1-14 修复：管理端 `/admin/users` 原来对每个用户单独调
        `get_org_for_user`，是"50 用户约 300 次串行查询"里的一部分。
        """
        if not user_ids:
            return {}
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT u.id AS user_id, o.id, o.name, o.is_platform, o.created_at
                FROM users u JOIN organizations o ON o.id = u.org_id
                WHERE u.id = ANY($1::text[])
                """,
                user_ids,
            )
        return {row["user_id"]: self._row_to_org(row) for row in rows}

    async def is_platform_admin(self, user_id: str) -> bool:
        org = await self.get_org_for_user(user_id)
        return bool(org and org.is_platform)

    async def set_user_organization(self, user_id: str, org_id: str) -> None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute("UPDATE users SET org_id = $1 WHERE id = $2", org_id, user_id)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            type(self)._pool = None
