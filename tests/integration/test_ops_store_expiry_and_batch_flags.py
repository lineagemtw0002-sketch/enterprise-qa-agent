"""连真实 Postgres 验证两处 2026-08-26 新增的 SQL 正确性——单测用假 pool 只能
验证"每个候选 id 是否被正确转移/跳过"这层循环逻辑，验证不了 SQL 本身
（`JOIN`、`ANY($1::text[])`）是否真的按预期工作，必须过真库。

1. `OpsStore.expire_stale_pending_approvals`：**按各自连接器的
   `approval_timeout_minutes` 算截止时间**，不是套一个全局 cutoff——用一个
   超时 5 分钟的连接器和一个超时 1440 分钟的连接器各挂一条"创建于 10 分钟前"
   的 `pending_approval` 动作，只有前者该被判定过期。这是判别力核心：如果
   实现退化成"随便一个全局 cutoff"，两条会同时过期或同时不过期，测试就会
   在这条断言上失败。
2. `OpsStore.is_module_enabled_batch`：批量版是否真的能读出 `organizations`
   表里的实际状态（含"一个企业开了、另一个没开"混合场景），以及缺失的
   org_id 不出现在返回值里（调用方靠 `.get(org_id, False)` 兜底，这里验证
   "不出现"这个契约本身，不是"返回 False"——两者对调用方效果相同，但如果
   将来有人把缺失的 key 也塞进去，会打破"字典只包含查到的" 这条约定，
   `assert missing_id not in result` 会先发现)。
"""

from __future__ import annotations

import time

import pytest

pytestmark = pytest.mark.asyncio

import os

os.environ.setdefault("RAGENT_DEBUG", "true")

from src.ragent_backend import db_pool
from src.ragent_backend.ops_store import STATUS_EXPIRED, STATUS_PENDING_APPROVAL, OpsStore
from src.ragent_backend.org_store import ORG_PLATFORM_ID


@pytest.fixture(autouse=True)
def _reset_pool_between_tests():
    db_pool._POOL_CACHE.clear()
    OpsStore._pool = None
    yield


async def _backdate(ops: OpsStore, action_id: str, minutes_ago: int) -> None:
    pool = await ops._get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE remediation_actions SET created_at = $1 WHERE id = $2",
            time.time() - minutes_ago * 60, action_id,
        )


class TestExpireStalePendingApprovalsUsesPerConnectorTimeout:
    async def test_only_the_short_timeout_connector_action_expires(self):
        ops = OpsStore()
        await ops._get_pool()

        short = await ops.register_connector(
            ORG_PLATFORM_ID, "expiry-test-short", "prometheus", "test",
            approval_timeout_minutes=5,
        )
        long = await ops.register_connector(
            ORG_PLATFORM_ID, "expiry-test-long", "prometheus", "test",
            approval_timeout_minutes=1440,
        )
        action_short = await ops.create_proposed_action(
            ORG_PLATFORM_ID, short.connection_id, "test", "短超时连接器上的动作", {"target": "x"},
        )
        action_long = await ops.create_proposed_action(
            ORG_PLATFORM_ID, long.connection_id, "test", "长超时连接器上的动作", {"target": "y"},
        )
        action_short = await ops.advance_status(action_short.action_id, STATUS_PENDING_APPROVAL)
        action_long = await ops.advance_status(action_long.action_id, STATUS_PENDING_APPROVAL)

        # 两条都"创建于 10 分钟前"——短超时(5分钟)的那条已经过期，
        # 长超时(1440分钟)的那条还早得很，同一个时间点两者的过期判定必须不同，
        # 这正是"不能用全局 cutoff"的判别力所在。
        await _backdate(ops, action_short.action_id, minutes_ago=10)
        await _backdate(ops, action_long.action_id, minutes_ago=10)

        try:
            expired_ids = await ops.expire_stale_pending_approvals()

            assert action_short.action_id in expired_ids
            assert action_long.action_id not in expired_ids

            refreshed_short = await ops.get_action(action_short.action_id)
            refreshed_long = await ops.get_action(action_long.action_id)
            assert refreshed_short.status == STATUS_EXPIRED
            assert refreshed_long.status == STATUS_PENDING_APPROVAL
        finally:
            pool = await ops._get_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM remediation_actions WHERE id = ANY($1::text[])",
                    [action_short.action_id, action_long.action_id],
                )
                await conn.execute(
                    "DELETE FROM ops_system_connections WHERE id = ANY($1::text[])",
                    [short.connection_id, long.connection_id],
                )

    async def test_does_not_touch_actions_within_timeout(self):
        """回归保护：没超时的动作原样留在 pending_approval，不能一扫就全过期。"""
        ops = OpsStore()
        await ops._get_pool()
        conn_ = await ops.register_connector(
            ORG_PLATFORM_ID, "expiry-test-fresh", "prometheus", "test",
            approval_timeout_minutes=1440,
        )
        action = await ops.create_proposed_action(
            ORG_PLATFORM_ID, conn_.connection_id, "test", "刚提的动作", {"target": "z"},
        )
        action = await ops.advance_status(action.action_id, STATUS_PENDING_APPROVAL)
        try:
            expired_ids = await ops.expire_stale_pending_approvals()
            assert action.action_id not in expired_ids
            refreshed = await ops.get_action(action.action_id)
            assert refreshed.status == STATUS_PENDING_APPROVAL
        finally:
            pool = await ops._get_pool()
            async with pool.acquire() as conn:
                await conn.execute("DELETE FROM remediation_actions WHERE id = $1", action.action_id)
                await conn.execute(
                    "DELETE FROM ops_system_connections WHERE id = $1", conn_.connection_id,
                )


class TestIsModuleEnabledBatch:
    async def test_mixed_enabled_and_disabled_orgs(self):
        from src.ragent_backend.org_store import OrgStore

        ops = OpsStore()
        org_store = OrgStore()
        await ops._get_pool()

        org_a = await org_store.create_organization("批量开关测试-A")
        org_b = await org_store.create_organization("批量开关测试-B")
        await ops.set_module_enabled(org_a.org_id, True)
        # org_b 保持默认（False），不显式调用 set_module_enabled——
        # 覆盖"从未设置过、走列的 DEFAULT FALSE"这条路径，不只是测显式设 False。

        try:
            result = await ops.is_module_enabled_batch([org_a.org_id, org_b.org_id, "org_does_not_exist"])

            assert result[org_a.org_id] is True
            assert result[org_b.org_id] is False
            assert "org_does_not_exist" not in result
        finally:
            pool = await org_store._get_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM organizations WHERE id = ANY($1::text[])",
                    [org_a.org_id, org_b.org_id],
                )

    async def test_empty_input_returns_empty_dict_without_querying(self):
        ops = OpsStore()
        assert await ops.is_module_enabled_batch([]) == {}
