"""角色系统迁移脚本 —— role.md 第 6 节「迁移计划」阶段一。

把 users 表里逐用户维护的 role / allowed_collections 两个老字段，回填成新的
角色表（roles / role_collections / user_roles）。老列本身不删、不改，只读不写，
回滚只需要不切读路径（本脚本执行前后，get_allowed_collections 的读路径已经在
代码层面切到了 RoleStore，见 user_store.py），所以这一步本身是安全的、可重复
执行的（幂等）。

用法：
    python scripts/migrate_to_roles.py            # 执行回填 + 校验
    python scripts/migrate_to_roles.py --dry-run   # 只跑校验 diff，不写库

处理规则（对应 role.md 3. 迁移计划 步骤 3）：
    - 每个用户按 users.role 的值，关联到对应的系统角色（super_admin/admin/user，
      这三个角色已经由 RoleStore._ensure_schema 作为种子数据建好）。
    - 每个用户按 users.allowed_collections：
        - 含 "*" → 关联到系统角色 "all_kb"（不存在则新建，is_system=True，
          role_collections 存 "*"）。
        - 其余每个 collection：命中已知部门 slug（it_kb/attendance_kb/
          logistics_kb/legal_kb）就建对应的部门角色；否则以 collection 名本身
          建一个同名角色（role.md 3. 步骤 3 的兜底规则）。
    - 最后对每个用户，用新表 JOIN 算出的并集与老的 allowed_collections 做 diff，
      不一致的只记日志告警，不阻断执行，供人工复核。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

from src.ragent_backend.user_store import UserStore
from src.ragent_backend.role_store import RoleStore, ROLE_SUPER_ADMIN, ROLE_ADMIN, ROLE_USER

ALL_KB_ROLE_NAME = "all_kb"
ALL_KB_ROLE_DISPLAY = "全部知识库"
WILDCARD = "*"

# collection slug -> (角色内部名, 展示名)。命中的走部门角色；没命中的以 collection
# 名本身建同名角色（见模块 docstring 的兜底规则）。
DEPARTMENT_ROLE_MAP = {
    "it_kb": ("it_dept", "IT部"),
    "attendance_kb": ("attendance_dept", "考勤部"),
    "logistics_kb": ("logistics_dept", "后勤部"),
    "legal_kb": ("legal_dept", "法务部"),
}

SYSTEM_ROLE_NAMES = {ROLE_SUPER_ADMIN, ROLE_ADMIN, ROLE_USER}


async def _role_for_collection(role_store: RoleStore, collection_name: str):
    if collection_name in DEPARTMENT_ROLE_MAP:
        name, display_name = DEPARTMENT_ROLE_MAP[collection_name]
    else:
        name, display_name = collection_name, collection_name
    role = await role_store.get_or_create_role_by_name(name, display_name)
    await role_store.add_role_collection(role.role_id, collection_name)
    return role


async def migrate(dry_run: bool = False) -> None:
    user_store = UserStore()
    role_store = RoleStore()
    try:
        # 触发 _ensure_schema：建三张新表 + 种子写入 super_admin/admin/user
        await role_store._get_pool()

        users = await user_store.list_users()
        print(f"共 {len(users)} 个用户，开始回填角色关联...")

        mismatches = []
        for user in users:
            if dry_run:
                print(f"  [dry-run] {user.username}: role={user.role} allowed_collections={user.allowed_collections}")
            else:
                # 1. 按系统权限档位关联系统角色
                system_role = await role_store.get_role_by_name(user.role)
                if system_role is None:
                    print(f"  [WARN] 用户 {user.username} 的 role={user.role!r} 不是合法系统角色，跳过")
                else:
                    await role_store.add_user_role(user.user_id, system_role.role_id)

                # 2. 按 allowed_collections 关联知识库角色
                if WILDCARD in user.allowed_collections:
                    all_kb_role = await role_store.get_or_create_role_by_name(
                        ALL_KB_ROLE_NAME, ALL_KB_ROLE_DISPLAY, is_system=True
                    )
                    await role_store.add_role_collection(all_kb_role.role_id, WILDCARD)
                    await role_store.add_user_role(user.user_id, all_kb_role.role_id)
                else:
                    for collection_name in user.allowed_collections:
                        role = await _role_for_collection(role_store, collection_name)
                        await role_store.add_user_role(user.user_id, role.role_id)

            # 3. 校验：新表并集 vs 老字段，不一致只记日志，不阻断
            new_allowed = set(await role_store.get_allowed_collections_for_user(user.user_id))
            old_allowed = set(user.allowed_collections)
            if WILDCARD in old_allowed:
                old_allowed = {WILDCARD}
            if not dry_run and new_allowed != old_allowed:
                mismatches.append((user.username, sorted(old_allowed), sorted(new_allowed)))

        print(f"\n迁移{'（dry-run，未写库）' if dry_run else ''}完成。")
        if mismatches:
            print(f"\n[WARN] {len(mismatches)} 个用户新旧口径不一致，请人工复核：")
            for username, old, new in mismatches:
                print(f"  - {username}: 老口径={old} 新口径={new}")
        elif not dry_run:
            print("校验通过：所有用户新旧口径一致。")
    finally:
        await user_store.close()
        await role_store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="角色系统迁移：回填 roles/role_collections/user_roles")
    parser.add_argument("--dry-run", action="store_true", help="只打印将要处理的数据，不写库、不校验")
    args = parser.parse_args()
    asyncio.run(migrate(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
