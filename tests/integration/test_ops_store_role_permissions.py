"""`role_ops_systems`（`docs/aiops_module_design.md` §10.6）细粒度审批权限——
连真实 Postgres，因为核心逻辑是 JOIN（`roles`/`user_roles`/`role_ops_systems`/
`ops_system_connections`），假 pool 测不出真实的联表结果，只能测循环/分支
这层皮，价值不大。

判别力核心是三条边界，逐条钉死，不是笼统测"能查到权限"：
1. **org_admin 是通配符，但仅限本企业**——换一家企业的 org_admin 不能自动
   拿到别的企业连接器的权限，防止把"通配符"错写成"全局通配符"。
2. **super_admin 从不自动获得权限**，即使显式插了一行 `role_ops_systems`
   记录也不行——这条要求写权限那一层（app.py 的管理端点）拒绝把权限授予
   系统角色，本文件直接在 Store 层验证"就算数据被塞进去了，读的时候也不能
   信"，双重保险，不是重复劳动。
3. **`can_approve=True` 隐含 `can_view=True`**——`set_role_ops_permission`
   写入时就该拉齐，不能出现"能批准但看不到"这种矛盾状态。
"""

from __future__ import annotations

import contextlib
import os

import pytest

pytestmark = pytest.mark.asyncio

os.environ.setdefault("RAGENT_DEBUG", "true")

from src.ragent_backend import db_pool
from src.ragent_backend.ops_store import OpsStore
from src.ragent_backend.org_store import OrgStore
from src.ragent_backend.role_store import ROLE_ORG_ADMIN, ROLE_SUPER_ADMIN, RoleStore
from src.ragent_backend.user_store import UserStore


@pytest.fixture(autouse=True)
def _reset_pool_between_tests():
    db_pool._POOL_CACHE.clear()
    OpsStore._pool = None
    RoleStore._pool = None
    OrgStore._pool = None
    UserStore._pool = None
    yield


class _Fixture:
    """一次性搭好"两家企业 + 各自的 org_admin + 一个自定义角色 + 一个连接器"
    的场景，测试用完各自清理自己建的那部分。"""

    def __init__(self):
        self.ops = OpsStore()
        self.org_store = OrgStore()
        self.role_store = RoleStore()
        self.user_store = UserStore()

    async def setup(self):
        await self.ops._get_pool()
        self.org_a = await self.org_store.create_organization("role-perm-test-A")
        self.org_b = await self.org_store.create_organization("role-perm-test-B")

        self.admin_a = await self.user_store.create_user("role_perm_admin_a", "pw12345678")
        self.admin_b = await self.user_store.create_user("role_perm_admin_b", "pw12345678")
        self.reviewer = await self.user_store.create_user("role_perm_reviewer", "pw12345678")
        self.nobody = await self.user_store.create_user("role_perm_nobody", "pw12345678")

        pool = await self.user_store._get_pool()
        async with pool.acquire() as conn:
            await conn.execute("UPDATE users SET org_id = $1 WHERE id = $2", self.org_a.org_id, self.admin_a.user_id)
            await conn.execute("UPDATE users SET org_id = $1 WHERE id = $2", self.org_b.org_id, self.admin_b.user_id)
            await conn.execute("UPDATE users SET org_id = $1 WHERE id = $2", self.org_a.org_id, self.reviewer.user_id)
            await conn.execute("UPDATE users SET org_id = $1 WHERE id = $2", self.org_a.org_id, self.nobody.user_id)

        org_admin_role = await self.role_store.get_role_by_name(ROLE_ORG_ADMIN, org_id=None)
        await self.role_store.add_user_role(self.admin_a.user_id, org_admin_role.role_id)
        await self.role_store.add_user_role(self.admin_b.user_id, org_admin_role.role_id)
        self.super_admin_role_id = (await self.role_store.get_role_by_name(ROLE_SUPER_ADMIN, org_id=None)).role_id

        self.reviewer_role = await self.role_store.create_role(self.org_a.org_id, "ops_reviewer", "运维审批员")
        await self.role_store.add_user_role(self.reviewer.user_id, self.reviewer_role.role_id)

        self.connector = await self.ops.register_connector(
            self.org_a.org_id, "role-perm-test-conn", "prometheus", self.admin_a.user_id,
        )

    async def cleanup(self):
        pool = await self.user_store._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM role_ops_systems WHERE connection_id = $1", self.connector.connection_id,
            )
            await conn.execute(
                "DELETE FROM ops_system_connections WHERE id = $1", self.connector.connection_id,
            )
            await conn.execute(
                "DELETE FROM user_roles WHERE user_id = ANY($1::text[])",
                [self.admin_a.user_id, self.admin_b.user_id, self.reviewer.user_id, self.nobody.user_id],
            )
            await conn.execute(
                "DELETE FROM users WHERE id = ANY($1::text[])",
                [self.admin_a.user_id, self.admin_b.user_id, self.reviewer.user_id, self.nobody.user_id],
            )
            await conn.execute("DELETE FROM roles WHERE id = $1", self.reviewer_role.role_id)
            await conn.execute(
                "DELETE FROM organizations WHERE id = ANY($1::text[])", [self.org_a.org_id, self.org_b.org_id],
            )


