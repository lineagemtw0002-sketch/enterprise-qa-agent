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
import logging
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import asyncpg

from src.ops import service_health

logger = logging.getLogger(__name__)

from src.ragent_backend.db_pool import get_shared_pool
from src.ragent_backend.role_store import ROLE_ORG_ADMIN

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
    # 可选，2026-08-27 新增（§9.2"事后复盘聚合视图"的最小可行版）：这次提议
    # 是不是由某一次 `analyze_ops_incident` 分析引出的，指向
    # `ops_analysis_summaries.id`。放在末尾且有默认值——这个 dataclass 在
    # 好几处是按位置构造的（`_row_to_action`），加个新字段插进中间会错位。
    summary_id: Optional[str] = None


@dataclass(frozen=True)
class PostmortemEntry:
    """`list_postmortems` 的一行——一条终态修复动作 + 它引用的分析摘要文本
    （没有链接时是 `None`，不是空字符串，区分"没关联"和"关联了但摘要是空
    字符串"这两件不该混淆的事）。"""
    action: RemediationAction
    linked_summary: Optional[str]


@dataclass(frozen=True)
class RoleOpsPermission:
    role_id: str
    connection_id: str
    can_view: bool
    can_approve: bool


@dataclass(frozen=True)
class AnalysisSummary:
    summary_id: str
    org_id: str
    connection_id: str
    summary: str
    evidence_refs: List[Dict[str, Any]]
    created_at: float


def _incident_row(row: Any) -> Dict[str, Any]:
    """asyncpg 的 JSONB 有时回字符串有时回 dict（取决于是否注册了编解码器），
    两种都要认——只处理其中一种会在换环境时静默变成空列表。"""
    import json

    def _j(v, default):
        if v is None:
            return default
        return json.loads(v) if isinstance(v, str) else v

    d = dict(row)
    d["targets"] = _j(d.get("targets"), [])
    d["labels"] = _j(d.get("labels"), {})
    return d


