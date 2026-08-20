"""
企业自建知识库存储 (Org Collection Store) — PostgreSQL 版

职责：
1. 记录"哪个 collection 归属哪家企业"——`role_collections`（role_store.py）只存
   role_id <-> collection_name 的关联，从不知道一个 collection 本身归属哪家企业；
   在这张表之前，`GET /admin/collections` 只能不加区分地列出 ChromaDB 里现存的
   全部 collection（见 app.py `admin_list_collections` 改造前的实现），任何一家
   企业的管理员都能看到、并把其他企业甚至平台自己的部门知识库配置给自己企业的
   角色——这张表就是补上这层"归属"信息，让"新增知识库"和"知识库权限"两个页面
   都能按企业过滤。
2. 只登记"这个 collection 存在、归哪家企业、叫什么展示名"，不做真正的物理创建——
   物理上的 ChromaDB collection 由后续摄入文档时自然创建（`VectorStoreFactory`
   走 Chroma 的 `get_or_create_collection` 语义，摄入前查询会稳定返回空结果，不
   报错，见 `query_knowledge_hub.py` `_execute_local_multi` 对空结果的处理）——
   跟"先注册身份证号、人可以晚一点再实际入职"是同一个道理，不需要为了列个名字
   就去初始化一个真正的向量库连接。

不负责：
- 6 个固定部门知识库（`hr_admin_kb` 等）——那是平台自己组织（org_platform）的
  专属概念，硬编码在 `query_knowledge_hub.py` 的 `DEPARTMENT_KB_COLLECTIONS`
  里，不登记进这张表（见该常量旁的说明）。
- 委托模式企业（Acme/Globex 这类，`tenant_connectors` 里配了 `http_api` 连接器）
  的"知识库"——那些库物理上在企业自己的微服务里，平台这边创建/管理任何本地
  collection 对它们都没有意义（`query_knowledge_hub.py` 的 `is_remote` 分支
  完全绕开本地检索），app.py 的端点在企业是委托模式时会直接拒绝创建/列出，
  不会把误导性的空列表当成"这家企业还没建库"。
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from typing import List, Optional

import asyncpg

# 跟 query_knowledge_hub.py 的 DEPARTMENT_KB_COLLECTIONS 保持独立引用（不在这里
# import 那个模块——那边反过来会 import 本模块判断 org 归属，避免循环导入），
# 校验时直接从调用方（app.py）传入完整的保留名集合。
_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{2,63}$")


def is_valid_collection_name(name: str) -> bool:
    """内部标识合法性：小写字母开头，只能有小写字母/数字/下划线，3-64 字符——
    跟 role 的"内部标识"字段用同一套朴素规则（RoleManagement.jsx 的展示名/
    内部标识两栏设计），保证后续能安全地当 ChromaDB collection 名和 BM25
    索引目录名用（`BM25Indexer(index_dir=f"data/db/bm25/{collection}")`，
    不做这层校验的话，用户输入里的 `/`、`..` 之类字符会变成路径穿越）。
    """
    return bool(_NAME_PATTERN.match(name))


@dataclass(frozen=True)
class OrgCollection:
    collection_name: str
    org_id: str
    display_name: str
    created_at: float
    created_by: Optional[str]


class OrgCollectionStore:
    """企业自建知识库归属存储 (PostgreSQL)。"""

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
                CREATE TABLE IF NOT EXISTS org_collections (
                    collection_name  TEXT PRIMARY KEY,
                    org_id           TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
                    display_name     VARCHAR(128) NOT NULL,
                    created_at       DOUBLE PRECISION NOT NULL,
                    created_by       TEXT
                )
                """
            )

    @staticmethod
    def _row_to_org_collection(row: asyncpg.Record) -> OrgCollection:
        return OrgCollection(
            collection_name=row["collection_name"],
            org_id=row["org_id"],
            display_name=row["display_name"],
            created_at=row["created_at"],
            created_by=row["created_by"],
        )

    async def list_for_org(self, org_id: str) -> List[OrgCollection]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT collection_name, org_id, display_name, created_at, created_by "
                "FROM org_collections WHERE org_id = $1 ORDER BY created_at ASC",
                org_id,
            )
        return [self._row_to_org_collection(r) for r in rows]

    async def get(self, collection_name: str) -> Optional[OrgCollection]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT collection_name, org_id, display_name, created_at, created_by "
                "FROM org_collections WHERE collection_name = $1",
                collection_name,
            )
        return self._row_to_org_collection(row) if row else None

    async def create(
        self, org_id: str, collection_name: str, display_name: str, created_by: Optional[str] = None,
    ) -> OrgCollection:
        """登记一个新知识库。`collection_name` 全平台唯一（PRIMARY KEY），冲突
        （包括撞到别的企业已经注册过的名字）统一报 ValueError，调用方（app.py）
        转成 400——不区分"名字被自己占用"还是"被别的企业占用"，两种情况都不该
        告诉调用方对方内部标识是否存在，避免探测出别的企业注册过哪些名字。"""
        if not is_valid_collection_name(collection_name):
            raise ValueError("知识库内部标识只能是小写字母、数字、下划线，且以字母开头（3-64 位）")

        pool = await self._get_pool()
        now = time.time()
        async with pool.acquire() as conn:
            try:
                await conn.execute(
                    """INSERT INTO org_collections (collection_name, org_id, display_name, created_at, created_by)
                       VALUES ($1, $2, $3, $4, $5)""",
                    collection_name, org_id, display_name, now, created_by,
                )
            except asyncpg.UniqueViolationError as e:
                raise ValueError(f"知识库标识 '{collection_name}' 已被占用，换一个试试") from e
        return OrgCollection(
            collection_name=collection_name, org_id=org_id, display_name=display_name,
            created_at=now, created_by=created_by,
        )

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