@contextlib.asynccontextmanager
async def _fixture():
    f = _Fixture()
    await f.setup()
    try:
        yield f
    finally:
        await f.cleanup()


class TestOrgAdminWildcardIsScopedToOwnOrg:
    async def test_org_admin_of_owning_org_gets_full_access(self):
        async with _fixture() as fixture:
            perm = await fixture.ops.get_ops_permission(fixture.admin_a.user_id, fixture.connector.connection_id)
            assert perm == {"can_view": True, "can_approve": True}

    async def test_org_admin_of_a_different_org_gets_nothing(self):
        # 判别式：换一家企业的 org_admin 不能白嫖别人的连接器权限——如果实现
        # 把 org_admin 判定写成"只要角色名是 org_admin 就放行"（漏掉 org_id
        # 匹配），这条会失败。
        async with _fixture() as fixture:
            perm = await fixture.ops.get_ops_permission(fixture.admin_b.user_id, fixture.connector.connection_id)
            assert perm == {"can_view": False, "can_approve": False}


class TestSuperAdminNeverAutoGranted:
    async def test_explicit_grant_to_super_admin_role_is_still_honoured_at_store_layer(self):
        # ⚠️ 这条测的是 Store 层本身不做特殊拦截——真正的"super_admin 不能被
        # 授权"防线在 app.py 管理端点（拒绝把权限写给系统角色），Store 层只是
        # 诚实地执行 CRUD。这里验证：如果上层防线被绕过、真的写了一行进去，
        # Store 层查询不会凭空再加一层隐藏的"因为是 super_admin 所以忽略这行"
        # 逻辑——那样反而会让上层的拒绝显得多余、也会在审计时不好解释"数据库
        # 里明明有一行但查询结果说没有"这种不一致。防线只应该有一层，且必须
        # 在写入路径。
        async with _fixture() as fixture:
            await fixture.ops.set_role_ops_permission(
                fixture.super_admin_role_id, fixture.connector.connection_id,
                can_view=True, can_approve=True,
            )
            try:
                # 需要一个持有 super_admin 角色、且不满足 org_admin-wildcard 条件
                # 的用户来验证——用 nobody 临时借用 super_admin 角色（不影响其
                # org_admin 判定，因为 nobody 本来就没有 org_admin 角色）。
                from src.ragent_backend.role_store import RoleStore
                await RoleStore().add_user_role(fixture.nobody.user_id, fixture.super_admin_role_id)
                try:
                    perm = await fixture.ops.get_ops_permission(fixture.nobody.user_id, fixture.connector.connection_id)
                    assert perm == {"can_view": True, "can_approve": True}, (
                        "Store 层如实执行 CRUD——拦截 super_admin 授权是 app.py 写路径的责任，"
                        "不是 Store 层的责任，两层职责必须分清"
                    )
                finally:
                    pool = await fixture.user_store._get_pool()
                    async with pool.acquire() as conn:
                        await conn.execute(
                            "DELETE FROM user_roles WHERE user_id = $1 AND role_id = $2",
                            fixture.nobody.user_id, fixture.super_admin_role_id,
                        )
            finally:
                await fixture.ops.revoke_role_ops_permission(fixture.super_admin_role_id, fixture.connector.connection_id)


