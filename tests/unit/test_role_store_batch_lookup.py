"""`RoleStore.get_roles_by_ids_batch` 回归保护 —— N+1 审计发现（2026-08-26，
P1-14 修复之后对其它管理端点做的排查，见 CLAUDE.md §5）。

`admin_list_workflow_approvers` 原来对每个不重复的 `approver_role_id` 单独调
`get_role_by_id`。绝对数量目前很小（受限于工作流模板数，通常个位数），跟
`/admin/users` 那次 300 次量级不是一个规模，但补批量版是同样的常数成本。

不碰真实 Postgres，理由与 `test_admin_users_batch_queries.py` 相同
（`conftest.py` 无 DB fixture）。判别力核心是查询次数不随 role_id 数量增长。
"""

from __future__ import annotations

from typing import Any, Dict
from unittest.mock import AsyncMock

import pytest

from src.ragent_backend.role_store import RoleStore


class _FakeAcquireCtx:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _FakeAcquireCtx(self._conn)


def _role_row(role_id: str, name: str, org_id: str = "org-1") -> Dict[str, Any]:
    return {
        "id": role_id, "name": name, "display_name": name,
        "is_system": False, "org_id": org_id, "created_at": 0.0,
    }


class TestGetRolesByIdsBatch:
    @pytest.mark.asyncio
    async def test_maps_by_role_id(self, monkeypatch):
        store = RoleStore()
        fake_conn = AsyncMock()
        fake_conn.fetch = AsyncMock(return_value=[
            _role_row("r1", "hr_approver"),
            _role_row("r2", "finance_approver"),
        ])
        monkeypatch.setattr(store, "_get_pool", AsyncMock(return_value=_FakePool(fake_conn)))

        result = await store.get_roles_by_ids_batch(["r1", "r2", "r3"])

        assert result["r1"].name == "hr_approver"
        assert result["r2"].name == "finance_approver"
        assert "r3" not in result, "查不到的 role_id 不应该出现在结果里"

    @pytest.mark.asyncio
    async def test_single_query_regardless_of_role_id_count(self, monkeypatch):
        """判别力核心：查询次数是常数——旧的逐个 get_role_by_id 循环会是 N 次。"""
        store = RoleStore()
        fake_conn = AsyncMock()
        fake_conn.fetch = AsyncMock(return_value=[])
        monkeypatch.setattr(store, "_get_pool", AsyncMock(return_value=_FakePool(fake_conn)))

        await store.get_roles_by_ids_batch([f"r{i}" for i in range(50)])

        assert fake_conn.fetch.await_count == 1

    @pytest.mark.asyncio
    async def test_empty_input_does_not_query(self, monkeypatch):
        store = RoleStore()
        fake_conn = AsyncMock()
        monkeypatch.setattr(store, "_get_pool", AsyncMock(return_value=_FakePool(fake_conn)))

        result = await store.get_roles_by_ids_batch([])

        assert result == {}
        fake_conn.fetch.assert_not_called()
