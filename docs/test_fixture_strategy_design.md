# 测试 fixture 策略 —— 设计方案

> **状态：设计草案，未实施。零代码改动。**
> **日期**：2026-08-27
> **死期**：2026-11-27。到期未拍板/未实施则本文作废（`CLAUDE.md` §7.4）。
> **范围**：只回答"`ragent_backend` + `tool_agent` 这 12,200+ 行零测试覆盖
> 该用什么 fixture 策略"这一个问题。不涉及要不要现在就动手补测试（那是下一次
> 拍板要决定的事，见 §5 实施路线）、不涉及前端测试策略、不涉及 ruff/mypy
> 接入 CI（`ci.yml` 头部注释已把这两条列为独立决策，未接入）。
> **调研依据**：本次会话对仓库现状的直接调研（§2），不含外部业界调研。

---

## 1. 三行决策

```
- 决策：主体测试继续走「Mock 分层」（候选 C），但把 tests/integration/ 现有的
        "直连共享开发库、无 fixture、手工清理" 模式，统一收编进一套
        testcontainers 起停的临时 Postgres/OpenSearch fixture（候选 A 的能力，
        但只用在这一层，不覆盖全部测试）。不选纯 B（复用共享开发库）作为
        任何自动化测试的默认路径。
- 理由：本仓库 12,200+ 行零覆盖里，可估算约 60-70% 是能被拆成纯函数、
        用现有 Mock 分层模式测掉的逻辑（判定/编排/响应组装），fixture 策略
        本身只对真正绕不开一次真实 DB 往返的那一部分（约 30-40%，
        主要是 *_store.py 5,612 行 + 部分端点的 SQL 拼装/JOIN 正确性）
        是决定性的。共享开发库当前已经是"多会话并发在用"的状态
        （本次任务的 worktree 本身就是从一个正在被其他会话使用的分支切出
        来的），把它接入自动化测试等于让 CI/本地测试成为又一个并发写者，
        直接撞上 CLAUDE.md §7.2 "多会话协作" 那条纪律想避免的情况；
        testcontainers 的镜像已经在 `docker-compose.yml` 里定义好且带
        健康检查（`postgres:16-alpine` / `opensearchproject/opensearch:2`），
        复用它不是引入新基础设施，是把已经写好的定义接到测试里。
- 作废：无——`tests/conftest.py` 目前没有 DB/LLM fixture，这是新增不是替换；
        `tests/integration/*.py` 现有的 22 个文件（手工连库+手工清理）不推翻
        重写，迁移路径见 §5 阶段二。影响面：`tests/conftest.py`、
        `tests/integration/` 下新增一个 `db.py`/`opensearch.py` fixture 模块、
        `requirements.lock` 新增 `testcontainers` 依赖、`.github/workflows/ci.yml`
        新增一个 `integration` job。
```

---

## 2. 现状盘点

### 2.1 `tests/conftest.py` 现状

全文 133 行，五个 fixture：`project_root`、`sample_documents_dir`、
`capture_json_logs`（捕获经过 `RedactingFilter`/`JSONFormatter` 渲染后的
结构化日志，供 §5 observability 相关测试用）、`_clear_request_context`
（autouse，防 contextvar 跨用例泄漏）、`config_dir`。

**没有任何 DB/OpenSearch/LLM fixture**——这一点 CLAUDE.md 的记录准确。
`ci.yml` 头部注释也明确写了这是已知空白、需要独立设计评审，正是本文要接的活。

### 2.2 `tests/` 下目前实际并存的四种测试模式

逐个数过，不是印象：

| 模式 | 数量 | 例子 | 代价 |
|---|---|---|---|
| **①鸭子类型假件 / Mock**（候选 C 的主体） | `tests/unit/` 97 个文件里 34 个用 `MagicMock`/`SimpleNamespace` | `test_admin_users_batch_queries.py` 用手写 `_FakePool`/`_FakeAcquireCtx` 接住 `asyncpg` 接口 | 测的是"我以为它怎么运作"；CLAUDE.md 记录过至少两次"单测全绿、真机一炸"（`remove_document` 死代码、`aiops_module_enabled` 从未在响应里出现） |
| **②直连真实 DB，零 fixture**（候选 B 但没有事务隔离） | `tests/integration/` 24 个文件里至少 8 个（`test_ops_store_*.py` 系列）直接读 `RAGENT_POSTGRES_URL` 默认值连本机共享库，各自手写 `_cleanup()` 用 `DELETE ... WHERE` 收尾 | `test_ops_store_postmortems.py`：测试内手工 `pool.acquire()` 建数据、`try/finally` 里手工删 | 不在 CI 里跑（`ci.yml` 明确排除）；本地跑会真的写共享库；测试之间无隔离，并发跑会互相踩 |
| **③真实服务 E2E 脚本，走 pytest 之外**（`scripts/verify_*.py`） | 至少 6 个（`verify_aiops_endpoints.py` 33 项、`verify_account_lifecycle.py` 27 条、`verify_account_endpoints_e2e.py` 30 条等） | `verify_aiops_endpoints.py` 走 `httpx.ASGITransport` + `fastapi.testclient.TestClient` 连真实 `create_app()` + 真实 Postgres | **已知的真实坑**：`TestClient` 内部另起事件循环（anyio portal 线程），而 asyncpg 连接池绑定创建它的事件循环，跨循环复用直接 `InterfaceError`；workaround 是 `_reset_pool_caches()` 手动清空 `db_pool._POOL_CACHE` + 4 个 Store 类的 `_pool` 类属性，CLAUDE.md 记录这个 workaround 本身出现了不止一次。不是 pytest 用例，人工跑、人工读输出，没有 CI 门禁 |
| **④静态/AST 一致性检查**（不执行代码，只解析源码文本） | 至少 2 个：`test_admin_user_response_sites_agree.py`、`test_org_store_select_completeness.py` | 用 `ast` 模块解析 `app.py`，比对 `AdminUserResponse(...)` 两处调用点的关键字参数集合是否一致 | 这类测试**证明力很窄**——只能抓"两处代码字面不一致"，抓不出"两处都错但错得一样"，CLAUDE.md 里 `test_last_turn_tokens_reset.py` 那次"断言命中的是 docstring 不是实现"就是这条路的已知翻车案例。属于"聊胜于无"的补充手段，不能当主力 |

**没有第五种**——即"用 `TestClient`/`ASGITransport` + 真实 `create_app()` +
**受控的临时**数据库"这种组合，在 `tests/` 下一次都没出现过。这正是本文要
新增的能力。

### 2.3 12,200+ 行怎么分布，fixture 策略到底该管多大范围

当前 `src/ragent_backend` + `src/tool_agent` 实测行数（`wc -l`）：

| | 行数 |
|---|---|
| `src/ragent_backend` | 15,758 |
| `src/tool_agent` | 2,179 |
| **合计** | **17,937** |

⚠️ **比 CLAUDE.md §4 P1 记录的"12,200 行"和 §2 记录的"10.4K 行 / 72 端点"
都要大**——两个数字应该是不同时点的快照（智能运维模块从 08-26 到今天陆续
加了十几次提交），不是同一把尺子量出来的矛盾数据，如实更正：
当前实测 REST 路由 **90 个**（`@app.get/post/put/delete`）+ WebSocket 路由
**2 个**，合计 **92 个端点**。这个数字本身不影响本文结论，只是顺手更正，
留给下次整理 §2 时处理。

**按"要不要真实 DB 才能测"分两类**（这个区分决定 fixture 策略的实际范围）：

- **A 类：已经是纯函数或可注入依赖的模块，本来就不需要 fixture 策略**——
  `account_import.py`（309 行）、`activation.py`（162 行）、
  `aiops_scope.py`（292 行）、`connector_crypto.py`（153 行）、
  `auth.py` 的 `resolve_jwt_secret`/`reject_if_disabled` 等纯函数部分、
  `intent.py`（1,159 行，规则判定 + 提示词拼装）、`workflow.py`（1,914 行，
  节点逻辑接收注入的 store/llm 参数，不直接 `import asyncpg`）。
  这一类**已经被验证可以用候选 C 测掉**——上面列的四个纯函数模块全部有
  对应单测且零 fixture 依赖，`workflow.py`/`intent.py` 也有大量单测
  （`test_sub_query_dependency_and_fanout.py` 53 条、
  `test_build_prompt_cross_material.py` 等）用 Mock/假 LLM 覆盖。
  粗算这一类约 **3,700+ 行有直接证据、外加 workflow.py/intent.py 里未覆盖的
  剩余部分**，估计整体占 17,937 行的 **55-65%**——这部分**不该被算进
  "fixture 策略要解决的问题"**，它缺的是"有没有人去写"，不是"有没有
  地方能测"。

- **B 类：结构上绕不开一次真实 DB 往返的部分**：
  - `*_store.py` 系列（14 个类，合计 **5,612 行**）——SQL 拼装、JOIN
    正确性、批量查询的分组逻辑、事务/状态机的原子性（如
    `_conditional_update` 那种 `UPDATE ... WHERE status=$3` 条件更新）。
    这一层**理论上能靠精细的 asyncpg mock 测到语法正确性**（现有
    `test_admin_users_batch_queries.py` 就是这么干的），但测不到
    "这条 SQL 在真实 Postgres 上执行结果对不对"——`tests/integration/`
    现有的 8 个 `test_ops_store_*.py` 之所以选择连真库，就是因为
    JOIN/JSONB/条件 UPDATE 这类行为 mock 测不出真实语义
    （CLAUDE.md 明确写过"这是纯 mock 测不出来的层面"）。
  - `app.py`（4,436 行，92 个端点）——**结构性障碍是闭包，不是"没写测试"**：
    `create_app()` 内部所有 Store 实例（`user_store`/`org_store`/`ops_store`
    等）是**局部变量被端点函数闭包捕获**，不是通过 FastAPI 的
    `Depends(get_xxx_store)` 注入。这意味着 FastAPI 标准的
    `app.dependency_overrides[...] = fake_store` 测试套路**在这个仓库里
    用不了**——唯一能替换 Store 行为的办法是"整个 `create_app()` 跑起来，
    Store 内部访问真实/测试 DB"，或者 monkeypatch 模块级别的类
    （粒度太粗、副作用不可控）。这是 CLAUDE.md §7.1 自己点名的反面教材，
    本文不重新论证，只确认它决定了"端点层测试必须用真实（或高保真）DB"
    这个前提无法绕过。
  - `db_pool.py`：**关键的正面事实**——14 个 Store 共享同一个按 dsn 缓存的
    连接池（`get_shared_pool(dsn)`），这是唯一的注入点。测试 fixture 只需要
    在这一处让 `dsn` 指向临时数据库，14 个 Store 不需要逐个改代码。

  B 类合计 **10,048 行**（5,612 + 4,436），占 17,937 行的 **约 56%**——
  但这不等于"56% 需要新 fixture 才能测"：`*_store.py` 里仍有相当比例是
  参数校验/分组这类可以 mock 测的部分（`test_admin_users_batch_queries.py`
  已经证明），真正**必须**连真实 DB 才能验证正确性的，是 SQL 语义本身
  （JOIN/JSONB/事务/迁移脚本），估计是 B 类里的 **一小部分但是关键部分**——
  这部分测不到，`docs/aiops_module_design.md` 记录的好几个"手工 mock 漏配
  `execute()` 返回值导致误判通过"的坑就会持续复发。

**结论**：fixture 策略的目标不是让 17,937 行全部有测试，而是**解除
"想测真实 DB 语义时无路可走"这一个结构性障碍**；纯逻辑覆盖率的提升
应该继续靠候选 C（抽纯函数 + mock），跟 fixture 策略是两条并行的工作，
不要合并成一个任务。

### 2.4 CI 现状与约束

`.github/workflows/ci.yml`（本仓库当前分支刚加的）只有一个 job，只跑
`pytest tests/unit -q`，头部注释明确写了排除 `tests/integration`/`tests/e2e`
的原因就是本文要解决的问题。GitHub Actions `ubuntu-latest` runner **预装
Docker**，支持在 workflow 里声明 `services:` 块（原生的临时容器，带
`options: --health-cmd=...`），或者在步骤里直接跑 `docker compose up`——
两条路都不需要额外的云资源审批。

`requirements.lock` 目前没有 `testcontainers` 包；`docker-compose.yml`
已经有 `postgres:16-alpine`（带 `pg_isready` 健康检查）和
`opensearchproject/opensearch:2`（带 `curl` 健康检查，且已经写好了
"仅本地开发关闭 security plugin"的注释和内存参数，115 行文件不是新起草，
是复用现成的）。本机 `docker info` 已确认 Docker Desktop 可用（版本
29.7.2）——本次任务在这台机器上具备验证 testcontainers 方案技术可行性的
条件，但**本文没有做这个验证**（见 §6，属于探针实验，任务书允许做但
明确要求不留在提交里，本文选择连探针都不做，把"验证可行"完整地放进
§5 实施路线第一步）。

`pyproject.toml` 已经声明了 pytest markers：`unit`/`integration`/`e2e`/
`llm`/`slow`，但**目前测试文件基本不用 marker 分流，用目录分流**
（`testpaths = ["tests"]`，CI 用 `tests/unit` 路径而非 `-m unit`）。
这个既有约定值得延续而不是推翻——本文的 fixture 策略仍按目录分层
（`tests/unit` 保持零外部依赖；`tests/integration` 挂新 fixture），
不建议这次顺带改成 marker 驱动。

### 2.5 一个容易被忽略的现状：现有 `tests/integration/*.py` 已经是"真实优先"的写法

这点对方案选择很关键，容易被误判——不是"这个仓库还没有真实集成测试的
先例，要从零设计"，而是**先例已经存在，只是没有 fixture 收编，各自为战**。
8 个 `test_ops_store_*.py` 里，判别力设计得相当讲究（`test_ops_store_
concurrent_approval.py` 用 `asyncio.Event` 屏障强制交错来复现 TOCTOU 竞态，
`git stash` 反证过），**这些测试本身的质量不是问题，问题只是它们连着一个
不受控的共享库、且没有进 CI**。本文的方案不应该重写这批测试的断言逻辑，
只应该换掉它们连接的东西（从"共享开发库直连"换成"testcontainers 临时库"）。

---

## 3. 候选方案逐条分析

### A. testcontainers 起临时 Postgres/OpenSearch

**优点**：真实隔离，测试之间零污染；镜像已经在 `docker-compose.yml` 里定义
好（不是新增基础设施决策，是复用）；本机和 GH Actions runner 都有 Docker；
`db_pool.py` 的单一注入点（`get_shared_pool(dsn)`）让"把 14 个 Store 全部
指向临时库"只需要在一处替换 dsn，不需要碰生产代码。

**缺点**：容器启动有秒级开销（Postgres 通常 2-5s、OpenSearch 因为是 JVM
服务通常 10-20s 起，`docker-compose.yml` 里的健康检查设了
`start_period: 30s`），会话级/模块级共享容器能把这个开销摊到整个测试会话
只付一次，但如果每个测试函数都新起容器代价就不可接受；需要新增
`testcontainers` 依赖（社区维护，非 Anthropic/官方仓库既有的技术栈，
是一次新的三方依赖决策，但成熟度高、`docker compose` 语义本身就是它的
底层能力之一）。

**对本仓库的适配度**：高。`docker-compose.yml` 已经把镜像版本和健康检查
方式选定，唯一要做的是把这套定义从"人工 `docker compose up -d`"变成
"pytest fixture 自动起停"。这是 testcontainers-python 最常见的用法
（`DockerContainer("postgres:16-alpine")` 或直接用
`testcontainers.postgres.PostgresContainer`），不需要发明新东西。

### B. 复用现有共享开发库 + 事务回滚

**优点**：零新增基础设施，本地跑最快（没有容器启动开销）。

**缺点，且是本仓库场景下被放大的缺点**：
1. **"事务回滚"这个补偿手段在本仓库结构上不好落地**。经典的
   "每个测试包一层事务，结束时回滚"要求被测代码全程复用**同一个连接**
   （不能是"每次操作从池里新拿一个连接"，否则事务隔离不生效）。而本仓库
   14 个 Store 的方法内部各自 `pool.acquire()`，不接受外部传入连接——
   要让事务回滚生效，必须先改造 `db_pool.get_shared_pool` 让它在测试模式下
   返回一个"`acquire()` 恒返回同一个连接"的假池，这本身已经是不小的一块
   实现（且和候选 A 需要的"替换 dsn 指向临时库"是两种不同的改法，不能
   顺手一起做）。
2. **共享开发库当前已经处于多会话并发写入状态**——这不是假设风险，是
   本次任务启动时观察到的真实现状（本次工作的 worktree 从一个正被其他
   会话使用的分支切出）。CLAUDE.md §7.2 专门记录过"多会话协作时数据现场
   被提前清理导致复核失败"的真实教训。把这个库接入**自动化**测试（尤其是
   CI，一旦接入意味着每次 push/PR 都会写它）比"人工验证完不立刻清理"
   风险更高——人工验证是一次性的、双方知情的；CI 是持续的、无人盯着的。
3. **CI 里根本不存在这个库**——GitHub Actions runner 是一次性环境，没有
   "本机共享 Postgres"这回事。选 B 意味着 CI 必须走另一套（大概率还是要
   起一次性容器），这样"本地方案"和"CI 方案"就是两套不同的东西，
   任务书里已经点出了这个矛盾：**行为不一致本身就是负债**——本地测试通过
   不能保证 CI 通过（反之亦然），且没人愿意维护两份 fixture 代码。

**对本仓库的适配度**：低。不是"不能用"，是"用了会制造一个新的、
CLAUDE.md 已经吃过亏的协作风险，换来的收益（省下容器启动的几秒）不成比例"。

### C. Mock 分层 + 少量真实集成测试

**优点**：跟现有 97 个 `tests/unit` 文件里 34 个已经在用的模式一致，
学习成本最低；不需要任何新基础设施；运行最快（这也是 CI 目前只跑
`tests/unit` 能在合理时间内完成的原因）。

**缺点**：CLAUDE.md 自己记录的反例已经点名——"单测用 MagicMock + 自造 id，
测不出来"（`remove_document` 死代码那次）、"手工 mock 未配置 `execute()`
返回值导致的 `TypeError`"被静默吞掉（TOCTOU 竞态那次）、"猜字段名，
猜错了页面不报错只是显示 `—`"（总览大屏指标接线那次）。这三次坑的共同
形态是：**mock 精确复刻了"我以为的接口"，但真实接口的某个细节（返回值
形状、异常时机、字段命名）跟"我以为的"不一致，而这种不一致 mock 测试
天然无法发现**——因为写 mock 和写实现往往是同一个人、同一次会话里做的，
两边的"想当然"是同源的。

**对本仓库的适配度**：单独作为**唯一**策略不够（历史已经反复证明），
但作为**主体**（配合一小部分真实集成测试兜底）是对的——本文最终方案
就是"C 为主体 + A 补足 C 测不到的那一层"，不是抛弃 C。

### D（未在候选清单里，但值得记录）：静态/AST 一致性检查

已经在用（§2.2 第④类），证明力窄，但成本极低（不需要跑起来任何东西，
毫秒级）。**建议保留作为补充手段，不作为主力**，专门用来钉死"两处
必须逐字段一致但没有共享代码路径"这种特定形态的回归（`AdminUserResponse`
两个构造点那次是典型场景）。不单独展开成一个"方案"，因为它解决的是
一个更窄的问题（源码一致性），不是"这个函数运作对不对"。

### 推荐：A 为真实 DB 语义的唯一路径，C 为主体，不采用 B

不是"A/B/C 三选一"，是**分层**：
- 能拆成纯函数/注入依赖的逻辑（§2.3 A 类，占多数）→ 继续用 C，该拆的拆
  （这部分工作量比 fixture 策略本身大得多，但不是本文要解决的问题）
- 真正需要验证 SQL/JOIN/事务语义、或者需要走一次真实 HTTP 请求验证
  端点接线（§2.3 B 类里"必须连真库"的那一小部分）→ 用 A，**只用 A**，
  不引入 B 作为任何自动化路径的选项
- 本地开发者如果不想等容器启动，仍然可以用现成的 `docker-compose.yml`
  起一个持久的本地 Postgres/OpenSearch 手工调试——**但那是开发者本机的
  个人选择，不是测试基础设施的一部分，不出现在 `conftest.py` 里**

---

## 4. 测试设计

### 4.1 `conftest.py` 大概长什么样

新增 `tests/integration/conftest.py`（不改 `tests/unit/` 那份，保持
`tests/unit` 零外部依赖的既有边界）：

```python
# tests/integration/conftest.py（示意，非可运行代码）

import pytest
from testcontainers.postgres import PostgresContainer
# OpenSearch 没有官方 testcontainers 模块，用通用 DockerContainer
# 起 opensearchproject/opensearch:2，复刻 docker-compose.yml 里的
# 环境变量（DISABLE_SECURITY_PLUGIN=true 等）与健康检查方式。
from testcontainers.core.container import DockerContainer

@pytest.fixture(scope="session")
def postgres_container():
    # 镜像版本跟 docker-compose.yml 保持一致，不是另选一个。
    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg

@pytest.fixture(scope="session")
def opensearch_container():
    with DockerContainer("opensearchproject/opensearch:2") \
            .with_env("discovery.type", "single-node") \
            .with_env("DISABLE_SECURITY_PLUGIN", "true") \
            .with_env("DISABLE_INSTALL_DEMO_CONFIG", "true") \
            .with_env("OPENSEARCH_JAVA_OPTS", "-Xms512m -Xmx512m") \
            .with_exposed_ports(9200) as os_container:
        # 等健康检查通过（wait_for_logs 或轮询 /  端点）
        yield os_container

@pytest.fixture(scope="session")
def _schema_ready(postgres_container):
    """会话级建表一次——复用各 Store 类既有的 `_ensure_schema()`，
    不新写一份 SQL DDL（14 个类各自的 schema 定义已经是唯一事实来源，
    重复一份会立刻产生"两处要一起改"的新负债）。"""
    dsn = postgres_container.get_connection_url().replace(
        "postgresql+psycopg2", "postgresql"  # testcontainers 默认带的 driver 前缀，asyncpg 不认
    )
    # 触发式建表：对每个 Store 类调一次 `_get_pool()`，
    # `_ensure_schema()` 是既有的双检锁幂等逻辑，见 §2.3。
    ...
    return dsn

@pytest.fixture
async def db(_schema_ready, monkeypatch):
    """函数级 fixture：把 db_pool 的缓存指向临时库的 dsn，
    每个测试结束后清空本测试写入的行（不是整库重建，容器级隔离已经
    在 session 层面做了"跟其他任何东西都不共享"这件事，函数间只需要
    "不看到彼此的数据"，用最小粒度的 TRUNCATE 或按测试自己建的 id 前缀
    DELETE 都可以，具体选哪种是实施阶段的细节，不在本文拍死）。
    """
    dsn = _schema_ready
    monkeypatch.setenv("RAGENT_POSTGRES_URL", dsn)
    # 关键：清掉 db_pool 的缓存 + 各 Store 类的 class 级 _pool 缓存，
    # 强迫下一次 _get_pool() 重新走 get_shared_pool(dsn) 拿到指向
    # 临时库的池——这一步复用 scripts/verify_aiops_endpoints.py 里
    # 已经写过、已经在生产实践里验证过的 _reset_pool_caches() 逻辑，
    # 不是重新发明。
    ...
    yield dsn
    # 清理策略见上面注释，具体实现留给实施阶段
```

### 4.2 一个典型端点测试大概怎么写（示意）

```python
# tests/integration/test_admin_list_users_e2e.py（示意）

import pytest
from httpx import ASGITransport, AsyncClient
from src.ragent_backend.app import create_app

pytestmark = pytest.mark.integration

@pytest.fixture
async def client(db):
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c

async def test_admin_list_users_returns_org_and_aiops_flag(client, db):
    # 造数据：走真实注册/建号流程，不是直接 INSERT——这样测试同时验证了
    # "建号端点写对了" + "列表端点读对了"两件事，跟 verify_account_
    # lifecycle.py 现有的做法一致。
    token = await _login_as_platform_admin(client)
    await client.post("/api/v1/admin/organizations", json={...}, headers=...)
    resp = await client.get("/api/v1/admin/users", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    body = resp.json()
    # 这条断言正是 CLAUDE.md 记录过的那次真实回归
    # （disabled_at 恒为 null）本该被钉住的地方——
    # mock 版本测不出来，因为它绕过了 admin_list_users 内部
    # 真实的批量 SQL 组装路径。
    assert "aiops_module_enabled" in body[0]["organization"]
```

### 4.3 一个典型 Store 层测试大概怎么写（示意，比端点测试更常见）

```python
# tests/integration/test_ops_store_postmortems.py 现有写法几乎不用改，
# 只需要把开头的
#   os.environ.setdefault("RAGENT_DEBUG", "true")
#   ops = OpsStore()  # 隐式连 RAGENT_POSTGRES_URL 默认值（共享开发库）
# 换成依赖新 fixture：
async def test_pending_and_approved_are_excluded(db):
    ops = OpsStore()  # 此时 RAGENT_POSTGRES_URL 已被 db fixture 指向临时库
    ...  # 断言逻辑完全不变——这批测试的判别力设计本来就是对的，
         # 只是换了连接目标
```

**这一点是本方案对现有 22 个 `tests/integration/*.py` 文件最大的善意**：
迁移成本被压到"改 fixture 依赖 + 删掉手工 `_cleanup()`"，不需要重写断言。

---

## 5. 实施路线

不建议一次做完，按 CLAUDE.md 一贯的分阶段节奏：

**阶段零（本文档，已完成）**：设计评审，等用户拍板。

**阶段一：验证可行性 + 落地 `tests/integration/conftest.py`**
（只覆盖 Postgres，不含 OpenSearch）
- 加 `testcontainers` 依赖，写 §4.1 那套 fixture 的真实可跑版本
- 挑 1-2 个现有 `tests/integration/test_ops_store_*.py` 做迁移试点，
  确认"断言不用改、只换连接目标"这个假设成立
- CI 新增一个 `integration` job，只跑迁移过的这 1-2 个文件，确认
  GH Actions runner 上容器起停正常、耗时可接受
- **验收标准**：迁移后的测试在 CI 和本地都能跑，且不再触碰
  `RAGENT_POSTGRES_URL` 默认值指向的共享库

**阶段二：存量迁移**
- 把 `tests/integration/` 剩余直连共享库的文件全部迁移到新 fixture
- 补 OpenSearch 容器 fixture（`test_opensearch_store.py`、
  `test_hierarchy_narrowing_recall.py` 等依赖它）
- CI `integration` job 扩到全量 `tests/integration`

**阶段三（可选，看阶段一二暴露出的真实痛点决定要不要做）**：
- 把 `scripts/verify_*.py` 那类 E2E 脚本里价值高、判别力强的场景
  （比如 `verify_aiops_endpoints.py` 的审批状态机竞态那几项）迁移成
  `tests/e2e/` 下的正式 pytest 用例，接入同一套 fixture，从"人工跑"
  变成"CI 跑"。**不建议无差别全迁**——部分脚本本来就是一次性验收工具，
  迁移的边际收益要逐个判断。

**每个阶段都是独立的可交付单元，阶段一验证不通过（比如容器在 GH Actions
上启动稳定性差、耗时超出可接受范围）就停下来重新评估，不绑定后续阶段。**

---

## 6. 本次未覆盖的范围

- **没有实际起过 testcontainers 验证可行性**——§4 的 fixture 代码是示意/
  伪代码，不保证一次跑通；实际接口（如 testcontainers-python 的
  `PostgresContainer` API 细节、`get_connection_url()` 返回的 driver 前缀
  跟 asyncpg 是否需要转换）没有验证过，这些细节留给阶段一去发现和处理。
- **没有验证 OpenSearch 用 `DockerContainer` 通用容器起停的稳定性**——
  `docker-compose.yml` 里写了"空容器 831MiB、灌 10000 块后 862MiB"这类
  实测数据，但那是常驻开发容器的数据，不是"每次测试会话冷启动"的数据，
  冷启动到健康检查通过要多久（`start_period: 30s` 是不是够）没有实测。
- **没有测过"事务回滚"这条候选 B 的补偿路径的实际工作量**——只是分析了
  它在本仓库结构上（Store 方法自己 `acquire()`，不接受外部连接）会
  比较别扭，没有真的动手估算改造 14 个 Store 类需要多少行改动。
  如果阶段一发现 testcontainers 启动开销在 CI 上不可接受，这是需要
  重新拿出来评估的备选项。
- **没有估算"抽纯函数"这部分工作量**（§2.3 A 类的覆盖率提升）——那是
  跟本文平行的另一块工作，本文只是为了正确界定 fixture 策略的范围而
  提到它，没有展开成可执行的任务清单。
- **CI 耗时预算没有拍板**——阶段一说"耗时可接受"但没有给出具体数字
  上限，这需要用户或团队定一个阈值（比如"integration job 不超过 5 分钟"）
  才能作为验收标准，本文没有替用户做这个决定。
- **没有涉及 LLM/Ollama 相关的 fixture**——`pytest.mark.llm` 这个既有
  marker 覆盖的是"需要真实 LLM API 调用"的测试（如
  `test_d1_real_router_split.py`），跟本文讨论的 DB/OpenSearch fixture
  是不同的问题（模型服务不是"起个容器就能有"的东西，本机走 Ollama），
  本文不涉及。

---

## 交付说明（供本次评审使用，非文档正文）

**验收怎么做**：用户读 §3 候选方案分析和 §1 三行决策，判断"A 为真实 DB
语义的唯一路径、C 为主体、不用 B"这个分层是否认同；如果认同，下一步是
拍板阶段一是否现在启动（还是继续排在其它优先级之后）。§4 的两个示意
测试用例是用来让用户直观判断"迁移后测试长什么样、好不好维护"，不是
用来验收代码正确性的（因为没有代码）。

**回归怎么保**：不适用——本次是纯设计文档产出，没有生产代码或测试代码
改动，不存在需要回归保护的行为变化。

**什么没做**：见 §6，最核心的一条是——testcontainers 方案目前只有分析、
没有实际跑通过，可行性判断基于"镜像已经在 docker-compose.yml 里定义好
+ GH Actions 预装 Docker + db_pool.py 有单一注入点"这三个真实观察到的
事实推出来的合理预期，不是已验证的结论。
