# 纯代码层面架构审计 — enterprise-qa-agent

审计范围：`/Users/david/Documents/enterprise-qa-agent`，仅读源码，未访问网络，未修改任何文件。
严重程度按「企业内部 QA agent、小团队、非公网高并发」的实际场景判定。

---

## 一、代码规模与结构统计

### 1.1 各模块规模

| 模块 | 文件数 | 行数 | 说明 |
|---|---|---|---|
| `src/ragent_backend/` | 23 | 10,156 | FastAPI 后端 + LangGraph 主图 + 14 个 Store |
| `src/libs/` | 42 | 7,461 | LLM/embedding/reranker/vector_store/loader 抽象层 |
| `src/ingestion/` | 23 | 4,881 | 摄入流水线 |
| `src/core/` | 17 | 4,089 | 检索引擎 + 响应构建 + trace + settings |
| `src/observability/` | 20 | 3,946 | Streamlit 独立仪表盘 + 评估 |
| `src/mcp_server/` | 7 | 2,978 | MCP 协议层 + 3 个工具 |
| `src/tool_agent/` | 8 | 2,050 | ReAct 子图 + 工具注册表 |
| `src/security/` | 2 | 95 | prompt 注入规则检测 |
| `scripts/` | 45 | 5,360 | 运维/迁移/一次性调试脚本 |
| `services/` | 4 | 620 | 演示用租户微服务（mock） |
| `tests/` | 77 | 26,849 | 见 1.3 |
| `frontend/src/` | 44 文件 | 5,900 (js/jsx) | React + Vite |
| **合计（src 下 Python）** | — | **约 35,700** | 全仓 Python 约 73,000 行 |

### 1.2 最大的几个文件

| 文件 | 行数 |
|---|---|
| `src/ragent_backend/app.py` | 2,976 |
| `src/ragent_backend/workflow.py` | 1,575 |
| `src/mcp_server/tools/query_knowledge_hub.py` | 1,528 |
| `frontend/src/App.jsx` | 1,334 |
| `tests/fixtures/generate_qa_test_pdfs.py` | 1,039 |
| `tests/unit/test_get_document_summary.py` | 1,009 |
| `src/ingestion/pipeline.py` | 846 |
| `src/core/query_engine/hybrid_search.py` | 813 |
| `src/ragent_backend/intent.py` | 812 |
| `src/ragent_backend/workflow_store.py` | 784 |

### 1.3 测试覆盖情况

- 测试总量：77 文件 / 26,849 行（unit 49 个 / 19,172 行，integration 14 个 / 4,250 行，e2e 4 个 / 1,451 行）。
- **覆盖分布严重失衡**：全部测试集中在「老 RAG 库层」（`src/libs`、`src/core`、`src/ingestion`、`src/mcp_server`、`src/observability`）。
- `grep -rn "from src.ragent_backend" tests/` → **0 命中**。
- `grep -rn "src.tool_agent" tests/` → **0 命中**。
- `grep -rn "TestClient|ASGITransport|create_app" tests/` → **0 命中**（无任何 API 层测试）。
- 即：占后端核心 12,200 行的 `ragent_backend` + `tool_agent`（多租户权限、鉴权、LangGraph 工作流、工具子图）**零回归保护**。
- `tests/conftest.py` 仅 3 个路径 fixture，无 DB/LLM/依赖注入基础设施——这解释了为什么后端层写不出测试（构造 `RAGWorkflow` 必须先起 Postgres）。

### 1.4 工程设施

- **无 `Dockerfile`、无 `docker-compose.yml`**。
- **无 CI**：`.github/` 下只有 `skills/`，没有 `workflows/`。
- **无依赖锁定**：只有 `pyproject.toml`，全部 `>=` 下界约束，无 lock 文件、无 `requirements.txt`。
- 前端有 `package.json` 但无 lint/test/typecheck script。
- 根目录 9 个 markdown 共约 300KB（`auto-operations.md` 57KB、`PROJECT_ARCHITECTURE_SUMMARY.md` 52KB、`work-flow.md` 47KB…）。

---

## 二、P0（阻塞级）

### P0-1　流式对话的 token/trace 队列挂在共享单例上，两个并发请求会串流

**证据**
- `src/ragent_backend/workflow.py:191-192`（`self._token_queue` / `self._trace_queue` 是**实例属性**）
- `src/ragent_backend/workflow.py:362-363`（`run_stream` 每次调用直接覆写这两个属性）
- `src/ragent_backend/workflow.py:380-381`（生成器从 `self._token_queue` 取）
- `src/ragent_backend/workflow.py:419-420`（`finally` 里置 `None`）
- `src/ragent_backend/workflow.py:1254-1255`、`1264-1265`、`1271-1272`（`_generate_node` 往 `self._token_queue` 推 token）
- `src/ragent_backend/app.py:457-468`（`create_app()` 里只 new 了**一个** `RAGWorkflow`，被所有请求闭包共享）

**为什么不合理**
`create_app()` 全进程只有一个 `RAGWorkflow`，但流式所需的队列是 per-request 状态却存成了 per-instance 状态。请求 A 进 `run_stream` 建队列 QA；请求 B 紧接着进来把 `self._token_queue` 覆写成 QB。此后：A 的 `_generate_node` 读 `self._token_queue` 拿到的是 QB，A 的 token 被推进 B 的队列；两个生成器又都在 `await self._token_queue.get()`（同一个 QB），谁先被唤醒谁拿到这个 token。结果是**两个用户的回答内容被随机切碎、交叉投递到对方的 SSE 流里**。更糟的是 A 先结束时 `finally` 把 `self._token_queue = None`，B 剩下的 token 全部被 `if self._token_queue is not None` 静默丢弃，B 只能靠 `run_stream:414` 的兜底吐一次 `final_answer`（或者直接空屏）。

注意 `workflow.py:224-227` 的注释明确声称"多个并发请求互不串"——这个论断只对"子图不把队列锁进闭包"成立，但它没有解决队列本身是共享实例属性这个根因，注释给出的安全感是错的。

**实际影响与严重程度**：**P0**。多租户 QA agent 里两个人同时提问是常态；后果是跨会话（可能跨企业）的回答内容泄露 + 回答被截断，且症状随机、极难从日志复现（后端还没有结构化日志，见 P1-5）。

**调整方向**：把 per-request 的队列从实例状态里拿出来——`run_stream` 内部建局部队列，通过参数/`contextvars`/LangGraph `config["configurable"]` 传给 `_generate_node` 和 `_emit_trace`；或者干脆改用 LangGraph 原生的 `astream_events`，不自己维护旁路队列。

---

### P0-2　JWT 签名密钥有硬编码默认值，且全仓没有任何地方设置它

**证据**
- `src/ragent_backend/auth.py:30`：`_JWT_SECRET = os.getenv("RAGENT_JWT_SECRET", "dev-only-insecure-secret-change-me")`
- 全仓搜索 `RAGENT_JWT_SECRET`：**只有这一处命中**。`.env`、`.env.example`、readme、docs 里都没有这个变量。

**为什么不合理**
`get_current_user`（`auth.py:63`）是整个系统身份的唯一来源，`require_role` / `require_platform_admin` / 多租户 collection ACL 全部建立在它解出的 `user_id` 之上。密钥是一个公开在源码里的字符串，且没有任何配置样例提醒运维覆盖，意味着实际部署里它**几乎必然就是这个默认值**。任何人可以用 PyJWT 三行代码签一个 `{"sub": "<任意 user_id>", "username": "...", "exp": ...}`，直接获得该用户（包括 super_admin）的完整权限。上面所有精心设计的多租户隔离（`_validate_role_assignment`、`tenant_` 前缀拦截、`_org_owned_collections` 二次校验、fail-closed 类目过滤）在这一步之后全部失效。

`auth.py:29` 的注释说"生产环境必须通过环境变量覆盖"，但既没有 fail-fast 也没有写进 `.env.example`，纯靠人记住。

**实际影响与严重程度**：**P0**。这是唯一一个能一击瓦解整套权限模型的问题，且修复成本极低。

