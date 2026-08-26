"""
智能运维模块存储 (Ops Store) — PostgreSQL 版

设计见 `docs/aiops_module_design.md`。**这是 V1 的第一个实施阶段**：数据模型 +
连接器注册 + 修复范围白名单 + 审批状态机的存储层，不含 BYOC 连接器的 WebSocket
协议、联邦查询、AI 分析、LangGraph 接入——那些是后续阶段，见 `CLAUDE.md` §5
该条"什么没做"。

职责：
1. `ops_system_connections`：企业接入的运维系统连接器元数据。**不含任何凭证
   字段**——这是刻意的架构约束，不是遗漏（§3.2 BYOC 原则：平台永远不持有
   客户系统凭证）。
2. `ops_remediation_scopes`：每个连接器针对四类动作类型分别登记的取值范围
   约束，配合 `aiops_scope.py` 的纯函数在生成审批记录前做越界拦截。
3. `remediation_actions`：审批工作流核心表，状态机见 `_STATUS_TRANSITIONS`。
4. `organizations.aiops_module_enabled`：模块按企业分级开通的开关，只有
   `super_admin` 能切换（权限检查在 app.py 端点层，这里只存布尔值）。

不负责（本阶段之外）：
- 凭证/心跳的实时会话管理（WebSocket 协议，§10.1）
- 联邦查询层、AI 分析（§3.5）
- `role_ops_systems` 权限位的 CRUD、`ops_analysis_summaries` 的 CRUD——
  表已经建了（schema 在下面 `_ensure_schema`），但存储方法本阶段没有实现，
  等接入权限模型 / AI 分析那两个阶段再补，避免现在写出没有调用方的方法。
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import asyncpg

from src.ragent_backend.db_pool import get_shared_pool

# 状态机（docs/aiops_module_design.md §3.3）：
#   proposed -> pending_approval -> approved -> executing -> completed
#                                 -> rejected                -> failed -> rolled_back
#   proposed -> rejected_pre（越界，未进入审批）
#   pending_approval -> expired（超时无人处理）
STATUS_PROPOSED = "proposed"
STATUS_REJECTED_PRE = "rejected_pre"
STATUS_PENDING_APPROVAL = "pending_approval"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_EXPIRED = "expired"
STATUS_EXECUTING = "executing"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_ROLLED_BACK = "rolled_back"

# 每个状态允许转移到的下一状态集合——`_assert_transition_allowed` 用它做
# 硬校验。§3.3 的"硬性不变量"要求 executing 只能从 approved 转移而来，
# 这张表就是那条不变量的机器可读版本，不允许任何调用方绕过。
_STATUS_TRANSITIONS: Dict[str, frozenset] = {
    STATUS_PROPOSED: frozenset({STATUS_PENDING_APPROVAL, STATUS_REJECTED_PRE}),
    STATUS_PENDING_APPROVAL: frozenset({STATUS_APPROVED, STATUS_REJECTED, STATUS_EXPIRED}),
    STATUS_APPROVED: frozenset({STATUS_EXECUTING}),
    STATUS_EXECUTING: frozenset({STATUS_COMPLETED, STATUS_FAILED}),
    STATUS_FAILED: frozenset({STATUS_ROLLED_BACK}),
    STATUS_REJECTED_PRE: frozenset(),
    STATUS_REJECTED: frozenset(),
    STATUS_EXPIRED: frozenset(),
    STATUS_COMPLETED: frozenset(),
    STATUS_ROLLED_BACK: frozenset(),
}


class IllegalStatusTransition(ValueError):
    """状态机不允许的转移——不接受任何调试开关绕过，见 §3.3 的硬性不变量。"""


def _assert_transition_allowed(current: str, target: str) -> None:
    allowed = _STATUS_TRANSITIONS.get(current, frozenset())
    if target not in allowed:
        raise IllegalStatusTransition(
            f"不允许从 '{current}' 转移到 '{target}'（允许的下一状态：{sorted(allowed)}）"
        )


@dataclass(frozen=True)
class OpsSystemConnection:
    connection_id: str
    org_id: str
    name: str
    system_type: str
    connector_status: str  # "online" / "offline"，由心跳更新，不是持久意图
    last_heartbeat_at: Optional[float]
    created_by: str
    approval_timeout_minutes: int
    created_at: float


@dataclass(frozen=True)
class OpsRemediationScope:
    scope_id: str
    org_id: str
    connection_id: str
    action_type: str
    scope_config: Dict[str, Any]
    configured_by: str
    updated_at: float


@dataclass(frozen=True)
class RemediationAction:
    action_id: str
    org_id: str
    connection_id: str
    proposed_by: str
    intent: str
    plan: Dict[str, Any]
    impact_radius: Optional[str]
    status: str
    approver_user_id: Optional[str]
    approved_at: Optional[float]
    executed_at: Optional[float]
    result: Optional[Dict[str, Any]]
    rollback_plan: Optional[Dict[str, Any]]
    outcome_effective: Optional[bool]
    created_at: float


class OpsStore:
    """智能运维模块存储 (PostgreSQL)。"""

    # 类级别共享连接池，见 store.py 同名字段的注释。P1-2 之后走共享池
    # （db_pool.py），不再各自 create_pool。
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
            # 模块开通开关（§4.1）——挂在 organizations 表上，但刻意不在
            # org_store.py 里加：那是另一块职责边界（org_store 自己的文档已经
            #划过这条线："组织专属的外部数据源接入是另一块设计，这里不涉及"）。
            await conn.execute(
                "ALTER TABLE organizations ADD COLUMN IF NOT EXISTS "
                "aiops_module_enabled BOOLEAN NOT NULL DEFAULT FALSE"
            )

            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ops_system_connections (
                    id                       TEXT PRIMARY KEY,
                    org_id                   TEXT NOT NULL REFERENCES organizations(id),
                    name                     VARCHAR(128) NOT NULL,
                    system_type              VARCHAR(64) NOT NULL,
                    connector_status         VARCHAR(16) NOT NULL DEFAULT 'offline',
                    last_heartbeat_at        DOUBLE PRECISION,
                    created_by               TEXT NOT NULL,
                    approval_timeout_minutes INTEGER NOT NULL DEFAULT 30,
                    created_at               DOUBLE PRECISION NOT NULL
                )
                """
                # 刻意不含任何凭证字段（§3.2 BYOC 原则）。
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ops_conn_org ON ops_system_connections(org_id)"
            )

            # 连接器注册握手用的一次性 token（§10.1）——只存哈希，一个连接器
            # 同一时刻只有一个有效的 register_token（生成新的会顶掉旧的，见
            # set_register_token 用 UPSERT），跟激活码"单次使用"的模式一致，
            # 只是这里的"作废"是覆盖而不是标记 used，因为握手成功后这个
            # token 就该失效，没有必要保留历史。
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ops_connector_register_tokens (
                    connection_id TEXT PRIMARY KEY REFERENCES ops_system_connections(id),
                    token_hash    TEXT NOT NULL,
                    expires_at    DOUBLE PRECISION NOT NULL,
                    used_at       DOUBLE PRECISION
                )
                """
            )

            # refresh_token 需要保留"已消费"历史才能做重放检测（§10.1：
            # "如果检测到一个已消费的 refresh_token 被重复使用，视为泄露信号"）
            # ——这是它不能像 register_token 那样直接覆盖、必须用独立表存
            # 每一代 token 的原因。
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ops_connector_refresh_tokens (
                    id            TEXT PRIMARY KEY,
                    connection_id TEXT NOT NULL REFERENCES ops_system_connections(id),
                    token_hash    TEXT NOT NULL,
                    issued_at     DOUBLE PRECISION NOT NULL,
                    consumed_at   DOUBLE PRECISION,
                    expires_at    DOUBLE PRECISION NOT NULL
                )
                """
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ops_refresh_conn ON ops_connector_refresh_tokens(connection_id)"
            )

            # role_ops_systems（§4 权限模型扩展）：表已建，CRUD 留给权限接入阶段。
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS role_ops_systems (
                    role_id       TEXT NOT NULL,
                    connection_id TEXT NOT NULL REFERENCES ops_system_connections(id),
                    can_view      BOOLEAN NOT NULL DEFAULT FALSE,
                    can_approve   BOOLEAN NOT NULL DEFAULT FALSE,
                    PRIMARY KEY (role_id, connection_id)
                )
                """
            )

            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ops_remediation_scopes (
                    id             TEXT PRIMARY KEY,
                    org_id         TEXT NOT NULL REFERENCES organizations(id),
                    connection_id  TEXT NOT NULL REFERENCES ops_system_connections(id),
                    action_type    VARCHAR(32) NOT NULL,
                    scope_config   JSONB NOT NULL,
                    configured_by  TEXT NOT NULL,
                    updated_at     DOUBLE PRECISION NOT NULL,
                    UNIQUE (connection_id, action_type)
                )
                """
            )

            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS remediation_actions (
                    id                 TEXT PRIMARY KEY,
                    org_id             TEXT NOT NULL REFERENCES organizations(id),
                    connection_id      TEXT NOT NULL REFERENCES ops_system_connections(id),
                    proposed_by        TEXT NOT NULL,
                    intent             TEXT NOT NULL,
                    plan               JSONB NOT NULL,
                    impact_radius      TEXT,
                    status             VARCHAR(24) NOT NULL,
                    approver_user_id   TEXT,
                    approved_at        DOUBLE PRECISION,
                    executed_at        DOUBLE PRECISION,
                    result             JSONB,
                    rollback_plan      JSONB,
                    outcome_effective  BOOLEAN,
                    created_at         DOUBLE PRECISION NOT NULL
                )
                """
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_remediation_org_status "
                "ON remediation_actions(org_id, status)"
            )

            # ops_analysis_summaries（§3.1）：表已建，CRUD 留给 AI 分析阶段。
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ops_analysis_summaries (
                    id                TEXT PRIMARY KEY,
                    org_id            TEXT NOT NULL REFERENCES organizations(id),
                    connection_id     TEXT NOT NULL REFERENCES ops_system_connections(id),
                    summary           TEXT NOT NULL,
                    evidence_refs     JSONB NOT NULL,
                    created_at        DOUBLE PRECISION NOT NULL
                )
                """
                # 刻意不存原始运维数据——只存"分析结论摘要 + 依据引用"（§3.2 BYOC）。
            )

    async def close(self) -> None:
        # 池现在是跨 Store 共享的（db_pool.py，P1-2），这里只清引用，不触发
        # 真实关闭。真正关闭见 db_pool.close_shared_pools()。
        type(self)._pool = None

    # ------------------------------------------------------------------
    # 模块开通开关
    # ------------------------------------------------------------------

    async def is_module_enabled(self, org_id: str) -> bool:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT aiops_module_enabled FROM organizations WHERE id = $1", org_id,
            )
        return bool(row["aiops_module_enabled"]) if row else False

    async def set_module_enabled(self, org_id: str, enabled: bool) -> None:
        """权限检查（只有 super_admin 能调）在 app.py 端点层，这里不重复判断——
        跟其它 Store 的既有分工一致（Store 只管数据，端点管权限）。"""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE organizations SET aiops_module_enabled = $1 WHERE id = $2",
                enabled, org_id,
            )

    # ------------------------------------------------------------------
    # 连接器
    # ------------------------------------------------------------------

    def _row_to_connection(self, row) -> OpsSystemConnection:
        return OpsSystemConnection(
            connection_id=row["id"], org_id=row["org_id"], name=row["name"],
            system_type=row["system_type"], connector_status=row["connector_status"],
            last_heartbeat_at=row["last_heartbeat_at"], created_by=row["created_by"],
            approval_timeout_minutes=row["approval_timeout_minutes"], created_at=row["created_at"],
        )

    async def register_connector(
        self, org_id: str, name: str, system_type: str, created_by: str,
        approval_timeout_minutes: int = 30,
    ) -> OpsSystemConnection:
        """调用方必须先用 `aiops_scope.validate_approval_timeout_minutes` 校验
        `approval_timeout_minutes`——这里不重复校验，保持"纯函数校验、Store 只
        管落库"的既有分工（跟 account_import.py 的模式一致）。"""
        connection_id = f"opsconn_{uuid.uuid4().hex[:12]}"
        now = time.time()
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO ops_system_connections
                    (id, org_id, name, system_type, connector_status, created_by,
                     approval_timeout_minutes, created_at)
                VALUES ($1, $2, $3, $4, 'offline', $5, $6, $7)
                """,
                connection_id, org_id, name, system_type, created_by,
                approval_timeout_minutes, now,
            )
        return OpsSystemConnection(
            connection_id=connection_id, org_id=org_id, name=name, system_type=system_type,
            connector_status="offline", last_heartbeat_at=None, created_by=created_by,
            approval_timeout_minutes=approval_timeout_minutes, created_at=now,
        )

    async def get_connector(self, connection_id: str) -> Optional[OpsSystemConnection]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM ops_system_connections WHERE id = $1", connection_id,
            )
        return self._row_to_connection(row) if row else None

    async def list_connectors_for_org(self, org_id: str) -> List[OpsSystemConnection]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM ops_system_connections WHERE org_id = $1 ORDER BY created_at ASC",
                org_id,
            )
        return [self._row_to_connection(r) for r in rows]

    async def record_heartbeat(self, connection_id: str, at: Optional[float] = None) -> None:
        """§3.2 的"连接器离线/心跳超时"判断必须来自实时心跳，不能缓存假设——
        这个方法只做一件事：更新时间戳，`connector_status` 的"在线/离线"推断
        留给读路径（比较 `last_heartbeat_at` 与当前时间），不在写路径里猜。"""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE ops_system_connections SET last_heartbeat_at = $1, "
                "connector_status = 'online' WHERE id = $2",
                at if at is not None else time.time(), connection_id,
            )

    async def mark_offline(self, connection_id: str) -> None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE ops_system_connections SET connector_status = 'offline' WHERE id = $1",
                connection_id,
            )

    async def online_status(self, connection_ids: List[str]) -> Dict[str, bool]:
        """批量在线状态——一次 DB 往返覆盖任意多个 connection_id，不是按 id
        循环调。是 `ConnectorTransport.online_status` 的存储层实现，专门为
        联邦查询 fan-out 场景设计（跟 P1-14 那批 N+1 是同一个教训：调用方
        循环调用单个查询方法，会随并发扇出的连接器数量线性增加 DB 往返）。

        "在线"用 `connector_session.is_heartbeat_fresh` 现算，不直接信
        `connector_status` 这个写路径缓存的字符串——见该函数的说明。"""
        from src.ops.connector_session import is_heartbeat_fresh

        result: Dict[str, bool] = {cid: False for cid in connection_ids}
        if not connection_ids:
            return result
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, last_heartbeat_at FROM ops_system_connections WHERE id = ANY($1::text[])",
                connection_ids,
            )
        for row in rows:
            result[row["id"]] = is_heartbeat_fresh(row["last_heartbeat_at"])
        return result

    # ------------------------------------------------------------------
    # 连接器会话令牌（§10.1：register_token 一次性握手 + refresh_token 轮换）
    # 判定逻辑在 src/ops/connector_session.py（纯函数），这里只管落库/取数。
    # ------------------------------------------------------------------

    async def set_register_token(self, connection_id: str, token_hash: str, expires_at: float) -> None:
        """生成新的 register_token 会顶掉旧的（UPSERT，不保留历史）——旧的
        没握手成功就作废是刻意的，管理员重新点一次"生成"就该让上一个失效。
        """
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO ops_connector_register_tokens (connection_id, token_hash, expires_at, used_at)
                VALUES ($1, $2, $3, NULL)
                ON CONFLICT (connection_id) DO UPDATE SET
                    token_hash = EXCLUDED.token_hash,
                    expires_at = EXCLUDED.expires_at,
                    used_at = NULL
                """,
                connection_id, token_hash, expires_at,
            )

    async def get_register_token_state(self, connection_id: str) -> Optional[Dict[str, Any]]:
        """返回喂给 `connector_session.check_register_token` 的原始字段，
        不在这里做判定——判定是纯函数的职责。"""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT token_hash, expires_at, used_at FROM ops_connector_register_tokens "
                "WHERE connection_id = $1",
                connection_id,
            )
        if row is None:
            return None
        return {
            "token_hash": row["token_hash"], "expires_at": row["expires_at"],
            "used": row["used_at"] is not None,
        }

    async def mark_register_token_used(self, connection_id: str) -> None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE ops_connector_register_tokens SET used_at = $1 WHERE connection_id = $2",
                time.time(), connection_id,
            )

    async def issue_refresh_token(
        self, connection_id: str, token_hash: str, *, ttl_seconds: int
    ) -> None:
        """签发新一代 refresh_token——旧的那一代（如果这是一次轮换而不是
        首次签发）由调用方在同一次业务操作里先调 `consume_refresh_token`
        标记消费，两步不合并成一个方法，是为了让"消费旧的"和"签发新的"
        在测试里能分别验证。"""
        pool = await self._get_pool()
        now = time.time()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO ops_connector_refresh_tokens
                    (id, connection_id, token_hash, issued_at, expires_at)
                VALUES ($1, $2, $3, $4, $5)
                """,
                f"opsrt_{uuid.uuid4().hex[:16]}", connection_id, token_hash, now, now + ttl_seconds,
            )

    async def get_refresh_token_state(self, connection_id: str, token_hash: str) -> Optional[Dict[str, Any]]:
        """按 `(connection_id, token_hash)` 精确查——同一个连接器历史上会有
        多代 refresh_token（每次轮换都插一条新的），必须按哈希定位到调用方
        实际提交的那一代，不能只按 connection_id 取"最新一条"（如果取最新
        一条，用旧的已消费 token 来"刷新"会被误判成 NOT_FOUND 而不是
        REPLAYED，重放检测就失效了）。"""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT token_hash, consumed_at, expires_at FROM ops_connector_refresh_tokens "
                "WHERE connection_id = $1 AND token_hash = $2",
                connection_id, token_hash,
            )
        if row is None:
            return None
        return {
            "token_hash": row["token_hash"], "consumed_at": row["consumed_at"],
            "expires_at": row["expires_at"],
        }

    async def consume_refresh_token(self, connection_id: str, token_hash: str) -> None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE ops_connector_refresh_tokens SET consumed_at = $1 "
                "WHERE connection_id = $2 AND token_hash = $3",
                time.time(), connection_id, token_hash,
            )

    async def revoke_all_refresh_tokens(self, connection_id: str) -> None:
        """§10.1 吊销 + 重放检测触发的"强制重新注册"共用这个方法：把这个
        连接器名下**所有未消费**的 refresh_token 标记成已消费——之后任何一个
        旧 token 拿来刷新都会被判成 REPLAYED（如果是攻击者手里还有一份的话）
        或者单纯失效（如果是合法连接器但会话已被吊销），两种情况都应该逼它
        重新走一遍 register_token 握手，不需要在这里区分。"""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE ops_connector_refresh_tokens SET consumed_at = $1 "
                "WHERE connection_id = $2 AND consumed_at IS NULL",
                time.time(), connection_id,
            )

    # ------------------------------------------------------------------
    # 修复范围白名单（配合 aiops_scope.py 的纯函数使用）
    # ------------------------------------------------------------------

    def _row_to_scope(self, row) -> OpsRemediationScope:
        import json
        scope_config = row["scope_config"]
        if isinstance(scope_config, str):
            scope_config = json.loads(scope_config)
        return OpsRemediationScope(
            scope_id=row["id"], org_id=row["org_id"], connection_id=row["connection_id"],
            action_type=row["action_type"], scope_config=scope_config,
            configured_by=row["configured_by"], updated_at=row["updated_at"],
        )

    async def upsert_remediation_scope(
        self, org_id: str, connection_id: str, action_type: str,
        scope_config: Dict[str, Any], configured_by: str,
    ) -> OpsRemediationScope:
        """调用方必须先用 `aiops_scope.validate_action_type` 校验 `action_type`。
        §3.3.1："谁能配置这份白名单——收紧为 org 管理员专属权限"，权限检查在
        端点层（判据是 `ROLE_ORG_ADMIN in 用户角色集合`），这里不重复判断。"""
        import json

        scope_id = f"opsscope_{uuid.uuid4().hex[:12]}"
        now = time.time()
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO ops_remediation_scopes
                    (id, org_id, connection_id, action_type, scope_config, configured_by, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (connection_id, action_type) DO UPDATE SET
                    scope_config = EXCLUDED.scope_config,
                    configured_by = EXCLUDED.configured_by,
                    updated_at = EXCLUDED.updated_at
                RETURNING id, org_id, connection_id, action_type, scope_config, configured_by, updated_at
                """,
                scope_id, org_id, connection_id, action_type,
                json.dumps(scope_config), configured_by, now,
            )
        return self._row_to_scope(row)

    async def get_remediation_scope(
        self, connection_id: str, action_type: str
    ) -> Optional[OpsRemediationScope]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM ops_remediation_scopes WHERE connection_id = $1 AND action_type = $2",
                connection_id, action_type,
            )
        return self._row_to_scope(row) if row else None

    async def list_remediation_scopes(self, connection_id: str) -> List[OpsRemediationScope]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM ops_remediation_scopes WHERE connection_id = $1 ORDER BY action_type",
                connection_id,
            )
        return [self._row_to_scope(r) for r in rows]

    # ------------------------------------------------------------------
    # 审批工作流（remediation_actions 状态机）
    # ------------------------------------------------------------------

    def _row_to_action(self, row) -> RemediationAction:
        import json

        def _maybe_json(v):
            return json.loads(v) if isinstance(v, str) else v

        return RemediationAction(
            action_id=row["id"], org_id=row["org_id"], connection_id=row["connection_id"],
            proposed_by=row["proposed_by"], intent=row["intent"], plan=_maybe_json(row["plan"]),
            impact_radius=row["impact_radius"], status=row["status"],
            approver_user_id=row["approver_user_id"], approved_at=row["approved_at"],
            executed_at=row["executed_at"], result=_maybe_json(row["result"]),
            rollback_plan=_maybe_json(row["rollback_plan"]),
            outcome_effective=row["outcome_effective"], created_at=row["created_at"],
        )

    async def create_proposed_action(
        self, org_id: str, connection_id: str, proposed_by: str, intent: str,
        plan: Dict[str, Any], impact_radius: Optional[str] = None,
        rollback_plan: Optional[Dict[str, Any]] = None,
    ) -> RemediationAction:
        """总是从 `proposed` 状态开始——是否放行到 `pending_approval` 由调用方
        先跑 `aiops_scope.check_target_in_scope`，再调 `advance_status` 转移，
        不在这里内联判断（越界判定是纯函数，故意保持可以脱离数据库单独测试）。
        """
        import json

        action_id = f"remact_{uuid.uuid4().hex[:12]}"
        now = time.time()
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO remediation_actions
                    (id, org_id, connection_id, proposed_by, intent, plan, impact_radius,
                     status, rollback_plan, created_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                """,
                action_id, org_id, connection_id, proposed_by, intent, json.dumps(plan),
                impact_radius, STATUS_PROPOSED,
                json.dumps(rollback_plan) if rollback_plan is not None else None, now,
            )
        return RemediationAction(
            action_id=action_id, org_id=org_id, connection_id=connection_id,
            proposed_by=proposed_by, intent=intent, plan=plan, impact_radius=impact_radius,
            status=STATUS_PROPOSED, approver_user_id=None, approved_at=None, executed_at=None,
            result=None, rollback_plan=rollback_plan, outcome_effective=None, created_at=now,
        )

    async def get_action(self, action_id: str) -> Optional[RemediationAction]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM remediation_actions WHERE id = $1", action_id)
        return self._row_to_action(row) if row else None

    async def advance_status(self, action_id: str, target_status: str) -> RemediationAction:
        """状态机的通用转移入口，给不需要额外字段的转移用
        （`proposed`→`pending_approval`/`rejected_pre`，`pending_approval`→
        `rejected`/`expired`，`executing`→`completed`/`failed`，
        `failed`→`rolled_back`）。**`approved` 和 `executing` 不走这个方法**
        ——那两步各自有硬性不变量要求额外字段，见 `approve_action` /
        `mark_executing`，防止调用方漏传 `approver_user_id`/`approved_at`。
        """
        if target_status in (STATUS_APPROVED, STATUS_EXECUTING):
            raise IllegalStatusTransition(
                f"'{target_status}' 必须通过专用方法转移（approve_action / mark_executing），"
                "不能走通用的 advance_status"
            )
        current = await self.get_action(action_id)
        if current is None:
            raise ValueError(f"remediation_action '{action_id}' 不存在")
        _assert_transition_allowed(current.status, target_status)

        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE remediation_actions SET status = $1 WHERE id = $2",
                target_status, action_id,
            )
        return await self.get_action(action_id)

    async def approve_action(self, action_id: str, approver_user_id: str) -> RemediationAction:
        """§3.3 硬性不变量：`executing` 状态只能从 `approved` 转移而来，且必须
        有 `approver_user_id` + `approved_at`——这条不允许任何调试开关绕过。
        这里在写入前就校验当前状态是 `pending_approval`，不是先写后验。"""
        current = await self.get_action(action_id)
        if current is None:
            raise ValueError(f"remediation_action '{action_id}' 不存在")
        _assert_transition_allowed(current.status, STATUS_APPROVED)

        approved_at = time.time()
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE remediation_actions SET status = $1, approver_user_id = $2, "
                "approved_at = $3 WHERE id = $4",
                STATUS_APPROVED, approver_user_id, approved_at, action_id,
            )
        return await self.get_action(action_id)

    async def mark_executing(self, action_id: str) -> RemediationAction:
        """硬性前置条件：当前状态必须是 `approved`，且 `approver_user_id` +
        `approved_at` 必须已经落库——不是"转移到 executing 就顺便查一下"，
        是"没有这两个字段就不允许转移"，这条不变量在这里第二次校验
        （第一次在 `approve_action` 写入时），双重保险。"""
        current = await self.get_action(action_id)
        if current is None:
            raise ValueError(f"remediation_action '{action_id}' 不存在")
        _assert_transition_allowed(current.status, STATUS_EXECUTING)
        if current.approver_user_id is None or current.approved_at is None:
            raise IllegalStatusTransition(
                "缺少 approver_user_id/approved_at，不允许转移到 executing——"
                "这条硬性不变量不接受任何绕过"
            )

        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE remediation_actions SET status = $1, executed_at = $2 WHERE id = $3",
                STATUS_EXECUTING, time.time(), action_id,
            )
        return await self.get_action(action_id)

    async def mark_result(
        self, action_id: str, target_status: str, result: Dict[str, Any]
    ) -> RemediationAction:
        """`executing` -> `completed`/`failed`，附带执行结果。"""
        if target_status not in (STATUS_COMPLETED, STATUS_FAILED):
            raise IllegalStatusTransition(f"mark_result 只接受 completed/failed，收到 '{target_status}'")
        current = await self.get_action(action_id)
        if current is None:
            raise ValueError(f"remediation_action '{action_id}' 不存在")
        _assert_transition_allowed(current.status, target_status)

        import json

        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE remediation_actions SET status = $1, result = $2 WHERE id = $3",
                target_status, json.dumps(result), action_id,
            )
        return await self.get_action(action_id)

    async def set_outcome_effective(self, action_id: str, effective: bool) -> RemediationAction:
        """§10.5 新增字段：事后人工标注"这次修复是否真的有效"，跟状态机无关，
        可以在任何终态之后补标，不做状态限制。"""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE remediation_actions SET outcome_effective = $1 WHERE id = $2",
                effective, action_id,
            )
        result = await self.get_action(action_id)
        if result is None:
            raise ValueError(f"remediation_action '{action_id}' 不存在")
        return result

    async def list_actions_for_org(
        self, org_id: str, status: Optional[str] = None
    ) -> List[RemediationAction]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            if status is not None:
                rows = await conn.fetch(
                    "SELECT * FROM remediation_actions WHERE org_id = $1 AND status = $2 "
                    "ORDER BY created_at DESC",
                    org_id, status,
                )
            else:
                rows = await conn.fetch(
                    "SELECT * FROM remediation_actions WHERE org_id = $1 ORDER BY created_at DESC",
                    org_id,
                )
        return [self._row_to_action(r) for r in rows]

    async def list_pending_approval_older_than(self, cutoff_ts: float) -> List[RemediationAction]:
        """给超时扫描用（§3.3 的 `expired` 状态）——本阶段只提供查询方法，
        实际的定时扫描任务是后续阶段（LangGraph 接入 / 后台任务）的工作，
        见 `CLAUDE.md` §5 该条"什么没做"。"""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM remediation_actions WHERE status = $1 AND created_at < $2",
                STATUS_PENDING_APPROVAL, cutoff_ts,
            )
        return [self._row_to_action(r) for r in rows]
