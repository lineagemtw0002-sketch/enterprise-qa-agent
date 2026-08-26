"""
角色存储 (Role Store) — PostgreSQL 版

职责：
1. 角色主数据：新增/改名(仅 display_name)/删除角色
2. 用户 <-> 角色关联（多对多存储，业务层限定"一人一角色"，见下方说明）
3. 角色 <-> 知识库（collection）关联：一个角色直接携带它能访问哪些知识库

2026-08-23 起角色和知识库分组重新合并成一套（此前 2026-08-22 短暂拆成两套
独立系统，见本文件 git 历史）——用户反馈"身份"和"角色"对企业管理员来说是
同一个概念："每个用户拥有自己的角色，每个角色会拥有知识库的权限；特殊情况：
运营商（平台 super_admin/admin）的角色没有知识库权限"。这正是角色系统最早
的设计（role.md），中途拆分是过度设计，现在改回来。

角色分两类，用 `org_id` 是否为空区分：
- 全局角色（`org_id IS NULL`）：固定只有两个内置系统角色（super_admin/
  org_admin），`is_system=True`，平台管理员只能改 display_name，不能新建/
  删除——`app.py::admin_create_role` 已经去掉了"建全局角色"这个分支，只接受
  企业角色。系统角色不带知识库权限——这是"运营商的角色没有知识库权限"这条
  业务规则的自然结果：`role_collections` 从来不会给 super_admin 挂关联，
  压根不提供入口。

  全局角色曾经还设想过第三种用法："建一个跨企业共用的部门身份"（比如
  "IT部"），让不同企业的员工都挂到同一个角色上、知识库权限各企业分别配置
  （`role_collections` 按 `org_id` 隔离存储就是为这个场景设计的），工作流
  模板挂这个全局角色当审批人，实现"角色名字跨企业统一"。但 2026-08-23
  工作流审批人分配改成按企业独立配置（`workflow_approver_roles` 表按
  `(workflow_type, org_id)` 存，`app.py` 的审批人设置端点显式要求
  `approver_role.org_id == org.org_id`，全局角色永远过不了这道校验），这个
  用法没有别的消费方，"新建全局角色"的入口已经跟着去掉——现在全局角色是
  两个写死的系统身份，不是一个可扩展的角色类别。

  2026-08-24 起运营方（平台侧）只保留 super_admin/org_admin 两个系统角色——
  用户确认"运营方这边的角色，就留下超级管理员和企业管理员"，原来的 admin
  （平台"管理员"次级档位）和 user（"普通用户"基础身份）两个系统角色已彻底
  下线：不再seed、不再是任何权限档位的一部分。历史上持有这两个角色的账号
  已迁移（原 admin 持有者升级为 super_admin；原 user 持有者摘除该角色，
  0 个角色跟持有 user 在权限上本就等价——user 角色从未关联任何
  role_collections）。企业内部自建的角色（下面第二类）不受影响。
- 企业角色（`org_id` 非空）：某家企业的管理员自己建的角色（如"产品需求库
  权限"），只在这家企业内可见/可分配，是原来 kb_group 的直接延续——企业
  管理员对自己的角色有完整的建/改名/删/配置知识库/分配给员工的能力。

知识库权限直接挂在角色上（`role_collections`），按 `org_id` 隔离——这一层
是为企业角色设计的：同一家企业管理员建的角色，只在自己企业内配置知识库，
不影响其他企业。

一人一角色（当前业务规则，2026-08-23 与用户确认"暂时一个用户就一个角色"）：
`user_roles` 表结构仍是多对多（不排除以后放开成多角色），但所有写路径
（`assign_user_roles`）目前只接受 0 或 1 个 role_id，上层 API（app.py）会拒绝
更多。
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import List, Optional

import asyncpg

from src.ragent_backend.db_pool import get_shared_pool

ROLE_SUPER_ADMIN = "super_admin"
ROLE_ORG_ADMIN = "org_admin"
SYSTEM_ROLE_SEEDS = (
    (ROLE_SUPER_ADMIN, "超级管理员"),
    (ROLE_ORG_ADMIN, "企业管理员"),
)
_WILDCARD = "*"


@dataclass(frozen=True)
class Role:
    role_id: str
    name: str
    display_name: str
    is_system: bool
    org_id: Optional[str]
    created_at: float


@dataclass(frozen=True)
class RoleWithCollections(Role):
    collection_names: List[str] = field(default_factory=list)


class RoleStore:
    """角色存储 (PostgreSQL)。"""

    # 类级别共享连接池，调用方经常每次都 new 一个新实例（如 auth.py 的
    # require_role 每次请求都 `RoleStore()`），池必须挂在类属性上才不会被
    # 重复创建、打满 Postgres 连接数。
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
                CREATE TABLE IF NOT EXISTS roles (
                    id            TEXT PRIMARY KEY,
                    name          VARCHAR(64) NOT NULL,
                    display_name  VARCHAR(128) NOT NULL,
                    is_system     BOOLEAN NOT NULL DEFAULT FALSE,
                    created_at    DOUBLE PRECISION NOT NULL
                )
                """
            )
            # 2026-08-23：角色和知识库分组合并回一套，roles 表可能是改造前就
            # 建好的旧表（早于这次改动），需要补一个 org_id 列——ALTER TABLE
            # ADD COLUMN IF NOT EXISTS 对已经有这列的库是安全的空操作，同一
            # 套写法在 user_store.py/org_store.py 已经用过多次。旧表上还挂着
            # 一个 `roles_name_key`（全局 UNIQUE(name)）——企业角色允许跨企业
            # 同名（不同企业各自的 org_id 区分），必须先把这个约束丢掉，改用
            # 下面两条分段的 partial unique index 兜底，不然企业管理员建同名
            # 角色会直接撞旧约束报错。
            await conn.execute("ALTER TABLE roles ADD COLUMN IF NOT EXISTS org_id TEXT REFERENCES organizations(id) ON DELETE CASCADE")
            await conn.execute("ALTER TABLE roles DROP CONSTRAINT IF EXISTS roles_name_key")
            # name 的唯一性分两段：全局角色（org_id IS NULL）互相之间要唯一，
            # 企业角色在各自企业内要唯一，但允许不同企业各自起同名角色（跟
            # 全局角色重名的极端情况理论上也允许，两边语义上不是同一个角色，
            # 分组过滤时靠 org_id 区分，不靠 name 唯一性兜底）。
            await conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS roles_name_global_uniq ON roles(name) WHERE org_id IS NULL"
            )
            await conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS roles_org_name_uniq ON roles(org_id, name) WHERE org_id IS NOT NULL"
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
            # 角色 <-> 知识库关联，按 org_id 隔离（见文件顶部说明）；同一个
            # role_id 可以在不同 org_id 下有各自独立的一份关联。
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS role_collections (
                    role_id         TEXT NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
                    org_id          TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
                    collection_name TEXT NOT NULL,
                    PRIMARY KEY (role_id, org_id, collection_name)
                )
                """
            )
            for name, display_name in SYSTEM_ROLE_SEEDS:
                await conn.execute(
                    """
                    INSERT INTO roles (id, name, display_name, is_system, org_id, created_at)
                    VALUES ($1, $2, $3, TRUE, NULL, $4)
                    ON CONFLICT (name) WHERE org_id IS NULL DO NOTHING
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
            org_id=row["org_id"],
            created_at=row["created_at"],
        )

    # ------------------------------------------------------------------
    # 角色 CRUD
    # ------------------------------------------------------------------

    async def create_role(self, org_id: Optional[str], name: str, display_name: str) -> Role:
        """`org_id=None` 建全局角色（平台管理员用）；非空建企业自己的角色（企业
        管理员用，等价于原来的 create_kb_group）。"""
        pool = await self._get_pool()
        role_id = str(uuid.uuid4())
        now = time.time()
        async with pool.acquire() as conn:
            try:
                await conn.execute(
                    """INSERT INTO roles (id, name, display_name, is_system, org_id, created_at)
                       VALUES ($1, $2, $3, FALSE, $4, $5)""",
                    role_id, name, display_name, org_id, now,
                )
            except asyncpg.UniqueViolationError as e:
                raise ValueError(f"角色标识 '{name}' 已存在") from e
        return Role(role_id=role_id, name=name, display_name=display_name, is_system=False, org_id=org_id, created_at=now)

    async def list_roles(self) -> List[Role]:
        """只返回全局角色——供平台「角色管理」页面用。"""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, name, display_name, is_system, org_id, created_at FROM roles "
                "WHERE org_id IS NULL ORDER BY created_at ASC"
            )
        return [self._row_to_role(row) for row in rows]

    async def list_roles_for_org(self, org_id: str) -> List[RoleWithCollections]:
        """企业管理员视角下能看到的角色：全局角色（部门身份/系统身份）+ 自己
        企业建的角色，附带每个角色在这家企业下配置的知识库关联。供「用户管理」
        分配下拉框、「角色管理」列表共用。"""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            role_rows = await conn.fetch(
                "SELECT id, name, display_name, is_system, org_id, created_at FROM roles "
                "WHERE org_id IS NULL OR org_id = $1 ORDER BY created_at ASC",
                org_id,
            )
            collection_rows = await conn.fetch(
                "SELECT role_id, collection_name FROM role_collections WHERE org_id = $1",
                org_id,
            )
        collections_by_role: dict[str, List[str]] = {}
        for row in collection_rows:
            collections_by_role.setdefault(row["role_id"], []).append(row["collection_name"])
        return [
            RoleWithCollections(
                role_id=row["id"],
                name=row["name"],
                display_name=row["display_name"],
                is_system=row["is_system"],
                org_id=row["org_id"],
                created_at=row["created_at"],
                collection_names=sorted(collections_by_role.get(row["id"], [])),
            )
            for row in role_rows
        ]

    async def get_role_by_id(self, role_id: str) -> Optional[Role]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, name, display_name, is_system, org_id, created_at FROM roles WHERE id = $1", role_id,
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
    # 角色 <-> 知识库关联（按 org_id 隔离，整体替换）
    # ------------------------------------------------------------------

    async def set_role_collections(self, role_id: str, org_id: str, collection_names: List[str]) -> None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "DELETE FROM role_collections WHERE role_id = $1 AND org_id = $2", role_id, org_id,
                )
                if collection_names:
                    await conn.executemany(
                        "INSERT INTO role_collections (role_id, org_id, collection_name) VALUES ($1, $2, $3)",
                        [(role_id, org_id, name) for name in collection_names],
                    )

    async def remove_collection_everywhere(self, collection_name: str) -> None:
        """一个知识库被删除时，要把它从所有角色的关联里摘掉——`collection_name`
        在 `role_collections` 里是裸字符串，不是外键，删知识库不会自动级联清掉
        这些引用，不摘的话角色上会留着一个指向不存在知识库的"僵尸关联"。调用方
        （app.py `admin_delete_collection`）在物理清空知识库数据之后调用这个
        方法。"""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM role_collections WHERE collection_name = $1", collection_name)

    # ------------------------------------------------------------------
    # 用户 <-> 角色关联（整体替换）
    # ------------------------------------------------------------------

    async def add_user_role(self, user_id: str, role_id: str) -> None:
        """增量关联（幂等）：给用户追加一个角色，已存在则忽略。供迁移脚本使用。"""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO user_roles (user_id, role_id) VALUES ($1, $2)
                   ON CONFLICT (user_id, role_id) DO NOTHING""",
                user_id, role_id,
            )

    async def get_role_by_name(self, name: str, org_id: Optional[str] = None) -> Optional[Role]:
        """`org_id=None` 查全局角色；非空查该企业自己的角色。"""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            if org_id is None:
                row = await conn.fetchrow(
                    "SELECT id, name, display_name, is_system, org_id, created_at FROM roles "
                    "WHERE name = $1 AND org_id IS NULL",
                    name,
                )
            else:
                row = await conn.fetchrow(
                    "SELECT id, name, display_name, is_system, org_id, created_at FROM roles "
                    "WHERE name = $1 AND org_id = $2",
                    name, org_id,
                )
        return self._row_to_role(row) if row else None

    async def get_or_create_role_by_name(
        self, name: str, display_name: str, is_system: bool = False, org_id: Optional[str] = None,
    ) -> Role:
        """幂等版 create_role：已存在则直接返回，不存在才建。供迁移脚本使用。"""
        existing = await self.get_role_by_name(name, org_id=org_id)
        if existing is not None:
            return existing

        pool = await self._get_pool()
        role_id = str(uuid.uuid4())
        now = time.time()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO roles (id, name, display_name, is_system, org_id, created_at)
                   VALUES ($1, $2, $3, $4, $5, $6)
                   ON CONFLICT DO NOTHING""",
                role_id, name, display_name, is_system, org_id, now,
            )
        role = await self.get_role_by_name(name, org_id=org_id)
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

    async def get_user_roles_batch(self, user_ids: List[str]) -> "dict[str, List[Role]]":
        """`get_user_roles` 的批量版——1 次查询覆盖任意多用户，不是 N 次。

        2026-08-26 P1-14 修复：管理端 `/admin/users` 原来对每个用户单独调
        `get_user_roles`，是"50 用户约 300 次串行查询"里的一部分。
        """
        result: "dict[str, List[Role]]" = {uid: [] for uid in user_ids}
        if not user_ids:
            return result
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT ur.user_id, r.id, r.name, r.display_name, r.is_system, r.org_id, r.created_at
                FROM roles r JOIN user_roles ur ON ur.role_id = r.id
                WHERE ur.user_id = ANY($1::text[])
                ORDER BY r.created_at ASC
                """,
                user_ids,
            )
        for row in rows:
            result.setdefault(row["user_id"], []).append(self._row_to_role(row))
        return result

    async def get_allowed_collections_for_users_batch(self, user_ids: List[str]) -> "dict[str, List[str]]":
        """`get_allowed_collections_for_user` 的批量版——3 次查询覆盖任意多
        用户，不是 3×N 次。`(org_id, role_id)` 双重过滤跟单用户版保持完全
        一致的语义（不是简化成只按 `role_id` 过滤——即使 role_id 本身已经
        全局唯一，这里也不改变原有的过滤条件组合，降低行为漂移风险）。

        2026-08-26 P1-14 修复：这是"50 用户约 300 次串行查询"里占比最大的
        一块——单用户版本本身就是 3 次查询，原来是 3×N。
        """
        result: "dict[str, List[str]]" = {uid: [] for uid in user_ids}
        if not user_ids:
            return result
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            user_rows = await conn.fetch(
                "SELECT id, org_id FROM users WHERE id = ANY($1::text[])", user_ids,
            )
            user_org = {r["id"]: r["org_id"] for r in user_rows}

            role_rows = await conn.fetch(
                """
                SELECT ur.user_id, r.id AS role_id, r.name AS role_name
                FROM roles r JOIN user_roles ur ON ur.role_id = r.id
                WHERE ur.user_id = ANY($1::text[])
                """,
                user_ids,
            )
            user_roles: "dict[str, list]" = {}
            for r in role_rows:
                user_roles.setdefault(r["user_id"], []).append({"id": r["role_id"], "name": r["role_name"]})

            all_role_ids = list({r["role_id"] for r in role_rows})
            collection_rows = []
            if all_role_ids:
                collection_rows = await conn.fetch(
                    "SELECT org_id, role_id, collection_name FROM role_collections WHERE role_id = ANY($1::text[])",
                    all_role_ids,
                )

        by_org_role: "dict[tuple, list]" = {}
        for row in collection_rows:
            by_org_role.setdefault((row["org_id"], row["role_id"]), []).append(row["collection_name"])

        for uid in user_ids:
            org_id = user_org.get(uid)
            roles = user_roles.get(uid, [])
            role_names = {r["name"] for r in roles}
            if ROLE_ORG_ADMIN in role_names:
                result[uid] = [_WILDCARD]
                continue
            if not roles or org_id is None:
                result[uid] = []
                continue
            names: set = set()
            for r in roles:
                names.update(by_org_role.get((org_id, r["id"]), []))
            result[uid] = [_WILDCARD] if _WILDCARD in names else sorted(names)
        return result

    async def get_user_roles(self, user_id: str) -> List[Role]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT r.id, r.name, r.display_name, r.is_system, r.org_id, r.created_at
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
        """并集去重；任一角色关联了通配符 "*"，直接返回 ["*"]。

        持有 org_admin 角色的用户视为通配符——"企业管理员=企业内全部知识库"，
        不看具体持有哪个角色配了什么，最终会在 query_knowledge_hub.py 里收窄成
        "这家企业自己名下的 collection"，不会导致跨企业越权。运营商角色
        （super_admin）从不出现在 role_collections 里（没有入口配置），
        自然得到空列表——这就是"运营商的角色没有知识库权限"这条业务规则的
        落地方式，不需要额外的特判代码。
        """
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            user_row = await conn.fetchrow("SELECT org_id FROM users WHERE id = $1", user_id)
            org_id = user_row["org_id"] if user_row else None

            role_rows = await conn.fetch(
                "SELECT r.id, r.name FROM roles r JOIN user_roles ur ON ur.role_id = r.id WHERE ur.user_id = $1",
                user_id,
            )
            role_ids = [r["id"] for r in role_rows]
            role_names = {r["name"] for r in role_rows}

            if ROLE_ORG_ADMIN in role_names:
                return [_WILDCARD]
            if not role_ids or org_id is None:
                return []

            rows = await conn.fetch(
                """
                SELECT DISTINCT collection_name FROM role_collections
                WHERE org_id = $1 AND role_id = ANY($2::text[])
                """,
                org_id, role_ids,
            )

        collection_names = [row["collection_name"] for row in rows]
        if _WILDCARD in collection_names:
            return [_WILDCARD]
        return collection_names

    async def close(self) -> None:
        # 池现在是跨 14 个 Store 共享的（db_pool.py，P1-2），这里只清掉
        # 本 Store 持有的引用，不触发真实关闭——那会把其它 Store 正在用的
        # 连接一起关掉。真正关闭见 db_pool.close_shared_pools()，只在 app
        # 关闭时调一次。
        type(self)._pool = None
