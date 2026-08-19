"""
角色存储 (Role Store) — PostgreSQL 版

职责：
1. 角色主数据：新增/改名(仅 display_name)/删除角色
2. 角色 <-> 知识库（collection）关联：给一个角色配一批共享知识库
3. 用户 <-> 角色关联（多对多）：给一个用户分配一批角色
4. 权限并集计算：给定 user_id，算出他所有角色关联知识库的并集
   （任一角色关联了通配符 "*"，直接视为不限制）

不负责：
- 用户身份/密码（那是 user_store.py 的事）
- ACL 判定本身（那是 acl.py 的事，它只认 List[str]，不关心数据怎么来的）

四个内置系统角色（super_admin/admin/org_admin/user）由 `_ensure_schema` 种子写入，
`is_system=True`，不可删除、不可改名（`name`）。其中 super_admin/admin 是平台运营方
角色，org_admin（企业管理员）是客户企业侧角色——平台管理员能管平台（建企业、建
连接器、任命某企业的 org_admin），但不了解客户企业内部的部门架构，所以不直接管理
某个企业内部的员工角色/知识库权限，那是 org_admin 被任命后自己的事（见 app.py
`_validate_role_assignment` 里的权限边界判断）。
"""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass, field
from typing import List, Optional

import asyncpg

ROLE_SUPER_ADMIN = "super_admin"
ROLE_ADMIN = "admin"
ROLE_ORG_ADMIN = "org_admin"
ROLE_USER = "user"
SYSTEM_ROLE_SEEDS = (
    (ROLE_SUPER_ADMIN, "超级管理员"),
    (ROLE_ADMIN, "管理员"),
    (ROLE_ORG_ADMIN, "企业管理员"),
    (ROLE_USER, "普通用户"),
)

_WILDCARD = "*"


@dataclass(frozen=True)
class Role:
    role_id: str
    name: str
    display_name: str
    is_system: bool
    created_at: float


@dataclass(frozen=True)
class RoleWithCollections(Role):
    collection_names: List[str] = field(default_factory=list)


