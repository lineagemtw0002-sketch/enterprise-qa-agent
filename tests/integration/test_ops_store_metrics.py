"""`OpsStore.compute_ops_metrics`（§10.5 四个 V1 验收指标）—— 连真实
Postgres，把动作真的推过完整状态机（proposed→pending_approval→approved→
executing→completed/failed）来产生指标要用的样本，而不是直接摆一行伪造好
的 status 字符串进数据库——后者测不出状态转移本身对不对，也测不出
`approver_user_id IS NOT NULL` 这个"审批口径"在真实转移链路下是否真的成立。

判别力核心：
1. **分母为 0 时必须是 `None` 不是 `0.0`**——"还没有样本"和"比例恰好是 0"
   是两件不同的事，糊在一起会让刚开始用的企业看起来"表现很差"。
2. **告警合并率是加总再算比例，不是对每条记录的 noise_reduction 取平均**——
   用两条样本量差异很大的记录验证：如果实现退化成平均比例，小样本会把
   结果拉偏，这条会失败。
3. **`connection_ids` 过滤是真的按连接器隔离，不是摆设**——只统计传入的
   连接器集合，另一个连接器的数据完全不掺进来。
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.asyncio

os.environ.setdefault("RAGENT_DEBUG", "true")

from src.ragent_backend import db_pool
from src.ragent_backend.ops_store import (
    STATUS_COMPLETED,
    STATUS_EXPIRED,
    STATUS_FAILED,
    STATUS_PENDING_APPROVAL,
    STATUS_REJECTED,
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


class TestApprovalAndExecutionRates:
    async def test_full_lifecycle_produces_correct_ratios(self):
        ops = OpsStore()
        await ops._get_pool()
        connector = await ops.register_connector(ORG_PLATFORM_ID, "metrics-test-1", "prometheus", "test")
        try:
            # 一条走到 completed
            a1 = await ops.create_proposed_action(
                ORG_PLATFORM_ID, connector.connection_id, "test", "第一条", {"target": "x"},
            )
            a1 = await ops.advance_status(a1.action_id, STATUS_PENDING_APPROVAL)
            a1 = await ops.approve_action(a1.action_id, approver_user_id="admin-1")
            a1 = await ops.mark_executing(a1.action_id)
            a1 = await ops.mark_result(a1.action_id, STATUS_COMPLETED, result={"ok": True})
            await ops.set_outcome_effective(a1.action_id, True)

            # 一条走到 failed，事后标注为无效
            a2 = await ops.create_proposed_action(
                ORG_PLATFORM_ID, connector.connection_id, "test", "第二条", {"target": "y"},
            )
            a2 = await ops.advance_status(a2.action_id, STATUS_PENDING_APPROVAL)
            a2 = await ops.approve_action(a2.action_id, approver_user_id="admin-1")
            a2 = await ops.mark_executing(a2.action_id)
            a2 = await ops.mark_result(a2.action_id, STATUS_FAILED, result={"error": "timeout"})
            await ops.set_outcome_effective(a2.action_id, False)

            # 一条被拒绝（走完审批流程，只是结论是拒绝）
            a3 = await ops.create_proposed_action(
                ORG_PLATFORM_ID, connector.connection_id, "test", "第三条", {"target": "z"},
            )
            a3 = await ops.advance_status(a3.action_id, STATUS_PENDING_APPROVAL)
            a3 = await ops.advance_status(a3.action_id, STATUS_REJECTED)

            # 一条超时未处理
            a4 = await ops.create_proposed_action(
                ORG_PLATFORM_ID, connector.connection_id, "test", "第四条", {"target": "w"},
            )
            a4 = await ops.advance_status(a4.action_id, STATUS_PENDING_APPROVAL)
            a4 = await ops.advance_status(a4.action_id, STATUS_EXPIRED)

            metrics = await ops.compute_ops_metrics(ORG_PLATFORM_ID, connection_ids=[connector.connection_id])

            # 审批处理及时率 = (approved+rejected) / (approved+rejected+expired)
            # approved 用 approver_user_id 是否非空来数——a1/a2 都真的被批准过
            # （即使后来继续流转到了 completed/failed），必须算进"approved"里，
            # 不能因为它们当前状态字段已经不是 'approved' 就漏计。
            # = (2 + 1) / (2 + 1 + 1) = 0.75
            assert metrics["approval_timeliness_rate"] == 0.75

            # 执行成功率 = completed / (completed+failed) = 1/2 = 0.5
            assert metrics["execution_success_rate"] == 0.5

            assert metrics["outcome_effective_counts"] == {"effective": 1, "ineffective": 1, "unlabeled": 0}
            assert metrics["sample_sizes"]["approved"] == 2
            assert metrics["sample_sizes"]["rejected"] == 1
            assert metrics["sample_sizes"]["expired"] == 1
            assert metrics["sample_sizes"]["completed"] == 1
            assert metrics["sample_sizes"]["failed"] == 1
        finally:
            await _cleanup(ops, connector.connection_id, [])

    async def test_zero_denominator_is_none_not_zero(self):
        """判别式：一条动作都没有时，比例必须是 None，不能是 0.0——两者含义
        完全不同（"没有样本"vs"比例恰好是0"），实现如果用 `x / max(y, 1)`
        这类"避免除零"的写法会把这条测出来。"""
        ops = OpsStore()
        await ops._get_pool()
        connector = await ops.register_connector(ORG_PLATFORM_ID, "metrics-test-empty", "prometheus", "test")
        try:
            metrics = await ops.compute_ops_metrics(ORG_PLATFORM_ID, connection_ids=[connector.connection_id])
            assert metrics["approval_timeliness_rate"] is None
            assert metrics["execution_success_rate"] is None
            assert metrics["alert_noise_reduction"] is None
            assert metrics["outcome_effective_counts"] == {"effective": 0, "ineffective": 0, "unlabeled": 0}
        finally:
            await _cleanup(ops, connector.connection_id, [])


class TestAlertNoiseReductionIsVolumeWeighted:
    async def test_aggregates_totals_not_average_of_ratios(self):
        ops = OpsStore()
        await ops._get_pool()
        connector = await ops.register_connector(ORG_PLATFORM_ID, "metrics-test-noise", "prometheus", "test")
        summary_ids = []
        try:
            # 记录 A：小样本，比例很好看（2条告警合并成1个事件，noise_reduction=0.5）
            s_a = await ops.save_analysis_summary(
                ORG_PLATFORM_ID, connector.connection_id, "小样本分析",
                [{"source": "alert_correlation_stats", "description": "x",
                  "detail": {"alert_count": 2, "incident_count": 1, "noise_reduction": 0.5}}],
            )
            # 记录 B：大样本，比例较差（100条告警合并成80个事件，noise_reduction=0.2）
            s_b = await ops.save_analysis_summary(
                ORG_PLATFORM_ID, connector.connection_id, "大样本分析",
                [{"source": "alert_correlation_stats", "description": "y",
                  "detail": {"alert_count": 100, "incident_count": 80, "noise_reduction": 0.2}}],
            )
            summary_ids = [s_a.summary_id, s_b.summary_id]

            metrics = await ops.compute_ops_metrics(ORG_PLATFORM_ID, connection_ids=[connector.connection_id])

            # 简单平均会得到 (0.5+0.2)/2 = 0.35；加总再算是
            # 1 - (1+80)/(2+100) = 1 - 81/102 ≈ 0.2059——两者差得足够远，
            # 断言能明确区分实现走的是哪条路。
            assert metrics["alert_noise_reduction"] == round(1 - 81 / 102, 4)
            assert metrics["alert_noise_reduction"] != 0.35
        finally:
            await _cleanup(ops, connector.connection_id, summary_ids)


class TestConnectionIdsFilterIsReallyIsolated:
    async def test_other_connectors_data_does_not_leak_in(self):
        ops = OpsStore()
        await ops._get_pool()
        conn_a = await ops.register_connector(ORG_PLATFORM_ID, "metrics-test-iso-a", "prometheus", "test")
        conn_b = await ops.register_connector(ORG_PLATFORM_ID, "metrics-test-iso-b", "prometheus", "test")
        try:
            a1 = await ops.create_proposed_action(
                ORG_PLATFORM_ID, conn_a.connection_id, "test", "A 库的动作", {"target": "x"},
            )
            await ops.advance_status(a1.action_id, STATUS_PENDING_APPROVAL)
            await ops.approve_action(a1.action_id, approver_user_id="admin-1")

            b1 = await ops.create_proposed_action(
                ORG_PLATFORM_ID, conn_b.connection_id, "test", "B 库的动作", {"target": "y"},
            )
            await ops.advance_status(b1.action_id, STATUS_PENDING_APPROVAL)
            await ops.advance_status(b1.action_id, STATUS_REJECTED)

            only_a = await ops.compute_ops_metrics(ORG_PLATFORM_ID, connection_ids=[conn_a.connection_id])
            assert only_a["sample_sizes"]["approved"] == 1
            assert only_a["sample_sizes"]["rejected"] == 0, "B 库的拒绝不该混进 A 库的统计"

            both = await ops.compute_ops_metrics(
                ORG_PLATFORM_ID, connection_ids=[conn_a.connection_id, conn_b.connection_id],
            )
            assert both["sample_sizes"]["approved"] == 1
            assert both["sample_sizes"]["rejected"] == 1
        finally:
            await _cleanup(ops, conn_a.connection_id, [])
            await _cleanup(ops, conn_b.connection_id, [])
