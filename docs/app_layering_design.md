# `create_app()` 分层设计

> **状态：设计待确认，未实施** · 2026-08-27 · 死期 2026-11-27
> （超过死期仍未实施则删除本文，不要让一份没人执行的设计留在仓库里冒充计划）

---

## 1. 问题不是"文件太长"

`CLAUDE.md` P1 把这条记成「`create_app()` 3038 行 / 72 端点，无路由分层、无依赖注入」。
2026-08-27 实测数字已经变了：**`app.py` 4497 行、`create_app()` 函数体 4098 行、
内嵌 143 个函数、93 个端点**。但更要紧的是——**行数不是问题本身**。

真正的问题是这一句（CLAUDE.md §7.1 自己写过）：

> 测一个端点就得建整个 app 并连 Postgres，这是**后端 12,200 行零测试的结构性原因**。

93 个端点全是 `create_app()` 里的内嵌函数，它们用到的 `ops_store` / `workflow` /
`tool_registry` / `ops_toolset` 等等**全部靠闭包捕获**。这意味着：

- 想测一个端点，必须先跑完 `create_app()` 的全部初始化（建 14 个 Store 的连接池、
  预热 reranker/embedding/LLM、注册工具、建 LangGraph 工作流）
- 没有任何办法把其中某个依赖换成假件
- 于是这 93 个端点的**真实测试证据只有一条**：`scripts/verify_aiops_endpoints.py`
  这类打真实 HTTP 的脚本，且只覆盖智能运维那一小片

**所以分层的目的不是让文件变短，是让端点能在不构造整个 app 的情况下被测。**
一个只把代码搬进多个文件、依赖仍靠闭包捕获的"分层"，**一行测试都换不来**。

## 2. 三行决策

- **决策**：改成 FastAPI 标准的 `APIRouter` + `Depends` 依赖注入。端点从
  `create_app()` 的内嵌函数变成**模块级函数**，依赖通过 `Depends(get_ops_store)`
  这类 provider 注入；测试时用 `app.dependency_overrides` 替换成假件。
- **理由**：这是 FastAPI 官方就是这么设计的用法，`dependency_overrides` 正是
  为测试提供的；不需要自己造依赖注入容器。**而"把端点搬进多个文件但依赖照旧
  闭包捕获"这个更省事的做法要明确拒绝**——它让文件变短、可测试性一点没变，
  是这次最容易走上的歧路。
- **作废**：`create_app()` 作为唯一装配点的现状；影响面：93 个端点的定义位置、
  `main.py`/部署入口的 import 路径、所有打真实 HTTP 的验证脚本（路径不变所以
  这些脚本应当零改动通过——这正是验收判据）。

## 3. 前置条件（**没有它就不要开始**）

**必须先有 Postgres 测试隔离 fixture。** 理由很直接：这是一次纯重构，唯一的
验收方式是"行为没变"，而现在能证明行为没变的东西**只有几个手写的 HTTP 脚本**。
在没有安全网的情况下拆 93 个端点，等于拆承重墙之前不搭支撑。

fixture 正在做（2026-08-27 并行开工）。它落地之后，本方案第一批才可以开始。

## 4. 分批顺序（按风险从低到高）

**每一批一个提交，可单独 revert。**

| 批次 | 内容 | 端点数 | 为什么排这个位置 |
|---|---|---|---|
| 0 | **把 7 个跨域共用的辅助函数提取到公共层** + 建 provider 骨架，**不搬任何端点** | 0 | 见 §4.1——不先做这步，后面每一批都切不干净 |
| 1 | `admin/ops`（智能运维）+ `admin/roles` 下的 ops-permissions | 23 | 最近写的、有 `verify_aiops_endpoints.py` 27 项真实 HTTP 覆盖，**验收证据最强** |
| 2 | `admin/dashboard`、`notifications`、`collections` | ~10 | 只读为主，权限判据简单 |
| 3 | `admin/users`、`admin/roles`、`admin/organizations` | ~12 | 写操作 + 权限敏感，但有账号体系那批脚本 |
| 4 | `workflows` | 8 | 状态机，改错了不容易一眼看出来 |
| 5 | `auth`、`conversations`、`chat` | ~12 | **最后**：核心链路，含 SSE 流式和 WebSocket，
`contextvars` 那条 P0 的隔离契约就挂在这里 |

