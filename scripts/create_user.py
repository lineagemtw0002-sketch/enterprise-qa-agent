"""创建用户（种子管理员账号，或给某个业务方开新账号）。

用法：
    python scripts/create_user.py --username admin --password xxxxx --collections "*" --role super_admin
    python scripts/create_user.py --username alice --password xxxxx --collections default,hr_docs

--collections 传 "*" 表示不限制（能访问所有共享 collection）；不传则该用户只能访问
自己对话私有的 conv_{id} collection，不能访问任何共享知识库。

--role 决定的是"能不能用管理后台"，跟 --collections 决定的"能看哪些知识库"是两件
独立的事：super_admin（超级管理员，能管人）、admin（管理用户）、user（普通用户，默认）。
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

from src.ragent_backend.user_store import UserStore, VALID_ROLES, ROLE_USER
from src.ragent_backend.role_store import RoleStore, ROLE_SUPER_ADMIN, ROLE_ADMIN
from src.ragent_backend.org_store import OrgStore, ORG_PLATFORM_ID


async def main() -> None:
    parser = argparse.ArgumentParser(description="创建用户")
    parser.add_argument("--username", required=True)
    parser.add_argument("--password", default=None, help="不传则交互式输入（不回显）")
    parser.add_argument(
        "--collections",
        default="",
        help="逗号分隔的允许访问的共享 collection 列表，'*' 表示不限制，留空表示只能访问自己的对话",
    )
    parser.add_argument("--role", default=ROLE_USER, choices=sorted(VALID_ROLES))
    args = parser.parse_args()

    password = args.password or getpass.getpass("密码: ")
    allowed_collections = [c.strip() for c in args.collections.split(",") if c.strip()]

    store = UserStore()
    try:
        user = await store.create_user(args.username, password, allowed_collections, role=args.role)
    except ValueError as e:
        print(f"创建失败: {e}")
        return
    finally:
        await store.close()

    # `UserStore.create_user` 的 role 参数只写老的 users.role 列，鉴权（auth.require_role /
    # /auth/me）现在只认 role_store 的 roles/user_roles 表——两边已经脱节，不在这里同步
    # 分配对应的 RBAC 角色的话，--role super_admin 建出来的账号实际登进去是零权限的
    # 普通用户。跟 app.py 里 admin_create_user 的写法保持一致：role=user（默认）不建
    # user_roles 记录，其余角色按名字查到系统角色后分配。
    if args.role != ROLE_USER:
        role_store = RoleStore()
        try:
            system_role = await role_store.get_role_by_name(args.role)
            if system_role is None:
                print(f"警告: 系统角色 '{args.role}' 不存在，管理后台权限未生效")
            else:
                await role_store.assign_user_roles(user.user_id, [system_role.role_id])
        finally:
            await role_store.close()

    # 不管 --role 是什么都要挂到一个组织下（这里没有"创建者自己所在企业"这个
    # 上下文可以继承，跟 app.py admin_create_user 不一样，统一落到平台组织）——
    # 之前这段代码只在 super_admin/admin 分支里才设置 org_id，--role user（默认、
    # 最常用）建出来的账号 org_id 一直是 NULL，query_knowledge_hub.py 这类按
    # org_id 判断归属的路径会直接把它当成"账号未关联任何企业"拒绝。历史上这个
    # 问题被"OrgStore 首次建池时会把所有 org_id IS NULL 的账号统一回填成平台
    # 组织"（org_store.py `_ensure_schema`）意外掩盖了——但那只在同一个后端进程
    # 生命周期里触发一次，进程重启之后建的第一个用户能捡到这次回填，之后建的
    # 全部落空，2026-08-22 验证知识库上传功能时踩到过。
    org_store = OrgStore()
    try:
        await org_store.set_user_organization(user.user_id, ORG_PLATFORM_ID)
    finally:
        await org_store.close()

    print(f"用户创建成功: username={user.username} user_id={user.user_id} role={user.role}")
    print(f"允许访问的共享 collection: {user.allowed_collections or '(无，仅自己的对话)'}")


if __name__ == "__main__":
    asyncio.run(main())
