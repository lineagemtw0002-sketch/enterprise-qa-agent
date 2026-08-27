"""一次性迁移脚本：把 Acme / Globex 从"委托模式"（knowledge_base 连接器配成
`http_api`，查询路由到企业自己的微服务）切换成"本地存储模式"——2026-08-23
用户反馈"企业的知识库保存在本地的数据库，企业管理员能够管理知识库，就够了"，
不再需要委托这层架构，所有企业统一走平台自己的本地 Chroma（跟"测试新公司"
现在的模式一致）。

搬运规则：
1. 每个租户在 `data/tenant_demo/{tenant}/kb_corpus/{category}/` 下已有的 6 个
   分类、每类 20 篇的示例语料（之前是摄入到各自独立的、模拟"企业自己微服务"
   的 Chroma 目录 `data/tenant_demo/{tenant}/chroma/` 里，供委托查询用）——现在
   原样复用这份语料，但改摄入到平台统一的本地 Chroma（`IngestionPipeline(settings,
   collection=...)`，不传 persist_directory 覆盖，跟 `admin_create_collection` /
   `upload_collection_document` 用的是同一份 settings），这样数据不会丢，员工
   /企业管理员从今往后能在平台里直接看到、管理这些内容。
2. 每个分类建一个 `org_collections` 记录（`{tenant}_{category}_kb`，比如
   `acme_hr_admin_kb`），归属对应企业——分 6 个库而不是合并成 1 个，是保留原来
   "6 个部门类目"这个有意义的划分，企业管理员后续可以按类目把知识库分给不同
   角色（比如"人力行政知识库"只给行政部角色）。
3. 最后把 `tenant_connectors` 里这两家企业的 `knowledge_base` 连接器改成
   `internal_chroma`——改完 `_require_local_retrieval_org` 自然放行，委托相关
   的 `http_api` 逻辑不再对这两家生效。

幂等：`org_collection_store.create()` 冲突会跑 ValueError，这里捕获后跳过（说明
已经注册过）；`IngestionPipeline(..., force=True)` 本身也支持重跑不重复摄入
同一份文件（去重历史见 collection_store.py 顶部说明引用的摄入去重机制）。

用法：
    python scripts/migrate_delegated_orgs_to_local_kb.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

import asyncpg

from src.core.settings import load_settings
from src.ingestion.pipeline import IngestionPipeline
from src.ragent_backend.collection_store import OrgCollectionStore
from src.ragent_backend.tenant_connector_store import (
    TenantConnectorStore, CAPABILITY_KNOWLEDGE_BASE, CONNECTOR_TYPE_INTERNAL_CHROMA,
)

CATEGORY_DISPLAY_NAMES = {
    "hr_admin": "人力行政知识库",
    "finance": "财务知识库",
    "it_support": "IT支持知识库",
    "sales_marketing": "销售市场知识库",
    "rd_product": "研发产品知识库",
    "customer_success": "客户成功知识库",
}

TENANT_ORG_NAMES = {
    "acme": "Acme 有限公司",
    "globex": "Globex 环球集团",
}


async def get_org_id(pool: asyncpg.Pool, org_name: str) -> str:
    row = await pool.fetchrow("SELECT id FROM organizations WHERE name = $1", org_name)
    if row is None:
        raise RuntimeError(f"找不到企业: {org_name}")
    return row["id"]


async def migrate_tenant(pool: asyncpg.Pool, tenant: str, settings) -> None:
    org_name = TENANT_ORG_NAMES[tenant]
    org_id = await get_org_id(pool, org_name)
    print(f"\n{'='*60}\n迁移 {org_name} ({org_id})\n{'='*60}")

    org_collection_store = OrgCollectionStore()
    corpus_root = Path("data/tenant_demo") / tenant / "kb_corpus"

    for category, display_suffix in CATEGORY_DISPLAY_NAMES.items():
        category_dir = corpus_root / category
        files = sorted(category_dir.glob("*.txt"))
        if not files:
            print(f"  [SKIP] {category_dir} 没有语料文件")
            continue

        collection_name = f"{tenant}_{category}_kb"
        display_name = f"{org_name} {display_suffix}"

        try:
            await org_collection_store.create(
                org_id=org_id, collection_name=collection_name,
                display_name=display_name, created_by=None,
            )
            print(f"  [注册] {collection_name} -> {display_name}")
        except ValueError:
            print(f"  [已存在] {collection_name}，跳过注册")

        pipeline = IngestionPipeline(settings, collection=collection_name, force=True)
        t0 = time.monotonic()
        ok, failed = 0, 0
        for f in files:
            result = pipeline.run(str(f))
            if result.success:
                ok += 1
            else:
                failed += 1
                print(f"    [FAIL] {f}: {result.error}")
        print(f"    摄入 {len(files)} 篇 -> 成功 {ok}，失败 {failed}，耗时 {time.monotonic()-t0:.1f}s")

    connector_store = TenantConnectorStore()
    await connector_store.upsert(
        org_id=org_id, capability=CAPABILITY_KNOWLEDGE_BASE, connector_type=CONNECTOR_TYPE_INTERNAL_CHROMA,
    )
    print(f"  [连接器] {org_name} knowledge_base -> internal_chroma（本地存储）")


async def main() -> None:
    dsn = os.getenv("RAGENT_POSTGRES_URL", "postgresql://postgres:postgres@localhost:5432/ragent")
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=3)
    settings = load_settings()
    try:
        for tenant in ("acme", "globex"):
            await migrate_tenant(pool, tenant, settings)
    finally:
        await pool.close()

    print("\n迁移完成。")


if __name__ == "__main__":
    asyncio.run(main())