class RoleStore:
    """角色存储 (PostgreSQL)。"""

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
                CREATE TABLE IF NOT EXISTS roles (
                    id            TEXT PRIMARY KEY,
                    name          VARCHAR(64) UNIQUE NOT NULL,
                    display_name  VARCHAR(128) NOT NULL,
                    is_system     BOOLEAN NOT NULL DEFAULT FALSE,
                    created_at    DOUBLE PRECISION NOT NULL
                )
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS role_collections (
                    role_id         TEXT NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
                    collection_name TEXT NOT NULL,
                    PRIMARY KEY (role_id, collection_name)
                )
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_roles (
                    user_id  TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    role_id  TEXT NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
                    PRIMARY KEY (user_id, role_id)
                )
                """
            )
            for name, display_name in SYSTEM_ROLE_SEEDS:
                await conn.execute(
                    """
                    INSERT INTO roles (id, name, display_name, is_system, created_at)
                    VALUES ($1, $2, $3, TRUE, $4)
                    ON CONFLICT (name) DO NOTHING
                    """,
                    str(uuid.uuid4()), name, display_name, time.time(),
                )

    @staticmethod
    def _row_to_role(row: asyncpg.Record) -> Role:
        return Role(
            role_id=row["id"],
            name=row["name"],
            display_name=row["display_name"],
            is_system=row["is_system"],
            created_at=row["created_at"],
        )

    # ------------------------------------------------------------------
    # 角色 CRUD
    # ------------------------------------------------------------------

    async def create_role(self, name: str, display_name: str) -> Role:
        pool = await self._get_pool()
        role_id = str(uuid.uuid4())
        now = time.time()
        async with pool.acquire() as conn:
            try:
                await conn.execute(
                    """INSERT INTO roles (id, name, display_name, is_system, created_at)
                       VALUES ($1, $2, $3, FALSE, $4)""",
                    role_id, name, display_name, now,
                )
            except asyncpg.UniqueViolationError as e:
                raise ValueError(f"Role name '{name}' already exists") from e
        return Role(role_id=role_id, name=name, display_name=display_name, is_system=False, created_at=now)

    async def list_roles(self) -> List[RoleWithCollections]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            role_rows = await conn.fetch(
                "SELECT id, name, display_name, is_system, created_at FROM roles ORDER BY created_at ASC"
            )
            collection_rows = await conn.fetch("SELECT role_id, collection_name FROM role_collections")

        collections_by_role: dict[str, List[str]] = {}
        for row in collection_rows:
            collections_by_role.setdefault(row["role_id"], []).append(row["collection_name"])

        return [
            RoleWithCollections(
                role_id=row["id"],
                name=row["name"],
                display_name=row["display_name"],
                is_system=row["is_system"],
                created_at=row["created_at"],
                collection_names=sorted(collections_by_role.get(row["id"], [])),
            )
            for row in role_rows
        ]

    async def get_role_by_id(self, role_id: str) -> Optional[Role]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, name, display_name, is_system, created_at FROM roles WHERE id = $1", role_id,
            )
        return self._row_to_role(row) if row else None

    async def update_role(self, role_id: str, display_name: str) -> Optional[Role]:
        """改 display_name；is_system 角色的 name 从不允许改（这个方法压根不接受改 name）。"""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute("UPDATE roles SET display_name = $1 WHERE id = $2", display_name, role_id)
        return await self.get_role_by_id(role_id)

    async def delete_role(self, role_id: str) -> bool:
        role = await self.get_role_by_id(role_id)
        if role is None:
            return False
        if role.is_system:
            raise ValueError(f"系统内置角色 '{role.name}' 不可删除")

        pool = await self._get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute("DELETE FROM roles WHERE id = $1", role_id)
        return result.split()[-1] != "0"

    # ------------------------------------------------------------------
    # 关联关系（整体替换）
    # ------------------------------------------------------------------

    async def set_role_collections(self, role_id: str, collection_names: List[str]) -> None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("DELETE FROM role_collections WHERE role_id = $1", role_id)
                if collection_names:
                    await conn.executemany(
                        "INSERT INTO role_collections (role_id, collection_name) VALUES ($1, $2)",
                        [(role_id, name) for name in collection_names],
                    )

    async def add_role_collection(self, role_id: str, collection_name: str) -> None:
        """增量关联（幂等）：给角色追加一个知识库，已存在则忽略。供迁移脚本使用——
        迁移脚本要反复可执行（见风险与开放问题第 3 条），不能像 set_role_collections
        那样整体替换，否则多次运行会互相冲掉对方写入的关联。"""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO role_collections (role_id, collection_name) VALUES ($1, $2)
                   ON CONFLICT (role_id, collection_name) DO NOTHING""",
                role_id, collection_name,
            )

    async def add_user_role(self, user_id: str, role_id: str) -> None:
        """增量关联（幂等）：给用户追加一个角色，已存在则忽略。供迁移脚本使用。"""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO user_roles (user_id, role_id) VALUES ($1, $2)
                   ON CONFLICT (user_id, role_id) DO NOTHING""",
                user_id, role_id,
            )

    async def get_role_by_name(self, name: str) -> Optional[Role]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, name, display_name, is_system, created_at FROM roles WHERE name = $1", name,
            )
        return self._row_to_role(row) if row else None

    async def get_or_create_role_by_name(self, name: str, display_name: str, is_system: bool = False) -> Role:
        """幂等版 create_role：已存在则直接返回，不存在才建。供迁移脚本使用。"""
        existing = await self.get_role_by_name(name)
        if existing is not None:
            return existing

        pool = await self._get_pool()
        role_id = str(uuid.uuid4())
        now = time.time()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO roles (id, name, display_name, is_system, created_at)
                   VALUES ($1, $2, $3, $4, $5)
                   ON CONFLICT (name) DO NOTHING""",
                role_id, name, display_name, is_system, now,
            )
        # 并发场景下上面的 INSERT 可能因为 ON CONFLICT 没有真正插入，重新查一次拿到准确的行
        role = await self.get_role_by_name(name)
        assert role is not None
        return role

    async def assign_user_roles(self, user_id: str, role_ids: List[str]) -> None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("DELETE FROM user_roles WHERE user_id = $1", user_id)
                if role_ids:
                    await conn.executemany(
                        "INSERT INTO user_roles (user_id, role_id) VALUES ($1, $2)",
                        [(user_id, role_id) for role_id in role_ids],
                    )

    # ------------------------------------------------------------------
    # 读 / 权限并集
    # ------------------------------------------------------------------

    async def get_user_roles(self, user_id: str) -> List[Role]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT r.id, r.name, r.display_name, r.is_system, r.created_at
                FROM roles r
                JOIN user_roles ur ON ur.role_id = r.id
                WHERE ur.user_id = $1
                ORDER BY r.created_at ASC
                """,
                user_id,
            )
        return [self._row_to_role(row) for row in rows]

    async def get_user_ids_by_role(self, role_id: str) -> List[str]:
        """`get_user_roles` 的反过来版本：给定一个角色，查有哪些用户持有它。
        供工作流的站内信通知用（work-flow-web.md 6.2 节）——一条新工单提交后，
        要通知模板 `approver_role_id` 下的所有审批人，而不是某一个特定用户。"""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT user_id FROM user_roles WHERE role_id = $1", role_id,
            )
        return [row["user_id"] for row in rows]

    async def get_allowed_collections_for_user(self, user_id: str) -> List[str]:
        """并集去重；任一角色关联了通配符 "*"，直接返回 ["*"]。"""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT DISTINCT rc.collection_name
                FROM role_collections rc
                JOIN user_roles ur ON ur.role_id = rc.role_id
                WHERE ur.user_id = $1
                """,
                user_id,
            )
        collection_names = [row["collection_name"] for row in rows]
        if _WILDCARD in collection_names:
            return [_WILDCARD]
        return collection_names

    async def list_all_collection_names_from_roles(self) -> List[str]:
        """管理界面用：所有角色曾经关联过的 collection 名去重列表（不含通配符）。"""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT DISTINCT collection_name FROM role_collections WHERE collection_name != $1", _WILDCARD,
            )
        return sorted(row["collection_name"] for row in rows)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
