"""Postgres 测试隔离 fixture —— 每个测试会话开一个一次性数据库。

这是 `CLAUDE.md` P1-4（`ragent_backend` + `tool_agent` 12,200 行零测试覆盖）
那条技术债的**唯一阻塞项**："没有 DB fixture 隔离方案"。方案已由用户拍板，
本模块是它的实现。


怎么用
------

绝大多数测试只需要 `clean_postgres` 这一个 fixture（函数级，异步）::

    import pytest

    pytestmark = pytest.mark.asyncio


    class TestSomething:
        async def test_x(self, clean_postgres):
            store = OrgStore()          # 注意：一定要在 fixture 之后再构造
            await store._get_pool()     # 连的是本次会话的一次性库
            ...                         # 不用写任何手工 DELETE 清理

拿到 `clean_postgres` 就意味着：

1. `RAGENT_POSTGRES_URL` 已经指向本次会话独享的 `ragent_test_<随机后缀>` 库；
2. 全部 Store 的表结构已经建好（跟应用真实启动走的是同一条 `_ensure_schema`）；
3. 上一条用例写进去的数据已经被清空，种子数据（平台组织、内置角色、内置
   工作流模板）已经恢复到刚建库时的样子；
4. 连接池是在**当前用例自己的事件循环**里新建的，用例结束时会被正常关闭。

需要拿到 DSN 字符串本身（比如要自己开一条裸连接做断言）时用
`postgres_test_db`，它是会话级的，返回一次性库的 DSN。


为什么这么设计
--------------

**为什么不是事务回滚。** 常见做法是"每条用例开一个事务、结束回滚"，本仓库
用不了：14+ 个 Store 走的是 `db_pool.py` 的共享连接池，测试自己开的事务盖不住
它们从池里另外拿到的连接，回滚不了。这条路已经排除，不要再走回去。

**为什么建库而不是清表就行。** 建库能顺带验证 `_ensure_schema()` 在一个真正
全新的库上跑得起来——这条路径在共用开发库上永远测不到（表早就存在，
`CREATE TABLE IF NOT EXISTS` 全是空转）。事实上写这个 fixture 时就撞到了：
`org_store._ensure_schema` 里有 `ALTER TABLE users`，`ops_store` 里有
`ALTER TABLE organizations`，全新库上建表顺序是有依赖的（见 `_BOOTSTRAP_HEAD`）。

**为什么绝不碰共用开发库。** 这台机器上 `RAGENT_POSTGRES_URL` 默认指向多个
会话共用的本地库，里面有别人的数据。本模块的每个破坏性操作（DROP / TRUNCATE /
pg_terminate_backend）之前都有一道 `_assert_disposable_db_name()`：库名必须以
`ragent_test_` 开头，且不得等于应用库名，否则直接抛异常。

**为什么用 AST 发现 Store 类，而不是写一张清单。** 池缓存有两层——
`db_pool._POOL_CACHE`（按 DSN）**和每个 Store 类自己的类级别 `_pool` 属性**，
换了 DSN 之后只清前者，`OpsStore._pool` 这类类属性还攥着连旧库的池，测试会
继续读写共用开发库。既有代码里两处清理（`scripts/verify_aiops_endpoints.py`
的 `_reset_pool_caches`、`test_ops_store_metrics.py` 顶部的 autouse fixture）
都是手写清单，一个列了 4 个类、一个列了 1 个，实际有 15 个——**手写清单会漏、
会过期，而漏掉的后果是静默污染共用库**。这里改成扫 `src/` 的 AST 找"类体里
声明了 `_pool` 类属性"的类（只解析、不 import 无关模块），新增 Store 自动
被覆盖。不用 `inspect.getsource()` 做字符串匹配是仓库的硬性要求（会命中注释
和 docstring，本仓库踩过两次）。

**为什么函数级 fixture 是 async 的。** asyncpg 的池绑定创建它的事件循环，
跨循环复用会报 `InterfaceError: another operation is in progress`。
pytest-asyncio 默认每条用例一个新循环，所以池必须在用例自己的循环里创建、
也在同一个循环里关闭——async fixture 的 setup/teardown 都跑在用例的循环里，
正好满足；会话级 fixture 反而**不能**是 async 的（它的循环活不到用例里），
所以它是同步的，内部用 `asyncio.run()` 起临时循环干活，yield 之前把池全部
关掉，不把任何池带进用例。

**为什么清数据用 TRUNCATE ... RESTART IDENTITY CASCADE。** 快，而且不用关心
外键顺序——本仓库刚因为删除顺序踩过 `ForeignKeyViolationError`
（`ops_store` 删连接器时子表没有 ON DELETE CASCADE）。

**为什么清完还要恢复种子数据。** 三个 Store 的 `_ensure_schema` 会写入种子行
（`organizations` 的平台组织、`roles` 的 super_admin/org_admin、
`workflow_templates` 的内置模板），而 `_ensure_schema` 每个 Store 类只跑一次，
TRUNCATE 之后不会自动重来。这里的做法是建库时把全部表的初始行拍一张快照，
每条用例前 TRUNCATE 完按**建表顺序**重新插回去（建表顺序天然满足外键依赖，
不需要另外做拓扑排序）。


什么情况下会 skip
-----------------

连不上 Postgres、或者连得上但没有 `CREATE DATABASE` 权限时，`pytest.skip`
并给出可操作的原因，**不会**让 `tests/unit` 整体挂掉。找"维护连接"的顺序：

1. 环境变量 `RAGENT_TEST_POSTGRES_ADMIN_URL`（显式指定，指定了就只试这一个）；
2. 把 `RAGENT_POSTGRES_URL` 的库名换成 `postgres`（应用角色本身有建库权限时）；
3. `postgresql://<当前系统用户>@<同一 host:port>/postgres`（Homebrew 装的
   Postgres 上通常是超级用户）。

候选必须满足 `rolsuper OR rolcreatedb`，否则继续试下一个。
"""

