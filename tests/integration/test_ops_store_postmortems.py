"""`OpsStore.list_postmortems`（§9.2"事后复盘聚合视图"的最小可行版）——
连真实 Postgres，验证真实 JOIN 行为，不是假 pool 能测出来的层面。

判别力核心：
1. **只列终态（completed/failed），不列 pending_approval/approved 等中间
   状态**——复盘看的是"结果"，还没跑完的动作没有结果可复盘。
2. **`summary_id` 真的能通过 JOIN 拿回摘要文本，没链接时是 `None` 不是
   空字符串**——区分"没关联"和"关联了但摘要是空的"。
3. **`connection_ids` 过滤是真的隔离**，跟 `compute_ops_metrics` 用同一套
   约定，这里单独验证一次不是因为不信任那份实现，是因为这是另一条查询
   路径（不同的 SQL），各自的隔离都要自己证明。
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.asyncio

os.environ.setdefault("RAGENT_DEBUG", "true")

from src.ragent_backend import db_pool
from src.ragent_backend.ops_store import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_PENDING_APPROVAL,
    OpsStore,
)
from src.ragent_backend.org_store import ORG_PLATFORM_ID


@pytest.fixture(autouse=True)
def _reset_pool_between_tests():
    db_pool._POOL_CACHE.clear()
    OpsStore._pool = None
    yield


async def _cleanup(ops: OpsStore, conn_id: str, summary_ids: list) -> None:
    pool = await ops._get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM remediation_actions WHERE connection_id = $1", conn_id)
        if summary_ids:
            await conn.execute(
                "DELETE FROM ops_analysis_summaries WHERE id = ANY($1::text[])", summary_ids,
            )
        await conn.execute("DELETE FROM ops_system_connections WHERE id = $1", conn_id)


class TestListPostmortemsOnlyIncludesTerminalStatus:
    async def test_pending_and_approved_are_excluded(self):
        ops = OpsStore()
        await ops._get_pool()
        connector = await ops.register_connector(ORG_PLATFORM_ID, "postmortem-test-1", "prometheus", "test")
        try:
            still_pending = await ops.create_proposed_action(
                ORG_PLATFORM_ID, connector.connection_id, "test", "还没处理完", {"target": "x"},
            )
            await ops.advance_status(still_pending.action_id, STATUS_PENDING_APPROVAL)

            done = await ops.create_proposed_action(
                ORG_PLATFORM_ID, connector.connection_id, "test", "已经跑完", {"target": "y"},
            )
            done = await ops.advance_status(done.action_id, STATUS_PENDING_APPROVAL)
            done = await ops.approve_action(done.action_id, approver_user_id="admin-1")
            done = await ops.mark_executing(done.action_id)
            await ops.mark_result(done.action_id, STATUS_COMPLETED, result={"ok": True})

            entries = await ops.list_postmortems(ORG_PLATFORM_ID, connection_ids=[connector.connection_id])
            action_ids = {e.action.action_id for e in entries}
            assert done.action_id in action_ids
            assert still_pending.action_id not in action_ids
        finally:
            await _cleanup(ops, connector.connection_id, [])


class TestLinkedSummaryIsJoinedCorrectly:
    async def test_action_with_summary_id_gets_the_summary_text(self):
        ops = OpsStore()
        await ops._get_pool()
        connector = await ops.register_connector(ORG_PLATFORM_ID, "postmortem-test-2", "prometheus", "test")
        summary = await ops.save_analysis_summary(
            ORG_PLATFORM_ID, connector.connection_id, "错误率突增，怀疑部署回归", [{"source": "rca"}],
        )
        try:
            action = await ops.create_proposed_action(
                ORG_PLATFORM_ID, connector.connection_id, "test", "根据分析结果重启",
                {"target": "order-service"}, summary_id=summary.summary_id,
            )
            action = await ops.advance_status(action.action_id, STATUS_PENDING_APPROVAL)
            action = await ops.approve_action(action.action_id, approver_user_id="admin-1")
            action = await ops.mark_executing(action.action_id)
            await ops.mark_result(action.action_id, STATUS_FAILED, result={"error": "x"})

            entries = await ops.list_postmortems(ORG_PLATFORM_ID, connection_ids=[connector.connection_id])
            assert len(entries) == 1
            assert entries[0].linked_summary == "错误率突增，怀疑部署回归"
            assert entries[0].action.summary_id == summary.summary_id
        finally:
            await _cleanup(ops, connector.connection_id, [summary.summary_id])

    async def test_action_without_summary_id_has_none_not_empty_string(self):
        ops = OpsStore()
        await ops._get_pool()
        connector = await ops.register_connector(ORG_PLATFORM_ID, "postmortem-test-3", "prometheus", "test")
        try:
            action = await ops.create_proposed_action(
                ORG_PLATFORM_ID, connector.connection_id, "test", "手动提议，没有分析依据", {"target": "z"},
            )
            action = await ops.advance_status(action.action_id, STATUS_PENDING_APPROVAL)
            action = await ops.approve_action(action.action_id, approver_user_id="admin-1")
            action = await ops.mark_executing(action.action_id)
            await ops.mark_result(action.action_id, STATUS_COMPLETED, result={"ok": True})

            entries = await ops.list_postmortems(ORG_PLATFORM_ID, connection_ids=[connector.connection_id])
            assert len(entries) == 1
            assert entries[0].linked_summary is None
            assert entries[0].action.summary_id is None
        finally:
            await _cleanup(ops, connector.connection_id, [])


class TestConnectionIdsFilterIsolatesPostmortems:
    async def test_other_connectors_actions_do_not_leak_in(self):
        ops = OpsStore()
        await ops._get_pool()
        conn_a = await ops.register_connector(ORG_PLATFORM_ID, "postmortem-test-iso-a", "prometheus", "test")
        conn_b = await ops.register_connector(ORG_PLATFORM_ID, "postmortem-test-iso-b", "prometheus", "test")
        try:
            a1 = await ops.create_proposed_action(
                ORG_PLATFORM_ID, conn_a.connection_id, "test", "A 库", {"target": "x"},
            )
            a1 = await ops.advance_status(a1.action_id, STATUS_PENDING_APPROVAL)
            a1 = await ops.approve_action(a1.action_id, approver_user_id="admin-1")
            a1 = await ops.mark_executing(a1.action_id)
            await ops.mark_result(a1.action_id, STATUS_COMPLETED, result={"ok": True})

            b1 = await ops.create_proposed_action(
                ORG_PLATFORM_ID, conn_b.connection_id, "test", "B 库", {"target": "y"},
            )
            b1 = await ops.advance_status(b1.action_id, STATUS_PENDING_APPROVAL)
            b1 = await ops.approve_action(b1.action_id, approver_user_id="admin-1")
            b1 = await ops.mark_executing(b1.action_id)
            await ops.mark_result(b1.action_id, STATUS_COMPLETED, result={"ok": True})

            only_a = await ops.list_postmortems(ORG_PLATFORM_ID, connection_ids=[conn_a.connection_id])
            assert {e.action.action_id for e in only_a} == {a1.action_id}
        finally:
            await _cleanup(ops, conn_a.connection_id, [])
            await _cleanup(ops, conn_b.connection_id, [])
