"""
用户存储 (User Store) — PostgreSQL 版

职责：
1. 用户身份：username + 密码哈希（bcrypt），供登录校验
2. 数据权限：allowed_collections，即该用户能访问哪些共享知识库 collection
   （每个对话私有的 conv_{id} collection 不受此限制，见 acl.py）
3. 角色：role，决定能不能用管理后台（人员管理），跟 allowed_collections 是两件
   独立的事——一个人能看哪些知识库，和他能不能管理其他用户，不是同一个维度。

不负责：
- 签发/校验 token（那是 auth.py 的事）
- 会话/长期记忆/对话归档（各自有独立的 store）
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import asyncpg
import bcrypt

# 角色先分三档：超级管理员（管理后台，能增删用户、配权限）、管理用户（比普通用户
# 权限更高，但不能管别人）、普通用户（默认）。数据库里存的就是这几个字符串。
ROLE_SUPER_ADMIN = "super_admin"
ROLE_ADMIN = "admin"
ROLE_USER = "user"
VALID_ROLES = {ROLE_SUPER_ADMIN, ROLE_ADMIN, ROLE_USER}


@dataclass(frozen=True)
class User:
    user_id: str
    username: str
    allowed_collections: List[str]
    role: str
    created_at: float


class UserStore:
    """用户存储 (PostgreSQL)。"""

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
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    username VARCHAR(64) UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    allowed_collections TEXT[] NOT NULL DEFAULT '{}',
                    created_at DOUBLE PRECISION NOT NULL
                )
                """
            )
            # 迁移：老表没有 role 列，历史账号一律按"普通用户"处理
            await conn.execute(
                f"ALTER TABLE users ADD COLUMN IF NOT EXISTS role VARCHAR(32) NOT NULL DEFAULT '{ROLE_USER}'"
            )

    @staticmethod
    def _row_to_user(row: asyncpg.Record) -> User:
        return User(
            user_id=row["id"],
            username=row["username"],
            allowed_collections=list(row["allowed_collections"]),
            role=row["role"],
            created_at=row["created_at"],
        )

    # ------------------------------------------------------------------
    # 写
    # ------------------------------------------------------------------

    async def create_user(
        self,
        username: str,
        password: str,
        allowed_collections: Optional[List[str]] = None,
        role: str = ROLE_USER,
    ) -> User:
        """创建用户，密码用 bcrypt 哈希后存储，明文不落盘。"""
        if role not in VALID_ROLES:
            raise ValueError(f"Invalid role: {role!r}, must be one of {VALID_ROLES}")

        pool = await self._get_pool()
        password_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
        user_id = str(uuid.uuid4())
        now = time.time()
        allowed = allowed_collections or []

        async with pool.acquire() as conn:
            try:
                await conn.execute(
                    """INSERT INTO users (id, username, password_hash, allowed_collections, role, created_at)
                       VALUES ($1, $2, $3, $4, $5, $6)""",
                    user_id, username, password_hash, allowed, role, now,
                )
            except asyncpg.UniqueViolationError as e:
                raise ValueError(f"Username '{username}' already exists") from e

        return User(user_id=user_id, username=username, allowed_collections=allowed, role=role, created_at=now)

    # ------------------------------------------------------------------
    # 读 / 鉴权
    # ------------------------------------------------------------------

    async def authenticate(self, username: str, password: str) -> Optional[User]:
        """校验用户名密码，成功返回 User，失败（用户不存在或密码错）统一返回 None。

        不区分"用户不存在"和"密码错误"两种失败原因地对外暴露，避免给攻击者
        提供用户名是否存在的信息。
        """
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT id, username, password_hash, allowed_collections, role, created_at
                   FROM users WHERE username = $1""",
                username,
            )

        if row is None:
            return None

        if not bcrypt.checkpw(password.encode("utf-8"), row["password_hash"].encode("utf-8")):
            return None

        return self._row_to_user(row)

    async def get_user_by_id(self, user_id: str) -> Optional[User]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT id, username, allowed_collections, role, created_at
                   FROM users WHERE id = $1""",
                user_id,
            )
        return self._row_to_user(row) if row else None

    async def list_users(self) -> List[User]:
        """管理后台用：列出所有用户（不含密码哈希）。"""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT id, username, allowed_collections, role, created_at
                   FROM users ORDER BY created_at ASC"""
            )
        return [self._row_to_user(row) for row in rows]

    async def set_allowed_collections(self, username: str, allowed_collections: List[str]) -> bool:
        """管理员操作：按用户名重设允许访问的共享 collection 列表。返回是否找到该用户。"""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE users SET allowed_collections = $1 WHERE username = $2",
                allowed_collections, username,
            )
        return result.split()[-1] != "0"

    async def update_user(
        self,
        user_id: str,
        role: Optional[str] = None,
        allowed_collections: Optional[List[str]] = None,
    ) -> Optional[User]:
        """管理后台用：改角色和/或改权限，传 None 的字段不变。返回更新后的用户，找不到返回 None。"""
        if role is not None and role not in VALID_ROLES:
            raise ValueError(f"Invalid role: {role!r}, must be one of {VALID_ROLES}")

        pool = await self._get_pool()
        async with pool.acquire() as conn:
            if role is not None:
                await conn.execute("UPDATE users SET role = $1 WHERE id = $2", role, user_id)
            if allowed_collections is not None:
                await conn.execute(
                    "UPDATE users SET allowed_collections = $1 WHERE id = $2",
                    allowed_collections, user_id,
                )
        return await self.get_user_by_id(user_id)

    async def delete_user(self, user_id: str) -> bool:
        """管理后台用：删除用户。返回是否真的删到了一行。"""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute("DELETE FROM users WHERE id = $1", user_id)
        return result.split()[-1] != "0"

    async def change_password(self, user_id: str, old_password: str, new_password: str) -> bool:
        """校验旧密码后更新为新密码，返回是否成功（旧密码错/用户不存在都返回 False）。"""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT password_hash FROM users WHERE id = $1", user_id,
            )
            if row is None:
                return False
            if not bcrypt.checkpw(old_password.encode("utf-8"), row["password_hash"].encode("utf-8")):
                return False

            new_hash = bcrypt.hashpw(new_password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
            await conn.execute(
                "UPDATE users SET password_hash = $1 WHERE id = $2", new_hash, user_id,
            )
        return True

    async def get_allowed_collections(self, user_id: str) -> List[str]:
        """给 ACL 检查用：签名不变，内部委托 RoleStore 按角色关联的知识库并集
        计算——调用方（3 个 MCP 工具）不用动。users.allowed_collections 列本身
        已不再是权限真相源，只在角色迁移脚本里作为回填数据源使用。"""
        from src.ragent_backend.role_store import RoleStore

        return await RoleStore().get_allowed_collections_for_user(user_id)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            type(self)._pool = None