**调整方向**：启动时校验密钥——未设置或等于默认值时直接拒绝启动（生产模式下），并把变量补进 `.env.example`；密钥交由部署环境注入，不留代码内默认值。

---

## 三、P1（重要）

### P1-1　`create_app()` 单函数 2,976 行、71 个端点，没有任何路由分层

**证据**
- `src/ragent_backend/app.py:281`（`def create_app()` 起）到 `2953` 结束，整个应用的全部端点都定义在这一个函数体内。
- 端点分布：`app.py:556`（health）→ `2891`（memory stats），共 71 个 `@app.*` 装饰器，覆盖鉴权、用户管理、组织、连接器、网关监控、debug 工具、运营仪表盘、角色、collection、上传、工作流模板、工作流实例、通知、会话文件、对话、chat、WebSocket、回滚、历史——18 个业务域。
- 项目根本没有引入 `APIRouter`（全仓 0 命中）。

**为什么不合理**
所有端点共享同一个闭包作用域，依赖（`user_store`/`role_store`/`workflow`/`_audit_log`…）靠闭包捕获而不是依赖注入。后果是：(1) 无法对任何一组端点单独做测试——要测一个端点必须构造整个 app，而构造 app 就要连 Postgres、建 checkpointer、连 MCP server（`app.py:371`、`498-531`），这直接解释了 1.3 里"零 API 测试"的现状；(2) 权限守卫散落在每个端点的 `Depends` 参数里（如 `app.py:1091-1093` 用 `_`/`__`/`___` 三个占位参数挂三层守卫），无法在路由级统一审查"哪些端点该有哪层守卫"；(3) 任何一处改动都要在近 3000 行里定位，代码评审无法做到按域切分。

**严重程度**：**P1**。不是运行时故障，但它是"后端零测试""权限守卫难以审计"这两件事的共同结构性原因。

**调整方向**：按业务域拆成多个 `APIRouter` 模块（auth / admin-users / admin-org / kb / workflow / chat / dashboard），把 store 实例改为 FastAPI 依赖（`Depends`）而不是闭包捕获，让路由模块可以独立注入 mock 依赖做测试。

---

### P1-2　14 个 Store 各自复制同一套连接池样板，明文默认 DSN 复制 14 遍，池上限累计约 68 条连接

**证据**（每个文件同一段代码几乎逐字重复）
- `src/ragent_backend/store.py:54-68`
- `src/ragent_backend/user_store.py:50-64`
- `src/ragent_backend/role_store.py:85-99`
- `src/ragent_backend/org_store.py:49-63`
- `src/ragent_backend/collection_store.py:72-86`
- `src/ragent_backend/audit_store.py:53-67`
- `src/ragent_backend/workflow_store.py:158-172`
- `src/ragent_backend/ltm_store.py:36-50`（`max_size=3`，唯一不同的一个）
- `src/ragent_backend/attendance_store.py:59-73`
- `src/ragent_backend/conversation_store.py:44-58`
- `src/ragent_backend/file_store.py:58-75`
- `src/ragent_backend/tenant_connector_store.py:76-90`
- `src/ragent_backend/dashboard_stats.py:96-113`
- `src/ragent_backend/tenant_identity_store.py:34-48`

每处都有 `self._dsn = os.getenv("RAGENT_POSTGRES_URL", "postgresql://postgres:postgres@localhost:5432/ragent")`——同一个带明文口令的默认 DSN 出现 14 次。

**为什么不合理**
(1) 13×5 + 1×3 = 68 条连接是同一个进程对同一个数据库开的池上限，远超一个内部工具的实际需要，而且这个数字是"各文件默认值之和"这种没人负责的方式算出来的，改起来要动 14 个文件；(2) 明文默认 DSN 复制 14 份，未来换默认值或去掉默认值必须逐个改，漏一个就静默连到错误的库；(3) `_get_pool()` 的双检锁把 `type(self)._pool = await create_pool(...)`（`store.py:66`）写在 `_ensure_schema()`（:67）**之前**，另一个协程在锁外命中 `:61` 的快路径时可能拿到一个 schema 还没建好的池；(4) 一半的 Store 有 `close()` 但 `create_app()`/`lifespan` 从来不调用（`app.py:498-537` 的 lifespan 只断 MCP、停保活任务），进程退出时连接靠 OS 回收。

**严重程度**：**P1**。当前规模不会打满连接，但这是"同一个决策散落在 14 个文件"的典型形态，任何数据库层调整（超时、SSL、只读副本、池大小、观测）都要改 14 处。

**调整方向**：抽一个共享的连接管理模块（单一池 + DSN 来自统一配置层），各 Store 只接收 `pool`；池的生命周期挂到 FastAPI `lifespan` 上统一创建/关闭。

---

### P1-3　建表 DDL 内嵌在各个 Store 里靠 `IF NOT EXISTS` 打补丁，没有迁移体系

**证据**
- `src/ragent_backend/tenant_connector_store.py:92-118`：`CREATE TABLE IF NOT EXISTS tenant_connectors (...)` 之后跟着 5 条 `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`。
- 同样模式：`store.py:180-183`、`user_store.py:67-70`、`role_store.py:102-147`（roles / user_roles / role_collections 三张表）、`workflow_store.py:175-243`（四张表）、`conversation_store.py:61-64`、`file_store.py:78-81`、`org_store.py:66-69`、`collection_store.py:89-92`、`audit_store.py:70-73`、`ltm_store.py:53-56`、`attendance_store.py:76-79`、`tenant_identity_store.py:51-54`。
- `scripts/` 下并存 4 个方向互相矛盾的手写迁移：`migrate_to_roles.py`、`migrate_roles_to_kb_groups.py`、`migrate_kb_groups_to_roles.py`、`migrate_delegated_orgs_to_local_kb.py`。
- 遗留死列：`user_store.py:229-235` 注释 "`users.allowed_collections` 列本身已不再是权限真相源"，但列还在表里、方法还在（只是转发给 `RoleStore`）。

**为什么不合理**
schema 的真相源是"第一个碰这张表的进程恰好跑到的那段 Python"。没有版本号，就无法回答"这个环境的 schema 是哪个版本""能不能回滚"。加列只能靠 `ADD COLUMN IF NOT EXISTS`（永远只能往后加、不能改类型/改约束/删列），所以废弃的列（`users.allowed_collections`）只能永久留着。`role_collections` 这类语义反复变过的表（4 个方向相反的迁移脚本可以佐证）已经经历了至少两轮来回，靠人手跑脚本对齐。

**严重程度**：**P1**。小团队单实例部署下不会立刻炸，但已经积累了不可回退的历史包袱。

**调整方向**：引入 Alembic（或等价的版本化迁移），把现有 DDL 一次性 baseline 成初始版本，Store 里只留查询、不留建表；把 4 个 migration 脚本归档进迁移历史。

---

### P1-4　`src/ragent_backend/` 与 `src/tool_agent/` 零测试覆盖

**证据**
- `grep -rn "from src.ragent_backend\|import src.ragent_backend" tests/` → 0 命中。
- `grep -rn "src.tool_agent" tests/` → 0 命中。
- `grep -rn "TestClient\|ASGITransport\|create_app" tests/` → 0 命中。
- 对照：`tests/unit/` 里 49 个文件全部测 `libs`/`core`/`ingestion`/`observability`（如 `test_fusion_rrf.py` 538 行、`test_response_builder.py` 544 行）。
- `tests/conftest.py:1-50` 只有 `project_root`/`sample_documents_dir`/`config_dir` 三个路径 fixture。

**为什么不合理**
风险最高、变更最频繁的那部分代码恰好一行测试都没有：多租户 ACL（`acl.py`、`role_store.get_allowed_collections_for_user`、`query_knowledge_hub.execute` 里的三层拦截）、角色授予边界（`app.py:646-702` 的 `_validate_role_assignment` 四条规则）、LangGraph 路由（`workflow.py:263-299` 的 `_route_after_intent`）、prompt 泄露过滤（`prompt_guard.py` 全模块）。这些全是"改错了不会报错、只会静默放行/静默拒绝"的逻辑——正是最需要回归保护的类型。而 `git log` 显示最近 5 个提交全是关于权限模型和租户 KB 架构的返工，说明这块正在高频变动。

