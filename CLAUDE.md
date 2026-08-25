# 项目共识

> **每个会话开始必读。改架构前必须先更新本文件。**
> 本文件是当前架构的唯一事实来源——`docs/` 下的设计文档可能已过期，冲突时以本文件为准。
> 建立于 2026-08-24，起因见 `docs/collaboration_retrospective.md`。

---

## 这是什么 / 交付给谁（2026-08-25 确定）

企业级 RAG 问答 Agent，自建自托管。

| | 答案 |
|---|---|
| **交付给谁** | **10000 名员工以上的大企业**，企业自己有庞大的知识库 |
| **他们怎么用** | 日常工作流、查资料、提高运维水平 |
| **什么算做完** | 满足**基本的、普适的**功能需求和性能需求 |

### ⚠️ 这个答案对既有结论的影响（重要）

`docs/review_2026-08-24/` 那三份评审报告，**严重程度基线是按"小团队内部系统、
低并发、不是面向公众的高并发服务"判定的**。10000+ 员工规模使该前提失效，
**报告里的部分 P1 在真实目标场景下应重估为 P0**。已知需要重估的至少有：

- **检索链路每次查询重建全套组件**（`query_knowledge_hub.py:296-353`）：
  一个 6 库企业每次提问 = 6 次 Chroma client bootstrap + 6 次 BM25 索引从磁盘反序列化，
  全链路无缓存。单人测试看不出来，并发下是灾难。
- **`OLLAMA_NUM_PARALLEL` 默认为 1**（Ollama 官方 FAQ）：一次用户请求可能触发约 5 次模型调用，
  若串行排队，延迟直接叠加。当前部署方式在目标规模下不成立。
- **管理端 N+1**（`/admin/users` 约 300 次串行查询）：按 10000 员工推算不是 300 次而是上万次。
- **后端 48 处 `print()`、0 处 logger、无 request id**：万人规模下无结构化日志等于不可调试。
- **14 个 Store 各自建连接池**（池上限累计约 68 条）：并发下连接耗尽风险。
- **业务层零测试**：该规模下无回归网不可接受。

**结论：`review_codebase_findings.md` 的 P0/P1/P2 分级需要按新场景重新过一遍，
不要直接沿用它的优先级排序。**

### ❓ 尚未确定的关键歧义

「10000 员工以上的大企业」有两种读法，**当前代码是按第二种建的，但描述听起来像第一种**：

1. **单租户**：部署在一家万人企业内部，部门/角色是权限边界
2. **多租户 SaaS**：客户是多家万人企业，企业之间要隔离

现有架构（org 边界、tenant connector、委托知识库）是按 (2) 建的。
**这一条确定之前，不要对多租户相关代码做删减或简化。**

---

## ⚠️ 当前阶段：停止新增功能，先补设计（2026-08-24 起）

用户明确决定：**新需求先不做，先把设计做好。**

对 AI 的直接含义：

- **不要主动提议或开始任何新功能。** 用户提出新需求时，先确认是否属于本阶段范围，
  而不是直接开工。
- 本阶段允许做的事：补设计文档、补测试、修已知缺陷（P0/P1）、清理死代码、
  把散落的决策上浮到本文件。
- 12 天里已经三次战略级转向、四次权限模型迁移，每次都推翻数天工作。这个阶段
  存在的意义就是让架构先稳下来，不要再用新功能覆盖旧问题。

---

## 当前架构（唯一事实来源）

**技术栈**：FastAPI + LangGraph（后端）· React + Vite（前端）· PostgreSQL（会话/用户/角色/组织）·
ChromaDB（向量）· 本地 Ollama 提供模型

**工作流**：`RAGWorkflow` 的 StateGraph
`session → intent → (retrieve | tool_subgraph | workflow | clarify) → generate → memory_manage → archive`

**模型**（两个，分工固定）
- `qwen2.5:7b` —— 生成回答 / 工具调用决策 / 工作流字段抽取 / 记忆摘要
- `qwen2.5-1.5b-router` —— 仅用于 `_intent_node`：query 改写 + 子查询拆分 + 四分类，一次调用完成
  （LoRA 微调，`RAGENT_INTENT_MODEL` 可覆盖，模型不存在时自动回退到 7b）