剩余未归类端点在批次 5 之后按同样方式收尾。

### 4.1 批次 0 为什么变成"提取共用辅助函数"（2026-08-27 调用图实测）

对 `create_app()` 做了一次 AST 调用图分析，**推翻了原来"批次 0 只是建骨架"的
安排**，也修正了几个数字：

| 原设计写的 | 实测 |
|---|---|
| 内嵌 143 个函数 | **138 个**（93 端点 + 45 辅助函数） |
| `admin/ops` 10 个端点 | **21 个**（原来按 URL 前两段分组，漏了 `connectors/{id}/…` 这类子路径） |

**关键发现：7 个辅助函数被多个域共用。**

| 辅助函数 | 被几个域调用 | 涉及的域 |
|---|---|---|
| `_audit_log` | **9** | 几乎所有写操作 |
| `_require_conversation_owner` | 5 | chat / conversations / history / memory / ws |
| `_require_aiops_enabled_org` | 2 | admin/ops · admin/roles |
| `_get_owned_connector` | 2 | admin/ops · admin/roles |
| `_role_ops_permission_response` | 2 | admin/ops · admin/roles |
| `_require_local_retrieval_org` | 2 | admin/collections · collections |
| `_workflow_template_response` | 2 | admin/workflow-templates · workflow-templates |

**按 URL 前缀切会出事**：批次 1 搬 `admin/ops` 时会把 `_get_owned_connector`
一起带走，而 `admin/roles` 下的 ops-permissions 端点还在用它——要么编译不过，
要么有人顺手复制一份，**两个副本从此各自演化**。这正是 §7 那条风险的具体形态。

所以批次 0 改成：**先把这 7 个提取到公共层**（`create_app()` 里改为 import），
零行为变化、零端点搬迁，但它是后面每一批能干净切开的前提。

**顺带一条归属调整**：`admin/roles` 下那两个 `ops-permissions` 端点，URL 在
roles 域、语义属于运维权限，且跟 `admin/ops` 共用 3 个辅助函数——**批次 1 应当
把它们一起搬**，而不是留到批次 3 再拆一次。

## 5. 每批的验收判据（三条都要满足）

1. **既有的真实 HTTP 验证脚本零改动通过**（路径、请求体、响应形状都没变）
2. **新增至少一条"不建整个 app 就能测这批端点"的测试**——否则这一批等于只搬了
   文件。这条是本设计存在的全部意义，不满足就是没做完
3. **全量 `tests/unit` 通过**

## 6. 明确不做的

- **不改任何端点的 URL、请求/响应模型、权限判据、审计行为。** 纯搬迁 + 依赖注入。
  任何一处"顺手改好一点"都会让"行为没变"这个验收判据失效
- 不引入第三方依赖注入框架（`Depends` 够用）
- 不拆 `lifespan`（预热逻辑）——它本来就该在装配层，跟端点分层是两件事
- 不动 `schemas.py`（Pydantic 模型本来就是模块级的，没有闭包问题）

## 7. 已知风险

- **闭包捕获的依赖有 143 个内嵌函数在用，其中不少是辅助函数而非端点**
  （`_require_can_approve`、`_org_response` 之类）。这些也要跟着搬，且它们之间
  有调用关系——搬迁顺序要按调用图来，不能只按 URL 前缀切
- **`app.state` / 模块级可变状态**（如 `active_ops_connector_ws`、
  `active_ops_pending_requests` 这两个 WebSocket 注册表）跨端点共享，搬迁时
  必须保持"同一个进程内只有一份"，否则连接器会连上一份注册表、查询去问另一份
- SSE 端点里显式绑过 `request_id` 的 contextvar（`chat_stream`），
  搬迁后要复验 P0-1 那条"并发请求跨用户串流"的隔离契约仍然成立