`prompt_guard.py` 只有 95 行纯函数、零依赖，写测试的成本几乎为零，却也一个测试都没有——说明这不是"难测"，而是测试习惯没有延伸到新模块。

**严重程度**：**P1**（在小团队场景下不到 P0，但结合 P0-1/P0-2 看，正是缺乏这层保护才让那两个问题一直没被发现）。

**调整方向**：优先补三类：(1) 纯函数（`acl.py`、`prompt_guard.py`、`_validate_workflow_field` 等静态方法）——立刻能写；(2) ACL 决策表测试（给定角色/组织/collection 组合断言允许或拒绝），用 fake store 而不是真库；(3) 配合 P1-1 拆路由后，用 `TestClient` + 依赖覆盖做端点级冒烟测试。

---

### P1-5　后端唯一的日志手段是 48 处 `print()`，与其余模块的 logging 体系完全割裂

**证据**
- `src/ragent_backend/`：`print(` **48 处**，`logger.` **0 处**。典型：`workflow.py:491`、`493`、`1057`、`1150`、`1293`、`1450`、`1537`、`1539`、`1556`；`app.py:133`、`153`、`169`、`171`、`249`、`258`、`261`、`355`、`429`、`445`、`493`、`517`、`523`、`525`、`531`、`2649`、`2731`。
- 对照：`src/ingestion/` 111 处 `logger.`、`src/mcp_server/` 46 处、`src/core/` 41 处、`src/libs/` 39 处。
- 已有的可观测基础设施：`src/observability/logger.py:29`（`get_logger`）、`:60`（`JSONFormatter`）、`:107`（`get_trace_logger`）、`:145`（`write_trace`）——`grep` 显示**没有任何 `ragent_backend` 文件导入它**。
- `config/settings.yaml:75-79` 配了 `log_level` / `structured_logging: true` / `trace_file`——后端一个字段都不读。

**为什么不合理**
`print()` 没有级别、没有时间戳、没有 logger 名、没有异常栈、不受 `log_level` 控制、不能被 handler 路由到文件/采集系统，也无法附加 `conversation_id`/`user_id`/`request_id` 这类结构化字段。具体后果：P0-1 那种"随机串流"的问题在现有日志里根本无法排查（分不清哪条 `print` 属于哪个请求）；`app.py:355` 的 `print(f"[Audit] Failed to record: {e}")` 意味着审计落库失败只在 stdout 留一行、没有告警；`workflow.py:1305` 的 `answer = f"生成失败：{str(e)}"` 把原始异常文本直接当成回答返回给用户，同时**没有记录任何栈信息**。

同时项目里已经并存了三套互不相通的可观测机制：`print()`（后端）、`logging`（其余模块）、`TraceCollector`/`trace_events`（写 `logs/traces.jsonl`，只给 Streamlit 仪表盘看）。没有一条贯穿的 request id 把它们串起来。

**严重程度**：**P1**。日志质量直接决定了 P0 级 bug 能不能被发现——这是本次审计里最"划算"的一条。

**调整方向**：`ragent_backend` 统一改用 `src/observability/logger.get_logger`，接上 `settings.observability`；加一个 `request_id` middleware，通过 `contextvars` 注入到每条日志和每个 trace 事件；`print` 全部替换（异常路径改 `logger.exception` 保留栈）。

---

### P1-6　trace WebSocket 无任何鉴权

**证据**
- `src/ragent_backend/app.py:2741-2745`：`@app.websocket("/ws/trace/{conversation_id}")` → `await websocket.accept()` 直接接受，没有 `Depends(get_current_user)`，也没有在握手时校验 token 或校验 `conversation_id` 归属。
- 对照：同文件里 HTTP 侧的对话访问都走了 `_require_conversation_owner`（`app.py:376-385`）。
- 推送内容：`app.py:2702` → `broadcast_trace(thread_id, event)`；事件负载见 `workflow.py:653`（`original_query`）、`:583-585`（`rewritten_query`）、`:626-628`（`clarify_prompt`）、`subgraph.py:211`（工具入参）、`:218-220`（工具结果状态）。

**为什么不合理**
任何能连到后端的人，只要拿到/猜到一个 `conversation_id`（它就出现在前端 URL 和 `localStorage` 里，也会通过 SSE 的 `conversation_created` 事件明文下发），就能订阅该对话的实时执行轨迹：用户原始问题、重写后的查询、命中的知识库名、工具调用参数。这绕开了整套 collection 级 ACL——ACL 管的是"能不能查到内容"，这里泄露的是"别人查了什么、命中了哪个库"。在多租户场景下，命中的库名本身就是敏感信息（模块里 `query_knowledge_hub.py:86-88` 专门讨论过"不要间接暴露某类资料存在与否"，这条通道把那份克制全绕过去了）。

**严重程度**：**P1**（内网部署降低了暴露面，但它是权限模型上一个完整的旁路）。

**调整方向**：WebSocket 握手时校验 token（query param 或 subprotocol），并复用 `_require_conversation_owner` 校验该用户是不是这个对话的主人；订阅关系按 `(user_id, conversation_id)` 建立，而不是只按 `conversation_id`。

---

### P1-7　分层倒置 + 循环依赖：工具层反向依赖后端层，靠 10+ 处函数内延迟导入绕开

**证据**
- 正向：`src/ragent_backend/workflow.py:30` → `from src.mcp_server.tools.query_knowledge_hub import QueryKnowledgeHubTool`；`app.py:86-92` 还从 `ingestion`/`core`/`tool_agent`/`mcp_server` 各导入一批。
- 反向（全部是函数体内延迟导入，用来躲循环）：
  - `query_knowledge_hub.py:240`（`UserStore`）、`:248`（`OrgStore`）、`:256`（`TenantConnectorStore`）、`:264`（`OrgCollectionStore`）、`:406-410`、`:432`（`RoleStore`）、`:477`（`acl`）、`:593`、`:646`
  - `list_collections.py:130`、`:313`
  - `get_document_summary.py:171`、`:595`
  - `auth.py:89`（`RoleStore`）、`:120`（`OrgStore`）、`:139`
  - `workflow.py:724`（`OrgStore`）、`:843`（`RoleStore`）
- `query_knowledge_hub.py:404-405` 的注释直接承认了这一点："顶层导入 tenant_connector_store 会触发 `src.ragent_backend` 包初始化，而该包的 `workflow.py` 又导入本模块，形成循环导入。"

**为什么不合理**
`src/mcp_server/` 名义上是"通用 MCP 工具层"，`TOOL_DESCRIPTION`（`query_knowledge_hub.py:113-132`）和入参 schema 都是按通用工具写的，但实现里塞满了本项目特有的多租户业务规则：组织归属、连接器路由、角色→类目映射、org_admin 特判。这导致：(1) 这个"MCP 工具"根本无法脱离 `ragent_backend` 独立跑（`main.py` 那个独立 MCP server 入口只能在 `user_id=None` 的降级模式下工作）；(2) 循环依赖只能靠十几处延迟导入压住，任何一次顶层导入的顺序调整都可能突然炸出 `ImportError`，而且是运行时才发现；(3) 权限判断逻辑被劈成两半——一半在 `app.py` 的 `Depends`，一半在 `query_knowledge_hub.execute` 里（`:474-514` 三层拦截），没有单一的权限决策点。

**严重程度**：**P1**。

**调整方向**：把"谁能查哪些 collection / 路由到本地还是委托"抽成后端侧一个独立的授权服务，工具层只接收一个已经解析好的 `SearchScope`（候选 collection 列表 + 远端连接器句柄），不再反向 import 任何 store；工具层变回真正无状态、可独立测试的检索实现。

---

### P1-8　检索链路每次查询都重建全套组件，全链路没有任何缓存层