def _threshold_row(row: Any) -> Dict[str, Any]:
    """asyncpg 的 JSONB 有时回字符串有时回 dict（取决于是否注册了编解码器），
    两种都得认——只处理其中一种会在换环境时静默变成空配置。"""
    import json

    raw = row["thresholds"]
    return {
        "service": row["service"],
        "thresholds": json.loads(raw) if isinstance(raw, str) else (raw or {}),
        "updated_at": row["updated_at"] if "updated_at" in row.keys() else None,
    }


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
                CREATE TABLE IF NOT EXISTS ops_service_health_thresholds (
                    id             TEXT PRIMARY KEY,
                    org_id         TEXT NOT NULL REFERENCES organizations(id),
                    connection_id  TEXT NOT NULL REFERENCES ops_system_connections(id),
                    -- '*' 表示该连接器的默认配置，其余是具体服务名。
                    service        TEXT NOT NULL,
                    -- 可以只写其中几个字段，缺的按 `service_health.resolve_thresholds`
                    -- 逐字段回退。**不要求写全六个**——要求写全等于把没打算改的
                    -- 那几个也冻结在填写那天的值上。
                    thresholds     JSONB NOT NULL,
                    configured_by  TEXT NOT NULL,
                    updated_at     DOUBLE PRECISION NOT NULL,
                    UNIQUE (connection_id, service)
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
                # ⚠️ `summary_id` 故意不放在这张 CREATE TABLE 里，即使它逻辑上
                # 属于这张表——`remediation_actions` 在 schema 创建顺序里排在
                # `ops_analysis_summaries` 之前（见下面那张表的 CREATE 语句），
                # 这里内联一个指向还不存在的表的外键，在全新数据库上会直接
                # 建表失败。改成 ALTER TABLE，放在 `ops_analysis_summaries`
                # 建表之后执行，见下方"给 remediation_actions 补 summary_id
                # 外键列"那一段。
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

            # 给 remediation_actions 补 summary_id 外键列（2026-08-27，
            # §9.2"事后复盘聚合视图"的最小可行版）——必须放在这里（这张表建
            # 完之后），`remediation_actions` 的 CREATE TABLE 排在它前面，
            # 不能内联这个外键。`ALTER TABLE ... ADD COLUMN IF NOT EXISTS`
            # 幂等，新库/老库都安全：新库上这条其实不需要（下面 CREATE TABLE
            # 已经没有这一列，需要它才有意义），老库（本机开发库今晚已经建过
            # 表）靠这条才能真正拿到这一列。可空 + 无历史回填——早于这次改动
            # 创建的动作永远不会有这个链接，复盘视图里显示"未关联分析"，
            # 这是诚实的空，不是缺陷。
            await conn.execute(
                "ALTER TABLE remediation_actions ADD COLUMN IF NOT EXISTS "
                "summary_id TEXT REFERENCES ops_analysis_summaries(id)"
            )

            # ops_incidents：持续存在的"事件"，是告警推送去重的落点。
            # 设计见 docs/alert_push_design.md §4。
            #
            # ⚠️ **`fingerprint` 上的唯一约束是 (connection_id, fingerprint)，
            # 不是全局唯一**——两个企业、两套系统完全可能产生相同的标签组合，
            # 全局唯一会让它们互相覆盖，那是跨企业的数据串台。
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ops_incidents (
                    id             TEXT PRIMARY KEY,
                    org_id         TEXT NOT NULL REFERENCES organizations(id),
                    connection_id  TEXT NOT NULL REFERENCES ops_system_connections(id),
                    fingerprint    TEXT NOT NULL,
                    status         VARCHAR(16) NOT NULL,
                    targets        JSONB NOT NULL,
                    labels         JSONB NOT NULL,
                    alert_count    INTEGER NOT NULL DEFAULT 0,
                    -- 被从 resolved 拉回 open 的次数。越过阈值就是"反复重启"，
                    -- 那是一类独立故障，不是 N 次独立故障。
                    flap_count     INTEGER NOT NULL DEFAULT 0,
                    first_seen_at  DOUBLE PRECISION NOT NULL,
                    last_seen_at   DOUBLE PRECISION NOT NULL,
                    resolved_at    DOUBLE PRECISION,
                    summary_id     TEXT,
                    UNIQUE (connection_id, fingerprint)
                )
                """
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ops_incidents_open "
                "ON ops_incidents(org_id, status, last_seen_at DESC)"
            )
            # 分析摘要记录触发来源：auto（告警推送自动触发）/ manual（人点的）。
            # 不区分的话，时间线上"系统主动发现的"和"有人手动查的"分不出来，
            # 而这两件事在复盘时意义完全不同（设计 §4.4）。
            await conn.execute(
                "ALTER TABLE ops_analysis_summaries ADD COLUMN IF NOT EXISTS "
                "trigger_source VARCHAR(16) NOT NULL DEFAULT 'manual'"
            )
            await conn.execute(
                "ALTER TABLE ops_analysis_summaries ADD COLUMN IF NOT EXISTS "
                "triggered_by TEXT"
            )

            # ⚠️ **`role_ops_systems.role_id` 补上外键 + 级联删除。**
            #
            # 建表时它是裸 TEXT，而对照组 `role_collections.role_id` 一直有
            # `REFERENCES roles(id) ON DELETE CASCADE`。后果不是安全漏洞
            # （role_id 是 UUID4 不会复用，权限查询又是从用户当前持有的角色出发，
            # 已删角色的行永远匹配不上），而是**授权列表会返回幽灵条目**：
            # 管理员在「授权管理」里审视"谁能批准这个连接器"时看到一个已经不存在
            # 的角色，而且撤销不掉——对应的角色已经没了。
            #
            # **顺序很要紧**：先清存量孤儿行，再加约束。反过来的话，库里只要有
            # 一行孤儿，`ADD CONSTRAINT` 就会直接失败、整个 schema 初始化炸掉。
            #
            # ⚠️ 整段包在 try 里：`roles` 表由 `role_store` 建，两个 Store 的
            # schema 初始化没有先后保证。首次启动时 `roles` 可能还不存在，
            # 那次加不上约束——下次启动会再试一遍（`_ensure_schema` 每次建池都跑），
            # 所以不需要为此引入初始化顺序依赖。
            try:
                await conn.execute(
                    "DELETE FROM role_ops_systems WHERE role_id NOT IN (SELECT id FROM roles)"
                )
                await conn.execute(
                    """
                    DO $$
                    BEGIN
                        IF NOT EXISTS (
                            SELECT 1 FROM pg_constraint
                            WHERE conname = 'role_ops_systems_role_id_fkey'
                        ) THEN
                            ALTER TABLE role_ops_systems
                                ADD CONSTRAINT role_ops_systems_role_id_fkey
                                FOREIGN KEY (role_id) REFERENCES roles(id) ON DELETE CASCADE;
                        END IF;
                    END $$;
                    """
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "role_ops_systems 外键暂未添加（多半是 roles 表还没建），下次启动会重试: %s", e
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

    async def is_module_enabled_batch(self, org_ids: List[str]) -> Dict[str, bool]:
        """给企业列表页用的批量版——同一个 N+1 教训（P1-14 等一路踩过）：
        列表页逐个企业调 `is_module_enabled` 就是又一次"读同一类数据 N 次"。
        缺失的 org_id（表里没有该企业，理论上不该发生）在返回的 dict 里同样
        不出现，调用方须用 `.get(org_id, False)` 兜底，跟 `organizations.
        aiops_module_enabled` 列本身 `NOT NULL DEFAULT FALSE` 的语义一致。
        """
        if not org_ids:
            return {}
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, aiops_module_enabled FROM organizations WHERE id = ANY($1::text[])",
                list(org_ids),
            )
        return {row["id"]: bool(row["aiops_module_enabled"]) for row in rows}

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

    async def delete_connector(self, connection_id: str) -> bool:
        """硬删除，级联清掉全部挂在这个连接器下的数据。原来这个方法不存在
        ——刘德华摸底"授权管理"时想清理自己建的联调用连接器才发现登记了就
        撤不掉，只能直接动数据库。返回 `False` 表示连接器本来就不存在
        （调用方决定是否要转成 404，跟 `_get_owned_connector` 的既有约定
        一致，这里不重复判断归属）。

        ⚠️ **不检查在飞状态就删**——跟 `admin_delete_collection` 硬删知识库
        是同一个信任模型：这是管理员的显式操作，不是自动清理，管理员要为
        "删掉一个还有未执行修复动作的连接器"这个后果负责，不由代码替他
        拦下来。真要加"有 pending/approved 动作时拒绝删除"这层防护，是
        一个独立的产品决策（要不要允许强制删除、要不要级联标记那些动作
        为某种"连接器已删除"的终态），本次没有擅自拍板。"""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                # 子表没有 ON DELETE CASCADE（外键默认 RESTRICT），必须先删
                # 子表再删父表——顺序反了会撞上 ForeignKeyViolationError
                # （这里不是猜的，`scripts/verify_aiops_endpoints.py` 早前
                # 清理测试数据时真实撞过一次同样的错误，见该脚本 `_cleanup`
                # 里的注释）。
                await conn.execute(
                    "DELETE FROM role_ops_systems WHERE connection_id = $1", connection_id,
                )
                await conn.execute(
                    "DELETE FROM ops_connector_register_tokens WHERE connection_id = $1", connection_id,
                )
                await conn.execute(
                    "DELETE FROM ops_connector_refresh_tokens WHERE connection_id = $1", connection_id,
                )
                await conn.execute(
                    "DELETE FROM remediation_actions WHERE connection_id = $1", connection_id,
                )
                await conn.execute(
                    "DELETE FROM ops_remediation_scopes WHERE connection_id = $1", connection_id,
                )
                # ⚠️ **摘要可能被别的连接器的动作引用着。**
                # `remediation_actions.summary_id` 没有限定同连接器——人工提议时
                # 完全可以关联一次跨连接器的分析（同一次故障波及多个系统时这很
                # 正常）。只删本连接器的动作不足以解除引用，所以先把**所有**
                # 指向这些摘要的动作解绑，再删摘要。
                # 2026-08-27 真实撞过：删仿真连接器时报
                # `remediation_actions_summary_id_fkey` 违反。
                await conn.execute(
                    "UPDATE remediation_actions SET summary_id = NULL "
                    "WHERE summary_id IN (SELECT id FROM ops_analysis_summaries "
                    "                      WHERE connection_id = $1)",
                    connection_id,
                )
                # ops_incidents 也挂在连接器下（2026-08-27 新增）。
                await conn.execute(
                    "DELETE FROM ops_incidents WHERE connection_id = $1", connection_id,
                )
                await conn.execute(
                    "DELETE FROM ops_analysis_summaries WHERE connection_id = $1", connection_id,
                )
                result = await conn.execute(
                    "DELETE FROM ops_system_connections WHERE id = $1", connection_id,
                )
        return result != "DELETE 0"

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

    # ==================== 事件（告警推送的落点） ====================

    async def apply_alert_batch(
        self, org_id: str, connection_id: str, *, fingerprint: str,
        targets: List[str], labels: Dict[str, Any], alert_count: int, now: float,
    ) -> Dict[str, Any]:
        """把一批同指纹的告警落到事件上，返回 `{incident, decision}`。

        ⚠️ **整段在一个事务里做，且对已有行加 `FOR UPDATE` 行锁。**
        告警风暴时同一指纹的推送会并发到达，"先查后写"在并发下会产生
        **两个同指纹的事件**——之后每一批告警落到哪一个全看运气，
        计数分裂、时间线上出现两条一模一样的记录。
        `UNIQUE (connection_id, fingerprint)` 是兜底，但让它报错不如不产生冲突。

        判定逻辑本身在 `src/ops/incident_identity.decide`（纯函数，可单测）；
        这里只负责"读 → 判 → 写"这三步的原子性。
        """
        import json as _json

        from src.ops.incident_identity import STATUS_OPEN, decide

        pool = await self._get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT id, status, resolved_at, flap_count, targets, alert_count "
                    "FROM ops_incidents WHERE connection_id = $1 AND fingerprint = $2 "
                    "FOR UPDATE",
                    connection_id, fingerprint,
                )
                existing = dict(row) if row else None
                d = decide(existing, now=now)

                if d.action == "create":
                    incident_id = f"opsinc_{uuid.uuid4().hex[:12]}"
                    # ⚠️ 用 upsert 而不是纯 INSERT：`decide` 判"新建"时，行可能
                    # 已被同一批次的另一个连接抢先建出来（FOR UPDATE 锁不住
                    # 还不存在的行）。冲突时退化成更新，不让整批告警丢掉。
                    saved = await conn.fetchrow(
                        """
                        INSERT INTO ops_incidents
                            (id, org_id, connection_id, fingerprint, status, targets, labels,
                             alert_count, flap_count, first_seen_at, last_seen_at)
                        VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7::jsonb,$8,0,$9,$9)
                        ON CONFLICT (connection_id, fingerprint) DO UPDATE
                            SET status = $5, alert_count = ops_incidents.alert_count + $8,
                                last_seen_at = $9, resolved_at = NULL
                        RETURNING *
                        """,
                        incident_id, org_id, connection_id, fingerprint, STATUS_OPEN,
                        _json.dumps(targets), _json.dumps(labels), alert_count, now,
                    )
                else:
                    saved = await conn.fetchrow(
                        """
                        UPDATE ops_incidents
                           SET status = $2, alert_count = alert_count + $3,
                               last_seen_at = $4, resolved_at = NULL,
                               flap_count = $5, targets = $6::jsonb
                         WHERE id = $1
                        RETURNING *
                        """,
                        existing["id"], STATUS_OPEN, alert_count, now,
                        d.flap_count, _json.dumps(targets),
                    )
        return {"incident": _incident_row(saved), "decision": d}

    async def resolve_incident(self, incident_id: str, *, now: float) -> bool:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE ops_incidents SET status = 'resolved', resolved_at = $2 "
                "WHERE id = $1 AND status = 'open'",
                incident_id, now,
            )
        return result.split()[-1] != "0"

    async def attach_summary_to_incident(self, incident_id: str, summary_id: str) -> None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE ops_incidents SET summary_id = $2 WHERE id = $1",
                incident_id, summary_id,
            )

    async def list_incidents(self, org_id: str, *, status: Optional[str] = None,
                             connection_ids: Optional[List[str]] = None,
                             limit: int = 50) -> List[Dict[str, Any]]:
        clauses = ["org_id = $1"]
        args: List[Any] = [org_id]
        if status:
            args.append(status); clauses.append(f"status = ${len(args)}")
        if connection_ids is not None:
            args.append(connection_ids); clauses.append(f"connection_id = ANY(${len(args)}::text[])")
        args.append(limit)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT * FROM ops_incidents WHERE {' AND '.join(clauses)} "
                f"ORDER BY last_seen_at DESC LIMIT ${len(args)}",
                *args,
            )
        return [_incident_row(r) for r in rows]

    async def upsert_service_thresholds(
        self, org_id: str, connection_id: str, service: str,
        thresholds: Dict[str, Any], configured_by: str,
    ) -> Dict[str, Any]:
        """写入某个服务（或 `*` 默认）的阈值覆盖。

        调用方必须先用 `service_health.validate_thresholds` 校验——跟白名单那条
        一样，校验放在纯函数里、权限判断放在端点层，这里都不重复。
        """
        import json

        row_id = f"opsthr_{uuid.uuid4().hex[:12]}"
        now = time.time()
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO ops_service_health_thresholds
                    (id, org_id, connection_id, service, thresholds, configured_by, updated_at)
                VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7)
                ON CONFLICT (connection_id, service) DO UPDATE
                    SET thresholds = EXCLUDED.thresholds,
                        configured_by = EXCLUDED.configured_by,
                        updated_at = EXCLUDED.updated_at
                RETURNING service, thresholds, updated_at
                """,
                row_id, org_id, connection_id, service, json.dumps(thresholds), configured_by, now,
            )
        return _threshold_row(row)

    async def list_service_thresholds(self, connection_id: str) -> List[Dict[str, Any]]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT service, thresholds, updated_at FROM ops_service_health_thresholds "
                "WHERE connection_id = $1 ORDER BY service",
                connection_id,
            )
        return [_threshold_row(r) for r in rows]

    async def thresholds_by_connection(self, connection_ids: List[str]) -> Dict[str, Dict[str, Any]]:
        """一次查完多个连接器的阈值覆盖，返回 `{connection_id: {service: 覆盖}}`。

        **批量**是因为总览大屏一次要渲染这个企业名下全部连接器的服务——
        逐个连接器查一次就是 N+1，跟 P1-14 那一路的纪律一致。
        """
        if not connection_ids:
            return {}
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT connection_id, service, thresholds FROM ops_service_health_thresholds "
                "WHERE connection_id = ANY($1::text[])",
                connection_ids,
            )
        out: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            parsed = _threshold_row(r)
            out.setdefault(r["connection_id"], {})[parsed["service"]] = parsed["thresholds"]
        return out

    async def delete_service_thresholds(self, connection_id: str, service: str) -> bool:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM ops_service_health_thresholds WHERE connection_id = $1 AND service = $2",
                connection_id, service,
            )
        return result.split()[-1] != "0"

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
    # role_ops_systems（§10.6：细粒度审批权限，can_view/can_approve）
    # ------------------------------------------------------------------
    #
    # ⚠️ 范围边界（跟 role_collections 刻意保持同一套语义，不是各判各的）：
    # - `org_admin` 是通配符——本企业名下**全部**连接器自动 can_view=True、
    #   can_approve=True，不看这张表；这条不能被这张表的显式行覆盖或收窄，
    #   跟 `role_store.get_allowed_collections_for_user` 的 org_admin 通配符
    #   处理是同一条业务规则（"企业管理员=企业内全部资源"）。
    # - `super_admin`（平台运营方）从不自动获得任何连接器的权限——没有入口
    #   配置，不是遗漏，对齐"role_collections 从来不会给 super_admin 挂
    #   关联"这条既有铁律，运维审批涉及"批准执行动作"，比知识库查看的
    #   爆炸半径更大，更不该放开。
    # - **只管 can_view/can_approve，不管连接器登记/白名单配置写权限**——
    #   那两个仍然是 org_admin 专属、判定依据是 `ROLE_ORG_ADMIN in 角色集合`，
    #   不进入这张表、不可通过自定义角色委托（§10.6 明确要求，避免跟日常
    #   审批权限混淆）。

    async def get_ops_permission(self, user_id: str, connection_id: str) -> Dict[str, bool]:
        """返回 `{"can_view": bool, "can_approve": bool}`。org_admin 通配符 +
        super_admin 零权限的规则见上方类注释。连接器不存在时两者皆 False
        （由调用方决定是否要转成 404——这里不对"连接器不存在"和"没权限"
        做区分，保持这个方法只回答权限问题）。"""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            conn_row = await conn.fetchrow(
                "SELECT org_id FROM ops_system_connections WHERE id = $1", connection_id,
            )
            if conn_row is None:
                return {"can_view": False, "can_approve": False}

            user_row = await conn.fetchrow("SELECT org_id FROM users WHERE id = $1", user_id)
            user_org_id = user_row["org_id"] if user_row else None

            role_rows = await conn.fetch(
                "SELECT r.id, r.name FROM roles r JOIN user_roles ur ON ur.role_id = r.id "
                "WHERE ur.user_id = $1",
                user_id,
            )
            role_ids = [r["id"] for r in role_rows]
            role_names = {r["name"] for r in role_rows}

            if ROLE_ORG_ADMIN in role_names and user_org_id == conn_row["org_id"]:
                return {"can_view": True, "can_approve": True}

            if not role_ids:
                return {"can_view": False, "can_approve": False}

            perm_rows = await conn.fetch(
                "SELECT can_view, can_approve FROM role_ops_systems "
                "WHERE role_id = ANY($1::text[]) AND connection_id = $2",
                role_ids, connection_id,
            )
        return {
            "can_view": any(r["can_view"] for r in perm_rows),
            "can_approve": any(r["can_approve"] for r in perm_rows),
        }

    async def get_ops_permission_summary(self, user_id: str) -> Dict[str, bool]:
        """给 `/auth/me` 用的**聚合**视图——不是"某个连接器上有没有权限"，是
        "这个用户在**任意**连接器上有没有 can_view/can_approve"，前端用它
        决定导航入口显示与否，不需要遍历每个连接器再自己取并集。

        ⚠️ org_admin 通配符在这里**不要求企业名下已经有连接器**——通配符
        本身是"企业管理员=企业内全部运维系统"这条身份性质，不该因为企业
        还没注册任何连接器就变成 False，那是运营状态，不是权限声明。"""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            user_row = await conn.fetchrow("SELECT org_id FROM users WHERE id = $1", user_id)
            if user_row is None or user_row["org_id"] is None:
                return {"can_view": False, "can_approve": False}

            role_rows = await conn.fetch(
                "SELECT r.id, r.name FROM roles r JOIN user_roles ur ON ur.role_id = r.id "
                "WHERE ur.user_id = $1",
                user_id,
            )
            role_ids = [r["id"] for r in role_rows]
            role_names = {r["name"] for r in role_rows}

            if ROLE_ORG_ADMIN in role_names:
                return {"can_view": True, "can_approve": True}

            if not role_ids:
                return {"can_view": False, "can_approve": False}

            perm_rows = await conn.fetch(
                "SELECT can_view, can_approve FROM role_ops_systems WHERE role_id = ANY($1::text[])",
                role_ids,
            )
        return {
            "can_view": any(r["can_view"] for r in perm_rows),
            "can_approve": any(r["can_approve"] for r in perm_rows),
        }

    async def viewable_connection_ids_for_user(self, user_id: str, org_id: str) -> Optional[List[str]]:
        """给"列出本企业修复动作"这类端点按 can_view 过滤用。

        返回 `None` 表示**不需要过滤**（该用户是这家企业的 org_admin，
        通配符覆盖全部连接器，调用方不用再拿到一份显式列表逐条比对）；
        返回列表（可能为空）表示"只有这些 connection_id 可见"。
        """
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            user_row = await conn.fetchrow("SELECT org_id FROM users WHERE id = $1", user_id)
            user_org_id = user_row["org_id"] if user_row else None

            role_rows = await conn.fetch(
                "SELECT r.id, r.name FROM roles r JOIN user_roles ur ON ur.role_id = r.id "
                "WHERE ur.user_id = $1",
                user_id,
            )
            role_ids = [r["id"] for r in role_rows]
            role_names = {r["name"] for r in role_rows}

            if ROLE_ORG_ADMIN in role_names and user_org_id == org_id:
                return None

            if not role_ids:
                return []

            rows = await conn.fetch(
                "SELECT DISTINCT ros.connection_id FROM role_ops_systems ros "
                "JOIN ops_system_connections c ON c.id = ros.connection_id "
                "WHERE ros.role_id = ANY($1::text[]) AND ros.can_view = TRUE AND c.org_id = $2",
                role_ids, org_id,
            )
        return [r["connection_id"] for r in rows]

    async def set_role_ops_permission(
        self, role_id: str, connection_id: str, can_view: bool, can_approve: bool,
    ) -> RoleOpsPermission:
        """`can_approve=True` 隐含 `can_view=True`——审批权限比查看权限更窄
        是设计要求的方向（§10.6），反过来（能批但看不见）没有意义，这里直接
        在写入前拉齐，不指望调用方（管理员点选界面）自己保证这条不变量。"""
        if can_approve:
            can_view = True
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO role_ops_systems (role_id, connection_id, can_view, can_approve)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (role_id, connection_id) DO UPDATE SET
                    can_view = EXCLUDED.can_view, can_approve = EXCLUDED.can_approve
                """,
                role_id, connection_id, can_view, can_approve,
            )
        return RoleOpsPermission(
            role_id=role_id, connection_id=connection_id, can_view=can_view, can_approve=can_approve,
        )

    async def revoke_role_ops_permission(self, role_id: str, connection_id: str) -> None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM role_ops_systems WHERE role_id = $1 AND connection_id = $2",
                role_id, connection_id,
            )

    async def list_role_ops_permissions(self, connection_id: str) -> List[RoleOpsPermission]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT role_id, connection_id, can_view, can_approve FROM role_ops_systems "
                "WHERE connection_id = $1 ORDER BY role_id",
                connection_id,
            )
        return [
            RoleOpsPermission(
                role_id=r["role_id"], connection_id=r["connection_id"],
                can_view=r["can_view"], can_approve=r["can_approve"],
            )
            for r in rows
        ]

    # ------------------------------------------------------------------
    # AI 分析结论摘要（§3.1：只落库摘要 + 依据引用，不落库原始运维数据）
    # ------------------------------------------------------------------

    def _row_to_summary(self, row) -> AnalysisSummary:
        import json

        evidence_refs = row["evidence_refs"]
        if isinstance(evidence_refs, str):
            evidence_refs = json.loads(evidence_refs)
        return AnalysisSummary(
            summary_id=row["id"], org_id=row["org_id"], connection_id=row["connection_id"],
            summary=row["summary"], evidence_refs=evidence_refs, created_at=row["created_at"],
        )

    async def save_analysis_summary(
        self, org_id: str, connection_id: str, summary: str,
        evidence_refs: List[Dict[str, Any]],
        trigger_source: str = "manual", triggered_by: Optional[str] = None,
    ) -> AnalysisSummary:
        """调用方（AI 分析层）负责保证 `summary`/`evidence_refs` 里不含原始运维
        数据本身（完整日志行、完整指标序列等）——这里不做内容过滤，只管落库。
        `evidence_refs` 的字段格式由分析层自己定义（比如
        `{"source": "prometheus", "query": "...", "window": "..."}`），Store 层
        不解析其内部结构，只当作不透明的 JSON 存取，避免两边为了"该有哪些
        字段"反复改这个方法签名。"""
        import json

        summary_id = f"opssum_{uuid.uuid4().hex[:16]}"
        now = time.time()
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO ops_analysis_summaries
                    (id, org_id, connection_id, summary, evidence_refs, created_at, trigger_source, triggered_by)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                """,
                summary_id, org_id, connection_id, summary, json.dumps(evidence_refs), now,
                trigger_source, triggered_by,
            )
        return AnalysisSummary(
            summary_id=summary_id, org_id=org_id, connection_id=connection_id,
            summary=summary, evidence_refs=evidence_refs, created_at=now,
        )

    async def get_analysis_summary(self, summary_id: str) -> Optional[AnalysisSummary]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM ops_analysis_summaries WHERE id = $1", summary_id,
            )
        return self._row_to_summary(row) if row else None

    async def list_analysis_summaries(
        self, org_id: str, connection_id: Optional[str] = None, limit: int = 50,
    ) -> List[AnalysisSummary]:
        """按 org 归属列出，供审批 UI 或 `propose_remediation` 关联"提议依据来自
        哪次分析"用。`connection_id` 可选——不传则列出该企业全部连接器的分析
        摘要（跨连接器场景，比如告警关联本来就可能横跨多个系统）。"""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            if connection_id is not None:
                rows = await conn.fetch(
                    "SELECT * FROM ops_analysis_summaries "
                    "WHERE org_id = $1 AND connection_id = $2 "
                    "ORDER BY created_at DESC LIMIT $3",
                    org_id, connection_id, limit,
                )
            else:
                rows = await conn.fetch(
                    "SELECT * FROM ops_analysis_summaries WHERE org_id = $1 "
                    "ORDER BY created_at DESC LIMIT $2",
                    org_id, limit,
                )
        return [self._row_to_summary(r) for r in rows]

    async def compute_ops_metrics(
        self, org_id: str, connection_ids: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """§10.5 定义的四个 V1 验收指标，全部按设计文档给的公式真实计算，
        不预设目标数字（那份态度本身就是设计的一部分："V1 阶段的目标是能测量，
        不是达到某个具体数字"）。任一指标分母为 0 时返回 `None`（不是 0.0 也
        不是省略字段）——"还没有样本"和"比例恰好是 0"是两件不同的事，糊在
        一起会让"这家企业刚开始用、数据太少"看起来像"表现很差"。

        - **审批处理及时率** = (approved+rejected) / (approved+rejected+expired)。
          `approved` 计数用 `approver_user_id IS NOT NULL`，不是"当前状态字段
          恰好等于 approved"——一个动作被批准之后还会继续流转到
          executing/completed/failed，用当前状态字段算会漏掉所有已经执行完的，
          `approver_user_id` 是"是否经过审批"这件事本身的忠实记录，不随后续
          状态推进而改变。
        - **执行成功率** = completed / (completed+failed)，两者都是终态，
          不存在这个问题。
        - **事后有效性**：设计文档没有给单一公式，只说"人工在时间线上补标即可"
          ——按 `outcome_effective` 三态（true/false/未标注）给计数，不是
          折成一个比例，未标注的数量本身就是有意义的信息（标注覆盖率）。
        - **告警合并率** = 1 − Σincident_count / Σalert_count，跨企业全部持久化
          的 `alert_correlation_stats` 条目**先加总再算比例**，不是对每条记录的
          `noise_reduction` 取平均——加总能按信号量加权，避免一次只有 2 条告警
          的小样本把整体比例拉偏。

        `connection_ids`：`None` 表示不按连接器过滤（给 org_admin 用，通配符
        语义跟 `viewable_connection_ids_for_user` 的既有约定一致）；传一个列表
        （含空列表）表示只统计这些连接器——空列表会让所有查询条件恒假，四个
        指标全部落到"没有样本"（`None`/空计数），不是意外聚合出全企业的数字。
        """
        import json

        pool = await self._get_pool()
        async with pool.acquire() as conn:
            if connection_ids is None:
                status_rows = await conn.fetch(
                    "SELECT status, approver_user_id IS NOT NULL AS was_approved, "
                    "outcome_effective, count(*) AS n "
                    "FROM remediation_actions WHERE org_id = $1 "
                    "GROUP BY status, was_approved, outcome_effective",
                    org_id,
                )
                summary_rows = await conn.fetch(
                    "SELECT evidence_refs FROM ops_analysis_summaries WHERE org_id = $1",
                    org_id,
                )
            else:
                status_rows = await conn.fetch(
                    "SELECT status, approver_user_id IS NOT NULL AS was_approved, "
                    "outcome_effective, count(*) AS n "
                    "FROM remediation_actions WHERE org_id = $1 AND connection_id = ANY($2::text[]) "
                    "GROUP BY status, was_approved, outcome_effective",
                    org_id, connection_ids,
                )
                summary_rows = await conn.fetch(
                    "SELECT evidence_refs FROM ops_analysis_summaries "
                    "WHERE org_id = $1 AND connection_id = ANY($2::text[])",
                    org_id, connection_ids,
                )

            if connection_ids is None:
                mttr_rows = await conn.fetch(
                    "SELECT a.executed_at, s.evidence_refs "
                    "FROM remediation_actions a "
                    "JOIN ops_analysis_summaries s ON s.id = a.summary_id "
                    "WHERE a.org_id = $1 AND a.status = 'completed' "
                    "AND a.executed_at IS NOT NULL",
                    org_id,
                )
            else:
                mttr_rows = await conn.fetch(
                    "SELECT a.executed_at, s.evidence_refs "
                    "FROM remediation_actions a "
                    "JOIN ops_analysis_summaries s ON s.id = a.summary_id "
                    "WHERE a.org_id = $1 AND a.connection_id = ANY($2::text[]) "
                    "AND a.status = 'completed' AND a.executed_at IS NOT NULL",
                    org_id, connection_ids,
                )

        approved_n = rejected_n = expired_n = completed_n = failed_n = 0
        effective_n = ineffective_n = unlabeled_n = 0
        for row in status_rows:
            n = row["n"]
            if row["was_approved"]:
                approved_n += n
            if row["status"] == STATUS_REJECTED:
                rejected_n += n
            elif row["status"] == STATUS_EXPIRED:
                expired_n += n
            elif row["status"] == STATUS_COMPLETED:
                completed_n += n
            elif row["status"] == STATUS_FAILED:
                failed_n += n
            if row["status"] in (STATUS_COMPLETED, STATUS_FAILED):
                if row["outcome_effective"] is True:
                    effective_n += n
                elif row["outcome_effective"] is False:
                    ineffective_n += n
                else:
                    unlabeled_n += n

        total_alerts = total_incidents = 0
        for row in summary_rows:
            refs = row["evidence_refs"]
            if isinstance(refs, str):
                refs = json.loads(refs)
            for ref in refs:
                if ref.get("source") == "alert_correlation_stats":
                    detail = ref.get("detail") or {}
                    total_alerts += detail.get("alert_count", 0)
                    total_incidents += detail.get("incident_count", 0)

        def _ratio(numerator: int, denominator: int) -> Optional[float]:
            return round(numerator / denominator, 4) if denominator > 0 else None

        durations: List[float] = []
        for row in mttr_rows:
            refs = row["evidence_refs"]
            refs = json.loads(refs) if isinstance(refs, str) else (refs or [])
            starts = [
                ref["detail"]["started_at"] for ref in refs
                if isinstance(ref, dict) and ref.get("source") == "alert_correlation"
                and isinstance(ref.get("detail"), dict)
                and isinstance(ref["detail"].get("started_at"), (int, float))
            ]
            if not starts:
                # 这条动作虽然链接了分析摘要，但摘要里没有告警关联依据
                # （比如只做了异常检测）——**跳过，不要用别的时间戳凑数**。
                # 拿 created_at 顶替会把"平台内部处理时长"混进 MTTR，
                # 那是另一个口径的数字，混在一起谁也解释不清。
                continue
            elapsed = float(row["executed_at"]) - float(min(starts))
            if elapsed >= 0:
                durations.append(elapsed)

        return {
            "mttr_seconds": service_health.median_seconds(durations),
            "approval_timeliness_rate": _ratio(approved_n + rejected_n, approved_n + rejected_n + expired_n),
            "execution_success_rate": _ratio(completed_n, completed_n + failed_n),
            "alert_noise_reduction": (
                round(1.0 - total_incidents / total_alerts, 4) if total_alerts > 0 else None
            ),
            "outcome_effective_counts": {
                "effective": effective_n, "ineffective": ineffective_n, "unlabeled": unlabeled_n,
            },
            "sample_sizes": {
                "mttr": len(durations),
                "approved": approved_n, "rejected": rejected_n, "expired": expired_n,
                "completed": completed_n, "failed": failed_n,
                "alert_count": total_alerts, "incident_count": total_incidents,
            },
        }

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
            summary_id=row["summary_id"],
        )

    async def create_proposed_action(
        self, org_id: str, connection_id: str, proposed_by: str, intent: str,
        plan: Dict[str, Any], impact_radius: Optional[str] = None,
        rollback_plan: Optional[Dict[str, Any]] = None,
        summary_id: Optional[str] = None,
    ) -> RemediationAction:
        """总是从 `proposed` 状态开始——是否放行到 `pending_approval` 由调用方
        先跑 `aiops_scope.check_target_in_scope`，再调 `advance_status` 转移，
        不在这里内联判断（越界判定是纯函数，故意保持可以脱离数据库单独测试）。

        `summary_id` 可选——这次提议是不是由某次 `analyze_ops_incident` 分析
        引出的。这里不校验它是否真的存在、是否属于同一个 org（外键约束会
        挡掉不存在的 id；跨 org 引用则留给调用方在 app.py 端点层做归属校验，
        Store 层不重复判断权限）。"""
        import json

        action_id = f"remact_{uuid.uuid4().hex[:12]}"
        now = time.time()
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO remediation_actions
                    (id, org_id, connection_id, proposed_by, intent, plan, impact_radius,
                     status, rollback_plan, created_at, summary_id)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                """,
                action_id, org_id, connection_id, proposed_by, intent, json.dumps(plan),
                impact_radius, STATUS_PROPOSED,
                json.dumps(rollback_plan) if rollback_plan is not None else None, now, summary_id,
            )
        return RemediationAction(
            action_id=action_id, org_id=org_id, connection_id=connection_id,
            proposed_by=proposed_by, intent=intent, plan=plan, impact_radius=impact_radius,
            status=STATUS_PROPOSED, approver_user_id=None, approved_at=None, executed_at=None,
            result=None, rollback_plan=rollback_plan, outcome_effective=None, created_at=now,
            summary_id=summary_id,
        )

    async def get_action(self, action_id: str) -> Optional[RemediationAction]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM remediation_actions WHERE id = $1", action_id)
        return self._row_to_action(row) if row else None

    async def _conditional_update(
        self, action_id: str, expected_status: str, target_status: str,
        set_clause: str = "", set_params: tuple = (),
    ) -> int:
        """状态转移的唯一落库入口——**条件 UPDATE**（`WHERE id = ... AND
        status = expected_status`），不是"先 SELECT 读状态判断、再无条件
        UPDATE"。

        2026-08-26 修复的 TOCTOU（变异测试实测发现）：原来 `approve_action`/
        `mark_executing`/`advance_status`/`mark_result` 都是"读出
        `current.status`，在 Python 里判断允不允许转移，再执行一条不带
        `WHERE status` 的 UPDATE"——两个并发请求可以都读到同一个"读时刻"的
        旧状态、都通过 Python 侧检查、都执行 UPDATE，后写的直接覆盖先写的，
        `approver_user_id`/`approved_at` 最终只记录了后到的那次调用，前一次
        调用却以为自己成功了，返回的也是"看起来成功"的响应。设计文档 §6
        明确要求"两个有 can_approve 权限的用户同时点批准，只能有一个生效，
        必须用真并发测试"，条件 UPDATE 是数据库层面唯一能真正保证这一点的
        机制——Python 里的先读后判断不管写得多严密，两个并发请求之间永远
        插得进第二个 UPDATE；数据库把"检查 + 修改"做成一条原子语句，才是
        真正堵死这条竞态的地方。

        返回受影响行数（0 或 1）。`set_clause`/`set_params` 是 `status`/
        `id`/预期旧 `status` 之外要一并设置的字段（比如 `approve_action`
        还要写 `approver_user_id`/`approved_at`），拼在 SET 子句里、参数
        顺序对齐 `$1` 开始。
        """
        extra = f", {set_clause}" if set_clause else ""
        query = (
            f"UPDATE remediation_actions SET status = $1{extra} "
            f"WHERE id = $2 AND status = $3"
        )
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(query, target_status, action_id, expected_status, *set_params)
        # asyncpg 的 execute() 对 UPDATE 返回形如 "UPDATE 1" 的命令标签，
        # 不是行数本身——按空格切出最后一段解析成整数。
        return int(result.rsplit(" ", 1)[-1])

    async def _raise_conflict(self, action_id: str, target_status: str, expected_status: str) -> None:
        latest = await self.get_action(action_id)
        raise IllegalStatusTransition(
            f"并发冲突：转移到 '{target_status}' 时状态已不是预期的 '{expected_status}'"
            f"（当前实际是 '{latest.status if latest else '(不存在)'}'），请重新加载后重试"
        )

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

        affected = await self._conditional_update(action_id, current.status, target_status)
        if affected == 0:
            await self._raise_conflict(action_id, target_status, current.status)
        return await self.get_action(action_id)

    async def approve_action(self, action_id: str, approver_user_id: str) -> RemediationAction:
        """§3.3 硬性不变量：`executing` 状态只能从 `approved` 转移而来，且必须
        有 `approver_user_id` + `approved_at`——这条不允许任何调试开关绕过。
        这里在写入前就校验当前状态是 `pending_approval`，不是先写后验；
        真正防并发靠的是下面条件 UPDATE 的 `AND status = 'pending_approval'`，
        不是这一步的 Python 判断（判断只是为了给不满足条件时一个更早、更
        准确的报错，竞态发生时条件 UPDATE 才是最终裁判）。"""
        current = await self.get_action(action_id)
        if current is None:
            raise ValueError(f"remediation_action '{action_id}' 不存在")
        _assert_transition_allowed(current.status, STATUS_APPROVED)

        approved_at = time.time()
        affected = await self._conditional_update(
            action_id, STATUS_PENDING_APPROVAL, STATUS_APPROVED,
            set_clause="approver_user_id = $4, approved_at = $5",
            set_params=(approver_user_id, approved_at),
        )
        if affected == 0:
            # 两个 org_admin 并发点批准时，只有一个会走到这里——另一个的
            # current.status 读到的还是 pending_approval（读的时候确实是），
            # 但条件 UPDATE 执行时状态已经被第一个请求改成 approved 了，
            # 这里如实报出"并发冲突"，不是"不存在"或别的误导性错误。
            await self._raise_conflict(action_id, STATUS_APPROVED, STATUS_PENDING_APPROVAL)
        return await self.get_action(action_id)

    async def mark_executing(self, action_id: str) -> RemediationAction:
        """硬性前置条件：当前状态必须是 `approved`，且 `approver_user_id` +
        `approved_at` 必须已经落库——不是"转移到 executing 就顺便查一下"，
        是"没有这两个字段就不允许转移"，这条不变量在这里第二次校验
        （第一次在 `approve_action` 写入时），双重保险。条件 UPDATE 的
        `AND status = 'approved'` 同时也挡住了并发场景下两次 `mark_executing`
        同时生效的问题。"""
        current = await self.get_action(action_id)
        if current is None:
            raise ValueError(f"remediation_action '{action_id}' 不存在")
        _assert_transition_allowed(current.status, STATUS_EXECUTING)
        if current.approver_user_id is None or current.approved_at is None:
            raise IllegalStatusTransition(
                "缺少 approver_user_id/approved_at，不允许转移到 executing——"
                "这条硬性不变量不接受任何绕过"
            )

        affected = await self._conditional_update(
            action_id, STATUS_APPROVED, STATUS_EXECUTING,
            set_clause="executed_at = $4", set_params=(time.time(),),
        )
        if affected == 0:
            await self._raise_conflict(action_id, STATUS_EXECUTING, STATUS_APPROVED)
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

        affected = await self._conditional_update(
            action_id, STATUS_EXECUTING, target_status,
            set_clause="result = $4", set_params=(json.dumps(result),),
        )
        if affected == 0:
            await self._raise_conflict(action_id, target_status, STATUS_EXECUTING)
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

    async def list_postmortems(
        self, org_id: str, connection_ids: Optional[List[str]] = None, limit: int = 100,
    ) -> List["PostmortemEntry"]:
        """§9.2"事后复盘聚合视图"的最小可行版——设计文档原话："没有这个视图，
        本模块的自动修复到底有没有用将无法被回顾评估，是一条真实的遗留风险"。
        这里做的**不是**完整 postmortem 工作流（标签分类/根因归档/改进项跟踪，
        那是明显更大的范围，不在这次要补的空白之内），只做设计文档点名的
        最小要求：把"这次修复解决了没有"这件事能被回顾——列出全部终态动作
        （`completed`/`failed`），附上触发它的分析摘要（如果提议时链接过）。

        `connection_ids` 语义跟 `compute_ops_metrics` 一致：`None` 不过滤，
        列表（含空列表）按连接器隔离。"""
        pool = await self._get_pool()
        base_sql = (
            "SELECT ra.*, s.summary AS linked_summary "
            "FROM remediation_actions ra "
            "LEFT JOIN ops_analysis_summaries s ON s.id = ra.summary_id "
            "WHERE ra.org_id = $1 AND ra.status = ANY($2::text[])"
        )
        terminal_statuses = [STATUS_COMPLETED, STATUS_FAILED]
        async with pool.acquire() as conn:
            if connection_ids is None:
                rows = await conn.fetch(
                    base_sql + " ORDER BY ra.created_at DESC LIMIT $3",
                    org_id, terminal_statuses, limit,
                )
            else:
                rows = await conn.fetch(
                    base_sql + " AND ra.connection_id = ANY($3::text[]) ORDER BY ra.created_at DESC LIMIT $4",
                    org_id, terminal_statuses, connection_ids, limit,
                )
        return [
            PostmortemEntry(action=self._row_to_action(row), linked_summary=row["linked_summary"])
            for row in rows
        ]

    async def expire_stale_pending_approvals(self) -> List[str]:
        """给超时扫描用（§3.3 的 `expired` 状态）——扫描全部 `pending_approval`
        动作，用**各自连接器**的 `approval_timeout_minutes` 算出过期时间点，
        过期的转成 `expired`。返回本次真正转移成功的 `action_id` 列表。

        ⚠️ **不能用一个全局 `cutoff_ts` 套所有动作**——`approval_timeout_minutes`
        是按连接器配置的（§10.4，5～1440 分钟范围），不同连接器的超时长度不同，
        必须 JOIN `ops_system_connections` 用各自的值算 `created_at + timeout`，
        不能假装全平台一个超时时长。这是本方法取代早前占位版
        `list_pending_approval_older_than`（只接收一个全局 `cutoff_ts`，从未被
        任何调用方使用过）的原因，不是简单改名。

        转移复用 `advance_status` 已有的条件 UPDATE（TOCTOU 修复的同一套机制）——
        扫描任务和"审批人这一刻刚好点了批准"是天然的并发场景，`advance_status`
        内部按 `WHERE status = 当前状态` 做原子转移，谁先谁赢，扫描到的动作如果
        已经在别处被改变状态，这里会拿到 `IllegalStatusTransition` 并跳过，
        不会覆盖真实的审批结果。
        """
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT ra.id FROM remediation_actions ra "
                "JOIN ops_system_connections c ON c.id = ra.connection_id "
                "WHERE ra.status = $1 "
                "AND ra.created_at + (c.approval_timeout_minutes * 60) < $2",
                STATUS_PENDING_APPROVAL, time.time(),
            )
        expired_ids: List[str] = []
        for row in rows:
            try:
                await self.advance_status(row["id"], STATUS_EXPIRED)
                expired_ids.append(row["id"])
            except IllegalStatusTransition:
                # 扫描到之后、真正转移之前，这条动作已经被别的路径改变了状态
                # （比如刚好被批准/拒绝）——正常竞态结果，不是错误，跳过即可。
                continue
        return expired_ids
