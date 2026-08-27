"""
一次性迁移：把"角色和知识库混为一谈"的历史数据拆分成独立的知识库分组。

背景：改造前，凡是需要"某个人能看哪些知识库"的场景，都是靠给他分配一个
"知识库壳子角色"实现的——`all_kb`/`test_kb_role`/`product_req_kb_role`（挂了
`role_collections` 关联的本地知识库）、以及 `hr_admin_kb`/`finance_kb`/
`it_support_kb`/`sales_marketing_kb`/`rd_product_kb`/`customer_success_kb`
（委托模式企业专用，按角色名匹配远程类目，见 query_knowledge_hub.py
`DEPARTMENT_KB_GROUP_TO_REMOTE_CATEGORIES`）。这些角色除了"决定能看哪些知识库"
之外，没有任何身份/权限含义，是这次要修的架构问题的具体数据体现。

这个脚本把这 9 个角色的语义原样迁移到新的 `kb_groups`（按持有者所在企业拆分
成独立的分组行，因为知识库分组从设计上就是按企业隔离的，不像角色是全平台
共享的词表），迁移完成后删除这些角色本身（级联清掉对应的 `role_collections`/
`user_roles` 行），最后 DROP 掉不再使用的 `role_collections` 表。

可重复执行：`get_or_create_kb_group_by_name` + `add_user_kb_group` 都是幂等的；
角色一旦被删除，第二次运行时 `get_role_by_name` 会返回 None，直接跳过。

不迁移的角色：`it_dept`/`attendance_dept`/`legal_dept`/`logistics_dept` 这类
纯身份角色（没有 role_collections，也不在远程类目映射里，用于工作流审批
路由）保持不变——它们从一开始就不是"知识库壳子"，不受这次拆分影响。
"""

from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, ".")

from src.ragent_backend.role_store import RoleStore
from src.ragent_backend.kb_group_store import KbGroupStore
from src.ragent_backend.org_store import OrgStore

# 跟 query_knowledge_hub.py DEPARTMENT_KB_GROUP_TO_REMOTE_CATEGORIES 的 key 集合一致。
REMOTE_CATEGORY_ROLE_NAMES = {
    "hr_admin_kb", "finance_kb", "it_support_kb",
    "sales_marketing_kb", "rd_product_kb", "customer_success_kb",
}
# 本地检索场景下曾经挂了 role_collections 的知识库壳子角色。
LOCAL_KB_ROLE_NAMES = {"all_kb", "test_kb_role", "product_req_kb_role"}

KB_SHELL_ROLE_NAMES = REMOTE_CATEGORY_ROLE_NAMES | LOCAL_KB_ROLE_NAMES


async def _get_role_collection_names(role_store: RoleStore, role_id: str) -> list[str]:
    pool = await role_store._get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT collection_name FROM role_collections WHERE role_id = $1", role_id,
        )
    return [row["collection_name"] for row in rows]


async def main() -> None:
    role_store = RoleStore()
    kb_group_store = KbGroupStore()
    org_store = OrgStore()

    try:
        await role_store._get_pool()
        await kb_group_store._get_pool()
        await org_store._get_pool()

        for role_name in sorted(KB_SHELL_ROLE_NAMES):
            role = await role_store.get_role_by_name(role_name)
            if role is None:
                print(f"[skip] 角色 '{role_name}' 不存在（可能已迁移过），跳过")
                continue

            collection_names = await _get_role_collection_names(role_store, role.role_id)
            holder_user_ids = await role_store.get_user_ids_by_role(role.role_id)

            groups_seen: dict[str, str] = {}  # org_id -> kb_group_id，避免同一企业重复 set_kb_group_collections
            migrated = 0
            for user_id in holder_user_ids:
                org = await org_store.get_org_for_user(user_id)
                if org is None:
                    print(f"[warn] 用户 {user_id} 没有归属企业，跳过其 '{role_name}' 迁移")
                    continue

                if org.org_id not in groups_seen:
                    group = await kb_group_store.get_or_create_kb_group_by_name(
                        org.org_id, role.name, role.display_name,
                    )
                    if collection_names:
                        await kb_group_store.set_kb_group_collections(group.kb_group_id, collection_names)
                    groups_seen[org.org_id] = group.kb_group_id

                await kb_group_store.add_user_kb_group(user_id, groups_seen[org.org_id])
                migrated += 1

            # 用原生 SQL 删除而不是 role_store.delete_role：`all_kb` 当初是带
            # `is_system=TRUE` 种子写入的（见老版 scripts/migrate_to_roles.py），
            # delete_role 会因为这个保护拒绝——那层保护是给管理后台 API 用的，
            # 防止管理员手滑删掉内置系统角色，这里是一次性架构迁移，不适用。
            pool = await role_store._get_pool()
            async with pool.acquire() as conn:
                await conn.execute("DELETE FROM roles WHERE id = $1", role.role_id)
            print(
                f"[done] 角色 '{role_name}' -> {len(groups_seen)} 个企业的知识库分组，"
                f"迁移 {migrated} 条用户成员关系，原角色已删除"
            )

        # role_collections 表不再被 role_store.py 管理，迁移完成后清理掉。
        pool = await role_store._get_pool()
        async with pool.acquire() as conn:
            await conn.execute("DROP TABLE IF EXISTS role_collections")
        print("[done] 已 DROP role_collections 表")

    finally:
        await role_store.close()
        await kb_group_store.close()
        await org_store.close()


if __name__ == "__main__":
    asyncio.run(main())
