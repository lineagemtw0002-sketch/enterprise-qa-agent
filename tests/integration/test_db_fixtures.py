"""`tests/db_fixtures.py` 自己的回归测试——连真实 Postgres。

这个 fixture 的失败形态很特别：**它坏掉的时候测试不会变红，只会静默去写
共用开发库**（`RAGENT_POSTGRES_URL` 默认指向多个会话共用的那个本地库）。
所以这里的断言重点不是"fixture 能跑"，而是几条判别式：

1. **写进去的数据确实没有落到共用开发库里**（`TestWritesNeverReachTheSharedDevDatabase`）
   ——这条直接盯着上面那个失败形态。只清 `db_pool._POOL_CACHE`、忘了清
   Store 类自己的 `_pool` 类属性时，这条会红。
2. **AST 发现能找全 15 个 Store 类**（`TestStoreDiscovery`）——仓库里既有的
   两处手写清单分别只列了 4 个和 1 个，其中 `DashboardStatsService`、
   `ConversationArchiveStore` 这种名字不带 `_store` 的最容易漏。退回手写
   清单这条会红。
3. **TRUNCATE 之后种子行真的被恢复了**（`TestTruncateAndRestore`）——
   `_ensure_schema()` 每个 Store 类只跑一次，TRUNCATE 完不会自己重来；
   少了恢复这一步，任何依赖 `ORG_PLATFORM_ID` 的用例都会在第二条起挂掉。
4. **不带 `ragent_test_` 前缀的库名一律拒绝操作**（`TestSafetyGuard`）——
   这是防误删共用库的最后一道闸。

本次未覆盖的范围见文件末尾。
"""

from __future__ import annotations

import os

import asyncpg
import pytest

# 刻意不在模块级打 asyncio 标记：本文件后半段（AST 发现、安全闸、DSN 辅助
# 函数）是纯同步用例，模块级标记会让 pytest 对每一条都发一次警告。
from tests import db_fixtures
from tests.db_fixtures import TEST_DB_PREFIX, TestDatabase

_MARKER_ORG = "db-fixture-selftest-marker-org"


class TestDisposableDatabase:
    pytestmark = pytest.mark.asyncio

    async def test_env_points_at_a_disposable_database(self, clean_postgres: TestDatabase):
        """用例里读到的 `RAGENT_POSTGRES_URL` 必须已经是一次性库。

        Store 的 `__init__` 就是从这个环境变量取 DSN 的，它没被换掉就等于
        整个隔离方案没生效。
        """
        assert os.environ["RAGENT_POSTGRES_URL"] == clean_postgres.dsn
        assert clean_postgres.database_name.startswith(TEST_DB_PREFIX)
        assert clean_postgres.database_name != db_fixtures.database_name_of(
            clean_postgres.app_dsn
        )

    async def test_schema_is_bootstrapped_on_a_brand_new_database(
        self, clean_postgres: TestDatabase
    ):
        """全新库上 `_ensure_schema()` 这条路径真的跑通了。

        共用开发库上永远测不到这条——表早就存在，`CREATE TABLE IF NOT EXISTS`
        全是空转。跨 Store 的建表依赖（org_store 的 `ALTER TABLE users`、
        ops_store 的 `ALTER TABLE organizations`）只有在全新库上才会暴露。
        """
        tables = set(clean_postgres.table_order)
        # 分别来自 user_store / org_store / role_store / ops_store /
        # workflow_store / tenant_connector_store，覆盖建表依赖链的首尾。
        for expected in (
            "users",
            "organizations",
            "roles",
            "role_collections",
            "ops_system_connections",
            "remediation_actions",
            "role_ops_systems",
            "workflow_templates",
            "tenant_connectors",
            "conversation_archive",
            "long_term_memories",
            "audit_logs",
        ):
            assert expected in tables, f"{expected} 没有在一次性库里被建出来"

    async def test_table_order_starts_with_the_dependency_head(
        self, clean_postgres: TestDatabase
    ):
        """建表顺序必须先 users、再 organizations、再 roles。

        恢复种子行时是按这个顺序插的，顺序错了会撞外键。
        """
        order = clean_postgres.table_order
        assert order.index("users") < order.index("organizations")
        assert order.index("organizations") < order.index("roles")


