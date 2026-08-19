"""一次性把所有账号的密码统一重置为 "{用户名}123"（方便本地测试记忆，不是给
生产环境用的）。

直接操作 users 表（不经过 UserStore.change_password，那个要求提供旧密码），
用跟 user_store.py 完全一致的 bcrypt 哈希方式写回，登录校验逻辑不用改。

用法：
    python scripts/reset_all_passwords.py
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
import bcrypt

DSN = os.getenv("RAGENT_POSTGRES_URL", "postgresql://postgres:postgres@localhost:5432/ragent")


async def main() -> None:
    conn = await asyncpg.connect(DSN)
    try:
        users = await conn.fetch("SELECT id, username FROM users ORDER BY created_at")
        if not users:
            print("[SKIP] users 表为空")
            return

        print(f"{'用户名':<20}{'新密码'}")
        print("-" * 40)
        for u in users:
            new_password = f"{u['username']}123"
            password_hash = bcrypt.hashpw(new_password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
            await conn.execute("UPDATE users SET password_hash = $1 WHERE id = $2", password_hash, u["id"])
            print(f"{u['username']:<20}{new_password}")

        print(f"\n共重置 {len(users)} 个账号的密码。")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