from __future__ import annotations

import ast
import asyncio
import getpass
import importlib
import os
import pathlib
import secrets
import urllib.parse
from collections.abc import Sequence
from typing import Any

import asyncpg
import pytest
import pytest_asyncio

# 一次性库的固定前缀。**所有破坏性操作都以它为准做白名单校验**，
# 不带这个前缀的库名一律拒绝操作（见 `_assert_disposable_db_name`）。
TEST_DB_PREFIX = "ragent_test_"

_PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
_SRC_DIR = _PROJECT_ROOT / "src"

# 建表顺序的依赖头部。全新库上这三个有硬依赖，不能靠字典序：
#   user_store  → 建 users
#   org_store   → 建 organizations，并 `ALTER TABLE users ADD COLUMN org_id`
#   role_store  → 建 roles/user_roles/role_collections，引用 organizations + users
# 其余 Store（ops/collection/tenant_*/workflow/attendance…）都只引用这三张，
# 排在后面即可，顺序无所谓。这里硬编码的是**依赖关系**，不是"有哪些 Store"
# 的清单——后者由 AST 发现，新增 Store 不需要改这里。
_BOOTSTRAP_HEAD: tuple[str, ...] = ("UserStore", "OrgStore", "RoleStore")


# --------------------------------------------------------------------------
# Store 类发现（AST，不 import 无关模块）
# --------------------------------------------------------------------------

