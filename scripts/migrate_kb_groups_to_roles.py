"""一次性迁移脚本：把 2026-08-22 拆出去的「知识库分组」(kb_groups /
kb_group_collections / user_kb_groups) 合回「角色」系统(roles / role_collections
/ user_roles)——2026-08-23 用户反馈"身份和角色其实是同一个概念"，角色重新
变成"直接携带知识库权限"的单一实体（见 role_store.py 顶部说明），kb_group_store.py
已经删除，这几张老表的代码入口没了，但数据还在库里，需要搬到新结构上。

搬运规则：
1. 每个 kb_group -> 一个新的企业角色（`roles.org_id` = kb_group 的 org_id），
   名字/展示名原样保留；kb_group_collections -> role_collections（按同一个
   org_id 落地）。
2. 每个用户当前的角色分配（业务规则改成"一人一角色"之后）：
   - 如果这个用户持有系统层级角色（super_admin/admin/org_admin）——不动，
     忽略他名下的 kb_group（运营商角色没有知识库权限是天然规则；org_admin
     本来就隐式拥有企业内全部知识库，kb_group 对他是冗余信息）。
   - 否则，如果这个用户有 kb_group（可能历史遗留不止一个，取 created_at
     最早的一个，其余打印警告丢弃），改用 kb_group 迁移出来的新角色替换掉
     他原来的角色（如果原来的角色本身没有任何知识库关联，说明就是个空身份，
     替换不丢有意义的信息；如果原来的角色恰好也配了知识库关联，说明数据本身
     就有歧义，一并打印警告，人工复核）。
   - 否则（没有 kb_group）：不动。

这是一次性、可重复执行的脚本（用 get_or_create_role_by_name + INSERT ON
CONFLICT，重跑不会重复建角色/重复关联），跑完之后打印一份汇总报告。

用法：
    python scripts/migrate_kb_groups_to_roles.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

import asyncpg

from src.ragent_backend.role_store import RoleStore, ROLE_SUPER_ADMIN, ROLE_ORG_ADMIN


# 历史快照：这个脚本迁移时系统层级角色还包含 "admin"，2026-08-24 起平台侧
# 已废弃该角色（role_store 不再导出对应常量，见 role_store.py 顶部说明），
# 这里直接写死字符串保留脚本描述的历史状态，不受影响。
SYSTEM_TIER_NAMES = {ROLE_SUPER_ADMIN, "admin", ROLE_ORG_ADMIN}


async def main() -> None:
    dsn = os.getenv("RAGENT_POSTGRES_URL", "postgresql://postgres:postgres@localhost:5432/ragent")
    conn = await asyncpg.connect(dsn)
    role_store = RoleStore()

    tables = await conn.fetch(
        "SELECT table_name FROM information_schema.tables WHERE table_name IN "
        "('kb_groups', 'kb_group_collections', 'user_kb_groups')"
    )
    if len(tables) < 3:
        print("kb_groups 相关的老表已经不存在，没有需要迁移的数据，直接退出。")
        await conn.close()
        await role_store.close()
        return

    kb_groups = await conn.fetch("SELECT id, org_id, name, display_name, created_at FROM kb_groups")
    group_to_role_id: dict[str, str] = {}
    print(f"共 {len(kb_groups)} 个知识库分组，开始迁移成角色……")
    for g in kb_groups:
        role = await role_store.get_or_create_role_by_name(
            g["name"], g["display_name"], is_system=False, org_id=g["org_id"],
        )
        group_to_role_id[g["id"]] = role.role_id
        print(f"  分组 [{g['name']}] (org={g['org_id']}) -> 角色 {role.role_id}")

    collection_rows = await conn.fetch("SELECT kb_group_id, collection_name FROM kb_group_collections")
    collections_by_group: dict[str, list[str]] = {}
    for row in collection_rows:
        collections_by_group.setdefault(row["kb_group_id"], []).append(row["collection_name"])
    for group_id, collection_names in collections_by_group.items():
        role_id = group_to_role_id.get(group_id)
        if role_id is None:
            continue
        org_id = next(g["org_id"] for g in kb_groups if g["id"] == group_id)
        await role_store.set_role_collections(role_id, org_id, collection_names)
        print(f"  角色 {role_id} 关联知识库：{collection_names}")

    user_group_rows = await conn.fetch(
        "SELECT ug.user_id, ug.kb_group_id, g.created_at FROM user_kb_groups ug "
        "JOIN kb_groups g ON g.id = ug.kb_group_id ORDER BY g.created_at ASC"
    )
    groups_by_user: dict[str, list[str]] = {}
    for row in user_group_rows:
        groups_by_user.setdefault(row["user_id"], []).append(row["kb_group_id"])

    skipped_system_tier = 0
    replaced = 0
    dropped_extra = 0
    for user_id, kb_group_ids in groups_by_user.items():
        current_roles = await role_store.get_user_roles(user_id)
        current_role_names = {r.name for r in current_roles}
        if current_role_names & SYSTEM_TIER_NAMES:
            print(
                f"  用户 {user_id} 持有系统层级角色 {current_role_names & SYSTEM_TIER_NAMES}，"
                f"忽略他名下的知识库分组 {kb_group_ids}（不动）"
            )
            skipped_system_tier += 1
            continue

        primary_group_id = kb_group_ids[0]
        new_role_id = group_to_role_id.get(primary_group_id)
        if new_role_id is None:
            continue
        if len(kb_group_ids) > 1:
            print(f"  用户 {user_id} 历史遗留多个知识库分组 {kb_group_ids}，只保留第一个，其余丢弃（一人一角色）")
            dropped_extra += len(kb_group_ids) - 1

        if current_roles and current_roles[0].role_id != new_role_id:
            print(f"  用户 {user_id} 原角色 {current_roles[0].name} 被替换成迁移出来的角色 {new_role_id}")
        await role_store.assign_user_roles(user_id, [new_role_id])
        replaced += 1

    print(
        f"\n迁移完成：{len(kb_groups)} 个分组 -> 角色，{replaced} 个用户角色分配已更新，"
        f"{skipped_system_tier} 个系统层级用户被跳过，{dropped_extra} 个多余的历史分组被丢弃。"
    )
    print("老表 kb_groups / kb_group_collections / user_kb_groups 数据保留未删除，确认无误后可以手动清理。")

    await conn.close()
    await role_store.close()


if __name__ == "__main__":
    asyncio.run(main())