**权限模型**（2026-08-23 定稿）
- 角色（roles，携带 org_id）**直接携带知识库权限**，`role_collections` 关联 collection
- **一个用户一个角色**
- **不存在 kb_group**——2026-08-22 曾拆分出去，08-23 合并回 roles（`scripts/migrate_kb_groups_to_roles.py`）。
  `role_store.py:41,192` 的注释里还提到这个词，仅为历史说明

**知识库存储**
- 全部走平台本地 Chroma
- **委托模式（http_api 连接器）已于 2026-08-23 停用**，但代码路径仍在：
  `tenant_connector_store.py:39`、`app.py:1502,1816`、`query_knowledge_hub.py:444,458`——属待清理死代码

**检索**：Chroma dense + BM25 sparse → RRF 融合 → bge-reranker-base 交叉编码重排
- `MIN_RELEVANCE_SCORE = 0.1` 打在 **cross-encoder 重排分**上（不是 RRF 融合分），依据见
  `query_knowledge_hub.py:56-65` 的实测记录。**reranker 降级时不适用这个阈值**

---

## 已作废的设计（不要复活，不要参考）

| 文档 | 状态 |
|---|---|
| `role.md` | 头部标"未实现"是错的，实际已实现且模型已演进两轮。仅作历史参考，勿按其描述改代码 |
| `knowledge-base-tenant-federation.md` | 头部标"已实现"是错的，架构已于 2026-08-23 拆除 |
| `readme.md` / `TECHNICAL_OVERVIEW.md` / `PROJECT_ARCHITECTURE_SUMMARY.md` | 停在多租户重构之前，落后 7~11 天 |
| `plan.md` | 聊天记录原样粘贴，非规格 |
| kb_group 三张表 | 已迁移回 roles |

---

## 已知未闭环

- 🟠 知识库文档投毒 → 间接提示注入，可跨话题传染，ACL 拦不住
  （见 `docs/security_prompt_injection_test_report.md` 案例2）
- 🟠 `app.py:1120,1141,1155` 的 `/api/v1/admin/test/knowledge-query*` 是绕过 ACL 的测试接口，**上线前必删**
- 🟠 `ragent_backend` + `tool_agent` 共 12,200 行**零测试覆盖**，`conftest.py` 无 DB/LLM fixture
- 🟠 后端 48 处 `print()`、0 处 logger，无结构化日志、无 request id
- 完整清单见 `docs/review_2026-08-24/review_codebase_findings.md`

## 已修复（保留记录，防止重新引入）

- ✅ **2026-08-24　P0：并发请求跨用户串流**
  token/trace 队列原本是 `RAGWorkflow` 的实例属性，而全进程共用一个实例，并发请求互相覆盖
  → 一个用户的回答会混进另一个用户的 SSE 流，先结束的还会把对方剩余 token 丢掉。
  改为 `contextvars` 按请求隔离（`workflow.py` 顶部 `_CURRENT_TOKEN_QUEUE`），
  实例属性改成只读 property。
  回归保护：`tests/unit/test_workflow_stream_isolation.py`（真并发，9 条）。
  该测试的判别力已用旧实现验证过：`scripts/verify_stream_isolation_regression.py`
  —— 旧设计下请求 A 收到空、请求 B 收到 `[B1,A1,B2,A2,B3,A3]`。
  **不要把 per-request 状态写回 `self`**，已有测试专门拦这件事。

- ✅ **2026-08-24　P0：JWT 密钥硬编码**
  `auth.py` 原本 `os.getenv("RAGENT_JWT_SECRET", "dev-only-insecure-secret-change-me")`，
  而全仓从未设置该变量 → 实际部署必然用公开默认值，任何人可伪造任意身份。
  改为 `resolve_jwt_secret()`：非 `RAGENT_DEBUG=true` 时，密钥缺失或等于默认值一律拒绝启动；
  `create_app()` 第一行调用 `get_jwt_secret()` 做启动期 fail-fast；变量已补进 `.env.example`。
  回归保护：`tests/unit/test_auth_jwt_secret.py`（11 条）。

---

## 硬性规则

### 需求与设计

