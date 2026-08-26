"""管理端用户列表 N+1 修复的回归保护 —— P1-14（2026-08-26）。

原始缺陷：`admin_list_users` 对每个用户单独调 `get_org_for_user` +
`get_user_roles` + `get_allowed_collections_for_user`（后者自己就是 3 次
查询），50 用户约 300 次串行查询。改为三个批量方法，查询数不再随用户数
线性增长。

不碰真实 Postgres——本仓库的 `RAGENT_POSTGRES_URL` 默认指向跨会话共用的
本地库，`conftest.py` 没有 DB fixture。用最小的手工 fake 替身接住
`asyncpg` 的 `Pool.acquire()` / `Connection.fetch()`，专门验证两件事：
① 分组/映射逻辑对不对；② 查询次数是常数，不随用户数增长
（这是判别力所在——旧的单用户方法在循环里调用，查询数会跟着 N 涨）。
"""

from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import AsyncMock

import pytest

from src.ragent_backend.org_store import OrgStore
from src.ragent_backend.role_store import ROLE_ORG_ADMIN, RoleStore


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


def _role_row(user_id: str, role_id: str, name: str, display_name: str = "", org_id: str = "org-1") -> Dict[str, Any]:
    return {
        "user_id": user_id, "id": role_id, "name": name, "display_name": display_name or name,
        "is_system": False, "org_id": org_id, "created_at": 0.0,
    }


class TestGetUserRolesBatch:
    @pytest.mark.asyncio
    async def test_groups_roles_by_user_id(self, monkeypatch):
        store = RoleStore()
        fake_conn = AsyncMock()
        fake_conn.fetch = AsyncMock(return_value=[
            _role_row("u1", "r1", "hr_admin"),
            _role_row("u1", "r2", "auditor"),
            _role_row("u2", "r1", "hr_admin"),
        ])
        monkeypatch.setattr(store, "_get_pool", AsyncMock(return_value=_FakePool(fake_conn)))

        result = await store.get_user_roles_batch(["u1", "u2", "u3"])

        assert {r.name for r in result["u1"]} == {"hr_admin", "auditor"}
        assert {r.name for r in result["u2"]} == {"hr_admin"}
        assert result["u3"] == [], "没有角色的用户应该拿到空列表，不是缺 key"

    @pytest.mark.asyncio
    async def test_single_query_regardless_of_user_count(self, monkeypatch):
        """判别力核心：查询次数是常数——旧实现在调用方循环里跑，会是 N 次。"""
        store = RoleStore()
        fake_conn = AsyncMock()
        fake_conn.fetch = AsyncMock(return_value=[])
        monkeypatch.setattr(store, "_get_pool", AsyncMock(return_value=_FakePool(fake_conn)))

        await store.get_user_roles_batch([f"u{i}" for i in range(200)])

        assert fake_conn.fetch.await_count == 1

    @pytest.mark.asyncio
    async def test_empty_input_does_not_query(self, monkeypatch):
        store = RoleStore()
        fake_conn = AsyncMock()
        monkeypatch.setattr(store, "_get_pool", AsyncMock(return_value=_FakePool(fake_conn)))

        result = await store.get_user_roles_batch([])

        assert result == {}
        fake_conn.fetch.assert_not_called()


