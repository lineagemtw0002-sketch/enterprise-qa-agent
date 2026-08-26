"""账号体系阶段一的 HTTP 端到端验证。

`scripts/verify_account_lifecycle.py` 验的是 store 层；这个脚本验的是
**端点真的调了那些规则吗** —— 纯函数层证明不了这件事
（`docs/account_lifecycle_design.md` §5 的原话）。

    RAGENT_BASE=http://localhost:8011 .venv/bin/python scripts/verify_account_endpoints_e2e.py

⚠️ **默认指向 8011 而不是 8010。** 8010 上常有另一个会话在跑的实例，
这个脚本会建号、导入、停用，不该打到别人正在用的进程上。

## 安全性

只创建带随机前缀（`__e2e_<hex>`）的企业与用户，结束时无条件清理；
**不读也不改任何既有用户**，尤其不重置任何人的密码。
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
import uuid
from pathlib import Path

import bcrypt
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ragent_backend.org_store import OrgStore  # noqa: E402
from src.ragent_backend.user_store import UserStore  # noqa: E402

BASE = os.getenv("RAGENT_BASE", "http://localhost:8011").rstrip("/")
API = f"{BASE}/api/v1"
TAG = f"__e2e_{uuid.uuid4().hex[:8]}"
ADMIN_PW = "E2eAdminPw!2026"

results: list[tuple[bool, str]] = []


def check(ok: bool, label: str, extra: str = "") -> None:
    results.append((bool(ok), label))
    print(f"  {'✅' if ok else '❌'} {label}{(' — ' + extra) if extra and not ok else ''}")


async def main() -> int:
    users, orgs = UserStore(), OrgStore()
    pool = await users._get_pool()
    await orgs._get_pool()

    org_id = f"{TAG}_org"
    admin_id = str(uuid.uuid4())
    try:
        # ---- 准备：一家临时企业 + 一个临时企业管理员 ----
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO organizations (id, name, is_platform, created_at) "
                "VALUES ($1, $2, FALSE, $3)", org_id, f"{TAG} 端到端验证企业", time.time())
            await conn.execute(
                "INSERT INTO users (id, username, password_hash, allowed_collections, role, created_at, org_id) "
                "VALUES ($1, $2, $3, '{}', 'user', $4, $5)",
                admin_id, f"{TAG}_admin",
                bcrypt.hashpw(ADMIN_PW.encode(), bcrypt.gensalt()).decode(),
                time.time(), org_id)
            org_admin_role = await conn.fetchval("SELECT id FROM roles WHERE name = 'org_admin'")
            if org_admin_role:
                await conn.execute(
                    "INSERT INTO user_roles (user_id, role_id) VALUES ($1, $2) "
                    "ON CONFLICT DO NOTHING", admin_id, org_admin_role)
            # 给临时企业建一个自己的角色。
            # ⚠️ 不去复用库里现成的角色：本库里**没有全局部门角色**
            # （2026-08-26 实测，全局只有 super_admin / org_admin，
            # 部门角色全是企业级的），借别家企业的角色恰恰是导入必须拒绝的情况。
            dept_role_id, dept_role = str(uuid.uuid4()), f"{TAG}_dept"
            await conn.execute(
                "INSERT INTO roles (id, name, display_name, is_system, org_id, created_at) "
                "VALUES ($1, $2, $3, FALSE, $4, $5)",
                dept_role_id, dept_role, f"{TAG} 测试部门", org_id, time.time())
        print(f"准备完成：企业 {org_id}，管理员 {TAG}_admin，导入用角色 {dept_role!r}")

        async with httpx.AsyncClient(timeout=30) as c:
            print("\n[1] 登录")
            r = await c.post(f"{API}/auth/login",
                             json={"username": f"{TAG}_admin", "password": ADMIN_PW})
            check(r.status_code == 200, "企业管理员能登录", r.text[:120])
            if r.status_code != 200:
                return 1
            H = {"Authorization": f"Bearer {r.json()['access_token']}"}

            print("\n[2] 批量导入：默认必须是预演")
            csv = f"username,role_name,display_name\n{TAG}_e1,{dept_role},员工一\n{TAG}_e2,{dept_role},员工二\n"
            files = {"file": ("u.csv", csv.encode(), "text/csv")}
            # 故意不传 validate_only —— 后端默认必须是 True
            r = await c.post(f"{API}/admin/users/bulk-import", headers=H, files=files)
            check(r.status_code == 200, "预演请求成功", r.text[:160])
            plan = r.json()
            check(plan["applied"] is False, "不传 validate_only 时默认只预演")
            check(plan["to_create"] == 2, f"预演算出新建 2（实际 {plan['to_create']}）")
            check(await users.count_active_users(org_id) == 1, "预演没有落库（企业仍只有管理员 1 人）")

            print("\n[3] 含密码列的 CSV 被整份拒收")
            bad = {"file": ("b.csv", f"username,role_name,password\n{TAG}_x,{dept_role},hunter2\n".encode(), "text/csv")}
            r = await c.post(f"{API}/admin/users/bulk-import", headers=H, files=bad,
                             data={"validate_only": "false"})
            check(r.json().get("fatal_error") is not None, "密码列 -> fatal_error")
            check(r.json()["applied"] is False, "含密码列时什么都不执行")

            print("\n[4] 真跑")
            r = await c.post(f"{API}/admin/users/bulk-import", headers=H,
                             files={"file": ("u.csv", csv.encode(), "text/csv")},
                             data={"validate_only": "false"})
            res = r.json()
            check(res["applied"] is True, "applied=True")
            check(len(res["credentials"]) == 2, f"返回 2 个激活码（实际 {len(res['credentials'])}）")
            creds = {c_["username"]: c_["activation_code"] for c_ in res["credentials"]}
            check(await users.count_active_users(org_id) == 3, "企业在用账号变成 3")

            print("\n[5] 幂等：同一份 CSV 再传一次")
            r = await c.post(f"{API}/admin/users/bulk-import", headers=H,
                             files={"file": ("u.csv", csv.encode(), "text/csv")},
                             data={"validate_only": "false"})
            again = r.json()
            check(again["to_create"] == 0 and again["to_update"] == 2, "第二次全是更新")
            check(len(again["credentials"]) == 0, "重传不再签发激活码")
            check(await users.count_active_users(org_id) == 3, "用户数没有变化")

            print("\n[6] 激活（无鉴权端点）")
            u1 = f"{TAG}_e1"
            r = await c.post(f"{API}/activate", json={
                "username": u1, "activation_code": "wrong-code", "new_password": "Pw123456"})
            wrong_detail, wrong_code = r.json().get("detail"), r.status_code
            check(wrong_code == 400, "错误的码被拒")

            r = await c.post(f"{API}/activate", json={
                "username": "__根本不存在的用户__", "activation_code": "x", "new_password": "Pw123456"})
            check(r.json().get("detail") == wrong_detail and r.status_code == wrong_code,
                  "「用户不存在」与「码不对」返回完全相同的响应（不泄露用户是否存在）",
                  f"{r.status_code}/{r.json().get('detail')} vs {wrong_code}/{wrong_detail}")

            r = await c.post(f"{API}/activate", json={
                "username": u1, "activation_code": creds[u1], "new_password": "Pw123456"})
            check(r.status_code == 200, "正确的码激活成功", r.text[:120])
            r = await c.post(f"{API}/auth/login", json={"username": u1, "password": "Pw123456"})
            check(r.status_code == 200, "激活后能登录")

            print("\n[7] 激活码单次使用")
            r = await c.post(f"{API}/activate", json={
                "username": u1, "activation_code": creds[u1], "new_password": "Hijacked999"})
            check(r.status_code == 400, "同一个码第二次被拒")
            r = await c.post(f"{API}/auth/login", json={"username": u1, "password": "Hijacked999"})
            check(r.status_code == 401, "第二次没有改掉密码")

            print("\n[8] 停用")
            e1 = await users.get_user_by_username(u1)
            r = await c.put(f"{API}/admin/users/{e1.user_id}/disabled", headers=H,
                            json={"disabled": True})
            check(r.status_code == 200, "停用成功", r.text[:120])
            check(r.json()["disabled_at"] is not None, "响应里带出 disabled_at（前端状态列靠它）")
            r = await c.post(f"{API}/auth/login", json={"username": u1, "password": "Pw123456"})
            check(r.status_code == 401, "停用后拿不到新 token")

            print("\n[9] 被停用的管理员立刻失去管理端权限")
            # 另建一个管理员来停用第一个，避免"不能停用自己"的保护
            admin2_id = str(uuid.uuid4())
            async with pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO users (id, username, password_hash, allowed_collections, role, created_at, org_id) "
                    "VALUES ($1, $2, $3, '{}', 'user', $4, $5)",
                    admin2_id, f"{TAG}_admin2",
                    bcrypt.hashpw(ADMIN_PW.encode(), bcrypt.gensalt()).decode(),
                    time.time(), org_id)
                if org_admin_role:
                    await conn.execute(
                        "INSERT INTO user_roles (user_id, role_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                        admin2_id, org_admin_role)
            r2 = await c.post(f"{API}/auth/login",
                              json={"username": f"{TAG}_admin2", "password": ADMIN_PW})
            H2 = {"Authorization": f"Bearer {r2.json()['access_token']}"}
            check((await c.get(f"{API}/admin/users", headers=H2)).status_code == 200,
                  "admin2 停用前能访问管理端")
            await users.set_disabled(admin2_id, True)
            r = await c.get(f"{API}/admin/users", headers=H2)
            check(r.status_code == 403, "**持旧 token 的已停用管理员立刻 403**（不用等 24 小时）",
                  f"got {r.status_code}")
            await users.set_disabled(admin2_id, False)

            print("\n[10] 企业管理员不能删除用户、不能改席位上限")
            r = await c.delete(f"{API}/admin/users/{e1.user_id}", headers=H)
            check(r.status_code == 403, "企业管理员删除用户 -> 403", f"got {r.status_code}")
            r = await c.put(f"{API}/admin/organizations/{org_id}/seat-limit", headers=H,
                            json={"seat_limit": 99})
            check(r.status_code == 403, "企业管理员改席位上限 -> 403", f"got {r.status_code}")

            print("\n[11] 席位上限拦住建号，停用的人不占席位")
            await orgs.set_seat_limit(org_id, await users.count_active_users(org_id))
            r = await c.post(f"{API}/admin/users", headers=H, json={
                "username": f"{TAG}_over", "password": "Pw123456", "role_ids": []})
            check(r.status_code == 403, "满员时建号 -> 403", f"got {r.status_code} {r.text[:100]}")
            check("席位" in r.text, "错误信息里说明是席位问题")
            await users.set_disabled(admin2_id, True)  # 腾出一个席位
            r = await c.post(f"{API}/admin/users", headers=H, json={
                "username": f"{TAG}_over", "password": "Pw123456", "role_ids": []})
            check(r.status_code == 200, "停用一人后建号成功（停用不占席位）", r.text[:120])

            print("\n[12] 重新启用也要过席位校验")
            r = await c.put(f"{API}/admin/users/{admin2_id}/disabled", headers=H,
                            json={"disabled": False})
            check(r.status_code == 403, "满员时重新启用 -> 403（最容易漏的那个校验点）",
                  f"got {r.status_code}")

    finally:
        print("\n[清理]")
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT id FROM users WHERE username LIKE $1", f"{TAG}%")
            for row in rows:
                await conn.execute("DELETE FROM user_roles WHERE user_id = $1", row["id"])
                await conn.execute("DELETE FROM users WHERE id = $1", row["id"])
            await conn.execute("DELETE FROM role_collections WHERE org_id = $1", org_id)
            await conn.execute("DELETE FROM roles WHERE org_id = $1", org_id)
            await conn.execute("DELETE FROM organizations WHERE id = $1", org_id)
            left = await conn.fetchval("SELECT COUNT(*) FROM users WHERE username LIKE $1", f"{TAG}%")
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
