#!/usr/bin/env python
"""智能运维模块阶段二（管理面 API 端点）的手工验证脚本。

按 CLAUDE.md §7.5"脚本一律落 tests/ 或 scripts/，禁止写临时目录后丢弃"落仓；
不放进 tests/ 是因为 `conftest.py` 无 DB fixture（同 `test_admin_users_batch_queries.py`
等文件遇到的限制），本脚本走 ASGITransport 直连真实的 `create_app()` + 本机
真实 Postgres（`RAGENT_POSTGRES_URL`，走 `.env`），跑完自行清理测试数据。

用法：
    .venv/bin/python scripts/verify_aiops_endpoints.py

覆盖：
  1. 模块未开通时注册连接器应 403
  2. org_admin 不能自己开通模块（只有 super_admin 能，§4.1）
  3. super_admin 开通模块后 org_admin 才能注册连接器
  4. approval_timeout_minutes 越界应 400，不是静默夹紧
  5. 修复范围白名单 upsert/list
  6. 非法 action_type 应 400
  7. 在白名单内的目标提议 -> pending_approval（§3.3.1 核心拦截点的正向路径）
  8. 越界目标提议 -> rejected_pre，带原因（负向路径）
  9. 没配白名单的动作类型 -> rejected_pre（默认拒绝，不是默认放行）
  10. 批准一条 pending_approval 的动作 -> approved
  11. 已经是终态的动作不能再被批准 -> 409（状态机不允许非法转移）
  12. 跨企业访问应 404（不是 403，避免泄露"这个连接器/动作存在但不是你的"）

本次未覆盖：BYOC 连接器的真实 WebSocket 心跳/联邦查询/AI 分析/审批后的真实
执行链路（approved 之后没有对应端点把它推进到 executing，见 CLAUDE.md §5
该条"什么没做"）；`role_ops_systems` 的 can_view/can_approve 精细权限（尚未
接线，当前只有 org_admin/super_admin 两档粗粒度门禁——任何本企业 org_admin
都能批准，不是只有被指定的审批人）；并发场景（两个 org_admin 同时批准同一条
动作等）；`reject` 端点本次没有单独测（跟 `approve` 共用同一段状态机校验，
判别力已经在 `approve` 的 409 用例里验过了）。
"""

from __future__ import annotations

import asyncio
import os

os.environ.setdefault("RAGENT_DEBUG", "true")

import httpx

from src.ragent_backend.app import create_app
from src.ragent_backend.auth import create_access_token
from src.ragent_backend.db_pool import close_shared_pools
from src.ragent_backend.org_store import ORG_PLATFORM_ID, OrgStore
from src.ragent_backend.role_store import ROLE_ORG_ADMIN, ROLE_SUPER_ADMIN, RoleStore
from src.ragent_backend.user_store import UserStore

_TEST_ORG_NAME = "AIOps-smoke-test-org"
_TEST_ORG_NAME_2 = "AIOps-smoke-test-org-2"
_TEST_USERNAMES = ["aiops_smoke_admin", "aiops_smoke_platform", "aiops_smoke_admin_2"]


async def _cleanup(user_store: UserStore) -> None:
    """幂等清理——脚本正常收尾会调一次，异常中断后重跑前也可以单独调这个
    （直接 `python -c "import asyncio; from scripts.verify_aiops_endpoints import _cleanup, ...`
    不方便，所以清理逻辑做成幂等：按用户名/组织名模式匹配删，不依赖上一次跑
    产生的具体 id）。"""
    pool = await user_store._get_pool()
    async with pool.acquire() as conn:
        user_ids = [
            r["id"] for r in await conn.fetch(
                "SELECT id FROM users WHERE username = ANY($1::text[])", _TEST_USERNAMES,
            )
        ]
        org_ids = [
            r["id"] for r in await conn.fetch(
                "SELECT id FROM organizations WHERE name = ANY($1::text[])",
                [_TEST_ORG_NAME, _TEST_ORG_NAME_2],
            )
        ]
        if org_ids:
            conn_ids = [
                r["id"] for r in await conn.fetch(
                    "SELECT id FROM ops_system_connections WHERE org_id = ANY($1::text[])", org_ids,
                )
            ]
            if conn_ids:
                await conn.execute(
                    "DELETE FROM remediation_actions WHERE connection_id = ANY($1::text[])", conn_ids,
                )
                await conn.execute(
                    "DELETE FROM ops_remediation_scopes WHERE connection_id = ANY($1::text[])", conn_ids,
                )
                await conn.execute(
                    "DELETE FROM ops_system_connections WHERE id = ANY($1::text[])", conn_ids,
                )
        if user_ids:
            await conn.execute("DELETE FROM user_roles WHERE user_id = ANY($1::text[])", user_ids)
            await conn.execute("DELETE FROM users WHERE id = ANY($1::text[])", user_ids)
        if org_ids:
            await conn.execute("DELETE FROM organizations WHERE id = ANY($1::text[])", org_ids)


