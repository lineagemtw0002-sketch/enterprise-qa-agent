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
    # 2026-08-26 账号生命周期（docs/account_lifecycle_design.md §4.2）。
    # 有默认值、放在最后，是为了不破坏既有的位置构造调用。
    disabled_at: Optional[float] = None
    activated_at: Optional[float] = None


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
            # 2026-08-26 账号生命周期与激活码
            # （docs/account_lifecycle_design.md §4.1b §4.2）。
            #
            # 时间列一律用 DOUBLE PRECISION 存 epoch 秒，跟本表既有的 created_at
            # 保持一致。设计文档里写的是 TIMESTAMP——**这里刻意不跟设计走**：
            # 同一张表里两种时间表示会让每个读这张表的人都要先确认"这一列是哪种"，
            # 而 `_row_to_user` 现在能直接把值透传给 dataclass 的 float 字段。
            # 统一比"用更好的类型"更值钱。
            for _col in (
                "disabled_at DOUBLE PRECISION",
                "activation_code_hash TEXT",
                "activation_expires_at DOUBLE PRECISION",
                "activated_at DOUBLE PRECISION",
            ):
                await conn.execute(f"ALTER TABLE users ADD COLUMN IF NOT EXISTS {_col}")

            # 未激活的账号还没有密码，所以 password_hash 不能再是 NOT NULL。
            # 这一步同时解掉了第二档（SSO）的一个阻塞：SSO 用户永远不会有本地
            # 密码，`CLAUDE.md` 记的现状正是"password_hash NOT NULL 挡住 SSO
            # 用户"。一次变更两档共用。
            #
            # DROP NOT NULL 对存量行是非破坏性的（不改任何已有值），且重复执行
            # 是幂等的——列已经可空时 Postgres 直接无操作，不报错。
            await conn.execute("ALTER TABLE users ALTER COLUMN password_hash DROP NOT NULL")

            # 按企业统计在用席位要扫 org_id，导入时按 username 批量查归属也要走
            # username（它本来就有 UNIQUE 索引）。org_id 上补一个索引。
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_users_org_id ON users (org_id)"
            )

    @staticmethod
    def _row_to_user(row: asyncpg.Record) -> User:
        # 有些查询没 SELECT 生命周期那几列（`get_user_by_id` 等），
        # asyncpg 的 Record 对缺失的 key 会抛 KeyError 而不是返回 None，
        # 所以这里按 key 是否存在取值，不能直接 row["disabled_at"]。
        keys = row.keys()
        return User(
            user_id=row["id"],
            username=row["username"],
            allowed_collections=list(row["allowed_collections"]),
            role=row["role"],
            created_at=row["created_at"],
            disabled_at=row["disabled_at"] if "disabled_at" in keys else None,
            activated_at=row["activated_at"] if "activated_at" in keys else None,
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
                """SELECT id, username, password_hash, allowed_collections, role,
                          created_at, disabled_at, activated_at
                   FROM users WHERE username = $1""",
                username,
            )

        if row is None:
            return None

        # 2026-08-26：password_hash 现在可空（未激活账号还没设密码）。
        # 不加这一层，`None.encode()` 会 AttributeError，把"这个账号还没激活"
        # 变成一个 500——既泄露了账号存在，又让人以为是系统故障。
        if row["password_hash"] is None:
            return None

        if not bcrypt.checkpw(password.encode("utf-8"), row["password_hash"].encode("utf-8")):
            return None

        # ⚠️ **停用检查放在密码校验之后，是刻意的。**
        # 放在前面能省一次 bcrypt，但会让"停用账号 + 任意密码"和
        # "正常账号 + 错误密码"的响应时间出现稳定差异，等于把"这个账号被停用了"
        # （进而"这个账号存在"）变成可探测的。本函数开头的注释已经声明了
        # 不区分失败原因，这里要连时间侧信道一起守住。
        if row["disabled_at"] is not None:
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

    async def get_user_by_username(self, username: str) -> Optional[User]:
        """按用户名取用户。批量导入的更新路径要用它把 username 换成 user_id。

        跟 `authenticate` 分开：那个要拿密码哈希做校验，这个只读身份，
        不该把 password_hash 拉出来。
        """
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT id, username, allowed_collections, role, created_at,
                          disabled_at, activated_at
                   FROM users WHERE username = $1""",
                username,
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

    # ------------------------------------------------------------------
    # 账号生命周期（docs/account_lifecycle_design.md §4.1b §4.2 §4.4）
    # ------------------------------------------------------------------

    async def create_pending_user(
        self,
        username: str,
        activation_code_hash: str,
        activation_expires_at: float,
        allowed_collections: Optional[List[str]] = None,
        role: str = ROLE_USER,
        org_id: Optional[str] = None,
    ) -> User:
        """建一个**还没有密码**的账号，等员工凭激活码自己设。

        跟 `create_user` 并存而不是给它加参数：两者的不变量不同——
        `create_user` 出来的账号立刻能登录，这个出来的账号 `password_hash IS NULL`
        且必须先激活。混在一个函数里，"到底建出来的是哪一种"就取决于调用点传了
        什么，读代码的人得回头看参数才知道。

        明文激活码**不经过这个函数** —— 调用方拿 `activation.issue_activation()`
        的第二个返回值传进来，明文只在管理端展示一次。
        """
        if role not in VALID_ROLES:
            raise ValueError(f"Invalid role: {role!r}, must be one of {VALID_ROLES}")

        pool = await self._get_pool()
        user_id = str(uuid.uuid4())
        now = time.time()
        async with pool.acquire() as conn:
            try:
                await conn.execute(
                    """INSERT INTO users
                       (id, username, password_hash, allowed_collections, role, created_at,
                        activation_code_hash, activation_expires_at, org_id)
                       VALUES ($1, $2, NULL, $3, $4, $5, $6, $7, $8)""",
                    user_id, username, allowed_collections or [], role, now,
                    activation_code_hash, activation_expires_at, org_id,
                )
            except asyncpg.UniqueViolationError as exc:
                raise ValueError(f"用户名已存在: {username}") from exc

        return User(user_id, username, list(allowed_collections or []), role, now)

    async def get_activation_state(self, username: str) -> Optional[Dict[str, Any]]:
        """读一次激活尝试要判定的全部字段。

        判定本身在 `activation.check_activation()` 里（纯函数，可单测），
        这里只负责取数——两者刻意分开，见 `activation.py` 的模块文档。
        """
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT id, activation_code_hash, activation_expires_at,
                          activated_at, disabled_at
                   FROM users WHERE username = $1""",
                username,
            )
        return dict(row) if row else None

    async def complete_activation(self, user_id: str, new_password: str) -> bool:
        """激活成功：设密码、记激活时间、**清掉激活码**。

        ⚠️ **清码（`activation_code_hash = NULL`）和写 `activated_at` 必须在
        同一条 UPDATE 里。** 分两条写的话，中间崩一次就会留下一个"已激活但码还
        在"的账号；而 `check_activation` 判的是 `activated_at`，那种状态下码
        虽然事实上作废了，却仍然明文躺在库里没有任何理由。

        `WHERE activated_at IS NULL` 让这条语句本身就是单次使用的最后一道闸：
        两个请求同时拿着同一个码打进来，只有一条能改到行，另一条 UPDATE 0 行
        返回 False。**不能只靠上层先读后判**——那是典型的 TOCTOU。
        """
        pool = await self._get_pool()
        pw_hash = bcrypt.hashpw(new_password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
        async with pool.acquire() as conn:
            result = await conn.execute(
                """UPDATE users
                   SET password_hash = $1, activated_at = $2,
                       activation_code_hash = NULL, activation_expires_at = NULL
                   WHERE id = $3 AND activated_at IS NULL""",
                pw_hash, time.time(), user_id,
            )
        return result.split()[-1] != "0"

    async def set_disabled(self, user_id: str, disabled: bool) -> bool:
        """停用 / 重新启用。

        ⚠️ 生效时机是不对称的，见 `CLAUDE.md` §3.2：走 `require_role` 的管理端
        端点会实时查库所以立刻生效；只走 `get_current_user` 的端点（问答等）
        不查库，已签发的 token 最长还能再用 24 小时。**新的登录会立刻被拒**
        （`authenticate` 里挡着），所以窗口是有界的，不会因为对方反复登录而延长。
        """
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE users SET disabled_at = $1 WHERE id = $2",
                (time.time() if disabled else None), user_id,
            )
        return result.split()[-1] != "0"

    async def count_active_users(self, org_id: str) -> int:
        """席位占用口径：**只数没被停用的**（docs/account_lifecycle_design.md §4.4）。

        停用的人不占席位——如果占，客户为了腾名额就会去删除离职员工，
        而改停用不改删除的全部理由正是"删除会破坏审计追溯"。
        """
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT COUNT(*) FROM users WHERE org_id = $1 AND disabled_at IS NULL",
                org_id,
            ) or 0

    async def get_org_ids_for_usernames(self, usernames: List[str]) -> Dict[str, str]:
        """`username -> org_id`，**全平台范围**，批量导入判归属用。

        ⚠️ **必须是全平台而不是本企业。** `username` 是全局 UNIQUE（本表建表语句），
        所以"这个名字在本企业查不到"不等于"可以建"——它可能属于另一家企业。
        只查本企业的话，`account_import._classify_existing` 会把别家的用户判成
        CREATE，然后在 INSERT 时撞 UNIQUE 约束报一个看不懂的数据库错误；
        更糟的是如果哪天改成 upsert，就直接变成跨企业账号接管。
        """
        if not usernames:
            return {}
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT username, org_id FROM users WHERE username = ANY($1::text[])",
                usernames,
            )
        return {r["username"]: r["org_id"] for r in rows if r["org_id"] is not None}

    async def is_disabled(self, user_id: str) -> bool:
        """给 `auth.py` 的角色守卫用：这个用户是不是已被停用。

        单独一个方法而不是复用 `get_user_by_id`，是因为守卫每次请求都会调，
        只需要一个布尔值，没必要把整行拉回来再拼 dataclass。
        """
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT disabled_at FROM users WHERE id = $1", user_id)
        return bool(row and row["disabled_at"] is not None)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            type(self)._pool = None
