"""
审计日志存储 (Audit Log Store) — PostgreSQL 版

职责：记录"谁在何时对哪个资源做了什么"，覆盖两类事件：
1. 管理后台的变更操作（建/删用户、改角色、配连接器、建组织/知识库……），
   由 app.py 各管理端点在操作成功后调用 `record(...)` 写入。
2. 工具调用（知识库检索、考勤查询、工作流操作……），由
   `tool_agent/subgraph.py` 的 tool_node 在每次工具执行后调用——覆盖
   "谁在何时查询了哪个知识库/触发了哪个工具"这条治理需求。

按组织隔离读取权限：平台管理员（super_admin/admin）能看全平台审计记录，
企业管理员（org_admin）只能看自己企业的——跟 admin_list_users 等端点
同一套过滤口径（见 app.py `_require_user_admin_tier` 旁的角色模型说明），
过滤发生在这个 store 的查询条件里，不是前端拿到全量自己藏几行。

不负责：
- 判断"这次操作是否需要审计"——那是调用方（app.py/subgraph.py）的事，
  这里只负责持久化和按条件查询。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import asyncpg

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AuditLogEntry:
    audit_id: str
    org_id: Optional[str]
    user_id: Optional[str]
    username: Optional[str]
    action: str
    resource_type: str
    resource_id: Optional[str]
    detail: Dict[str, Any]
    success: bool
    created_at: float


class AuditStore:
    """审计日志存储 (PostgreSQL)。"""

    # 类级别共享连接池，见其他 *_store.py 同名字段的注释——调用方经常每次都
    # new 一个新实例，池必须挂在类属性上才不会被重复创建、打满 Postgres 连接数。
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
                CREATE TABLE IF NOT EXISTS audit_logs (
                    id            TEXT PRIMARY KEY,
                    org_id        TEXT,
                    user_id       TEXT,
                    username      VARCHAR(128),
                    action        VARCHAR(64) NOT NULL,
                    resource_type VARCHAR(64) NOT NULL,
                    resource_id   TEXT,
                    detail        JSONB NOT NULL DEFAULT '{}'::jsonb,
                    success       BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at    DOUBLE PRECISION NOT NULL
                )
                """
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_audit_org_time ON audit_logs(org_id, created_at DESC)"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_audit_user_time ON audit_logs(user_id, created_at DESC)"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_audit_action_time ON audit_logs(action, created_at DESC)"
            )

    async def record(
        self,
        *,
        org_id: Optional[str],
        user_id: Optional[str],
        username: Optional[str],
        action: str,
        resource_type: str,
        resource_id: Optional[str] = None,
        detail: Optional[Dict[str, Any]] = None,
        success: bool = True,
    ) -> None:
        """写入一条审计记录。绝不抛异常给调用方——审计失败不能拖垮被审计的
        那个业务操作本身（跟 dashboard_stats.py 的"全新部署无数据"同一种
        克制：审计是旁路能力，不是业务操作能否成功的前提）。"""
        try:
            pool = await self._get_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    """INSERT INTO audit_logs
                       (id, org_id, user_id, username, action, resource_type, resource_id, detail, success, created_at)
                       VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9, $10)""",
                    str(uuid.uuid4()), org_id, user_id, username, action, resource_type,
                    resource_id, json.dumps(detail or {}), success, time.time(),
                )
        except Exception as e:
            # error：合规记录静默丢失。审计失败不拖垮业务操作（这是刻意的，
            # 见上面的 docstring），但**必须在应用日志里留下痕迹**，
            # 否则"审计表里为什么少了一段"永远查不出来。
            # detail 不记——它可能含工具参数/提示词片段（S2）。
            logger.error(
                "[AuditStore] Failed to record audit log",
                extra={
                    "event": "audit.record.failed",
                    "error_type": type(e).__name__,
                    "org_id": org_id,
                    "user_id": user_id,
                    "action": action,
                    "resource_type": resource_type,
                },
                exc_info=True,
            )

    @staticmethod
    def _row_to_entry(row: asyncpg.Record) -> AuditLogEntry:
        detail = row["detail"]
        if isinstance(detail, str):
            try:
                detail = json.loads(detail)
            except (TypeError, ValueError):
                detail = {}
        return AuditLogEntry(
            audit_id=row["id"], org_id=row["org_id"], user_id=row["user_id"], username=row["username"],
            action=row["action"], resource_type=row["resource_type"], resource_id=row["resource_id"],
            detail=detail or {}, success=row["success"], created_at=row["created_at"],
        )

    async def list_logs(
        self,
        *,
        org_id: Optional[str] = None,
        user_id: Optional[str] = None,
        action: Optional[str] = None,
        start: Optional[float] = None,
        end: Optional[float] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Tuple[List[AuditLogEntry], int]:
        """按条件筛选审计记录，返回 (本页记录, 满足条件的总数)——总数用于前端
        分页控件，不受 limit/offset 影响。"""
        pool = await self._get_pool()
        conditions = []
        params: List[Any] = []

        def _add(cond_tpl: str, value: Any) -> None:
            params.append(value)
            conditions.append(cond_tpl.format(idx=len(params)))

        if org_id is not None:
            _add("org_id = ${idx}", org_id)
        if user_id is not None:
            _add("user_id = ${idx}", user_id)
        if action is not None:
            _add("action = ${idx}", action)
        if start is not None:
            _add("created_at >= ${idx}", start)
        if end is not None:
            _add("created_at < ${idx}", end)

        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        async with pool.acquire() as conn:
            total = await conn.fetchval(f"SELECT COUNT(*) FROM audit_logs {where_clause}", *params)
            rows = await conn.fetch(
                f"""SELECT * FROM audit_logs {where_clause}
                    ORDER BY created_at DESC
                    LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}""",
                *params, limit, offset,
            )
        return [self._row_to_entry(r) for r in rows], int(total or 0)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            type(self)._pool = None