async def main() -> None:
    org_store = OrgStore()
    role_store = RoleStore()
    user_store = UserStore()

    await _cleanup(user_store)  # 防止上一次异常中断留下的脏数据干扰这一轮

    org = await org_store.create_organization(_TEST_ORG_NAME)
    user = await user_store.create_user("aiops_smoke_admin", "pw12345678")

    pool = await user_store._get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE users SET org_id = $1 WHERE id = $2", org.org_id, user.user_id)

    org_admin_role = await role_store.get_role_by_name(ROLE_ORG_ADMIN, org_id=None)
    await role_store.add_user_role(user.user_id, org_admin_role.role_id)

    token = create_access_token(user.user_id, user.username)
    headers = {"Authorization": f"Bearer {token}"}

    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/admin/ops/connectors", headers=headers,
            json={"name": "test-prometheus", "system_type": "prometheus"},
        )
        print("1) register before module enabled:", resp.status_code)
        assert resp.status_code == 403

        resp = await client.put(
            f"/api/v1/admin/organizations/{org.org_id}/aiops-module-enabled",
            headers=headers, json={"enabled": True},
        )
        print("2) org_admin tries to enable module:", resp.status_code)
        assert resp.status_code == 403

        platform_user = await user_store.create_user("aiops_smoke_platform", "pw12345678")
        super_admin_role = await role_store.get_role_by_name(ROLE_SUPER_ADMIN, org_id=None)
        await role_store.add_user_role(platform_user.user_id, super_admin_role.role_id)
        pool = await user_store._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET org_id = $1 WHERE id = $2", ORG_PLATFORM_ID, platform_user.user_id,
            )
        platform_token = create_access_token(platform_user.user_id, platform_user.username)
        platform_headers = {"Authorization": f"Bearer {platform_token}"}

        resp = await client.put(
            f"/api/v1/admin/organizations/{org.org_id}/aiops-module-enabled",
            headers=platform_headers, json={"enabled": True},
        )
        print("3) super_admin enables module:", resp.status_code)
        assert resp.status_code == 200

        resp = await client.post(
            "/api/v1/admin/ops/connectors", headers=headers,
            json={"name": "test-prometheus", "system_type": "prometheus"},
        )
        print("4) register after module enabled:", resp.status_code)
        assert resp.status_code == 200
        connection_id = resp.json()["connection_id"]

        resp = await client.post(
            "/api/v1/admin/ops/connectors", headers=headers,
            json={"name": "bad-timeout", "system_type": "prometheus", "approval_timeout_minutes": 99999},
        )
        print("5) register with out-of-range timeout:", resp.status_code)
        assert resp.status_code == 400

        resp = await client.get("/api/v1/admin/ops/connectors", headers=headers)
        print("6) list connectors:", resp.status_code, len(resp.json()))
        assert resp.status_code == 200 and len(resp.json()) == 1

        resp = await client.put(
            f"/api/v1/admin/ops/connectors/{connection_id}/remediation-scopes/restart_service",
            headers=headers, json={"scope_config": {"allowed_targets": ["order-service"]}},
        )
        print("7) upsert scope:", resp.status_code)
        assert resp.status_code == 200

        resp = await client.put(
            f"/api/v1/admin/ops/connectors/{connection_id}/remediation-scopes/delete_database",
            headers=headers, json={"scope_config": {}},
        )
        print("8) upsert scope with invalid action_type:", resp.status_code)
        assert resp.status_code == 400

        resp = await client.get(
            f"/api/v1/admin/ops/connectors/{connection_id}/remediation-scopes", headers=headers,
        )
        print("9) list scopes:", resp.status_code, len(resp.json()))
        assert resp.status_code == 200 and len(resp.json()) == 1

        # 11) 提议一个在白名单内的目标 -> 应该进 pending_approval
        resp = await client.post(
            f"/api/v1/admin/ops/connectors/{connection_id}/remediation-actions", headers=headers,
            json={
                "action_type": "restart_service", "intent": "服务卡死重启",
                "plan": {"target": "order-service"},
            },
        )
        print("11) propose in-scope action:", resp.status_code, resp.json()["status"])
        assert resp.status_code == 200
        in_scope_action = resp.json()
        assert in_scope_action["status"] == "pending_approval"
        assert in_scope_action["scope_check_reason"] is None

        # 12) 提议一个越界目标 -> 应该被拒到 rejected_pre，且带上原因
        resp = await client.post(
            f"/api/v1/admin/ops/connectors/{connection_id}/remediation-actions", headers=headers,
            json={
                "action_type": "restart_service", "intent": "尝试重启一个不在白名单里的服务",
                "plan": {"target": "admin-database"},
            },
        )
        print("12) propose out-of-scope action:", resp.status_code, resp.json()["status"])
        assert resp.status_code == 200
        out_of_scope_action = resp.json()
        assert out_of_scope_action["status"] == "rejected_pre"
        assert "admin-database" in out_of_scope_action["scope_check_reason"]

        # 13) 提议一个没配白名单的动作类型 -> 应该被拒（默认拒绝，不是默认放行）
        resp = await client.post(
            f"/api/v1/admin/ops/connectors/{connection_id}/remediation-actions", headers=headers,
            json={
                "action_type": "clean_disk", "intent": "磁盘满了清一下",
                "plan": {"path": "/var/log/app/x.log"},
            },
        )
        print("13) propose action with no scope configured:", resp.status_code, resp.json()["status"])
        assert resp.status_code == 200
        assert resp.json()["status"] == "rejected_pre"
        assert "尚未" in resp.json()["scope_check_reason"]

        # 14) 列出本企业的修复动作
        resp = await client.get("/api/v1/admin/ops/remediation-actions", headers=headers)
        print("14) list remediation actions:", resp.status_code, len(resp.json()))
        assert resp.status_code == 200 and len(resp.json()) == 3

        # 15) 批准那条在白名单内的（pending_approval -> approved）
        resp = await client.post(
            f"/api/v1/admin/ops/remediation-actions/{in_scope_action['action_id']}/approve",
            headers=headers,
        )
        print("15) approve pending action:", resp.status_code, resp.json()["status"])
        assert resp.status_code == 200
        assert resp.json()["status"] == "approved"
        assert resp.json()["approver_user_id"] == user.user_id

        # 16) 已经是终态（rejected_pre）的动作不能再被批准 -> 409
        resp = await client.post(
            f"/api/v1/admin/ops/remediation-actions/{out_of_scope_action['action_id']}/approve",
            headers=headers,
        )
        print("16) approve an already-rejected action:", resp.status_code)
        assert resp.status_code == 409

        other_org = await org_store.create_organization(_TEST_ORG_NAME_2)
        other_user = await user_store.create_user("aiops_smoke_admin_2", "pw12345678")
        pool = await user_store._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET org_id = $1 WHERE id = $2", other_org.org_id, other_user.user_id,
            )
        await role_store.add_user_role(other_user.user_id, org_admin_role.role_id)
        other_token = create_access_token(other_user.user_id, other_user.username)
        other_headers = {"Authorization": f"Bearer {other_token}"}

        await client.put(
            f"/api/v1/admin/organizations/{other_org.org_id}/aiops-module-enabled",
            headers=platform_headers, json={"enabled": True},
        )
        resp = await client.get(
            f"/api/v1/admin/ops/connectors/{connection_id}/remediation-scopes", headers=other_headers,
        )
        print("17) cross-org access:", resp.status_code)
        assert resp.status_code == 404

    print("\nALL CHECKS PASSED")
    await _cleanup(user_store)
    print("cleaned up")
    await close_shared_pools()


if __name__ == "__main__":
    asyncio.run(main())
