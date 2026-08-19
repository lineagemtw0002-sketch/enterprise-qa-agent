"""
租户连接器存储 (Tenant Connector Store) — PostgreSQL 版

职责：
1. 记录"某家企业的某项能力，委托给谁去查"——`tenant_connectors` 表，按
   `(org_id, capability)` 唯一，`capability` 目前有 `knowledge_base`（本文件落地
   的第一个能力，见 `knowledge-base-tenant-federation.md`），未来 `attendance`
   等能力接入时复用同一张表，不重新建模
   （见 `attendance-tenant-federation.md` 第 3 节的表设计，这里原样落地建表）。
2. `connector_type` 决定查询时走哪条路径：
   - `internal_chroma` / `internal_postgres`：内置示例连接器，指向我们自己的
     本地实现，未配置真实连接器的组织落到这个默认分支。
   - `http_api`：委托到企业自己的知识库微服务，`endpoint` 是该服务的 base URL，
     `auth_config.token` 是调用时带的 Bearer token。

不负责：
- 具体怎么发起委托请求（那是 `query_knowledge_hub.py` 的事，这里只管"查连接器
  配置"这一件事）。
- 组织归属（`org_store.py` 的事）。
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import asyncpg

CAPABILITY_KNOWLEDGE_BASE = "knowledge_base"
CAPABILITY_ATTENDANCE = "attendance"

CONNECTOR_TYPE_INTERNAL_CHROMA = "internal_chroma"
CONNECTOR_TYPE_INTERNAL_POSTGRES = "internal_postgres"
CONNECTOR_TYPE_HTTP_API = "http_api"
# 考勤委托用的连接器类型（attendance-tenant-federation.md 第 2/3 节的"更轻的 HTTP
# webhook 兜底路径"）——本次只落地这一种委托方式，不实现该文档设想的 mcp_sse，
# 因为一个通用的、按租户懒加载/回收连接的 MCP client 管理器是独立的一大块基础
# 设施，投入产出比在两个 demo 租户的验证场景下不划算；HTTP webhook 已经足以
# 验证"委托路由 + 字段归一化 + 降级"这套核心机制是否成立。
CONNECTOR_TYPE_HTTP_WEBHOOK = "http_webhook"


@dataclass(frozen=True)
class TenantConnector:
    connector_id: str
    org_id: str
    capability: str
    connector_type: str
    endpoint: Optional[str]
    auth_config: Dict[str, Any] = field(default_factory=dict)
    remote_tool_name: Optional[str] = None
    field_mapping: Dict[str, Any] = field(default_factory=dict)
    is_active: bool = True
    created_at: float = 0.0


class TenantConnectorStore:
    """租户连接器存储 (PostgreSQL)。"""

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
                CREATE TABLE IF NOT EXISTS tenant_connectors (
                    id                TEXT PRIMARY KEY,
                    org_id            TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
                    capability        VARCHAR(32) NOT NULL,
                    connector_type    VARCHAR(32) NOT NULL,
                    endpoint          TEXT,
                    auth_config       JSONB NOT NULL DEFAULT '{}',
                    remote_tool_name  VARCHAR(64),
                    field_mapping     JSONB NOT NULL DEFAULT '{}',
                    is_active         BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at        DOUBLE PRECISION NOT NULL,
                    UNIQUE (org_id, capability)
                )
                """
            )

    @staticmethod
    def _row_to_connector(row: asyncpg.Record) -> TenantConnector:
        auth_config = row["auth_config"]
        field_mapping = row["field_mapping"]
        return TenantConnector(
            connector_id=row["id"],
            org_id=row["org_id"],
            capability=row["capability"],
            connector_type=row["connector_type"],
            endpoint=row["endpoint"],
            auth_config=json.loads(auth_config) if isinstance(auth_config, str) else dict(auth_config),
            remote_tool_name=row["remote_tool_name"],
            field_mapping=json.loads(field_mapping) if isinstance(field_mapping, str) else dict(field_mapping),
            is_active=row["is_active"],
            created_at=row["created_at"],
        )

    async def get(self, org_id: str, capability: str) -> Optional[TenantConnector]:
        """取某个组织在某项能力上配置的连接器；未配置或已停用返回 None。"""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, org_id, capability, connector_type, endpoint,
                       auth_config, remote_tool_name, field_mapping, is_active, created_at
                FROM tenant_connectors
                WHERE org_id = $1 AND capability = $2 AND is_active = TRUE
                """,
                org_id, capability,
            )
        return self._row_to_connector(row) if row else None

    async def list_for_org(self, org_id: str) -> List[TenantConnector]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, org_id, capability, connector_type, endpoint,
                       auth_config, remote_tool_name, field_mapping, is_active, created_at
                FROM tenant_connectors WHERE org_id = $1 ORDER BY created_at ASC
                """,
                org_id,
            )
        return [self._row_to_connector(r) for r in rows]

    async def upsert(
        self,
        org_id: str,
        capability: str,
        connector_type: str,
        endpoint: Optional[str] = None,
        auth_config: Optional[Dict[str, Any]] = None,
        remote_tool_name: Optional[str] = None,
        field_mapping: Optional[Dict[str, Any]] = None,
        is_active: bool = True,
    ) -> TenantConnector:
        """按 `(org_id, capability)` 新建或覆盖一条连接器配置（幂等，供种子脚本/管理后台用）。"""
        pool = await self._get_pool()
        connector_id = str(uuid.uuid4())
        now = time.time()
        auth_config = auth_config or {}
        field_mapping = field_mapping or {}
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO tenant_connectors
                    (id, org_id, capability, connector_type, endpoint,
                     auth_config, remote_tool_name, field_mapping, is_active, created_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                ON CONFLICT (org_id, capability) DO UPDATE SET
                    connector_type = EXCLUDED.connector_type,
                    endpoint = EXCLUDED.endpoint,
                    auth_config = EXCLUDED.auth_config,
                    remote_tool_name = EXCLUDED.remote_tool_name,
                    field_mapping = EXCLUDED.field_mapping,
                    is_active = EXCLUDED.is_active
                RETURNING id, org_id, capability, connector_type, endpoint,
                          auth_config, remote_tool_name, field_mapping, is_active, created_at
                """,
                connector_id, org_id, capability, connector_type, endpoint,
                json.dumps(auth_config), remote_tool_name, json.dumps(field_mapping), is_active, now,
            )
        return self._row_to_connector(row)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
