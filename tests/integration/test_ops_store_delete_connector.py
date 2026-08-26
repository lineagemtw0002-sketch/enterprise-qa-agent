"""`OpsStore.delete_connector`（新增，闭环"连接器登记了就撤不掉"这个真实
缺口——刘德华摸底"授权管理"前端时想清理自己建的联调用连接器才发现的）。

连真实 Postgres：判别力核心是删除顺序——子表没有 `ON DELETE CASCADE`
（外键默认 RESTRICT），先删父表会撞 `ForeignKeyViolationError`，这条不是
假设，是 `scripts/verify_aiops_endpoints.py` 早前清理测试数据时真实撞过的
错误。这里验证的是"级联删除真的把全部子表清干净了"，不是"删除不报错"这么
弱的断言——每张子表删除前先插一行，删除后逐张确认真的空了。
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.asyncio

os.environ.setdefault("RAGENT_DEBUG", "true")

from src.ragent_backend import db_pool
from src.ragent_backend.ops_store import STATUS_PENDING_APPROVAL, OpsStore
from src.ragent_backend.org_store import ORG_PLATFORM_ID
from src.ragent_backend.role_store import RoleStore


@pytest.fixture(autouse=True)
def _reset_pool_between_tests():
    db_pool._POOL_CACHE.clear()
    OpsStore._pool = None
    RoleStore._pool = None
    yield


class TestDeleteConnectorCascades:
    async def test_deletes_connector_and_every_dependent_row(self):
        ops = OpsStore()
        await ops._get_pool()
        connector = await ops.register_connector(
            ORG_PLATFORM_ID, "delete-cascade-test", "prometheus", "test",
        )
        connection_id = connector.connection_id

        # 给每张有外键指向这个连接器的子表都插一行，逼级联删除真的走到每一张。
        role_store = RoleStore()
        role = await role_store.create_role(ORG_PLATFORM_ID, "delete-cascade-role", "级联删除测试角色")
        await ops.set_role_ops_permission(role.role_id, connection_id, can_view=True, can_approve=True)

        action = await ops.create_proposed_action(
            ORG_PLATFORM_ID, connection_id, "test", "级联删除测试动作", {"target": "x"},
        )
        await ops.advance_status(action.action_id, STATUS_PENDING_APPROVAL)

        await ops.upsert_remediation_scope(
            ORG_PLATFORM_ID, connection_id, "restart_service",
            {"allowed_targets": ["order-service"]}, "test",
        )

        summary = await ops.save_analysis_summary(
            ORG_PLATFORM_ID, connection_id, "级联删除测试摘要", [{"source": "test"}],
        )

        pool = await ops._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO ops_connector_register_tokens "
                "(connection_id, token_hash, expires_at) VALUES ($1, $2, $3)",
                connection_id, "fakehash", 9999999999.0,
            )
            await conn.execute(
                "INSERT INTO ops_connector_refresh_tokens "
                "(id, connection_id, token_hash, issued_at, expires_at) VALUES ($1, $2, $3, $4, $5)",
                "reftok_delete_cascade_test", connection_id, "fakehash", 0.0, 9999999999.0,
            )

        try:
            deleted = await ops.delete_connector(connection_id)
            assert deleted is True

            assert await ops.get_connector(connection_id) is None
            assert await ops.get_action(action.action_id) is None
            assert await ops.get_analysis_summary(summary.summary_id) is None
            assert await ops.list_remediation_scopes(connection_id) == []
            assert await ops.list_role_ops_permissions(connection_id) == []

            async with pool.acquire() as conn:
                assert await conn.fetchval(
                    "SELECT count(*) FROM ops_connector_register_tokens WHERE connection_id = $1",
                    connection_id,
                ) == 0
                assert await conn.fetchval(
                    "SELECT count(*) FROM ops_connector_refresh_tokens WHERE connection_id = $1",
                    connection_id,
                ) == 0
        finally:
            async with pool.acquire() as conn:
                # 兜底清理——正常路径下 delete_connector 已经清完了，这里只是
                # 防止断言失败时留下脏数据影响下一次跑。
                for table in (
                    "role_ops_systems", "ops_connector_register_tokens",
                    "ops_connector_refresh_tokens", "remediation_actions",
                    "ops_remediation_scopes", "ops_analysis_summaries",
                ):
                    await conn.execute(f"DELETE FROM {table} WHERE connection_id = $1", connection_id)
                await conn.execute("DELETE FROM ops_system_connections WHERE id = $1", connection_id)
                await conn.execute("DELETE FROM roles WHERE id = $1", role.role_id)

    async def test_deleting_nonexistent_connector_returns_false(self):
        ops = OpsStore()
        assert await ops.delete_connector("opsconn_does_not_exist") is False