**证据**
- `src/mcp_server/tools/query_knowledge_hub.py:296-353`（`_build_hybrid_search_for`）：每次调用都 `VectorStoreFactory.create(...)`（:328，新建 ChromaDB PersistentClient）、`BM25Indexer(index_dir=...)`（:339，从磁盘加载索引）、新建 dense/sparse retriever 和 `QueryProcessor`。
- `:1137-1140`：`_build_all_sync()` 对 **每一个候选 collection 各建一套**，一个 6 库的企业每次提问就是 6 个 Chroma client + 6 次 BM25 索引加载。
- `:355-363`（`_ensure_initialized`）：docstring 写着"只有 collection 变了才重建"，但函数体**无条件**调用 `_build_hybrid_search_for` 并覆写 `self._hybrid_search`——注释与实现不符，缓存实际不存在。
- `:1054-1063`：摘要层粗筛又串行建一遍所有 `{collection}__summary` 的 store。
- `:791`、`:807`、`:1238`：每次远端调用 `async with httpx.AsyncClient(...)` 新建客户端，不复用连接。
- 全仓无任何检索结果缓存 / embedding 缓存 / query 缓存（无 `lru_cache`、无 Redis、无内存 TTL 缓存）。
- `RAGWorkflow.__init__` 还额外 new 了一个自己的 `QueryKnowledgeHubTool()`（`workflow.py:198`），与 `builtin_tools.py:112` 注册进 registry 的那个是两个独立实例，各自持有一份 embedding client / reranker 状态。

**为什么不合理**
每次提问付出的固定成本包括：N 次 ChromaDB client bootstrap（作者自己在 `:1119-1127` 记录过并发 bootstrap 会偶发崩溃，只好把这一步串行化——串行化又把这部分固定成本变成了首字延迟的直接组成部分）、N 次 BM25 索引从磁盘反序列化。这些对象都是可以跨请求复用的（collection 数量有限且低频变化）。项目已经有一份 `docs/latency_report.md` 在优化首字延迟、并为此微调了一个 1.5b 路由模型省 5 秒，但检索侧这块每次重建的开销从未被处理——优化的力气花在了模型上，而没花在这个更容易拿到的地方。

**严重程度**：**P1**（性能/成本架构问题，不影响正确性）。

**调整方向**：按 `collection` 做一层进程级组件缓存（LRU + 显式失效钩子，摄入/清库时 invalidate）；`httpx.AsyncClient` 提升为长生命周期共享实例挂到 lifespan；合并 `workflow.py:198` 那个重复的工具实例，统一从 registry 取。

---

### P1-9　租户连接器凭证明文存库

**证据**
- `src/ragent_backend/tenant_connector_store.py:102`：`auth_config JSONB NOT NULL DEFAULT '{}'`（无加密）。
- `app.py:1000-1006`：`auth_config = {"token": request.token}` 直接原样落库。
- 使用处：`query_knowledge_hub.py:1223`（`token = connector.auth_config.get("token", "")`）、`:794`、`:811`、`_execute_remote:1242-1246` 放进 `Authorization: Bearer`。

**为什么不合理**
每一家客户企业访问其自有知识库微服务的 bearer token 以明文形式存在同一张 Postgres 表里。任何一次数据库备份、只读副本、误开的 DB 客户端、或 SQL 注入/越权读，都会一次性泄露**全部**租户的下游系统凭证——爆炸半径不是本平台，而是客户自己的内网服务。没有加密、没有外部密钥管理、没有轮换机制、没有"只写不读"的接口约束。

值得肯定的是 `app.py:996-1008` 处理了"token 留空 = 不修改"的语义，避免了误清空，说明作者对凭证有意识；但没有走到"不以明文持有"这一步。

**严重程度**：**P1**。

**调整方向**：至少做应用层信封加密（主密钥来自环境变量/KMS，DB 里只存密文），或把凭证外置到密钥管理服务、库里只存引用；同时给 `auth_config` 加"永不出现在任何 response/日志"的断言（当前 `_connector_response` 需要复核）。

---

### P1-10　CORS 全放开 + 允许携带凭证

**证据**
- `src/ragent_backend/app.py:548-554`：`allow_origins=["*"]`、`allow_credentials=True`、`allow_methods=["*"]`、`allow_headers=["*"]`。

**为什么不合理**
`allow_origins=["*"]` 与 `allow_credentials=True` 同时出现在规范上是自相矛盾的组合（多数浏览器会拒绝该组合下的凭证请求），但更关键的是：本项目的 token 存在 `localStorage`（`frontend/src/App.jsx:55`、`137`、`292`）并通过 `Authorization` header 手动携带——这类请求**不受** `allow_credentials` 约束，只受 `allow_origins` 约束。因此任何网页都可以在用户浏览器里向这个后端发起跨域请求；只要该页面能拿到 token（例如通过 XSS、或诱导用户粘贴），就能直接读取全部 API。配置本身也没有区分开发/生产。

**严重程度**：**P1**（内网部署减轻，但配置是全环境硬编码的，没有留出收紧的开关）。

**调整方向**：允许来源改为从配置读取的白名单（开发环境列 `localhost:5173`，生产列实际域名），去掉 `allow_credentials=True` 或改用真正需要 cookie 时再开。

---

### P1-11　没有 Dockerfile / CI / 依赖锁定，README 却在教人 `docker build`

**证据**
- 仓库根目录无 `Dockerfile`、无 `docker-compose.yml`（`find` 确认）。
- `.github/` 下只有 `skills/`，无 `workflows/`。
- `pyproject.toml:26-50`：全部 `>=` 下界（`langgraph>=0.2.0`、`chromadb>=0.4.0`、`langchain>=0.3.0`…），无上界、无 lock 文件。
- `pyproject.toml:52-61` 定义了 dev 依赖含 `ruff`/`mypy`/`pytest-cov`，但没有任何自动化在跑它们。
- `readme.md:598-616` 写着 "### Docker 部署（推荐）" + `docker build -t ragent:latest .`——这条命令在当前仓库必然失败。
- `readme.md:635-641` 的性能调优表引用了 `parallel_retrieval` 和 `summary_model` 两个配置项，`config/settings.yaml` 里都不存在。
- 唯一的启动物是 `start.bat`（Windows 批处理），且 `readme.md:632` 的生产检查清单里还在提"`start.bat` 中的端口未被占用"。

**为什么不合理**
"能在我机器上跑"是当前唯一的可重现性保证。`langgraph>=0.2.0` / `chromadb>=0.4.0` 这类无上界约束意味着重新装环境很可能装到不兼容的大版本（`pyproject.toml:32-34` 的注释显示作者已经被 `mcp>=2.0.0` 的 breaking change 咬过一次，并为此加了上界——但只加了这一个）。没有 CI 意味着那 26,849 行测试是否还全绿完全靠人手工跑，且 `ruff`/`mypy` 配置了但没人执行（本次审计顺手发现的 `app.py:212` 未定义名 `Settings` 就是 mypy 一跑就会报的问题）。README 教的部署方式与仓库现状完全脱节。

**严重程度**：**P1**。

**调整方向**：先补最小可用的 `Dockerfile` + `docker-compose.yml`（app + postgres + ollama 挂载）；用 `uv.lock` 或 `pip-compile` 锁定依赖；加一条 GitHub Actions 跑 `pytest -m "not llm and not integration"` + `ruff check`；README 的部署段改成与仓库一致的实际步骤。

---

### P1-12　配置管理双轨：有一套像样的 pydantic 配置层，但后端完全不用它