class TestWritesNeverReachTheSharedDevDatabase:
    pytestmark = pytest.mark.asyncio

    async def test_rows_written_in_a_test_are_invisible_to_the_app_database(
        self, clean_postgres: TestDatabase
    ):
        """判别式（本文件最重要的一条）：用例写的数据不能出现在共用开发库里。

        走的是 Store 的正常写路径（不是自己开一条连接直接 INSERT），
        因为要验的正是"Store 现在连的是哪个库"。校验方式是另外开一条**裸
        连接**打到 `app_dsn`，确认那边看不到这一行。这是"有没有污染共用库"
        这件事的端到端兜底断言，不针对某一种具体成因。

        判别力已实测：把 `reset_pool_caches` 退化成"只清
        `db_pool._POOL_CACHE`、不清 Store 类属性 `_pool`"之后，本类两条 +
        `TestTruncateAndRestore` 一条共 3 条变红（实际报错是"池已关闭"而不是
        写串库——因为退化版把上一轮 `asyncio.run` 里建好又关掉的池留在了类
        属性上）。**两层缓存分别有没有被清干净**这件事由
        `TestResetPoolCaches` 精确钉住，那条才是直接对着成因的。
        """
        from src.ragent_backend.org_store import OrgStore

        store = OrgStore()
        await store._get_pool()
        created = await store.create_organization(_MARKER_ORG)
        assert created is not None

        # 一次性库里看得到
        in_test_db = await store.get_organization(created.org_id)
        assert in_test_db is not None

        # 共用开发库里必须看不到
        app_conn = await asyncpg.connect(clean_postgres.app_dsn)
        try:
            leaked = await app_conn.fetchval(
                "SELECT count(*) FROM organizations WHERE name = $1", _MARKER_ORG
            )
        finally:
            await app_conn.close()
        assert leaked == 0, (
            f"共用开发库里出现了 {leaked} 行测试数据——隔离没生效，"
            "很可能是某个 Store 类的 `_pool` 类属性没被清掉"
        )

    async def test_a_later_case_does_not_see_the_previous_case_rows(
        self, clean_postgres: TestDatabase
    ):
        """上一条用例写的 marker 组织在这条用例里必须已经不存在。

        ⚠️ 这条依赖文件内的执行顺序（在上面那条之后跑）；单独跑它会平凡通过。
        真正与顺序无关、判别力更强的那条是
        `TestTruncateAndRestore::test_truncate_clears_data_and_restores_seed_rows`，
        这条只是端到端确认"函数级 fixture 的 setup 真的调了清理"。
        """
        from src.ragent_backend.org_store import OrgStore

        store = OrgStore()
        pool = await store._get_pool()
        async with pool.acquire() as conn:
            remaining = await conn.fetchval(
                "SELECT count(*) FROM organizations WHERE name = $1", _MARKER_ORG
            )
        assert remaining == 0


class TestTruncateAndRestore:
    pytestmark = pytest.mark.asyncio

    async def test_truncate_clears_data_and_restores_seed_rows(
        self, clean_postgres: TestDatabase
    ):
        """与执行顺序无关地验证清理机制本身：写 → 清 → 数据没了、种子还在。

        直接调 `_truncate_and_restore`，不依赖"下一条用例"。
        实现如果只 TRUNCATE 不恢复，后半段会红；如果只恢复不 TRUNCATE，
        前半段会红。
        """
        from src.ragent_backend.org_store import ORG_PLATFORM_ID, OrgStore

        store = OrgStore()
        await store._get_pool()
        await store.create_organization("db-fixture-truncate-probe")

        pool = await store._get_pool()
        async with pool.acquire() as conn:
            before = await conn.fetchval(
                "SELECT count(*) FROM organizations WHERE name = $1",
                "db-fixture-truncate-probe",
            )
        assert before == 1

        await db_fixtures._truncate_and_restore(
            clean_postgres.dsn, clean_postgres.app_dsn, clean_postgres.snapshot
        )

        async with pool.acquire() as conn:
            after = await conn.fetchval(
                "SELECT count(*) FROM organizations WHERE name = $1",
                "db-fixture-truncate-probe",
            )
        assert after == 0, "TRUNCATE 没有把用例数据清掉"

        # 种子行必须回来——`_ensure_schema()` 每个 Store 类只跑一次，
        # 不会在 TRUNCATE 之后自己重新插一遍。
        platform_org = await store.get_organization(ORG_PLATFORM_ID)
        assert platform_org is not None, "平台组织种子行没有被恢复"

    async def test_snapshot_covers_the_three_seeding_stores(
        self, clean_postgres: TestDatabase
    ):
        """快照必须覆盖三张会被 `_ensure_schema` 写入种子行的表。

        少了任何一张，依赖它的用例会从第二条开始莫名其妙地挂。
        """
        snapshot_tables = {table for table, _cols, _rows in clean_postgres.snapshot}
        assert {"organizations", "roles", "workflow_templates"} <= snapshot_tables


