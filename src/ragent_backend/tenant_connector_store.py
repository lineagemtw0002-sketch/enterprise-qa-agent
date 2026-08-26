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

安全（2026-08-26 P0 修复）：
- `auth_config` 落库前会被加密，密钥/存储格式/新旧数据兼容策略见
  `connector_crypto.py` 模块 docstring。存量明文数据的一次性迁移见
  `scripts/migrate_connector_auth_config_encryption.py`。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import asyncpg

from src.ragent_backend.connector_crypto import (
    build_fernet,
    decrypt_auth_config,
    encrypt_auth_config,
)

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
    # 运行时调用指标（网关监控页用，见 app.py 的 /api/v1/admin/gateway/connectors）——
    # 每次委托请求结束后由调用方（query_knowledge_hub._execute_remote /
    # builtin_tools._register_query_attendance）调用 record_call() 累加，不是配置的
    # 一部分，只是恰好也存在这张表里（避免为了 4 个计数器单开一张表）。
    call_count: int = 0
    failure_count: int = 0
    last_called_at: Optional[float] = None
    last_latency_ms: Optional[float] = None
    last_error: Optional[str] = None


class TenantConnectorStore:
    """租户连接器存储 (PostgreSQL)。"""

    # 类级别共享连接池，见 store.py 同名字段的注释——调用方经常每次都 new 一个
    # 新实例，池必须挂在类属性上才不会被重复创建、打满 Postgres 连接数。
    _pool: Optional[asyncpg.Pool] = None
    _pool_lock = asyncio.Lock()

    def __init__(self, encryption_key: Optional[str] = None) -> None:
        self._dsn = os.getenv("RAGENT_POSTGRES_URL", "postgresql://postgres:postgres@localhost:5432/ragent")
        # 密钥缺失/不安全在这里就会 raise RuntimeError 拒绝构造——`app.py::create_app`
        # 第一批语句里就 `TenantConnectorStore()`，而 `create_app()` 是进程启动时
        # 调一次的入口，效果等价于"进程启动即 fail-fast"，跟 `auth.py` 的
        # `resolve_jwt_secret` 是同一种模式，只是触发时机是"启动时构造这个 store"
        # 而不是"导入模块"。`encryption_key` 参数仅供单测注入，生产路径一律走
        # 环境变量 `RAGENT_CONNECTOR_ENCRYPTION_KEY`。
        self._fernet = build_fernet(key=encryption_key)

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
            for ddl in (
                "ALTER TABLE tenant_connectors ADD COLUMN IF NOT EXISTS call_count BIGINT NOT NULL DEFAULT 0",
                "ALTER TABLE tenant_connectors ADD COLUMN IF NOT EXISTS failure_count BIGINT NOT NULL DEFAULT 0",
                "ALTER TABLE tenant_connectors ADD COLUMN IF NOT EXISTS last_called_at DOUBLE PRECISION",
                "ALTER TABLE tenant_connectors ADD COLUMN IF NOT EXISTS last_latency_ms DOUBLE PRECISION",
                "ALTER TABLE tenant_connectors ADD COLUMN IF NOT EXISTS last_error TEXT",
            ):
                await conn.execute(ddl)

    _COLUMNS = (
        "id, org_id, capability, connector_type, endpoint, auth_config, remote_tool_name, "
        "field_mapping, is_active, created_at, call_count, failure_count, last_called_at, "
        "last_latency_ms, last_error"
    )

    def _row_to_connector(self, row: asyncpg.Record) -> TenantConnector:
        auth_config_raw = row["auth_config"]
        auth_config_stored = (
            json.loads(auth_config_raw) if isinstance(auth_config_raw, str) else dict(auth_config_raw)
        )
        # decrypt_auth_config 自己会分流：新数据是密文包装就解密，存量明文数据
        # （迁移脚本跑之前）原样透传——见 connector_crypto.py 模块 docstring。
        auth_config = decrypt_auth_config(auth_config_stored, self._fernet)
        field_mapping = row["field_mapping"]
        return TenantConnector(
            connector_id=row["id"],
            org_id=row["org_id"],
            capability=row["capability"],
            connector_type=row["connector_type"],
            endpoint=row["endpoint"],
            auth_config=auth_config,
            remote_tool_name=row["remote_tool_name"],
            field_mapping=json.loads(field_mapping) if isinstance(field_mapping, str) else dict(field_mapping),
            is_active=row["is_active"],
            created_at=row["created_at"],
            call_count=row["call_count"],
            failure_count=row["failure_count"],
            last_called_at=row["last_called_at"],
            last_latency_ms=row["last_latency_ms"],
            last_error=row["last_error"],
        )

    async def get(self, org_id: str, capability: str) -> Optional[TenantConnector]:
        """取某个组织在某项能力上配置的连接器；未配置或已停用返回 None。"""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT {self._COLUMNS} FROM tenant_connectors "
                "WHERE org_id = $1 AND capability = $2 AND is_active = TRUE",
                org_id, capability,
            )
        return self._row_to_connector(row) if row else None

    async def list_for_org(self, org_id: str) -> List[TenantConnector]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT {self._COLUMNS} FROM tenant_connectors WHERE org_id = $1 ORDER BY created_at ASC",
                org_id,
            )
        return [self._row_to_connector(r) for r in rows]

    async def list_all(self) -> List[TenantConnector]:
        """跨所有组织列出全部连接器——网关监控页用（`admin_gateway_connectors`）。"""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(f"SELECT {self._COLUMNS} FROM tenant_connectors ORDER BY org_id, capability")
        return [self._row_to_connector(r) for r in rows]

    async def record_call(
        self, connector_id: str, success: bool, latency_ms: Optional[float], error: Optional[str] = None,
    ) -> None:
        """委托请求结束后记一笔调用——网关监控页的调用/失败计数来源。失败也要记
        （`failure_count` 才有意义），所以调用方在每一条退出路径（成功/超时/鉴权
        失败/5xx）都要调这个方法，不能只在成功路径调。"""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE tenant_connectors SET
                    call_count = call_count + 1,
                    failure_count = failure_count + CASE WHEN $2 THEN 0 ELSE 1 END,
                    last_called_at = $3,
                    last_latency_ms = $4,
                    last_error = $5
                WHERE id = $1
                """,
                connector_id, success, time.time(), latency_ms, (None if success else error),
            )

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
        # 落库前一律加密——调用方（app.py 的管理端点、种子脚本）传进来的还是明文
        # dict，这里是唯一的加密点，跟"唯一的解密点在 _row_to_connector"对称。
        encrypted_auth_config = encrypt_auth_config(auth_config, self._fernet)
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
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
                RETURNING {self._COLUMNS}
                """,
                connector_id, org_id, capability, connector_type, endpoint,
                json.dumps(encrypted_auth_config), remote_tool_name, json.dumps(field_mapping), is_active, now,
            )
        return self._row_to_connector(row)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            type(self)._pool = None
