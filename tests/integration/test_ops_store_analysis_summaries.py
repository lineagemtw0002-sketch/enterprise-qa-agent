"""AI 分析结论摘要的存取（`docs/aiops_module_design.md` §3.1）—— 连真实
Postgres，验证 `save_analysis_summary`/`get_analysis_summary`/
`list_analysis_summaries` 这三个新增 CRUD 方法（`ops_analysis_summaries` 表
早前只建了表、CRUD 一直是 CLAUDE.md §5 记录的已知空白，本次补上，给「刘德华」
正在写的 `src/ops/analysis/` 层做落库依赖）。

判别力核心：`evidence_refs` 是 JSONB，存取一圈后字段结构必须逐项相同
（不是"能读出字符串就算过"），以及 `list_analysis_summaries` 按
`connection_id` 过滤/不过滤两条路径都要各自验证，不能只测其中一条。
"""

from __future__ import annotations

import os
import time

import pytest

pytestmark = pytest.mark.asyncio

os.environ.setdefault("RAGENT_DEBUG", "true")

from src.ragent_backend import db_pool
from src.ragent_backend.ops_store import OpsStore
from src.ragent_backend.org_store import ORG_PLATFORM_ID


@pytest.fixture(autouse=True)
def _reset_pool_between_tests():
    db_pool._POOL_CACHE.clear()
    OpsStore._pool = None
    yield


async def _cleanup(ops: OpsStore, conn_id: str, summary_ids: list) -> None:
    pool = await ops._get_pool()
    async with pool.acquire() as conn:
        if summary_ids:
            await conn.execute(
                "DELETE FROM ops_analysis_summaries WHERE id = ANY($1::text[])", summary_ids,
            )
        await conn.execute("DELETE FROM ops_system_connections WHERE id = $1", conn_id)


class TestSaveAndGetAnalysisSummary:
    async def test_round_trips_evidence_refs_structure_exactly(self):
        ops = OpsStore()
        await ops._get_pool()
        connector = await ops.register_connector(
            ORG_PLATFORM_ID, "analysis-summary-test", "prometheus", "test",
        )
        evidence = [
            {"source": "prometheus", "query": "rate(http_errors[5m])", "window": "2026-08-26T10:00Z/10:05Z"},
            {"source": "loki", "query": '{service="order"} |= "panic"', "window": "2026-08-26T10:02Z/10:03Z"},
        ]
        saved = None
        try:
            saved = await ops.save_analysis_summary(
                ORG_PLATFORM_ID, connector.connection_id,
                summary="order-service 错误率突增，日志中出现 panic，怀疑近期部署引入的回归",
                evidence_refs=evidence,
            )
            assert saved.summary_id.startswith("opssum_")
            assert saved.evidence_refs == evidence

            fetched = await ops.get_analysis_summary(saved.summary_id)
            assert fetched is not None
            assert fetched.evidence_refs == evidence, "JSONB 存取一圈后结构必须逐项相同"
            assert fetched.summary == saved.summary
            assert fetched.org_id == ORG_PLATFORM_ID
            assert fetched.connection_id == connector.connection_id
        finally:
            await _cleanup(ops, connector.connection_id, [saved.summary_id] if saved else [])

    async def test_get_missing_summary_returns_none(self):
        ops = OpsStore()
        await ops._get_pool()
        assert await ops.get_analysis_summary("opssum_does_not_exist") is None


class TestListAnalysisSummaries:
    async def test_filters_by_connection_id_when_given(self):
        ops = OpsStore()
        await ops._get_pool()
        conn_a = await ops.register_connector(ORG_PLATFORM_ID, "list-test-a", "prometheus", "test")
        conn_b = await ops.register_connector(ORG_PLATFORM_ID, "list-test-b", "datadog", "test")
        summary_ids = []
        try:
            s_a = await ops.save_analysis_summary(
                ORG_PLATFORM_ID, conn_a.connection_id, "A 库的分析结论", [{"source": "a"}],
            )
            s_b = await ops.save_analysis_summary(
                ORG_PLATFORM_ID, conn_b.connection_id, "B 库的分析结论", [{"source": "b"}],
            )
            summary_ids = [s_a.summary_id, s_b.summary_id]

            only_a = await ops.list_analysis_summaries(ORG_PLATFORM_ID, connection_id=conn_a.connection_id)
            assert {s.summary_id for s in only_a} == {s_a.summary_id}

            both = await ops.list_analysis_summaries(ORG_PLATFORM_ID)
            assert {s_a.summary_id, s_b.summary_id} <= {s.summary_id for s in both}
        finally:
            pool = await ops._get_pool()
            async with pool.acquire() as conn:
                if summary_ids:
                    await conn.execute(
                        "DELETE FROM ops_analysis_summaries WHERE id = ANY($1::text[])", summary_ids,
                    )
                await conn.execute(
                    "DELETE FROM ops_system_connections WHERE id = ANY($1::text[])",
                    [conn_a.connection_id, conn_b.connection_id],
                )

    async def test_orders_newest_first(self):
        ops = OpsStore()
        await ops._get_pool()
        connector = await ops.register_connector(ORG_PLATFORM_ID, "list-test-order", "prometheus", "test")
        summary_ids = []
        try:
            first = await ops.save_analysis_summary(
                ORG_PLATFORM_ID, connector.connection_id, "先发生的", [{"source": "x"}],
            )
            summary_ids.append(first.summary_id)
            # 用真实的时间差撑开顺序，而不是假设两次 INSERT 之间时钟一定前进
            # 到不同的浮点数（本机往返可能快到落在同一微秒）。
            pool = await ops._get_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE ops_analysis_summaries SET created_at = $1 WHERE id = $2",
                    time.time() - 60, first.summary_id,
                )
            second = await ops.save_analysis_summary(
                ORG_PLATFORM_ID, connector.connection_id, "后发生的", [{"source": "y"}],
            )
            summary_ids.append(second.summary_id)

            results = await ops.list_analysis_summaries(ORG_PLATFORM_ID, connection_id=connector.connection_id)
            ids_in_order = [s.summary_id for s in results]
            assert ids_in_order.index(second.summary_id) < ids_in_order.index(first.summary_id)
        finally:
            await _cleanup(ops, connector.connection_id, summary_ids)