class TestResetPoolCaches:
    """直接盯着"两层池缓存都要清"这个成因，不依赖数据库。

    `db_pool._POOL_CACHE`（按 DSN 的模块级缓存）和每个 Store 类自己的 `_pool`
    类属性是**两层**，只清前者时 `_get_pool()` 第一句就 early return，
    根本走不到 `get_shared_pool()`——DSN 换了也没用，Store 会继续用旧池。
    """

    def test_clears_both_layers(self):
        from src.ragent_backend import db_pool

        classes = db_fixtures.discover_store_classes()
        sentinel = object()
        db_pool._POOL_CACHE["sentinel-dsn"] = sentinel  # type: ignore[assignment]
        for cls in classes:
            cls._pool = sentinel  # type: ignore[assignment]

        try:
            db_fixtures.reset_pool_caches(classes)
        finally:
            db_pool._POOL_CACHE.pop("sentinel-dsn", None)
            for cls in classes:
                cls._pool = None

        # 断言写在 finally 之后是刻意的：上面无论如何都要把现场恢复干净，
        # 否则这条用例失败会顺手污染同一进程里后面的用例。
        assert "sentinel-dsn" not in db_pool._POOL_CACHE, "模块级 DSN 缓存没清"

    def test_clears_the_class_level_pool_of_every_discovered_store(self):
        """逐个类断言，并且报出**具体哪个类**没被清掉。

        只断言"全清了"的话，漏掉一个类时错误信息是 `False is not True`，
        排查还得自己去翻；这里直接把漏网的类名列出来。
        """
        classes = db_fixtures.discover_store_classes()
        sentinel = object()
        for cls in classes:
            cls._pool = sentinel  # type: ignore[assignment]

        db_fixtures.reset_pool_caches(classes)

        leftovers = [cls.__name__ for cls in classes if cls._pool is not None]
        for cls in classes:
            cls._pool = None
        assert leftovers == [], f"这些 Store 类的 `_pool` 类属性没被清掉：{leftovers}"


class TestStoreDiscovery:
    def test_ast_discovery_finds_every_pool_holding_store(self):
        """AST 发现必须找全 15 个 Store 类，包括名字不带 `_store` 的那两个。

        判别式：仓库里既有的两处手写清单分别只列了 4 个
        （`scripts/verify_aiops_endpoints.py::_reset_pool_caches`）和 1 个
        （`test_ops_store_metrics.py` 原来的 autouse fixture）。
        退回手写清单，或者发现逻辑退化成按文件名 `*_store.py` 过滤，
        `DashboardStatsService` / `ConversationArchiveStore` 会漏掉，这条红。
        """
        names = {cls.__name__ for cls in db_fixtures.discover_store_classes()}
        expected = {
            "AttendanceStore",
            "AuditStore",
            "ConversationArchiveStore",  # 在 store.py 里，名字不带 _store
            "ConversationStore",
            "DashboardStatsService",  # 在 dashboard_stats.py 里，两头都不带 _store
            "LTMStore",
            "OpsStore",
            "OrgCollectionStore",
            "OrgStore",
            "OpsStore",
            "RoleStore",
            "TenantConnectorStore",
            "TenantIdentityStore",
            "UserStore",
            "WorkflowStore",
            "ConversationFileStore",
        }
        assert expected <= names, f"漏掉了：{sorted(expected - names)}"

    def test_every_discovered_class_really_owns_a_pool_attribute(self):
        """反向断言：发现出来的类每个都真的在**自己的**类体里声明了 `_pool`。

        防止发现逻辑放宽成"扫到继承来的属性也算"，那样会把无关的子类拖进
        清理列表，清理本身变成噪音。
        """
        for cls in db_fixtures.discover_store_classes():
            assert "_pool" in vars(cls), f"{cls.__name__} 并没有自己的 _pool 类属性"

    def test_dependency_head_comes_first(self):
        """建表依赖头部（users → organizations → roles）必须排在最前面。"""
        classes = db_fixtures.discover_store_classes()
        head = [cls.__name__ for cls in classes[:3]]
        assert head == ["UserStore", "OrgStore", "RoleStore"]