**证据**
- 现成的配置层：`src/core/settings.py:41-157`（frozen pydantic 分区模型）、`:164-182`（`_ENV_OVERRIDES` 环境变量覆盖表）、`:206-229`（`load_settings` fail-fast 校验）。
- 后端绕过它直接 `os.getenv`：`auth.py:30`（JWT 密钥 + TTL 硬编码在 `:32`）、`app.py:106`（DSN）、`:440`（意图模型名）、`:462-463`（记忆窗口）、`:1081`（debug 开关）、`:2969`（端口），加上 P1-2 列的 14 处 DSN。
- 硬编码常量散落各处：`workflow.py:44`（确认语关键词）、`:57`（取消关键词）、`:76`（`GENERATE_MAX_TOKENS = 1200`）、`:93`（`_PROMPT_LEAK_CHECK_WINDOW = 200`）、`:1401`（`messages[-6:]`）；`app.py:175`（`INGEST_SEMAPHORE = asyncio.Semaphore(2)`）、`:199-203`（上传扩展名白名单）、`:488`（保活周期 240 秒）、`:2672`（心跳 2 秒）、`:2680`（超时 5 秒）；`query_knowledge_hub.py:41`（远端超时 8s）、`:65`（`MIN_RELEVANCE_SCORE = 0.1`）、`:101-108`（租户类目映射）；`intent.py:247-296`（四张关键词表）。
- **死配置**：`config/settings.yaml:89` 写了 `max_workers: 3`，但 `IngestionSettings`（`settings.py:120-127`）没有声明这个字段，而基类是 `extra="ignore"`（`settings.py:44`）→ 该值被静默丢弃；`pipeline.py:175-179` 的 `getattr(ingest_cfg, 'max_workers', None)` 永远返回 `None`，同时 `:176` 的 `isinstance(ingest_cfg, dict)` 分支是死代码。

**为什么不合理**
存在两套并行的配置真相源：YAML+pydantic（管 LLM/检索/摄入）和裸 `os.getenv`+代码常量（管后端一切）。后果之一就是 P0-2：如果 JWT 密钥走了 `core/settings.py` 那套（有 fail-fast 校验），"密钥没配"这件事在启动时就会被发现。后果之二是 `max_workers` 这类静默失效——配了一个值、以为生效了，实际被 pydantic 的 `extra="ignore"` 吃掉，没有任何警告。所有魔法数字（相关性阈值 0.1、泄露检测窗口 200、生成上限 1200、心跳 2 秒）都要改代码重启才能调，而其中几个（`MIN_RELEVANCE_SCORE`）恰恰是需要按语料反复调优的。

**严重程度**：**P1**。

**调整方向**：把 `ragent_backend` 的运行参数并入 `core/settings.py` 的模型（新增 `backend`/`security` 分区），保留环境变量覆盖；把可调阈值从代码常量提升为配置项；给 `IngestionSettings` 补上 `max_workers`，或者把基类改成 `extra="forbid"` 让打错的配置键立刻报错。

---

### P1-13　Prompt 硬编码在业务代码里，与已有的 `config/prompts/` 文件化方案并存

**证据**
- 硬编码在代码里的 prompt（全是最关键的几个）：
  - `workflow.py:1340-1385`（主生成 prompt，含整段指令层级安全声明，46 行）
  - `workflow.py:938-951`（工作流字段抽取 prompt）
  - `intent.py:141-166`（查询分析 prompt，含 3 个 few-shot 示例）
  - `intent.py:221-232`（查询重写 prompt）
  - `subgraph.py:498-522`（ReAct 决策 system prompt）、`:47-51`（三个 Agent persona）
  - `ltm_store.py:192` 附近（记忆提取 prompt）
- 已有的文件化 prompt 目录：`config/prompts/chunk_refinement.txt`、`doc_summary.txt`、`image_captioning.txt`、`metadata_enrichment.txt`、`rerank.txt`——只覆盖摄入侧。
- 安全相关的检测规则又单独硬编码在第三处：`src/security/prompt_guard.py:58-66`（`_PROMPT_LEAK_MARKERS` 逐字复制了 `workflow.py:1359-1376` 里的段落标题）。

**为什么不合理**
同一个项目里对 prompt 有两种截然不同的管理方式，边界是"摄入侧文件化、问答侧硬编码"，而问答侧恰恰是变更最频繁、效果最需要 A/B 对比的部分。没有版本号意味着 `docs/security_prompt_injection_test_report.md` 里的测试结论无法与某个具体 prompt 版本绑定——prompt 改一个字，那份测试报告的结论就可能失效但没人知道。更具体的耦合风险：`prompt_guard.py:58-66` 的泄露标记必须和 `workflow.py` 里 prompt 模板的段落标题**逐字一致**，两处分别硬编码在不同文件、无任何机制保证同步——改一处忘另一处，泄露检测就静默失效（而这正是 P0 级安全防线）。

**严重程度**：**P1**。

**调整方向**：问答侧 prompt 一并迁到 `config/prompts/`（带版本号或 git 追踪），代码只引用模板 id；`prompt_guard` 的泄露标记从模板文件里派生而不是二次硬编码，或至少加一个启动时断言/单测保证两处一致。

---

### P1-14　管理端普遍存在 N+1 查询

**证据**
- `app.py:715-731`（`admin_list_users`）：先 `list_users()` 拿全量，再**逐个** `org_store.get_org_for_user(u.user_id)`（:727）过滤，最后逐个 `_build_admin_user_response(u)`（:731）。
- `app.py:704-713`（`_build_admin_user_response`）：每个用户内部再发 3 次查询——`role_store.get_user_roles`（:705）、`role_store.get_allowed_collections_for_user`（:710，内部自己又是 3 次 fetch，见 `role_store.py:398-420`）、`_org_summary_for_user`（:711）。
- `app.py:1040-1049`（`admin_gateway_connectors`）：循环里 `await _check_connector_health(c)`（:1046），逐个连接器串行发健康探测 HTTP 请求。
- 鉴权本身也是每请求多次查库：`auth.py:94-95`（`require_role` 每次 new 一个 `RoleStore` 查角色）+ `require_platform_admin`（`:141`）+ `require_same_org_or_platform`（`:122-127` 两次 `get_org_for_user`）——`app.py:775-780` 这类端点挂了两个守卫，光鉴权就 3-4 次往返。

**为什么不合理**
`/api/v1/admin/users` 在 50 个用户时约产生 `1 + 50 + 50×(1+3+1) ≈ 300` 次数据库往返，全部串行 `await`。这是"逐行调用领域方法"而不是"批量查询"的架构惯性，随用户数线性劣化。网关健康探测那处更明显——串行 HTTP 探测，一个不可达的连接器就会让整页卡住直到超时。

**严重程度**：**P1**（内部工具用户数不大，不会立刻痛，但管理页已经在这条路径上出现过加载问题——`app.py:900-903` 的注释记录了 `UserRoleAssignment.jsx` 的 `Promise.all` 被拖垮的排查过程）。

**调整方向**：给 Store 加批量接口（`get_orgs_for_users(ids)` / `get_roles_for_users(ids)` / `get_allowed_collections_for_users(ids)`），端点侧一次拿全再在内存组装；健康探测改 `asyncio.gather` 并发 + 结果短 TTL 缓存；鉴权结果在单次请求内用 `contextvars` 复用，避免同一请求重复查同一份角色。

---

### P1-15　"测试专用、正式上线前删除"的绕过 ACL 功能横跨 8+ 文件长期驻留

**证据**（同一批标记散布的位置）
- 后端：`app.py:64-65`（schema import）、`:92`（工具 import）、`:311-327`（共享实例）、`:1052-1156`（4 个 debug 端点 + `_require_debug_mode`）
- 工具层：`query_knowledge_hub.py:571-635`（`execute_admin_bypass`）、`:637-786`（清空/查看企业知识库的一整套）
- schema：`schemas.py:146` 起
- 前端：`api/admin.js:116-124`、`components/admin/KnowledgeBaseTestQuery.jsx`（203 行整个组件）、`components/admin/OperationsDashboard.jsx:13`、`:32-33`、`:205`

**为什么不合理**
一个明确标注"临时、上线前删除"的功能已经长成了跨 8 个文件、含独立前端页面和独立 store 方法的完整特性，而且它做的事情正是**绕过全部用户级 ACL 直接读取任意企业的知识库内容**。`app.py:1059-1067` 的注释诚实地记录了这个演进过程（最初只有注释、后来才补上 `RAGENT_DEBUG` 运行时开关），三层守卫（super_admin + platform_admin + debug 模式）也确实做得扎实。但架构上的问题是：它把"绕过权限"这条代码路径永久编织进了主干——`query_knowledge_hub.py:588-591` 甚至需要用注释维护一份"删除时要连带删哪些文件"的清单，而 `app.py:1073-1077` 又说明其中一个共享实例**不能**删（因为正式功能也在用它）。删除的复杂度已经超过了继续留着的诱惑，这类"临时代码"在实践中就会永远留下。

