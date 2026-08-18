"""管理员操作：重设某个已有用户能访问哪些共享 collection。

用法：
    python scripts/set_permissions.py --username alice --collections it_kb,attendance_kb,logistics_kb
    python scripts/set_permissions.py --username admin --collections "*"
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


async def main() -> None:
    parser = argparse.ArgumentParser(description="重设用户的 collection 访问权限")
    parser.add_argument("--username", required=True)
    parser.add_argument(
        "--collections",
        required=True,
        help="逗号分隔的 collection 列表，'*' 表示不限制，空字符串表示只能访问自己的对话",
    )
    args = parser.parse_args()

    allowed = [c.strip() for c in args.collections.split(",") if c.strip()]

    store = UserStore()
    try:
        found = await store.set_allowed_collections(args.username, allowed)
    finally:
        await store.close()

    if not found:
        print(f"用户不存在: {args.username}")
        return

    print(f"已更新 {args.username} 的权限: {allowed or '(无，仅自己的对话)'}")


if __name__ == "__main__":
    asyncio.run(main())
