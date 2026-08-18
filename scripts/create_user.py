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

    print(f"用户创建成功: username={user.username} user_id={user.user_id} role={user.role}")
    print(f"允许访问的共享 collection: {user.allowed_collections or '(无，仅自己的对话)'}")


if __name__ == "__main__":
    asyncio.run(main())
