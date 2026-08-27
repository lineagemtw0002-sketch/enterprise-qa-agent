"""
工作流存储 (Workflow Store) — PostgreSQL 版

对应 work-flow.md 的数据模型/状态机设计。职责：
1. 流程模板（workflow_templates）：平台管理员配置的"某类流程需要哪些结构化
   字段"，附件材料不建模，只存一句提醒文案（attachments_note），齐不齐由
   审批人判断（见 work-flow.md 第 6.2 节）——这部分是跨企业共用的表单结构，
   不含审批人信息。
2. 审批人分配（workflow_approver_roles）：2026-08-23 起，"这类流程谁来批"
   改成按企业独立配置——工作流跟角色/知识库一样是"企业内部的事"，同一个
   workflow_type（比如"请假申请"）在不同企业应该能配不同的审批角色，且只能
   由该企业自己的管理员配置，不再是模板上挂一个全平台唯一的 approver_role_id。
   `(workflow_type, org_id) -> approver_role_id` 是这张表的全部内容。
3. 工作流实例（workflow_instances）：一条申请/工单，状态机见 work-flow.md 4.4 节。
4. 站内信（notifications）：通用的"事件 -> 提醒"投递表（work-flow-web.md 第 6 节），
   目前只有工作流状态变化会触发，但表结构不专属于工作流。

不负责：
- 结构化字段的 LLM 抽取（那是 workflow.py 的 `_workflow_node` 的事，本模块只存值）
- 审批权限判断（审批人是否有权限，由 app.py 的鉴权 helper 结合 RoleStore 判断）
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional

import asyncpg

from src.ragent_backend.db_pool import get_shared_pool

# ============== 状态常量 ==============

STATUS_PENDING_APPROVAL = "pending_approval"
STATUS_RETURNED_FOR_REVISION = "returned_for_revision"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_COMPLETED = "completed"
STATUS_CANCELLED = "cancelled"

# "在途"状态：占用"同类型只能一条在途"名额的状态（work-flow-web.md 第 7.1 节）
IN_FLIGHT_STATUSES = (STATUS_PENDING_APPROVAL, STATUS_RETURNED_FOR_REVISION)

# 允许的状态转换：{目标状态: (合法的起始状态集合,)}
_ALLOWED_TRANSITIONS: Dict[str, tuple] = {
    STATUS_APPROVED: (STATUS_PENDING_APPROVAL,),
    STATUS_RETURNED_FOR_REVISION: (STATUS_PENDING_APPROVAL,),
    STATUS_REJECTED: (STATUS_PENDING_APPROVAL,),
    STATUS_COMPLETED: (STATUS_APPROVED,),
    # 申请人随时可以取消，只要还没到终态（rejected/completed/cancelled 之后不能再取消）
    STATUS_CANCELLED: (STATUS_PENDING_APPROVAL, STATUS_RETURNED_FOR_REVISION, STATUS_APPROVED),
    # resubmit：打回后补充材料，回到待审批
    STATUS_PENDING_APPROVAL: (STATUS_RETURNED_FOR_REVISION,),
}

# 四个内置示例模板（work-flow.md 4.2 节），审批角色留空，需要管理员部署后配置
_SYSTEM_TEMPLATE_SEEDS: List[Dict[str, Any]] = [
    {
        "workflow_type": "laptop_repair",
        "display_name": "电脑报修",
        "description": "电脑/外设硬件或系统故障报修",
        "required_fields": [
            {"key": "issue_description", "label": "问题描述", "type": "text", "required": True,
             "description": "具体是什么坏了/什么现象"},
            {"key": "urgency", "label": "紧急程度", "type": "enum", "required": True,
             "options": ["低", "中", "高"]},
            {"key": "location", "label": "工位/楼层", "type": "text", "required": False},
        ],
        "attachments_note": "",
    },
    {
        "workflow_type": "leave_request",
        "display_name": "请假申请",
        "description": "事假/病假/年假/调休等请假申请",
        "required_fields": [
            {"key": "leave_type", "label": "假期类型", "type": "enum", "required": True,
             "options": ["事假", "病假", "年假", "调休"]},
            {"key": "start_date", "label": "开始日期", "type": "date", "required": True},
            {"key": "end_date", "label": "结束日期", "type": "date", "required": True},
            {"key": "reason", "label": "事由", "type": "text", "required": True},
        ],
        "attachments_note": "如果是病假，请把病假单发我；其它假期类型一般不需要额外材料。",
    },
    {
        "workflow_type": "business_trip",
        "display_name": "出差申请",
        "description": "因公出差申请",
        "required_fields": [
            {"key": "destination", "label": "目的地", "type": "text", "required": True},
            {"key": "start_date", "label": "开始日期", "type": "date", "required": True},
            {"key": "end_date", "label": "结束日期", "type": "date", "required": True},
            {"key": "purpose", "label": "出差事由", "type": "text", "required": True},
        ],
        "attachments_note": "请把出差审批单/邀请函之类的材料发我，审批人会看。",
    },
    {
        "workflow_type": "expense_reimbursement",
        "display_name": "报销申请",
        "description": "差旅/餐饮/办公用品等费用报销",
        "required_fields": [
            {"key": "expense_category", "label": "报销类别", "type": "enum", "required": True,
             "options": ["差旅", "餐饮", "办公用品", "其他"]},
            {"key": "amount", "label": "金额", "type": "number", "required": True},
            {"key": "note", "label": "备注说明", "type": "text", "required": False},
        ],
        "attachments_note": "请把发票/报销单据发我，缺票据审批人会打回。",
    },
]


@dataclass(frozen=True)
class WorkflowTemplate:
    template_id: str
    workflow_type: str
    display_name: str
    description: str
    required_fields: List[Dict[str, Any]]
    attachments_note: str
    is_system: bool
    created_at: float


@dataclass(frozen=True)
class WorkflowInstance:
    instance_id: str
    workflow_type: str
    requester_user_id: str
    conversation_id: Optional[str]
    fields: Dict[str, Any]
    status: str
    approver_user_id: Optional[str]
    approval_comment: Optional[str]
    history: List[Dict[str, Any]]
    created_at: float
    updated_at: float


@dataclass(frozen=True)
class Notification:
    notification_id: str
    user_id: str
    type: str
    title: str
    body: str
    link: Optional[str]
    is_read: bool
    created_at: float


class WorkflowStore:
    """工作流存储 (PostgreSQL)。"""

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
                CREATE TABLE IF NOT EXISTS workflow_templates (
                    id                TEXT PRIMARY KEY,
                    workflow_type     VARCHAR(64) UNIQUE NOT NULL,
                    display_name      VARCHAR(128) NOT NULL,
                    description       TEXT NOT NULL DEFAULT '',
                    required_fields   JSONB NOT NULL,
                    attachments_note  TEXT NOT NULL DEFAULT '',
                    approver_role_id  TEXT,
                    is_system         BOOLEAN NOT NULL DEFAULT FALSE,
                    created_at        DOUBLE PRECISION NOT NULL
                )
                """
            )
            # 审批人分配：按企业独立配置，见文件顶部说明。旧的
            # workflow_templates.approver_role_id 列保留在表里但从
            # 2026-08-23 起不再读写（历史遗留，不做 DROP COLUMN 迁移，
            # 只是彻底不用了）——全平台共用一个审批角色这件事本身就是要
            # 改掉的设计问题，不能只是换个新列名换汤不换药。
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS workflow_approver_roles (
                    workflow_type     VARCHAR(64) NOT NULL,
                    org_id            TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
                    approver_role_id  TEXT NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
                    updated_at        DOUBLE PRECISION NOT NULL,
                    PRIMARY KEY (workflow_type, org_id)
                )
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS workflow_instances (
                    id                  TEXT PRIMARY KEY,
                    workflow_type       VARCHAR(64) NOT NULL,
                    requester_user_id   TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    conversation_id     TEXT,
                    fields              JSONB NOT NULL,
                    status              VARCHAR(32) NOT NULL DEFAULT 'pending_approval',
                    approver_user_id    TEXT REFERENCES users(id) ON DELETE SET NULL,
                    approval_comment    TEXT,
                    history             JSONB NOT NULL DEFAULT '[]',
                    created_at          DOUBLE PRECISION NOT NULL,
                    updated_at          DOUBLE PRECISION NOT NULL
                )
                """
            )
            # 迁移：早期版本 status 列是 VARCHAR(20)，装不下 "returned_for_revision"
            # （21 字符），已经跑过旧版本的部署需要放宽这一列。
            await conn.execute(
                "ALTER TABLE workflow_instances ALTER COLUMN status TYPE VARCHAR(32)"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_workflow_instances_requester "
                "ON workflow_instances(requester_user_id)"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_workflow_instances_status "
                "ON workflow_instances(status)"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_workflow_instances_type "
                "ON workflow_instances(workflow_type)"
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS notifications (
                    id            TEXT PRIMARY KEY,
                    user_id       TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    type          VARCHAR(32) NOT NULL,
                    title         VARCHAR(200) NOT NULL,
                    body          TEXT NOT NULL DEFAULT '',
                    link          TEXT,
                    is_read       BOOLEAN NOT NULL DEFAULT FALSE,
                    created_at    DOUBLE PRECISION NOT NULL
                )
                """
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_notifications_user_unread "
                "ON notifications(user_id, is_read)"
            )

            for seed in _SYSTEM_TEMPLATE_SEEDS:
                await conn.execute(
                    """
                    INSERT INTO workflow_templates
                        (id, workflow_type, display_name, description, required_fields,
                         attachments_note, approver_role_id, is_system, created_at)
                    VALUES ($1, $2, $3, $4, $5, $6, NULL, TRUE, $7)
                    ON CONFLICT (workflow_type) DO NOTHING
                    """,
                    str(uuid.uuid4()), seed["workflow_type"], seed["display_name"],
                    seed["description"], _to_json(seed["required_fields"]),
                    seed["attachments_note"], time.time(),
                )

    @staticmethod
    def _row_to_template(row: asyncpg.Record) -> WorkflowTemplate:
        return WorkflowTemplate(
            template_id=row["id"],
            workflow_type=row["workflow_type"],
            display_name=row["display_name"],
            description=row["description"],
            required_fields=_from_json(row["required_fields"]),
            attachments_note=row["attachments_note"],
            is_system=row["is_system"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _row_to_instance(row: asyncpg.Record) -> WorkflowInstance:
        return WorkflowInstance(
            instance_id=row["id"],
            workflow_type=row["workflow_type"],
            requester_user_id=row["requester_user_id"],
            conversation_id=row["conversation_id"],
            fields=_from_json(row["fields"]),
            status=row["status"],
            approver_user_id=row["approver_user_id"],
            approval_comment=row["approval_comment"],
            history=_from_json(row["history"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _row_to_notification(row: asyncpg.Record) -> Notification:
        return Notification(
            notification_id=row["id"],
            user_id=row["user_id"],
            type=row["type"],
            title=row["title"],
            body=row["body"],
            link=row["link"],
            is_read=row["is_read"],
            created_at=row["created_at"],
        )

    # ------------------------------------------------------------------
    # 流程模板
    # ------------------------------------------------------------------

    async def create_template(
        self, workflow_type: str, display_name: str, description: str,
        required_fields: List[Dict[str, Any]], attachments_note: str = "",
    ) -> WorkflowTemplate:
        pool = await self._get_pool()
        template_id = str(uuid.uuid4())
        now = time.time()
        async with pool.acquire() as conn:
            try:
                await conn.execute(
                    """INSERT INTO workflow_templates
                       (id, workflow_type, display_name, description, required_fields,
                        attachments_note, approver_role_id, is_system, created_at)
                       VALUES ($1, $2, $3, $4, $5, $6, NULL, FALSE, $7)""",
                    template_id, workflow_type, display_name, description,
                    _to_json(required_fields), attachments_note, now,
                )
            except asyncpg.UniqueViolationError as e:
                raise ValueError(f"Workflow type '{workflow_type}' already exists") from e
        return WorkflowTemplate(
            template_id=template_id, workflow_type=workflow_type, display_name=display_name,
            description=description, required_fields=required_fields,
            attachments_note=attachments_note, is_system=False,
            created_at=now,
        )

    async def list_templates(self) -> List[WorkflowTemplate]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM workflow_templates ORDER BY created_at ASC")
        return [self._row_to_template(r) for r in rows]

    async def get_template_by_id(self, template_id: str) -> Optional[WorkflowTemplate]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM workflow_templates WHERE id = $1", template_id)
        return self._row_to_template(row) if row else None

    async def get_template_by_type(self, workflow_type: str) -> Optional[WorkflowTemplate]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM workflow_templates WHERE workflow_type = $1", workflow_type,
            )
        return self._row_to_template(row) if row else None

    async def update_template(
        self, template_id: str,
        display_name: Optional[str] = None,
        description: Optional[str] = None,
        required_fields: Optional[List[Dict[str, Any]]] = None,
        attachments_note: Optional[str] = None,
    ) -> Optional[WorkflowTemplate]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            if display_name is not None:
                await conn.execute(
                    "UPDATE workflow_templates SET display_name = $1 WHERE id = $2", display_name, template_id,
                )
            if description is not None:
                await conn.execute(
                    "UPDATE workflow_templates SET description = $1 WHERE id = $2", description, template_id,
                )
            if required_fields is not None:
                await conn.execute(
                    "UPDATE workflow_templates SET required_fields = $1 WHERE id = $2",
                    _to_json(required_fields), template_id,
                )
            if attachments_note is not None:
                await conn.execute(
                    "UPDATE workflow_templates SET attachments_note = $1 WHERE id = $2",
                    attachments_note, template_id,
                )
        return await self.get_template_by_id(template_id)

    async def delete_template(self, template_id: str) -> bool:
        template = await self.get_template_by_id(template_id)
        if template is None:
            return False
        if template.is_system:
            raise ValueError(f"系统内置流程模板 '{template.workflow_type}' 不可删除")
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute("DELETE FROM workflow_templates WHERE id = $1", template_id)
        return result.split()[-1] != "0"

    # ------------------------------------------------------------------
    # 审批人分配（按企业独立配置，见文件顶部说明）
    # ------------------------------------------------------------------

    async def set_org_approver_role(
        self, org_id: str, workflow_type: str, approver_role_id: Optional[str],
    ) -> None:
        """`approver_role_id=None` 表示这家企业还没给这类流程配审批人（清空/
        取消分配），删掉这一行而不是存一个 NULL——列本身是 NOT NULL，"没配置"
        用"这一行压根不存在"表达，跟 get_org_approver_role_id 查不到返回 None
        的语义天然一致。"""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            if approver_role_id is None:
                await conn.execute(
                    "DELETE FROM workflow_approver_roles WHERE org_id = $1 AND workflow_type = $2",
                    org_id, workflow_type,
                )
            else:
                await conn.execute(
                    """INSERT INTO workflow_approver_roles (workflow_type, org_id, approver_role_id, updated_at)
                       VALUES ($1, $2, $3, $4)
                       ON CONFLICT (workflow_type, org_id)
                       DO UPDATE SET approver_role_id = EXCLUDED.approver_role_id, updated_at = EXCLUDED.updated_at""",
                    workflow_type, org_id, approver_role_id, time.time(),
                )

    async def get_org_approver_role_id(self, org_id: str, workflow_type: str) -> Optional[str]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT approver_role_id FROM workflow_approver_roles WHERE org_id = $1 AND workflow_type = $2",
                org_id, workflow_type,
            )
        return row["approver_role_id"] if row else None

    async def list_org_approver_roles(self, org_id: str) -> Dict[str, str]:
        """这家企业当前给哪些 workflow_type 配了审批角色，`{workflow_type: approver_role_id}`。
        供企业管理员的「审批设置」页面用，跟 list_templates() 的结果拼在一起
        展示"每类流程 + 当前配的审批角色"。"""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT workflow_type, approver_role_id FROM workflow_approver_roles WHERE org_id = $1",
                org_id,
            )
        return {row["workflow_type"]: row["approver_role_id"] for row in rows}

    async def approvable_templates_for_org_and_role_ids(
        self, org_id: str, role_ids: List[str],
    ) -> List[WorkflowTemplate]:
        """当前用户在自己企业内，凭手头的角色能审批哪些流程类型。"""
        if not role_ids:
            return []
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT wt.* FROM workflow_templates wt
                JOIN workflow_approver_roles war ON war.workflow_type = wt.workflow_type
                WHERE war.org_id = $1 AND war.approver_role_id = ANY($2::text[])
                """,
                org_id, role_ids,
            )
        return [self._row_to_template(r) for r in rows]

    # ------------------------------------------------------------------
    # 工作流实例
    # ------------------------------------------------------------------

    async def get_in_flight_instance(self, requester_user_id: str, workflow_type: str) -> Optional[WorkflowInstance]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT * FROM workflow_instances
                   WHERE requester_user_id = $1 AND workflow_type = $2 AND status = ANY($3::text[])
                   ORDER BY created_at DESC LIMIT 1""",
                requester_user_id, workflow_type, list(IN_FLIGHT_STATUSES),
            )
        return self._row_to_instance(row) if row else None

    async def create_instance(
        self, workflow_type: str, requester_user_id: str,
        conversation_id: Optional[str], fields: Dict[str, Any],
        role_store: Optional[Any] = None, org_id: Optional[str] = None,
    ) -> WorkflowInstance:
        """提交一条新申请，直接进入 pending_approval。事务内先校验"同类型只能一条
        在途"（work-flow-web.md 7.1 节），避免竞态下重复创建。"""
        pool = await self._get_pool()
        instance_id = str(uuid.uuid4())
        now = time.time()
        history = [{"event": "submitted", "ts": now, "actor_user_id": requester_user_id}]

        async with pool.acquire() as conn:
            async with conn.transaction():
                existing = await conn.fetchrow(
                    """SELECT id FROM workflow_instances
                       WHERE requester_user_id = $1 AND workflow_type = $2 AND status = ANY($3::text[])
                       FOR UPDATE""",
                    requester_user_id, workflow_type, list(IN_FLIGHT_STATUSES),
                )
                if existing is not None:
                    raise ValueError(f"该用户已有一条在途的 '{workflow_type}' 申请")

                await conn.execute(
                    """INSERT INTO workflow_instances
                       (id, workflow_type, requester_user_id, conversation_id, fields,
                        status, history, created_at, updated_at)
                       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $8)""",
                    instance_id, workflow_type, requester_user_id, conversation_id,
                    _to_json(fields), STATUS_PENDING_APPROVAL, _to_json(history), now,
                )

        instance = WorkflowInstance(
            instance_id=instance_id, workflow_type=workflow_type,
            requester_user_id=requester_user_id, conversation_id=conversation_id,
            fields=fields, status=STATUS_PENDING_APPROVAL, approver_user_id=None,
            approval_comment=None, history=history, created_at=now, updated_at=now,
        )

        if role_store is not None and org_id is not None:
            template = await self.get_template_by_type(workflow_type)
            approver_role_id = await self.get_org_approver_role_id(org_id, workflow_type)
            if template is not None and approver_role_id:
                await self.notify_approvers(instance, template, role_store, approver_role_id, event="submitted")
        return instance

    async def get_instance(self, instance_id: str) -> Optional[WorkflowInstance]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM workflow_instances WHERE id = $1", instance_id)
        return self._row_to_instance(row) if row else None

    async def get_latest_instance_by_conversation(self, conversation_id: str) -> Optional[WorkflowInstance]:
        """按对话反查最近一条工作流实例——用来判断"这个对话的文件，除了对话
        所有者，还有谁（审批人）该有权限看"（见 work-flow.md 第 8 节风险）。"""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM workflow_instances WHERE conversation_id = $1 ORDER BY created_at DESC LIMIT 1",
                conversation_id,
            )
        return self._row_to_instance(row) if row else None

    async def list_instances_for_user(
        self, requester_user_id: str, status: Optional[str] = None,
    ) -> List[WorkflowInstance]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            if status:
                rows = await conn.fetch(
                    """SELECT * FROM workflow_instances WHERE requester_user_id = $1 AND status = $2
                       ORDER BY created_at DESC""",
                    requester_user_id, status,
                )
            else:
                rows = await conn.fetch(
                    "SELECT * FROM workflow_instances WHERE requester_user_id = $1 ORDER BY created_at DESC",
                    requester_user_id,
                )
        return [self._row_to_instance(r) for r in rows]

    async def list_pending_for_org_and_role_ids(self, org_id: str, role_ids: List[str]) -> List[WorkflowInstance]:
        """待审批列表：状态是 pending_approval，且实例的 workflow_type 在
        「当前用户所在企业」配置的审批角色落在当前用户持有的角色集合里。
        额外 JOIN users 卡一遍申请人所属企业，双重保险（虽然角色分配本身已经
        按企业隔离，理论上 role_id 不会跨企业撞车，但审批列表这种直接暴露
        别家企业申请内容的地方多一层显式校验不亏）。"""
        if not role_ids:
            return []
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT wi.* FROM workflow_instances wi
                   JOIN workflow_approver_roles war ON war.workflow_type = wi.workflow_type AND war.org_id = $1
                   JOIN users u ON u.id = wi.requester_user_id AND u.org_id = $1
                   WHERE wi.status = $2 AND war.approver_role_id = ANY($3::text[])
                   ORDER BY wi.created_at ASC""",
                org_id, STATUS_PENDING_APPROVAL, role_ids,
            )
        return [self._row_to_instance(r) for r in rows]

    async def transition(
        self, instance_id: str, new_status: str, actor_user_id: str,
        comment: Optional[str] = None, role_store: Optional[Any] = None,
    ) -> Optional[WorkflowInstance]:
        """状态机驱动的通用转换：approve/return/reject/complete/cancel/resubmit
        都是这一个方法的不同调用（不同的 new_status），统一在这里校验合法转移、
        追加 history、触发站内信。"""
        instance = await self.get_instance(instance_id)
        if instance is None:
            return None

        allowed_from = _ALLOWED_TRANSITIONS.get(new_status, ())
        if instance.status not in allowed_from:
            raise ValueError(f"不能从状态 '{instance.status}' 转换到 '{new_status}'")

        pool = await self._get_pool()
        now = time.time()
        event_name = {
            STATUS_APPROVED: "approved",
            STATUS_RETURNED_FOR_REVISION: "returned",
            STATUS_REJECTED: "rejected",
            STATUS_COMPLETED: "completed",
            STATUS_CANCELLED: "cancelled",
            STATUS_PENDING_APPROVAL: "resubmitted",
        }.get(new_status, new_status)
        history_entry = {"event": event_name, "ts": now, "actor_user_id": actor_user_id}
        if comment:
            history_entry["comment"] = comment
        new_history = [*instance.history, history_entry]

        approver_user_id = instance.approver_user_id
        approval_comment = instance.approval_comment
        if new_status in (STATUS_APPROVED, STATUS_RETURNED_FOR_REVISION, STATUS_REJECTED):
            approver_user_id = actor_user_id
            approval_comment = comment

        async with pool.acquire() as conn:
            await conn.execute(
                """UPDATE workflow_instances
                   SET status = $1, approver_user_id = $2, approval_comment = $3,
                       history = $4, updated_at = $5
                   WHERE id = $6""",
                new_status, approver_user_id, approval_comment, _to_json(new_history), now, instance_id,
            )

        updated = await self.get_instance(instance_id)
        if updated is None:
            return None

        template = await self.get_template_by_type(updated.workflow_type)
        if template is not None:
            if new_status == STATUS_PENDING_APPROVAL and role_store is not None:
                # resubmit：重新通知审批人——审批角色现在按企业配置（见文件
                # 顶部说明），要先查一下申请人所属企业才能知道该通知哪个角色。
                async with pool.acquire() as conn:
                    org_row = await conn.fetchrow(
                        "SELECT org_id FROM users WHERE id = $1", updated.requester_user_id,
                    )
                requester_org_id = org_row["org_id"] if org_row else None
                approver_role_id = (
                    await self.get_org_approver_role_id(requester_org_id, updated.workflow_type)
                    if requester_org_id else None
                )
                if approver_role_id:
                    await self.notify_approvers(updated, template, role_store, approver_role_id, event="resubmitted")
            elif new_status != STATUS_PENDING_APPROVAL:
                await self.notify_requester(updated, template, event=event_name)

        return updated

    # ------------------------------------------------------------------
    # 站内信
    # ------------------------------------------------------------------

    async def create_notification(
        self, user_id: str, type_: str, title: str, body: str, link: Optional[str] = None,
    ) -> Notification:
        pool = await self._get_pool()
        notification_id = str(uuid.uuid4())
        now = time.time()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO notifications (id, user_id, type, title, body, link, is_read, created_at)
                   VALUES ($1, $2, $3, $4, $5, $6, FALSE, $7)""",
                notification_id, user_id, type_, title, body, link, now,
            )
        return Notification(
            notification_id=notification_id, user_id=user_id, type=type_, title=title,
            body=body, link=link, is_read=False, created_at=now,
        )

    async def list_notifications(
        self, user_id: str, unread_only: bool = False, limit: int = 20, offset: int = 0,
    ) -> List[Notification]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            if unread_only:
                rows = await conn.fetch(
                    """SELECT * FROM notifications WHERE user_id = $1 AND is_read = FALSE
                       ORDER BY created_at DESC LIMIT $2 OFFSET $3""",
                    user_id, limit, offset,
                )
            else:
                rows = await conn.fetch(
                    """SELECT * FROM notifications WHERE user_id = $1
                       ORDER BY created_at DESC LIMIT $2 OFFSET $3""",
                    user_id, limit, offset,
                )
        return [self._row_to_notification(r) for r in rows]

    async def unread_count(self, user_id: str) -> int:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT count(*) FROM notifications WHERE user_id = $1 AND is_read = FALSE", user_id,
            )
        return int(count)

    async def mark_read(self, notification_id: str, user_id: str) -> bool:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE notifications SET is_read = TRUE WHERE id = $1 AND user_id = $2",
                notification_id, user_id,
            )
        return result.split()[-1] != "0"

    async def mark_all_read(self, user_id: str) -> int:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE notifications SET is_read = TRUE WHERE user_id = $1 AND is_read = FALSE", user_id,
            )
        return int(result.split()[-1])

    async def notify_requester(self, instance: WorkflowInstance, template: WorkflowTemplate, event: str) -> None:
        short_id = instance.instance_id[:8]
        titles = {
            "approved": "申请已通过",
            "returned": "申请被打回，需要补充材料",
            "rejected": "申请被驳回",
            "completed": "申请已办理完成",
        }
        bodies = {
            "approved": f"你的「{template.display_name}」申请（#{short_id}）已通过审批。",
            "returned": f"你的「{template.display_name}」申请（#{short_id}）被打回："
                        f"{instance.approval_comment or '（未填写原因）'}",
            "rejected": f"你的「{template.display_name}」申请（#{short_id}）被驳回："
                        f"{instance.approval_comment or '（未填写原因）'}",
            "completed": f"你的「{template.display_name}」申请（#{short_id}）已标记为办理完成。",
        }
        title = titles.get(event)
        body = bodies.get(event)
        if title is None:
            return
        await self.create_notification(
            user_id=instance.requester_user_id, type_="workflow_status_changed",
            title=title, body=body, link=f"workflow:{instance.instance_id}",
        )

    async def notify_approvers(
        self, instance: WorkflowInstance, template: WorkflowTemplate, role_store: Any,
        approver_role_id: Optional[str], event: str = "submitted",
    ) -> None:
        if not approver_role_id:
            return
        approver_user_ids = await role_store.get_user_ids_by_role(approver_role_id)
        short_id = instance.instance_id[:8]
        title = "申请已重新提交" if event == "resubmitted" else "新的待审批申请"
        body = (
            f"「{template.display_name}」申请（#{short_id}）材料已补充，请重新审批。"
            if event == "resubmitted"
            else f"有一条新的「{template.display_name}」申请（#{short_id}）等待你审批。"
        )
        for approver_id in approver_user_ids:
            await self.create_notification(
                user_id=approver_id, type_="workflow_status_changed",
                title=title, body=body, link=f"workflow:{instance.instance_id}",
            )

    async def close(self) -> None:
        # 池现在是跨 14 个 Store 共享的（db_pool.py，P1-2），这里只清掉
        # 本 Store 持有的引用，不触发真实关闭——那会把其它 Store 正在用的
        # 连接一起关掉。真正关闭见 db_pool.close_shared_pools()，只在 app
        # 关闭时调一次。
        type(self)._pool = None


def _to_json(value: Any) -> str:
    import json
    return json.dumps(value, ensure_ascii=False)


def _from_json(value: Any) -> Any:
    """asyncpg 对 JSONB 列默认返回原始字符串，不像 dict/list 那样自动解码。"""
    import json
    if isinstance(value, str):
        return json.loads(value)
    return value
