"""模拟两家企业（Acme / Globex），验证知识库多租户联邦查询真的按组织路由、不会串号。

对应设计：`knowledge-base-tenant-federation.md` 第 6 节。

做的事（幂等，可重复执行）：
1. 创建 2 个组织：Acme 有限公司 / Globex 环球集团（`is_platform=FALSE`）。
2. 各写入 1 条 `tenant_connectors`（capability=knowledge_base, connector_type=http_api），
   指向本地跑的两个模拟知识库微服务端口（9101 / 9102）。
3. 各创建 2 个测试用户，`org_id` 指向对应企业。
4. 把 `data/tenant_demo/{acme,globex}/*.txt` 摄入各自独立的 Chroma 持久化目录 +
   租户专属 collection（`tenant_{name}_kb`），供两个模拟微服务查询用。
5. 给这些新用户造 2026 年内的考勤打卡数据（复用 `seed_attendance_data.py` 的生成逻辑），
   这样 `query_attendance` 工具对这两家公司的员工也立刻有数据可查。

用法：
    python scripts/seed_tenant_kb_demo.py

然后照打印出来的命令，分别起两个模拟知识库微服务进程：
    TENANT_NAME=acme   TENANT_ORG_ID=<...> TENANT_TOKEN=acme-demo-token-2026   \\
        TENANT_DATA_DIR=data/tenant_demo/acme   uvicorn services.tenant_kb_demo.app:app --port 9101
    TENANT_NAME=globex TENANT_ORG_ID=<...> TENANT_TOKEN=globex-demo-token-2026 \\
        TENANT_DATA_DIR=data/tenant_demo/globex uvicorn services.tenant_kb_demo.app:app --port 9102
"""

from __future__ import annotations

import asyncio
import random
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

from src.core.settings import load_settings
from src.ingestion.pipeline import IngestionPipeline
from src.ragent_backend.attendance_store import AttendanceStore
from src.ragent_backend.org_store import OrgStore
from src.ragent_backend.tenant_connector_store import (
    CAPABILITY_KNOWLEDGE_BASE,
    CONNECTOR_TYPE_HTTP_API,
    TenantConnectorStore,
)
from src.ragent_backend.user_store import UserStore
from scripts.seed_attendance_data import _generate_day

# 2026 年内——今天是 2026-08-19（见 plan.md 需求"数据限 2026"）。1 月 1 日到今天。
ATTENDANCE_START = date(2026, 1, 1)
ATTENDANCE_END = date(2026, 8, 19)

TENANTS = [
    {
        "org_name": "Acme 有限公司",
        "tenant_name": "acme",
        "port": 9101,
        "token": "acme-demo-token-2026",
        "doc": "data/tenant_demo/acme/it_reimbursement.txt",
        "users": [("alice_acme", "IT 部门"), ("bob_acme", "财务部门")],
    },
    {
        "org_name": "Globex 环球集团",
        "tenant_name": "globex",
        "port": 9102,
        "token": "globex-demo-token-2026",
        "doc": "data/tenant_demo/globex/travel_reimbursement.txt",
        "users": [("carol_globex", "销售部门"), ("dave_globex", "行政部门")],
    },
]

DEMO_PASSWORD = "Demo@2026"


async def _get_or_create_org(org_store: OrgStore, name: str):
    for org in await org_store.list_organizations():
        if org.name == name:
            return org
    return await org_store.create_organization(name)


async def _get_or_create_user(user_store: UserStore, username: str):
    for user in await user_store.list_users():
        if user.username == username:
            return user
    return await user_store.create_user(username, DEMO_PASSWORD, allowed_collections=[])


async def _seed_attendance_for_user_async(store: AttendanceStore, rng: random.Random, user_id: str) -> int:
    count = 0
    d = ATTENDANCE_START
    while d <= ATTENDANCE_END:
        if d.weekday() < 5:
            day = _generate_day(rng, d)
            await store.upsert_record(
                user_id=user_id,
                work_date=d,
                check_in_at=day["check_in_at"],
                check_out_at=day["check_out_at"],
                status=day["status"],
                late_minutes=day["late_minutes"],
                early_leave_minutes=day["early_leave_minutes"],
            )
            count += 1
        d += timedelta(days=1)
    return count


def _ingest_tenant_kb(tenant_name: str, doc_path: str) -> None:
    settings = load_settings()
    tenant_settings = settings.model_copy(update={
        "vector_store": settings.vector_store.model_copy(update={
            "persist_directory": f"data/tenant_demo/{tenant_name}/chroma",
        }),
    })
    collection = f"tenant_{tenant_name}_kb"
    print(f"\n{'='*60}\nIngesting {doc_path} -> collection={collection} (persist_directory=data/tenant_demo/{tenant_name}/chroma)\n{'='*60}")
    pipeline = IngestionPipeline(tenant_settings, collection=collection, force=True)
    result = pipeline.run(doc_path)
    if result.success:
        print(f"  [OK] {result.chunk_count} chunks, {len(result.vector_ids)} vectors")
    else:
        print(f"  [FAIL] {result.error}")


async def main() -> None:
    org_store = OrgStore()
    connector_store = TenantConnectorStore()
    user_store = UserStore()
    attendance_store = AttendanceStore()
    rng = random.Random(2026)

    try:
        for tenant in TENANTS:
            org = await _get_or_create_org(org_store, tenant["org_name"])
            print(f"[OK] 组织: {org.name} (org_id={org.org_id})")

            connector = await connector_store.upsert(
                org_id=org.org_id,
                capability=CAPABILITY_KNOWLEDGE_BASE,
                connector_type=CONNECTOR_TYPE_HTTP_API,
                endpoint=f"http://localhost:{tenant['port']}",
                auth_config={"token": tenant["token"]},
            )
            print(f"[OK] 知识库连接器: {connector.endpoint} (org_id={connector.org_id})")

            for username, dept in tenant["users"]:
                user = await _get_or_create_user(user_store, username)
                await org_store.set_user_organization(user.user_id, org.org_id)
                n = await _seed_attendance_for_user_async(attendance_store, rng, user.user_id)
                print(f"[OK] 用户: {username} ({dept}) user_id={user.user_id} -> {n} 条 2026 年考勤记录")

            _ingest_tenant_kb(tenant["tenant_name"], tenant["doc"])

        print("\n" + "=" * 60)
        print("接下来分别起两个模拟知识库微服务进程：")
        print("=" * 60)
        for tenant in TENANTS:
            org = await _get_or_create_org(org_store, tenant["org_name"])
            print(
                f"\nTENANT_NAME={tenant['tenant_name']} TENANT_ORG_ID={org.org_id} "
                f"TENANT_TOKEN={tenant['token']} TENANT_DATA_DIR=data/tenant_demo/{tenant['tenant_name']} \\\n"
                f"    uvicorn services.tenant_kb_demo.app:app --port {tenant['port']}"
            )
        print(f"\n所有测试账号密码统一为: {DEMO_PASSWORD}")

    finally:
        await org_store.close()
        await connector_store.close()
        await user_store.close()
        await attendance_store.close()


if __name__ == "__main__":
    asyncio.run(main())