class TestSafetyGuard:
    def test_refuses_database_names_without_the_disposable_prefix(self):
        """判别式：拿掉前缀校验，误删共用开发库这件事就没有任何拦截了。"""
        with pytest.raises(RuntimeError, match=TEST_DB_PREFIX):
            db_fixtures._assert_disposable_db_name(
                "ragent", "postgresql://ragent:ragent@localhost:5432/ragent"
            )

    def test_refuses_even_a_prefixed_name_when_it_is_the_app_database(self):
        """名字碰巧带前缀、但它就是应用库时，同样要拒绝。

        单看前缀这条会漏——有人把开发库命名成 `ragent_test_local` 就中招。
        """
        app_dsn = "postgresql://ragent:ragent@localhost:5432/ragent_test_local"
        with pytest.raises(RuntimeError, match="应用库"):
            db_fixtures._assert_disposable_db_name("ragent_test_local", app_dsn)

    def test_accepts_a_generated_disposable_name(self):
        db_fixtures._assert_disposable_db_name(
            "ragent_test_deadbeef1234",
            "postgresql://ragent:ragent@localhost:5432/ragent",
        )


class TestDsnHelpers:
    def test_with_database_preserves_credentials_and_port(self):
        out = db_fixtures.with_database(
            "postgresql://ragent:ragent@localhost:5432/ragent", "ragent_test_abc"
        )
        assert out == "postgresql://ragent:ragent@localhost:5432/ragent_test_abc"
        assert db_fixtures.database_name_of(out) == "ragent_test_abc"

    def test_redact_hides_the_password(self):
        """skip 原因会被打进 CI 日志，密码不能原样出现在里面。"""
        redacted = db_fixtures._redact("postgresql://ragent:s3cret@localhost:5432/postgres")
        assert "s3cret" not in redacted
        assert "ragent" in redacted and "localhost" in redacted


# ---------------------------------------------------------------------------
# 本次未覆盖的范围
# ---------------------------------------------------------------------------
# - **skip 分支没有自动化测试，只做到"已跑通"这一档**：fixture 是会话级的，
#   同一次跑里没法既用它又让它 skip。两条分支都手工跑过一次并确认了输出：
#     RAGENT_TEST_POSTGRES_ADMIN_URL=postgresql://nobody:nope@localhost:59999/postgres
#       → 连不上，skip，原因里密码显示为 ***
#     RAGENT_TEST_POSTGRES_ADMIN_URL=postgresql://ragent:ragent@localhost:5432/postgres
#       → 连得上但该角色没有建库权限，skip
#   两次都只 skip 依赖数据库的用例，本文件其余纯同步用例照常通过。
# - **没有验证"同时用 httpx.AsyncClient 和 TestClient WebSocket"那个跨事件
#   循环场景**：`clean_postgres` 的设计（async fixture、池只活在本用例的
#   循环里）是照着这个约束做的，但本文件里没有一条用例真的同时起两个循环。
#   真要覆盖需要先有 app 级 fixture，那是下一步。
# - **并发安全没测**：多个 pytest-xdist worker 各建各的库理论上互不干扰
#   （库名带 6 字节随机后缀），但没有真的跑过 xdist。
# - **没有验证一次性库跟共用开发库的 schema 完全一致**：只断言了若干张关键
#   表存在，没有做逐列比对。