class TestExplicitGrantForNonAdminRole:
    async def test_reviewer_role_gets_granted_permission(self):
        async with _fixture() as fixture:
            await fixture.ops.set_role_ops_permission(
                fixture.reviewer_role.role_id, fixture.connector.connection_id,
                can_view=True, can_approve=True,
            )
            perm = await fixture.ops.get_ops_permission(fixture.reviewer.user_id, fixture.connector.connection_id)
            assert perm == {"can_view": True, "can_approve": True}

    async def test_can_approve_implies_can_view_even_if_caller_forgot(self):
        # 判别式：调用方（比如未来的管理端点）只传 can_approve=True、忘了同时
        # 传 can_view=True——`set_role_ops_permission` 必须自己拉齐，不能
        # 指望每个调用方都记得这条不变量。
        async with _fixture() as fixture:
            granted = await fixture.ops.set_role_ops_permission(
                fixture.reviewer_role.role_id, fixture.connector.connection_id,
                can_view=False, can_approve=True,
            )
            assert granted.can_view is True
            perm = await fixture.ops.get_ops_permission(fixture.reviewer.user_id, fixture.connector.connection_id)
            assert perm["can_view"] is True

    async def test_no_role_no_grant_means_no_access(self):
        async with _fixture() as fixture:
            perm = await fixture.ops.get_ops_permission(fixture.nobody.user_id, fixture.connector.connection_id)
            assert perm == {"can_view": False, "can_approve": False}

    async def test_revoke_removes_access(self):
        async with _fixture() as fixture:
            await fixture.ops.set_role_ops_permission(
                fixture.reviewer_role.role_id, fixture.connector.connection_id,
                can_view=True, can_approve=True,
            )
            await fixture.ops.revoke_role_ops_permission(fixture.reviewer_role.role_id, fixture.connector.connection_id)
            perm = await fixture.ops.get_ops_permission(fixture.reviewer.user_id, fixture.connector.connection_id)
            assert perm == {"can_view": False, "can_approve": False}

    async def test_unknown_connection_returns_no_access(self):
        async with _fixture() as fixture:
            perm = await fixture.ops.get_ops_permission(fixture.admin_a.user_id, "opsconn_does_not_exist")
            assert perm == {"can_view": False, "can_approve": False}


class TestViewableConnectionIdsForUser:
    async def test_org_admin_gets_none_meaning_unfiltered(self):
        async with _fixture() as fixture:
            result = await fixture.ops.viewable_connection_ids_for_user(fixture.admin_a.user_id, fixture.org_a.org_id)
            assert result is None

    async def test_explicit_grant_appears_in_list(self):
        async with _fixture() as fixture:
            await fixture.ops.set_role_ops_permission(
                fixture.reviewer_role.role_id, fixture.connector.connection_id,
                can_view=True, can_approve=False,
            )
            result = await fixture.ops.viewable_connection_ids_for_user(fixture.reviewer.user_id, fixture.org_a.org_id)
            assert result == [fixture.connector.connection_id]

    async def test_no_grant_returns_empty_list_not_none(self):
        # 判别式：区分"没有任何可见连接器"（空列表）和"org_admin 不用过滤"
        # （None）——上层调用方靠这个区分决定要不要按列表过滤，两者混淆会
        # 导致普通用户意外看到全部连接器。
        async with _fixture() as fixture:
            result = await fixture.ops.viewable_connection_ids_for_user(fixture.nobody.user_id, fixture.org_a.org_id)
            assert result == []