def _module_defines_pool_classes(path: pathlib.Path) -> list[str]:
    """解析一个 .py 文件，返回其中"类体里声明了 `_pool` 类属性"的类名。

    只做 `ast.parse`，不 import——`src/` 下有 `workflow.py`/`intent.py` 这类
    import 就会拉起模型客户端的重模块，为了找几个 Store 类把它们全 import
    一遍既慢又可能有副作用。
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return []

    found: list[str] = []
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        for stmt in node.body:
            targets: list[str] = []
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                targets = [stmt.target.id]
            elif isinstance(stmt, ast.Assign):
                targets = [t.id for t in stmt.targets if isinstance(t, ast.Name)]
            if "_pool" in targets:
                found.append(node.name)
                break
    return found


def discover_store_classes() -> list[type]:
    """扫 `src/` 找出全部持有类级别 `_pool` 的 Store 类，按建表依赖排序。

    返回的顺序 = `_BOOTSTRAP_HEAD` 里的几个在前，其余按"模块名.类名"字典序，
    保证每次跑的顺序稳定（否则建表顺序会随文件系统遍历顺序漂移）。
    """
    discovered: list[tuple[str, str]] = []
    for path in sorted(_SRC_DIR.rglob("*.py")):
        class_names = _module_defines_pool_classes(path)
        if not class_names:
            continue
        module_name = ".".join(path.relative_to(_PROJECT_ROOT).with_suffix("").parts)
        for class_name in sorted(class_names):
            discovered.append((module_name, class_name))

    classes: list[type] = []
    for module_name, class_name in sorted(discovered):
        module = importlib.import_module(module_name)
        classes.append(getattr(module, class_name))

    def sort_key(cls: type) -> tuple[int, str]:
        try:
            return (_BOOTSTRAP_HEAD.index(cls.__name__), "")
        except ValueError:
            return (len(_BOOTSTRAP_HEAD), f"{cls.__module__}.{cls.__name__}")

    return sorted(classes, key=sort_key)


def reset_pool_caches(store_classes: Sequence[type]) -> None:
    """清掉**两层**池缓存的引用。

    只清 `db_pool._POOL_CACHE` 不够：`_get_pool()` 第一句就是
    `if self._pool is not None: return self._pool`，类属性还在的话根本走不到
    `get_shared_pool()`，会继续用连着旧库/旧事件循环的那个池。

    这里只清引用、不 `await pool.close()`——调用方有两种情形，一种是池所属的
    事件循环已经关了（关不了），另一种是调用方自己会在正确的循环里关。
    """
    from src.ragent_backend import db_pool

    db_pool._POOL_CACHE.clear()
    for cls in store_classes:
        cls._pool = None


# --------------------------------------------------------------------------
# DSN 处理与安全校验
# --------------------------------------------------------------------------

def _split_dsn(dsn: str) -> urllib.parse.SplitResult:
    return urllib.parse.urlsplit(dsn)


def database_name_of(dsn: str) -> str:
    return _split_dsn(dsn).path.lstrip("/")


def with_database(dsn: str, dbname: str) -> str:
    """把 DSN 的库名换成 *dbname*，其余（用户/密码/host/port/参数）原样保留。"""
    parts = _split_dsn(dsn)
    return urllib.parse.urlunsplit(parts._replace(path=f"/{dbname}"))


def _assert_disposable_db_name(dbname: str, app_dsn: str) -> None:
    """破坏性操作前的白名单校验——不满足就抛，绝不"尽力而为"地继续。

    共用开发库里有别的会话的数据，误 DROP / 误 TRUNCATE 是不可逆的。
    两条都要过：① 名字必须带一次性前缀；② 不能等于应用库名（哪怕有人把
    应用库就命名成 `ragent_test_xxx`，也不允许动它）。
    """
    if not dbname.startswith(TEST_DB_PREFIX):
        raise RuntimeError(
            f"拒绝对数据库 {dbname!r} 执行破坏性操作：库名必须以 {TEST_DB_PREFIX!r} 开头。"
            "这道校验是为了防止误伤共用开发库。"
        )
    app_db = database_name_of(app_dsn)
    if dbname == app_db:
        raise RuntimeError(
            f"拒绝对数据库 {dbname!r} 执行破坏性操作：它就是 RAGENT_POSTGRES_URL "
            "指向的应用库。"
        )


# --------------------------------------------------------------------------
# 维护连接（用来 CREATE/DROP DATABASE）
# --------------------------------------------------------------------------

def _admin_dsn_candidates(app_dsn: str) -> list[str]:
    explicit = os.getenv("RAGENT_TEST_POSTGRES_ADMIN_URL")
    if explicit:
        # 显式指定了就只试这一个——试探性回退会让"我明明配了却没生效"变成
        # 一个很难看出来的静默行为。
        return [explicit]

    candidates = [with_database(app_dsn, "postgres")]
    parts = _split_dsn(app_dsn)
    host = parts.hostname or "localhost"
    port = parts.port or 5432
    try:
        os_user = getpass.getuser()
    except Exception:  # pragma: no cover - getuser 在极端环境下会抛
        os_user = ""
    if os_user:
        # Homebrew / Postgres.app 装出来的实例通常给当前系统用户建了超级用户。
        candidates.append(f"postgresql://{os_user}@{host}:{port}/postgres")
    return candidates


async def _probe_admin_dsn(candidates: Sequence[str]) -> tuple[str | None, list[str]]:
    """返回 (第一个可用的维护 DSN, 每个候选失败原因)。"""
    failures: list[str] = []
    for dsn in candidates:
        redacted = with_database(dsn, database_name_of(dsn))
        try:
            conn = await asyncpg.connect(dsn, timeout=5)
        except Exception as exc:
            failures.append(f"  - {_redact(redacted)}：连接失败（{type(exc).__name__}: {exc}）")
            continue
        try:
            can_create = await conn.fetchval(
                "SELECT rolsuper OR rolcreatedb FROM pg_roles WHERE rolname = current_user"
            )
        finally:
            await conn.close()
        if can_create:
            return dsn, failures
        failures.append(f"  - {_redact(redacted)}：连上了，但该角色没有 CREATE DATABASE 权限")
    return None, failures


def _redact(dsn: str) -> str:
    """打印 DSN 时抹掉密码——skip 原因会出现在 CI 日志里。"""
    parts = _split_dsn(dsn)
    if parts.password:
        netloc = f"{parts.username}:***@{parts.hostname}"
        if parts.port:
            netloc += f":{parts.port}"
        parts = parts._replace(netloc=netloc)
    return urllib.parse.urlunsplit(parts)


# --------------------------------------------------------------------------
# 建库 / 建表 / 快照 / 清数据
# --------------------------------------------------------------------------

async def _list_public_tables(conn: asyncpg.Connection) -> list[str]:
    rows = await conn.fetch(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
    )
    return [r["tablename"] for r in rows]


async def _create_database(admin_dsn: str, dbname: str, owner: str | None) -> None:
    conn = await asyncpg.connect(admin_dsn)
    try:
        # 建库时把 OWNER 指给应用 DSN 里的角色，测试才能用**跟生产同一个角色**
        # 连过去建表。本仓库真实踩过所有权不一致导致 `CREATE INDEX` 报权限错误
        # （见 CLAUDE.md §5 阶段二那条"过程中发现并修复一个环境问题"）。
        owner_clause = f' OWNER "{owner}"' if owner else ""
        await conn.execute(f'CREATE DATABASE "{dbname}"{owner_clause} ENCODING \'UTF8\'')
    finally:
        await conn.close()


async def _drop_database(admin_dsn: str, dbname: str, app_dsn: str) -> None:
    _assert_disposable_db_name(dbname, app_dsn)
    conn = await asyncpg.connect(admin_dsn)
    try:
        # 用例跑完后可能还留着没关的连接（池所属的事件循环已经关了、关不掉），
        # 有连接在 DROP DATABASE 会直接失败，所以先踢干净。
        await conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = $1 AND pid <> pg_backend_pid()",
            dbname,
        )
        await conn.execute(f'DROP DATABASE IF EXISTS "{dbname}"')
    finally:
        await conn.close()


async def _bootstrap_schema(test_dsn: str, store_classes: Sequence[type]) -> list[str]:
    """在全新库里跑一遍全部 Store 的 `_ensure_schema()`，返回建表顺序。

    走的是各 Store 自己的 `_get_pool()`，跟应用真实启动完全同一条路径——
    这既是"建表"，也顺带验证了这条路径在全新库上确实跑得通。

    **为什么是"反复跑到不动点"而不是一趟按顺序跑。**
    `_BOOTSTRAP_HEAD` 能表达线性依赖，但 `users` 和 `organizations` 之间是
    一个**真实存在的环**：

      - `user_store._ensure_schema` 最后一句建 `idx_users_org_id ON users(org_id)`，
        而 `org_id` 这一列是 `org_store._ensure_schema` 用 `ALTER TABLE users`
        加上去的；
      - `org_store._ensure_schema` 又要求 `users` 表已经存在。

    环没法靠排序解开。好在这些 DDL 是一句一个 autocommit、没有包在事务里，
    失败那句之前的语句是真的落库了（user_store 那一趟里 `CREATE TABLE users`
    已经成功，只是最后建索引那句报 `column "org_id" does not exist`），
    所以"失败的下一轮再试"能收敛：第 1 轮 user_store 失败但建出了 users，
    org_store 成功并补上 org_id，第 2 轮 user_store 就过了。

    一整轮下来一个都没成功（没有进展）才判定失败，并把每个 Store 最后一次的
    错误原样抛出来——**不静默跳过任何一个 Store**，少建一张表的后果是用到它的
    用例在别处莫名其妙地挂，那正是这个仓库反复吃亏的失败形态。

    ⚠️ 这个环是生产代码里的既有问题，不是 fixture 的问题：全新库上按任意
    单一顺序跑 `_ensure_schema()` 都会先失败一次。现实里没暴露，是因为库都是
    `scripts/init_postgres.py` 先建好的，`_ensure_schema` 只当增量迁移用。
    本模块只负责绕开它，**没有改生产代码**。

    返回的表名顺序按"哪个 Store 先建出来的"排，恢复种子数据时按这个顺序插入
    就天然满足外键依赖，不需要另做拓扑排序。
    """
    reset_pool_caches(store_classes)
    table_order: list[str] = []
    seen = set()

    probe = await asyncpg.connect(test_dsn)
    try:
        pending: list[type] = list(store_classes)
        last_errors: dict[str, BaseException] = {}
        while pending:
            still_pending: list[type] = []
            progressed = False
            for cls in pending:
                try:
                    await cls()._get_pool()
                except Exception as exc:  # noqa: BLE001 - 下一轮重试，收敛不了才抛
                    # `_get_pool()` 是先把池挂到 `cls._pool` 再调 `_ensure_schema()`，
                    # 失败时池引用已经挂上了；不清掉的话下一轮第一句就 early return，
                    # `_ensure_schema()` 根本不会重跑，永远收敛不了。
                    cls._pool = None
                    last_errors[cls.__name__] = exc
                    still_pending.append(cls)
                else:
                    progressed = True
                    last_errors.pop(cls.__name__, None)
                for table in await _list_public_tables(probe):
                    if table not in seen:
                        seen.add(table)
                        table_order.append(table)
            if not progressed:
                detail = "\n".join(
                    f"  - {name}: {type(exc).__name__}: {exc}"
                    for name, exc in sorted(last_errors.items())
                )
                raise RuntimeError(
                    "在全新库上初始化表结构失败，这一轮没有任何 Store 取得进展：\n" + detail
                )
            pending = still_pending
    finally:
        await probe.close()

    from src.ragent_backend.db_pool import close_shared_pools

    await close_shared_pools()
    reset_pool_caches(store_classes)
    return table_order


async def _snapshot_seed_rows(
    test_dsn: str, table_order: Sequence[str]
) -> list[tuple[str, list[str], list[tuple[Any, ...]]]]:
    """把建库后各表的初始行拍下来（平台组织、内置角色、内置工作流模板等）。

    TRUNCATE 会把它们一起清掉，而 `_ensure_schema()` 每个 Store 类只跑一次
    不会重来，所以必须自己恢复。
    """
    snapshot: list[tuple[str, list[str], list[tuple[Any, ...]]]] = []
    conn = await asyncpg.connect(test_dsn)
    try:
        for table in table_order:
            rows = await conn.fetch(f'SELECT * FROM "{table}"')
            if not rows:
                continue
            columns = list(rows[0].keys())
            values = [tuple(r[c] for c in columns) for r in rows]
            snapshot.append((table, columns, values))
    finally:
        await conn.close()
    return snapshot


async def _truncate_and_restore(
    test_dsn: str,
    app_dsn: str,
    snapshot: Sequence[tuple[str, list[str], list[tuple[Any, ...]]]],
) -> None:
    _assert_disposable_db_name(database_name_of(test_dsn), app_dsn)
    conn = await asyncpg.connect(test_dsn)
    try:
        tables = await _list_public_tables(conn)
        if tables:
            joined = ", ".join(f'"{t}"' for t in tables)
            # CASCADE 让我们不用关心外键顺序；RESTART IDENTITY 让 SERIAL 主键
            # 从头开始，否则"第 N 条用例拿到的 id 恰好是 1"这类断言会随执行
            # 顺序漂移。
            await conn.execute(f"TRUNCATE {joined} RESTART IDENTITY CASCADE")
        for table, columns, values in snapshot:
            cols = ", ".join(f'"{c}"' for c in columns)
            placeholders = ", ".join(f"${i + 1}" for i in range(len(columns)))
            await conn.executemany(
                f'INSERT INTO "{table}" ({cols}) VALUES ({placeholders})', values
            )
    finally:
        await conn.close()


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

class TestDatabase:
    """会话级一次性库的句柄，`postgres_test_db` fixture 的返回值。"""

    def __init__(
        self,
        dsn: str,
        admin_dsn: str,
        app_dsn: str,
        store_classes: Sequence[type],
        table_order: Sequence[str],
        snapshot: Sequence[tuple[str, list[str], list[tuple[Any, ...]]]],
    ) -> None:
        self.dsn = dsn
        self.admin_dsn = admin_dsn
        self.app_dsn = app_dsn
        self.store_classes = list(store_classes)
        self.table_order = list(table_order)
        self.snapshot = list(snapshot)

    @property
    def database_name(self) -> str:
        return database_name_of(self.dsn)

    def __repr__(self) -> str:  # pragma: no cover - 只影响失败输出的可读性
        return f"<TestDatabase {self.database_name}>"


@pytest.fixture(scope="session")
def postgres_test_db() -> TestDatabase:
    """会话级：建一个 `ragent_test_<随机后缀>` 库、建好全部表，用完 DROP。

    **刻意是同步 fixture。** asyncpg 的池绑定创建它的事件循环，而
    pytest-asyncio 默认每条用例一个新循环——会话级 async fixture 的循环活不到
    用例里，池带过去就报 `InterfaceError: another operation is in progress`。
    所以这里用 `asyncio.run()` 起临时循环干活，yield 之前把池全部关掉、
    引用全部清掉，不把任何池带进用例。

    Postgres 不可达或没有建库权限时 `pytest.skip`，不会让 `tests/unit` 挂掉。
    """
    app_dsn = os.getenv(
        "RAGENT_POSTGRES_URL", "postgresql://postgres:postgres@localhost:5432/ragent"
    )
    admin_dsn, failures = asyncio.run(_probe_admin_dsn(_admin_dsn_candidates(app_dsn)))
    if admin_dsn is None:
        pytest.skip(
            "需要一个有 CREATE DATABASE 权限的 Postgres 连接才能跑这些测试。\n"
            "已尝试：\n" + "\n".join(failures) + "\n"
            "解决办法：设置 RAGENT_TEST_POSTGRES_ADMIN_URL 指向一个有建库权限的连接"
            "（例如 postgresql://postgres:postgres@localhost:5432/postgres），"
            "或启动 docker-compose 里的 postgres。"
        )

    dbname = f"{TEST_DB_PREFIX}{secrets.token_hex(6)}"
    _assert_disposable_db_name(dbname, app_dsn)
    owner = _split_dsn(app_dsn).username
    test_dsn = with_database(app_dsn, dbname)

    store_classes = discover_store_classes()
    previous_env = {
        key: os.environ.get(key) for key in ("RAGENT_POSTGRES_URL", "RAGENT_CONNECTOR_ENCRYPTION_KEY")
    }

    asyncio.run(_create_database(admin_dsn, dbname, owner))
    try:
        # ⚠️ 必须在**任何 Store 被构造之前**改环境变量：各 Store 的 `__init__`
        # 把 `os.getenv("RAGENT_POSTGRES_URL")` 读进 `self._dsn`，构造完再改就晚了。
        os.environ["RAGENT_POSTGRES_URL"] = test_dsn
        if not previous_env["RAGENT_CONNECTOR_ENCRYPTION_KEY"]:
            # TenantConnectorStore 构造时要求有加密密钥（否则只有 RAGENT_DEBUG=true
            # 才放行）。给这个一次性库发一把随机的会话密钥，是为了让建表覆盖面
            # 不依赖"本机有没有配生产密钥"——否则少配一个环境变量就会**静默**
            # 少建一张表，正是这个仓库反复吃亏的那种失败形态。
            from cryptography.fernet import Fernet

            os.environ["RAGENT_CONNECTOR_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

        table_order = asyncio.run(_bootstrap_schema(test_dsn, store_classes))
        snapshot = asyncio.run(_snapshot_seed_rows(test_dsn, table_order))

        yield TestDatabase(test_dsn, admin_dsn, app_dsn, store_classes, table_order, snapshot)
    finally:
        reset_pool_caches(store_classes)
        for key, value in previous_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        asyncio.run(_drop_database(admin_dsn, dbname, app_dsn))


@pytest_asyncio.fixture
async def clean_postgres(postgres_test_db: TestDatabase) -> TestDatabase:
    """函数级：把一次性库清回"刚建好"的状态，并保证池活在**本用例的**循环里。

    **刻意是 async fixture。** setup 和 teardown 都跑在用例自己的事件循环里，
    于是用例期间建出来的池能在同一个循环里被正常 `close()`——同步 fixture 的
    teardown 时机跟循环关闭的先后不确定，靠不住。

    setup：清两层池缓存的引用（上一条用例的池绑在已经关掉的循环上，留着必炸）
    → TRUNCATE 全部表 → 按建表顺序把种子行插回去。
    teardown：在同一个循环里关掉本用例建出来的池。
    """
    reset_pool_caches(postgres_test_db.store_classes)
    await _truncate_and_restore(
        postgres_test_db.dsn, postgres_test_db.app_dsn, postgres_test_db.snapshot
    )
    try:
        yield postgres_test_db
    finally:
        from src.ragent_backend.db_pool import close_shared_pools

        await close_shared_pools()
        reset_pool_caches(postgres_test_db.store_classes)