- 当用户用「修改一个BUG」的说法描述一件实际是**设计变更**的事时，必须明确指出：
  「这不是 bug，这是 <文档/位置> 的既有设计，改它意味着 <影响面>，确认吗？」
  —— 参考 2026-08-22 14:55 那次没有指出的教训，代价是 36 小时架构往返 + 两个互为逆操作的迁移脚本
- **改动数据模型 / 权限模型前**，必须先更新本文件的「当前架构」节并等用户确认
- 命中以下任一条就**停下来做设计评审**，不要继续写代码：
  1. 要写 migration 脚本（意味着数据模型在变）
  2. 同一需求第 3 次被报"还是不行"（去查为什么前两次修的会失效，八成是下层模型变了）
  3. 方案里出现"会动到 X 个文件 / 涉及表结构"

### 测试与审计

- 用户说"测试"时，**先确认是要黑盒跑还是白盒审计**；不确定就问。两者找的 bug 类型完全不同：
  黑盒找逻辑/行为/性能问题；白盒找共享状态、密钥管理、并发、资源泄漏这类**不产生异常输出**的缺陷
- **任何测试/审计报告必须包含「本次未覆盖的范围」**，缺这一节视为未完成。
  用户无法知道你没测什么，只能靠你说
- 安全测试**默认必须覆盖**：认证伪造 / 越权 / 并发 / 密钥与配置管理，
  即使用户只点名了其中一类（如"提示词注入"）
- **并发缺陷必须用并发方式验证**，串行跑 N 条用例不构成并发测试
- **代码注释里声称的不变量，要么验证、要么标为未证实假设，不得当作依据**
  —— 参考 `workflow.py:224-227` 那条错误注释导致 P0 长期未被发现
- 凡涉及权限的改动，**必须同时提交 `tests/` 下的测试**

### 交付与汇报

- 每个功能交付时必须回答三句话：
  1. **验收怎么做**：谁用什么账号，点哪里，看到什么，算通过
  2. **回归怎么保**：哪个测试文件/脚本能自动复现这个验收，路径是什么
  3. **什么没做**：这次没覆盖的边界条件，明确列出
- 结论**分三档，不许混用**：
  **已验证通过**（有可复现的测试）/ **已跑通**（手工执行过一次）/ **已实现但未验证**

### 工程

- 所有测试脚本、探测脚本落到仓库（`tests/` 或 `scripts/`），**禁止写临时目录后丢弃**
  —— 参考 `jailbreak_test.py` / `latency_probe.py` 丢失，导致两份报告的数字至今无法复现
- 标了「临时 / 上线前删除」的代码，**下次提交时必须要么删掉、要么加运行时开关关闭**
- **每完成一个可描述的功能就提交，单次提交不超过 15 个文件**
- 会话结束前固定动作：更新本文件的「当前架构」与「已知未闭环」，然后按功能拆分提交

### 会话

- 每次会话开始**先读本文件**，不要靠 grep 重建项目认知
- 同一时刻**只允许一个会话有写权限**（问原理/学习类会话可以并行）
- 用户说单字"继续"且额度将尽时，**列出剩余步骤，什么都别改**

---

## 相关文档

| 文档 | 内容 |
|---|---|
| `docs/orchestration_design.md` | **编排层设计（草案，未实施）**：并行编排与思维错乱防护 + 记忆/归档异步化。<br>合并自 `parallel_reasoning_design.md` 与 `memory_manage_async_decouple_design.md`，<br>那两份已标记为被取代，**不要按它们改代码** |
| `docs/collaboration_retrospective.md` | 协作复盘：问题清单、改进指南、自查清单 |
| `docs/review_2026-08-24/review_codebase_findings.md` | 代码审计：2 P0 / 16 P1 / 9 P2，带行号证据 |
| `docs/review_2026-08-24/review_process_retro.md` | 过程复盘：Git + 会话记录量化分析 |
| `docs/review_2026-08-24/review_industry_baseline.md` | 业界对标：必备 / 规模上来才需要 / 不必跟 |
| `docs/security_prompt_injection_test_report.md` | 提示注入测试结果（脚本已丢失，不可复现） |
| `docs/prompt_injection_remediation_plan.md` | 对应修复方案 |
| `docs/optimization_tracking.md` | 优化前后对比（"优化后"一栏仍空着） |