**严重程度**：**P1**（当前有运行时开关兜底，但架构债在持续累积）。

**调整方向**：把这条绕过路径从主进程里移出去——做成一个独立的运维 CLI（直接连库/Chroma），或至少收进一个可以整体不挂载的独立 router 模块 + 独立前端 bundle，让"删除"退化成"删一个目录 + 去掉一行挂载"。

---

### P1-16　文档与代码严重脱节

**证据**
- `docs/api.md` 只覆盖 6 个端点（`:76` chat、`:142` chat/stream、`:186` history、`:231`/`:285`/`:338` files、`:366` memory stats），实际有 **71** 个（`app.py` 71 个 `@app.*`）。且 `:603-630` 的全部 curl 示例都**不带 `Authorization` 头**，与 `get_current_user`（`auth.py:63-72`）要求的鉴权完全不符——按文档抄一定 401。
- `readme.md:600-616` 的 Docker 部署段对应的 `Dockerfile` 不存在。
- `readme.md:635-641` 引用的 `parallel_retrieval` / `summary_model` 配置项在 `config/settings.yaml` 里不存在。
- `main.py:37`：`logger.info("MCP Server will be implemented in Phase E.")`——占位实现，但 `pyproject.toml:64` 把它注册成了正式入口 `mcp-server = "main:main"`。
- `query_knowledge_hub.py:355-363`：`_ensure_initialized` 的 docstring 说"只有 collection 变了才重建"，实现是无条件重建。
- `workflow.py:224-227`：注释断言"多个并发请求互不串"，实际见 P0-1。
- `intent.py:2`：模块 docstring 写"三分支路由（clarify / rag / tool）"，实际早已是四分支（`intent.py:37` 的 Literal 含 `workflow`）。
- 根目录 9 个 markdown 共约 300KB 设计文档（`work-flow.md` 47KB、`knowledge-base-tenant-federation.md` 29KB…），代码里大量注释以"见 xxx.md 第 N 节"的形式引用它们——文档实质上被当成了代码的一部分，但没有任何机制保证它们同步。

**为什么不合理**
新人（或半年后的自己）唯一的入口文档描述的是一个不存在的系统：没有鉴权、只有 6 个端点、能用 Docker 跑。而真实的系统有 71 个端点、强制鉴权、只能手工起。更麻烦的是注释断言与实现不符的几处（`_ensure_initialized`、并发安全声明）——**错误的注释比没有注释更危险**，它会让 reviewer 跳过本该仔细看的代码，P0-1 之所以能存在，`workflow.py:224-227` 那句"互不串"的注释很可能起了作用。

**严重程度**：**P1**。

**调整方向**：`docs/api.md` 改成从 FastAPI 自动生成的 OpenAPI 导出（消除手工同步）；README 的部署/配置段与仓库现状对齐；有断言性质的注释（"这样是线程安全的""这里有缓存"）改写成单元测试，让声明可被验证；根目录 9 个大文档收进 `docs/` 并标注最后校对日期。

---

## 四、P2（改进项）

### P2-1　`RAGWorkflow` 是上帝类，什么都往里塞

**证据**：`workflow.py` 1,575 行一个类里同时装着——图定义与路由（`:203-299`）、trace 推送（`:301-319`）、流式编排（`:341-426`）、6 个节点实现、**一个完整的表单收集引擎**（`:709-996`：字段抽取 prompt、日期换算表、类型校验、缺失字段计算、追问话术生成、提交与并发冲突处理）、prompt 模板（`:1325-1395`）、token 用量估算（`:96-137`）、prompt 泄露流式过滤（`:1239-1293`）、归档与后台任务管理（`:1471-1568`）。

**为什么不合理**：`_workflow_node`（`:709-882`，173 行）和它的 5 个辅助方法本质上是一个独立的"对话式表单引擎"，跟 RAG 没有任何关系，只是恰好也需要 LLM。它们和检索、生成、记忆压缩挤在同一个类里，共享同一个 `self._llm`，导致这个类无法按职责单独测试或替换。类似地，token 估算（纯函数）、泄露过滤（纯策略）都可以是独立模块。

**严重程度**：P2（可读性/可测性问题，不影响运行）。

**方向**：把工作流表单引擎、token 计量、输出过滤各抽成独立模块，`RAGWorkflow` 只保留图编排与节点粘合。

---

### P2-2　`_emit_trace` 的 fire-and-forget task 无强引用，且要求调用处必须有事件循环

**证据**：`workflow.py:310-319`：`asyncio.create_task(self._trace_queue.put({...}))` 返回值被丢弃。对比同文件 `:186-190` 的注释——作者明确知道这个陷阱并为归档任务建了 `self._background_tasks` 强引用集合，但 `_emit_trace` 没享受到同样的处理。另外 `subgraph.py:426-431` 记录了一次真实事故：在同步的条件边路由函数里调 `_trace` 会因为没有运行中的事件循环直接抛 `RuntimeError` 让整条请求崩掉，只好绕开不埋点。

**为什么不合理**：同一个已知陷阱在同一个文件里修了一处、漏了另一处，说明缺少统一的后台任务管理约定。`_emit_trace` 是同步签名却依赖异步上下文，这个隐式契约已经害得可观测性在一个关键分支（工具调用后的路由决策）上留了空白。

**严重程度**：P2。

**方向**：`_emit_trace` 改成 `put_nowait`（队列无界时不会阻塞，也不需要 task），彻底去掉对事件循环的依赖；或统一走一个后台任务管理器。

---

### P2-3　`RAGState` 是 30+ 字段的扁平 TypedDict

**证据**：`schemas.py:489-556`——同一个字典里混着身份（`user_id`）、UI 展示（`kb_sources`）、意图分类中间结果（6 个字段）、工作流表单进度（`active_workflow`）、检索上下文、token 计量、trace、以及两个下划线开头的"内部临时状态"（`_to_archive`、`_turn_start_ts`）。`workflow.py:1486` 还用 `state.pop("_to_archive")` 直接原地改状态。

**为什么不合理**：没有分组意味着任何节点都能读写任何字段，节点之间的真实数据依赖只能靠读代码推断；`_` 前缀字段靠命名约定而非类型系统区分"会不会进 checkpoint"；`state.pop` 在 LangGraph 的 reducer 语义下是可疑写法（节点应返回增量而非改入参）。

**严重程度**：P2。

**方向**：按域拆成嵌套子结构（`intent` / `retrieval` / `workflow` / `telemetry`），瞬态字段用 LangGraph 的 config 或独立通道而不是塞进 state。

---

### P2-4　`scripts/` 混杂：47 个条目里 13 个是一次性调试脚本，4 个是方向互斥的迁移

**证据**
- 一次性调试脚本（明显是排查某个具体 bug 时留下的，且互为版本）：`test_generator_exit.py`、`test_generator_exit2.py`、`test_generator_exit3.py`、`test_cancel_source.py`、`test_stream_direct.py`、`test_stream_manual.py`、`test_ainvoke_direct.py`、`test_chat_stream.py`、`test_ws_trace.py`、`test_ws_trace_existing.py`、`test_e2e_trace.py`、`test_rollback.py`、`check_writes_schema.py`。
- 方向互斥的迁移：`migrate_to_roles.py` / `migrate_roles_to_kb_groups.py` / `migrate_kb_groups_to_roles.py` / `migrate_delegated_orgs_to_local_kb.py`。
- 危险的运维脚本与调试脚本同级混放：`reset_all_passwords.py`、`set_permissions.py`。