class TestGetAllowedCollectionsForUsersBatch:
    @pytest.mark.asyncio
    async def test_org_admin_gets_wildcard(self, monkeypatch):
        store = RoleStore()
        fake_conn = AsyncMock()
        fake_conn.fetch = AsyncMock(side_effect=[
            [{"id": "u1", "org_id": "org-1"}],  # users
            [{"user_id": "u1", "role_id": "r1", "role_name": ROLE_ORG_ADMIN}],  # roles
            [],  # role_collections（org_admin 走通配符分支，不会用到这批数据，但仍会查一次）
        ])
        monkeypatch.setattr(store, "_get_pool", AsyncMock(return_value=_FakePool(fake_conn)))

        result = await store.get_allowed_collections_for_users_batch(["u1"])

        assert result["u1"] == ["*"]

    @pytest.mark.asyncio
    async def test_regular_role_gets_union_of_collections(self, monkeypatch):
        store = RoleStore()
        fake_conn = AsyncMock()
        fake_conn.fetch = AsyncMock(side_effect=[
            [{"id": "u1", "org_id": "org-1"}, {"id": "u2", "org_id": "org-1"}],
            [
                {"user_id": "u1", "role_id": "r1", "role_name": "hr_viewer"},
                {"user_id": "u2", "role_id": "r2", "role_name": "finance_viewer"},
            ],
            [
                {"org_id": "org-1", "role_id": "r1", "collection_name": "hr_kb"},
                {"org_id": "org-1", "role_id": "r2", "collection_name": "finance_kb"},
            ],
        ])
        monkeypatch.setattr(store, "_get_pool", AsyncMock(return_value=_FakePool(fake_conn)))

        result = await store.get_allowed_collections_for_users_batch(["u1", "u2"])

        assert result["u1"] == ["hr_kb"]
        assert result["u2"] == ["finance_kb"]

    @pytest.mark.asyncio
    async def test_role_collection_scoped_to_matching_org_only(self, monkeypatch):
        """双重过滤（org_id + role_id）不能退化成只按 role_id 过滤——即使两个
        不同组织意外用了同一个 role_id（不应该发生，但测的是过滤逻辑本身，
        不是"role_id 全局唯一"这个假设），也不该把 org-2 的 collection
        分给 org-1 的用户。"""
        store = RoleStore()
        fake_conn = AsyncMock()
        fake_conn.fetch = AsyncMock(side_effect=[
            [{"id": "u1", "org_id": "org-1"}],
            [{"user_id": "u1", "role_id": "r1", "role_name": "viewer"}],
            [
                {"org_id": "org-1", "role_id": "r1", "collection_name": "org1_kb"},
                {"org_id": "org-2", "role_id": "r1", "collection_name": "org2_kb"},
            ],
        ])
        monkeypatch.setattr(store, "_get_pool", AsyncMock(return_value=_FakePool(fake_conn)))

        result = await store.get_allowed_collections_for_users_batch(["u1"])

        assert result["u1"] == ["org1_kb"], f"不应该混进 org-2 的 collection: {result}"

    @pytest.mark.asyncio
    async def test_user_with_no_roles_gets_empty_list(self, monkeypatch):
        store = RoleStore()
        fake_conn = AsyncMock()
        fake_conn.fetch = AsyncMock(side_effect=[
            [{"id": "u1", "org_id": "org-1"}],
            [],
            [],
        ])
        monkeypatch.setattr(store, "_get_pool", AsyncMock(return_value=_FakePool(fake_conn)))

        result = await store.get_allowed_collections_for_users_batch(["u1"])

        assert result["u1"] == []

    @pytest.mark.asyncio
    async def test_fixed_query_count_regardless_of_user_count(self, monkeypatch):
        """判别力核心：固定 3 次查询，不随用户数增长——单用户版本本身就是
        3 次查询，旧实现在循环里调用相当于 3×N 次。用户都持有角色，确保
        第三次（role_collections）查询真的会跑，不是被"没有角色可查"这条
        优化路径跳过。"""
        store = RoleStore()
        fake_conn = AsyncMock()
        fake_conn.fetch = AsyncMock(side_effect=[
            [{"id": f"u{i}", "org_id": "org-1"} for i in range(100)],
            [{"user_id": f"u{i}", "role_id": "r_shared", "role_name": "viewer"} for i in range(100)],
            [{"org_id": "org-1", "role_id": "r_shared", "collection_name": "shared_kb"}],
        ])
        monkeypatch.setattr(store, "_get_pool", AsyncMock(return_value=_FakePool(fake_conn)))

        result = await store.get_allowed_collections_for_users_batch([f"u{i}" for i in range(100)])

        assert fake_conn.fetch.await_count == 3
        assert result["u0"] == ["shared_kb"]
        assert result["u99"] == ["shared_kb"]

    @pytest.mark.asyncio
    async def test_no_roles_at_all_skips_the_collection_query(self, monkeypatch):
        """没有任何用户持有角色时，跳过第三次查询是有意的优化，不是缺陷——
        这条测试把这个行为钉死，避免以后被误当成 bug"改回"三次。"""
        store = RoleStore()
        fake_conn = AsyncMock()
        fake_conn.fetch = AsyncMock(side_effect=[
            [{"id": "u1", "org_id": "org-1"}],
            [],
        ])
        monkeypatch.setattr(store, "_get_pool", AsyncMock(return_value=_FakePool(fake_conn)))

        result = await store.get_allowed_collections_for_users_batch(["u1"])

        assert fake_conn.fetch.await_count == 2
        assert result["u1"] == []

    @pytest.mark.asyncio
    async def test_empty_input_does_not_query(self, monkeypatch):
        store = RoleStore()
        fake_conn = AsyncMock()
        monkeypatch.setattr(store, "_get_pool", AsyncMock(return_value=_FakePool(fake_conn)))

        result = await store.get_allowed_collections_for_users_batch([])

        assert result == {}
        fake_conn.fetch.assert_not_called()


class TestGetOrgsForUsersBatch:
    @pytest.mark.asyncio
    async def test_maps_org_by_user_id(self, monkeypatch):
        store = OrgStore()
        fake_conn = AsyncMock()
        fake_conn.fetch = AsyncMock(return_value=[
            {"user_id": "u1", "id": "org-1", "name": "Acme", "is_platform": False, "created_at": 0.0},
            {"user_id": "u2", "id": "org-2", "name": "Globex", "is_platform": False, "created_at": 0.0},
        ])
        monkeypatch.setattr(store, "_get_pool", AsyncMock(return_value=_FakePool(fake_conn)))

        result = await store.get_orgs_for_users_batch(["u1", "u2", "u3"])

        assert result["u1"].name == "Acme"
        assert result["u2"].name == "Globex"
        assert "u3" not in result, "查不到组织的用户不应该出现在结果里（跟单用户版返回 None 是同一语义，只是容器形态不同）"

    @pytest.mark.asyncio
    async def test_single_query_regardless_of_user_count(self, monkeypatch):
        store = OrgStore()
        fake_conn = AsyncMock()
        fake_conn.fetch = AsyncMock(return_value=[])
        monkeypatch.setattr(store, "_get_pool", AsyncMock(return_value=_FakePool(fake_conn)))

        await store.get_orgs_for_users_batch([f"u{i}" for i in range(200)])

        assert fake_conn.fetch.await_count == 1

    @pytest.mark.asyncio
    async def test_empty_input_does_not_query(self, monkeypatch):
        store = OrgStore()
        fake_conn = AsyncMock()
        monkeypatch.setattr(store, "_get_pool", AsyncMock(return_value=_FakePool(fake_conn)))

        result = await store.get_orgs_for_users_batch([])

        assert result == {}
        fake_conn.fetch.assert_not_called()
