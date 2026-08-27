"""账号体系阶段一的真库冒烟验证。

设计 `docs/account_lifecycle_design.md`。**这不是单元测试的替代品** ——
单测（`tests/unit/test_account_import_validation.py` / `test_activation_code.py`，
53 条）覆盖的是纯判定逻辑；这个脚本覆盖的是它们碰不到的那一半：
schema 迁移真的执行了吗、SQL 写对了吗、`password_hash` 真的可空了吗。

本仓库 `conftest.py` 没有 DB fixture，所以这些验证目前只能以脚本形式存在
（设计 §5 的方案 A "新增 postgres_conn 集成 fixture" 未做）。
落进 `scripts/` 而不是临时目录，是因为本项目已经因为
`jailbreak_test.py` / `latency_probe.py` 丢失吃过一次亏（`CLAUDE.md` §7.5）。

## 安全性

- 只创建自己的临时数据（用户名带 `__acctverify_` 前缀 + 随机后缀），
  **结束时无条件清理**，不读也不改任何既有用户。
- schema 变更全部是幂等的 `ADD COLUMN IF NOT EXISTS` / `DROP NOT NULL`，
  对存量行零影响。

    RAGENT_DEBUG=true .venv/bin/python scripts/verify_account_lifecycle.py
"""

from __future__ import annotations

import asyncio
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ragent_backend import activation  # noqa: E402
from src.ragent_backend.org_store import OrgStore  # noqa: E402
from src.ragent_backend.user_store import UserStore  # noqa: E402

PREFIX = f"__acctverify_{uuid.uuid4().hex[:8]}"
results: list[tuple[bool, str]] = []


def check(ok: bool, label: str) -> None:
    results.append((bool(ok), label))
    print(f"  {'✅' if ok else '❌'} {label}")


async def main() -> int:
    users, orgs = UserStore(), OrgStore()
    pool = await users._get_pool()
    await orgs._get_pool()  # 触发 organizations 的 schema 迁移

    created: list[str] = []
    org_id = f"{PREFIX}_org"
    try:
        print("\n[1] schema 迁移")
        async with pool.acquire() as conn:
            cols = {
                r["column_name"]: r["is_nullable"]
                for r in await conn.fetch(
                    "SELECT column_name, is_nullable FROM information_schema.columns "
                    "WHERE table_name = 'users'"
                )
            }
            for c in ("disabled_at", "activation_code_hash",
                      "activation_expires_at", "activated_at"):
                check(c in cols, f"users.{c} 已存在")
            check(cols.get("password_hash") == "YES", "users.password_hash 已可空")
            org_cols = {
                r["column_name"] for r in await conn.fetch(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'organizations'"
                )
            }
            check("seat_limit" in org_cols, "organizations.seat_limit 已存在")

            await conn.execute(
                "INSERT INTO organizations (id, name, is_platform, created_at) "
                "VALUES ($1, $2, FALSE, $3) ON CONFLICT (id) DO NOTHING",
                org_id, f"{PREFIX} 验证用企业", time.time(),
            )

        print("\n[2] 待激活账号：建号后不能登录")
        code, code_hash, expires = activation.issue_activation(time.time())
        u = await users.create_pending_user(
            username=f"{PREFIX}_alice", activation_code_hash=code_hash,
            activation_expires_at=expires, org_id=org_id,
        )
        created.append(u.user_id)
        check(await users.authenticate(f"{PREFIX}_alice", "anything") is None,
              "未激活账号用任意密码都登不上（password_hash IS NULL 不再崩）")

        print("\n[3] 激活")
        st = await users.get_activation_state(f"{PREFIX}_alice")
        check(activation.check_activation(
            submitted_code=code, stored_hash=st["activation_code_hash"],
            expires_at=st["activation_expires_at"], activated_at=st["activated_at"],
            now=time.time()).ok, "正确的码判定通过")
        check(not activation.check_activation(
            submitted_code=code + "x", stored_hash=st["activation_code_hash"],
            expires_at=st["activation_expires_at"], activated_at=st["activated_at"],
            now=time.time()).ok, "错误的码被拒")
        check(st["activation_code_hash"] != code, "库里存的不是明文码")

        check(await users.complete_activation(u.user_id, "NewPass123"), "激活成功")
        check(await users.authenticate(f"{PREFIX}_alice", "NewPass123") is not None,
              "激活后能登录")

        print("\n[4] 单次使用（这条是 SQL 层的闸，不是上层判断）")
        check(not await users.complete_activation(u.user_id, "Hijack999"),
              "同一个码第二次兑换失败（WHERE activated_at IS NULL）")
        check(await users.authenticate(f"{PREFIX}_alice", "Hijack999") is None,
              "第二次兑换没有改掉密码")
        st2 = await users.get_activation_state(f"{PREFIX}_alice")
        check(st2["activation_code_hash"] is None, "激活后码已被清掉")

        print("\n[5] 停用")
        check(await users.set_disabled(u.user_id, True), "停用成功")
        check(await users.is_disabled(u.user_id), "is_disabled 返回 True")
        check(await users.authenticate(f"{PREFIX}_alice", "NewPass123") is None,
              "停用后拿不到新 token（窗口不会因反复登录而延长）")
        check(await users.set_disabled(u.user_id, False), "重新启用成功")
        check(await users.authenticate(f"{PREFIX}_alice", "NewPass123") is not None,
              "重新启用后能登录")

        print("\n[6] 席位口径：停用的人不占席位")
        base = await users.count_active_users(org_id)
        check(base == 1, f"当前在用 {base}（期望 1）")
        await users.set_disabled(u.user_id, True)
        check(await users.count_active_users(org_id) == 0, "停用后占用降为 0")
        await users.set_disabled(u.user_id, False)

        check(await orgs.set_seat_limit(org_id, 5), "设置 seat_limit=5")
        check((await orgs.get_organization(org_id)).seat_limit == 5,
              "Organization.seat_limit 能读回来")
        check(await orgs.get_seat_limit(org_id) == 5, "get_seat_limit 一致")

        print("\n[7] 批量导入查归属必须是全平台范围")
        m = await users.get_org_ids_for_usernames([f"{PREFIX}_alice", "__不存在__"])
        check(m.get(f"{PREFIX}_alice") == org_id, "已存在的用户查得到归属")
        check("__不存在__" not in m, "不存在的用户不出现在结果里")

    finally:
        print("\n[清理]")
        async with pool.acquire() as conn:
            for uid in created:
                await conn.execute("DELETE FROM user_roles WHERE user_id = $1", uid)
                await conn.execute("DELETE FROM users WHERE id = $1", uid)
            await conn.execute("DELETE FROM organizations WHERE id = $1", org_id)
        left = 0
        async with pool.acquire() as conn:
            left = await conn.fetchval(
                "SELECT COUNT(*) FROM users WHERE username LIKE $1", f"{PREFIX}%")
        print(f"  残留临时用户：{left}（应为 0）")
        await users.close()

    passed = sum(1 for ok, _ in results if ok)
    print(f"\n{'='*56}\n{passed}/{len(results)} 通过")
    for ok, label in results:
        if not ok:
            print(f"  ❌ {label}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