**为什么不合理**：`scripts/` 同时承担了"正式运维工具"（`init_postgres.py`、`ingest.py`、`seed_*.py`）、"一次性数据迁移"、"临时调试草稿"三种角色，没有目录区分。`test_generator_exit{,2,3}.py` 这种命名序列直接反映了缺少可复现测试环境时的排查方式——问题解决后脚本没删，下一个人无法判断哪些还有效。`migrate_roles_to_kb_groups` 和 `migrate_kb_groups_to_roles` 同时存在，说明"角色 vs 知识库分组"的模型来回改过两次（`role_store.py:16-22` 的注释也印证了），而这段历史只能从脚本名字推断。

**严重程度**：P2。

**方向**：拆成 `scripts/ops/`（长期维护）、`scripts/migrations/`（按日期归档，跑过即冻结）、删除调试草稿；调试草稿里有价值的场景（流式中断、回滚、WS trace）转成 `tests/` 下的正式测试。

---

### P2-5　前端 `App.jsx` 是 1,334 行上帝组件，且绕过自己建的 API 层

**证据**
- `frontend/src/App.jsx` 1,334 行，一个组件内管着：登录/登出/改密（`:286-380`）、axios 全局拦截器（`:188-208`）、对话 CRUD（`:385-443`）、非流式 chat（`:498`）、SSE 流式 chat（`:580-660`）、历史加载（`:665`）、文件上传/删除（`:700-760`）、设置持久化（`:82`）。
- 已经存在一个 API 层：`src/api/admin.js`（144 行）、`collections.js`、`notifications.js`、`workflow.js`，且都用统一的相对路径 `const BASE = '/api/v1/admin'` 走 Vite 代理（`api/admin.js:5`）。
- 但 `App.jsx` 完全不用它，全部内联 `axios.get(\`${settingsRef.current.apiBase}/...\`)`（`:286`、`:332`、`:385`、`:417`、`:435`、`:498`、`:665`、`:724`…共 20+ 处），基址来自 localStorage 里用户可改的设置（`:82`）。
- 两种基址约定并存：`api/*.js` 用相对路径 + Vite 代理，`App.jsx` 用可配置绝对地址。

**为什么不合理**：同一个前端里有两套互不兼容的后端访问约定，切换环境时只有一半代码会跟着走。核心聊天链路（最复杂的那部分：SSE 解析、中断处理、conversation 生命周期）恰恰是没有被抽出来的那部分，无法单独测试或复用。

**严重程度**：P2。

**方向**：把 App.jsx 里的所有请求收进 `api/`（新增 `chat.js`/`conversations.js`/`auth.js`），统一基址约定；聊天状态用 reducer/自定义 hook 从组件里抽出来。

---

### P2-6　租户业务策略被硬编码成源码常量

**证据**
- `query_knowledge_hub.py:101-108`：`DEPARTMENT_KB_GROUP_TO_REMOTE_CATEGORIES`——角色名 → 中文类目标签的映射写死在代码里，用于委托模式下的权限过滤。`:79-80` 的注释自己承认："这份映射是我们自己按分组名字面意思推断出来的默认策略，不是企业自己配的（后台还没有让企业管理员自定义这份映射的入口）"。
- `intent.py:247-296`：`_TOOL_KEYWORDS` / `_TOOL_INTENT_KEYWORDS` / `_WORKFLOW_KEYWORDS` / `_WORKFLOW_ACTION_PREFIXES` / `_WORKFLOW_QUESTION_CUE_WORDS` / `_VAGUE_PRONOUNS` / `_TEMPORAL_SUFFIXES_AFTER_ZHE_NA` 七张关键词表。
- `workflow.py:44`、`:57`：确认/取消关键词。
- `app.py:199-203`：上传扩展名白名单。

**为什么不合理**：`DEPARTMENT_KB_GROUP_TO_REMOTE_CATEGORIES` 是最严重的一处——它是一条**安全边界**（fail-closed 过滤，见 `:85-88`），却硬编码了 6 个具体中文标签。接入一个类目命名不同的新客户，要么改代码重新部署，要么这个客户的所有非管理员员工什么都查不到（fail-closed 的必然结果）。这把"多租户配置"变成了"多租户分支代码"，与整个 connector 架构的意图（把租户差异外部化）相矛盾。关键词表则是每次发现误判就要改代码的形态（`intent.py:294-296` 记录了一次这样的修复）。

**严重程度**：P2（当前只有演示租户，还没痛；但它会随客户数线性恶化）。

**方向**：类目映射落到 `tenant_connectors.field_mapping`（这个字段已经存在，见 `tenant_connector_store.py:104`）或单独的租户策略表，由企业管理员在后台配置；关键词表外置成配置文件。

---

### P2-7　三套持久化并存，清库逻辑必须手工同步三处

**证据**：`query_knowledge_hub.py:757-786`（`_local_collection_clear`）一个函数里同时操作——ChromaDB（`:768` `delete_collection`）、文件系统 BM25 索引目录（`:770-772` `shutil.rmtree`）、SQLite（`:774-783` 手动 `sqlite3.connect` 删两张表）。另外 Postgres 存元数据（`collection_store.py`）。摄入侧对应地写四处：`pipeline.py:152`（SQLite 完整性）、`:158`（SQLite 去重）、`:213`（Chroma）、`:216`（BM25 目录）、`:230`（摘要 collection）。

**为什么不合理**：一个"知识库"的物理表示分散在 4 个存储里（Postgres 元数据 / Chroma 向量 / 磁盘 BM25 / SQLite 摄入历史 + 去重索引），没有事务、没有一致性校验。删除时要靠人记住四处都要清（`:758-760` 的注释说明这段逻辑原本还只存在于临时脚本里）。任何一步失败都会留下孤儿数据，而且没有任何对账机制能发现。

**严重程度**：P2。

**方向**：把"collection 生命周期"收敛成一个显式的领域服务（create/clear/delete 各自负责全部四种存储，带补偿），并加一个一致性检查脚本；长期考虑把 SQLite 里的摄入历史/去重索引并入 Postgres，减少一种存储。

---

### P2-8　后台任务缺乏优雅关闭

**证据**：`workflow.py:1527-1541`（归档 task）、`:1558-1560`（LTM 抽取 task）加进 `self._background_tasks` 只是为了防 GC，进程关闭时**没有任何地方 await 它们**。`app.py:498-537` 的 `lifespan` 只处理了 MCP 断连和保活任务取消，没碰这些。`app.py:206-264` 的 `ingest_file_task` 同样是裸 `create_task`（`app.py:2280` 附近的上传端点触发），进程退出时正在摄入的文件会停在 `ingesting` 状态。

**为什么不合理**：重启时正在飞的归档写入、LTM 提取、文件摄入会被直接掐断——用户会看到最后一轮对话丢失、或文件永远卡在"处理中"。这类内部工具重启频繁（改代码就重启），命中概率不低。

**严重程度**：P2。

**方向**：`lifespan` 关闭阶段 `await asyncio.gather(*workflow._background_tasks, ...)` 加超时；摄入任务改为可恢复的状态机（重启后扫描 `ingesting` 状态的记录重新入队）。

---

### P2-9　零散的正确性/整洁性问题

| 问题 | 证据 | 说明 |
|---|---|---|
| 未定义的类型注解 | `app.py:212` `settings: Settings` | `Settings` 从未 import；靠 `from __future__ import annotations`（`:11`）不求值才没炸。mypy 一跑就报，但 mypy 没在跑（P1-11）。 |
| 死配置 | `config/settings.yaml:89` + `settings.py:120-127` + `pipeline.py:175-179` | `ingestion.max_workers: 3` 被 `extra="ignore"` 静默丢弃，`max_workers` 永远是 `None`；`:176` 的 dict 分支是死代码。 |
| 死入口 | `main.py:37` + `pyproject.toml:64` | 「Phase E 未实现」的占位实现被注册成正式命令行入口 `mcp-server`。 |
| 死列 | `user_store.py:229-235` | `users.allowed_collections` 已不是权限真相源，方法退化成纯转发，列仍在表里。 |
| 重复实例 | `workflow.py:198` vs `builtin_tools.py:112` | 两个独立的 `QueryKnowledgeHubTool`，各持一份 embedding/reranker 状态；前者没有注入任何 store（走懒加载分支）。 |
| 单实例上的可变检索状态 | `query_knowledge_hub.py:360-362`（`_ensure_initialized` 写 `self._hybrid_search`）+ `:924-926`（`_perform_search` 读它） | 注册表里是共享单例，显式指定 collection 的路径仍会在并发下互相覆写——作者在 `:296-306` 为多库路径解决了这个问题，但单库路径没改。 |
| 异常文本直接当回答返回 | `workflow.py:1305` `answer = f"生成失败：{str(e)}"` | 原始异常字符串进入用户可见的回答并落库，可能泄露内部细节；同时该分支不记录任何栈信息。 |
| 端点权限守卫靠占位参数堆叠 | `app.py:1091-1093`（`_`/`__`/`___` 三个 Depends） | 权限组合无法被静态检查，只能逐个端点肉眼核对；71 个端点全靠这种方式。 |

---

## 五、项目做得好的地方

诚实地讲，这个项目有几处明显高于同类个人项目的水准：

1. **注释质量极高，且大量记录了「为什么」而不是「是什么」**。`workflow.py:33-44`（简短确认语兜底的完整推理链）、`query_knowledge_hub.py:56-65`（`MIN_RELEVANCE_SCORE=0.1` 的实测依据："问 iPhone16 充电器时 6 个库凑出的噪音稳定在 0.03 以下，真相关的稳定在 0.13 以上"）、`:1119-1127`（ChromaDB 并发 bootstrap 崩溃的根因与"只把建 client 串行化"的取舍）、`store.py:49-53`（连接池挂类属性的原因，附排查日期）。这些是真实踩坑记录，价值远超普通注释。

2. **对「本地小模型会编造」这个风险有系统性的防御设计，且做法一致**。三处关键短路都是「固定模板 + 完全跳过 LLM」：ACL 拒绝（`workflow.py:1165-1184`、`subgraph.py:343-355`）、知识库空命中（`:1197-1217`）、简短确认语无草稿（`:586-600`）。作者明确意识到「把免责声明交给本地小模型自由发挥」本身就是风险源（`:62-65`），并给出了一致的解法。这个判断是对的，且贯彻得很彻底。

3. **多租户隔离做了多层防御，而不是只靠一层**。`query_knowledge_hub.py:474-514` 对显式指定 collection 的路径叠了三道：ACL 判断 → `tenant_*_kb` 前缀硬拦截 → `_org_owned_collections` 归属二次校验，且 `:484-491` 说明了第二道存在的意义是「防止未来有人改动存储布局时悄悄破坏这个保证」。委托模式的类目过滤明确选择 fail-closed（`:85-88`），并写清了「宁可员工发现用不了去找管理员，也不要在判断不出来时默认放行」。`app.py:646-702` 的角色授予四条边界也考虑到了「企业管理员编辑自己时会带上已有角色」这种真实的边界情况。

4. **性能优化有数据支撑，不是拍脑袋**。意图模型的选型（`workflow.py:169-176`、`app.py:432-448`）记录了"7b 跑合并任务 3/3 误判 → 换微调 1.5b → 3.2s vs 8.7s"的完整实验过程，并明确标注「这个结论只对这一个特定任务成立，其他子任务没验证过，继续用 7b」——这种对结论适用范围的自我限制很少见。`subgraph.py:395-425` 跳过冗余 think 轮次的四个前置条件也定义得很克制。

5. **老 RAG 库层（`src/libs`、`src/core`、`src/ingestion`）的分层与抽象是标准的、干净的**。factory 模式统一（`llm_factory`/`embedding_factory`/`reranker_factory`/`vector_store_factory`/`splitter_factory`），每个都有 base 抽象 + 多个 provider 实现 + 契约测试（`test_vector_store_contract.py`、`test_metadata_enricher_contract.py`）。这部分 26,849 行测试的质量是真实的，覆盖了降级 fallback（`test_reranker_fallback.py` 488 行）、幂等（`test_vector_upserter_idempotency.py`）、RRF 融合（`test_fusion_rrf.py` 538 行）这些正确的关注点。

6. **`src/core/settings.py` 是一个设计良好的配置层**——frozen pydantic 分区、CWD 无关的路径解析（`:27-34`）、显式的环境变量覆盖表（`:164-182`）、fail-fast 校验并包装成统一异常（`:226-229`）。问题不在它本身，而在后端没有用它（P1-12）。

7. **降级路径普遍考虑到了**：混合检索单路失败降级、reranker 不可用 fallback、记忆压缩 LLM 失败退回拼接、意图合并调用失败退回两次调用（`intent.py:8-10`）、`_extract_token_usage` 拿不到真实 usage 就估算并标记 `estimated=True`（`workflow.py:108-137`）——且都明确标注了降级状态，不假装数据是精确的。

**需要说明的是**：上面这些优点集中在「单点判断的质量」上——每一个具体决策都想得很清楚。本次审计发现的问题则几乎全部在「跨文件的结构约束」上——同一个决策在 14 个文件里各写一遍、per-request 状态放进了单例、新模块没继承老模块的日志/测试惯例。这是一个典型的「深度够、约束层缺失」的形态。

---

## 六、按严重程度汇总

| # | 问题 | 级别 |
|---|---|---|
| P0-1 | 流式 token/trace 队列是共享实例属性 → 并发请求跨用户串流 | P0 |
| P0-2 | JWT 密钥硬编码默认值且全仓无处设置 → 可伪造任意身份 | P0 |
| P1-1 | `create_app()` 2976 行 / 71 端点，无路由分层、无依赖注入 | P1 |
| P1-2 | 14 个 Store 复制连接池样板 + 明文默认 DSN ×14 + 池上限 ~68 | P1 |
| P1-3 | DDL 内嵌各 Store，无迁移体系，4 个互斥迁移脚本 | P1 |
| P1-4 | `ragent_backend` + `tool_agent`（12,200 行）零测试，无 API 层测试 | P1 |
| P1-5 | 后端 48 处 `print()` / 0 处 logger，无结构化日志、无 request id | P1 |
| P1-6 | trace WebSocket 无鉴权 | P1 |
| P1-7 | 分层倒置 + 循环依赖，靠 10+ 处延迟导入压住 | P1 |
| P1-8 | 检索链路每查询重建全套组件，全链路无缓存 | P1 |
| P1-9 | 租户连接器 token 明文存 Postgres | P1 |
| P1-10 | CORS `allow_origins=["*"]` + `allow_credentials=True` | P1 |
| P1-11 | 无 Dockerfile / CI / 依赖锁定，README 却教 `docker build` | P1 |
| P1-12 | 配置双轨：pydantic 配置层存在但后端不用；含静默失效的死配置 | P1 |
| P1-13 | 关键 prompt 硬编码在代码，与 `config/prompts/` 并存；泄露标记两处重复 | P1 |
| P1-14 | 管理端普遍 N+1（`/admin/users` 约 300 次串行查询） | P1 |
| P1-15 | 「上线前删除」的绕过 ACL 功能横跨 8+ 文件长期驻留 | P1 |
| P1-16 | 文档与代码严重脱节（api.md 6/71 端点、注释断言与实现相反） | P1 |
| P2-1 | `RAGWorkflow` 上帝类（含一整个表单引擎） | P2 |
| P2-2 | `_emit_trace` fire-and-forget task 无强引用 + 隐式事件循环依赖 | P2 |
| P2-3 | `RAGState` 30+ 字段扁平 TypedDict，`state.pop` 原地改状态 | P2 |
| P2-4 | `scripts/` 混杂 13 个一次性调试脚本 + 4 个互斥迁移 | P2 |
| P2-5 | 前端 `App.jsx` 1334 行上帝组件，绕过自建 API 层，两套基址约定 | P2 |
| P2-6 | 租户类目映射等业务策略硬编码成源码常量 | P2 |
| P2-7 | 四种存储并存，清库需手工同步 | P2 |
| P2-8 | 后台任务（归档/LTM/摄入）无优雅关闭 | P2 |
| P2-9 | 零散：未定义注解、死配置、死入口、死列、重复实例、异常文本当回答 | P2 |
