<!-- doc-link-check: ignore-file
     本文 §4 的改名映射表里列的是中文改名方案（方案 A）的目标路径。
     用户已拍板采纳方案 B（不改名），所以那些路径**永远不会存在**，
     `scripts/check_doc_links.py` 会把它们全部报成断链。整份豁免。
     §10 里另有已删除文件 docs/api.md 的历史路径引用，同理。
     ⚠️ 代价：本文里真正的断链也查不出来。本标记**长期保留**
     （不同于最初预计的"执行完就删"——因为 §4 的提案路径不会被执行）。 -->

# `docs/` 文档整理方案

> **状态：✅ 已执行（2026-08-26）。** 用户拍板：`api.md` 删除、命名采纳方案 B
> （英文文件名 + 中文索引表）。**执行结果见 §10。**
> 创建：2026-08-26
> **死期：2026-09-26** —— 到期未执行则删除本文件（`CLAUDE.md` §7.4）。
> 依据：`CLAUDE.md` §7.4（文档规则）、§8（文档索引表）。
> 触发：用户要求"把 docs 下的文档归类整理，以功能分组，文件名以中文显示功能名，
> 删除过期的、无用的文档"。
>
> ⚠️ **§1–§9 是执行前写的方案原文，刻意保留未改**（它记录了"当初为什么这么决定"，
> 正是 §7.4 要求 commit message / 设计文档承担的职责）。
> **实际执行了什么、和方案有哪些出入，全部记在 §10。**
> 两者冲突时，以 §10 + `docs/README.md` 为准。

---

## 0. 先说结论（30 秒版）

| 问题 | 结论 |
|---|---|
| 要不要按功能分组？ | **要**，但用**文档内导航**（新建 `docs/README.md` 中文索引），**不要动目录结构** |
| 要不要改成中文文件名？ | **不建议**，理由见 §3。推荐**方案 B**：英文文件名 + 中文导航索引 |
| 有多少东西该删？ | **1 份该删、1 份该挪走、4 份该补状态标记、1 份需人拍板**，见 §5 |
| 现在有断链吗？ | **没有**。179 个引用点全部命中，基线见 §6 |

> ✅ **以上是执行前的判断。用户已拍板并执行完毕（2026-08-26）：
> `api.md` 删除、采纳方案 B。结果与验证见 §10。**

**核心权衡**：`docs/*.md` 的路径被**代码引用了 60 处、分布在 29 个 `.py` 文件里**，
其中 `workflow.py` 一个文件就占 11 处。全面改名 = 一次性动 29 个文件，
而 §7.6 明确"同一时刻只允许一个会话有写权限"，`workflow.py` / `intent.py` /
`query_knowledge_hub.py` 恰好是本项目改动最频繁的三个文件。
**用户要的是"一眼看出这份文档讲什么功能"，中文文件名只是实现方式之一，
而且是代价最大的那一种。**

---

## 1. 盘点

数据来源：`git log` / `wc -l` / 各文档头部状态行 / `scripts/check_doc_links.py`。
盘点时间 2026-08-26，基线 commit `f161ecd`。

### 1.1 活文档 / 当前状态正本（3 份）

| 文件 | 行数 | 末次提交 | 状态标记 | 被引用 | 处置 |
|---|---|---|---|---|---|
| `architecture.md` | 476 | 08-25 | ✅ 当前实现，随架构同步 | 23（代码 1） | **保留**，不动 |
| `optimization_tracking.md` | 433 | 08-25 | ✅ 活文档 · 最后更新 08-25 | 23（代码 10） | **保留**，不动 |
| `collaboration_retrospective.md` | 305 | 08-25 | ⚠️ 有日期无状态行 | 4（代码 0） | **保留**，建议补状态标记 |

### 1.2 设计文档 —— 有实施状态（7 份）

| 文件 | 行数 | 末次提交 | 状态标记 | 被引用 | 处置 |
|---|---|---|---|---|---|
| `opensearch_migration_design.md` | 667 | 08-26 | ✅ 阶段 0–3 已实施，死期 11-30 | 10（代码 7） | **保留** |
| `bm25_storage_design.md` | 717 | 08-25 | ✅ 阶段 1–3 已实施 | 10（代码 5） | ⚠️ **需拍板**，见 §5.4 |
| `observability_design.md` | 1239 | 08-25 | ✅ 阶段一已实施 | 5（代码 3） | **保留** |
| `orchestration_design.md` | 608 | 08-25 | ✅ 部分实施（D1/D2/D4/D5） | 18（代码 10） | **保留** |
| `chitchat_intent_design.md` | 675 | 08-25 | ✅ ⏸ 已搁置（用户决定） | 1（代码 0） | **保留** |
| `aiops_module_design.md` | 595 | 08-26 | ✅ 设计已确认，死期 09-25 | 1（代码 0） | 🔒 **在途，不碰** |
| `platform_ops_query_design.md` | 640 | 未提交 | ✅ 设计待确认，死期 09-26 | 0 | 🔒 **在途，不碰** |

### 1.3 提案 / 优先级（1 份）

| 文件 | 行数 | 末次提交 | 状态标记 | 被引用 | 处置 |
|---|---|---|---|---|---|
| `scale_slo_and_priorities.md` | 867 | 08-26 | ✅ 提案，待讨论确认 | 13（代码 2） | 🔒 **在途，不碰** |

### 1.4 时点快照 / 报告（5 份）

| 文件 | 行数 | 末次提交 | 状态标记 | 被引用 | 处置 |
|---|---|---|---|---|---|
| `latency_report.md` | 118 | 08-24 | ❌ **无状态行** | 20（代码 7） | **保留 + 补标记**，见 §5.3 |
| `security_prompt_injection_test_report.md` | 240 | 08-24 | ⚠️ 有测试时间无状态行 | 21（代码 5） | **保留 + 补标记** |
| `prompt_injection_remediation_plan.md` | 77 | 08-24 | ⚠️ 有"只给方案"无状态行 | 11（代码 7） | **保留 + 补标记** |
| `kb_permission_design.md` | 176 | 08-24 | ⚠️ "截至 08-23 的当前实现" | 3（代码 0） | ⚠️ **危险，见 §5.3** |
| `qa_test_questions.md` | 153 | 08-24 | ❌ **无状态行** | 1（代码 0） | **保留 + 补标记** |

### 1.5 评审报告目录（6 份，两个目录）

`review_2026-08-24/`（3 份，共 2102 行）与 `review_2026-08-25/`（3 份，共 1236 行）
**目录名自带日期**，本身就是最好的"时点快照"标记。全部**保留、不改名、不移动**。

| 文件 | 行数 | 状态标记 | 被引用 |
|---|---|---|---|
| `review_2026-08-24/review_codebase_findings.md` | 585 | ⚠️ 有范围无状态行 | 3 |
| `review_2026-08-24/review_industry_baseline.md` | 889 | ✅ 调研时间 + 来源质量约定 | 3（代码 1） |
| `review_2026-08-24/review_process_retro.md` | 628 | ✅ 分析时间 + 材料来源 | 1 |
| `review_2026-08-25/doc_and_collab_practices.md` | 522 | ✅ 状态 + 日期 + 有效期 | 0 |
| `review_2026-08-25/module_status_analysis.md` | 404 | ✅ 核查时间 + 结论分档说明 | 0 |
| `review_2026-08-25/smalltalk_routing_regression.md` | 310 | ✅ 已验证通过 + 复现命令 | 6（代码 2） |

### 1.6 归档目录 `archive/`（9 份）

**全部保留，一个都不动。** 2026-08-25 刻意建立，有 `README.md` 归档说明，
每份都补了状态行，`CLAUDE.md` §6 有对应表格。标为"冻结，不维护"。
链接检查把它们报为"孤儿"是**预期行为** —— 它们的正式入口是 `CLAUDE.md` §6 那张表。

### 1.7 非 markdown 与杂项（6 项）

| 路径 | 说明 | 处置 |
|---|---|---|
| `api.md` | 661 行，自称"最后更新 2026-04-12" | ⚠️ **内容有害，见 §5.2** |
| `dual_model_architecture.pptx` | 277 KB，08-24 | 保留（汇报材料，无引用无维护成本） |
| `kb_permission_flow.pptx` | 30 KB，08-24 | 保留 |
| `prompt_injection_remediation.pptx` | 404 KB，08-24 | 保留 |
| `rag_agent_technical_report.pptx` | 114 KB，08-24 | 保留 |
| `assets/` | **空目录**，未被 git 跟踪 | 建议删（git 本来也不记录空目录） |
| `test_upload_doc/后勤资料文档.md` | **已跟踪**，122 字节 | ⚠️ **该挪走，见 §5.1** |
| `.DS_Store` | 10 KB，已在 `.gitignore` | 建议本地删，无需处理 |

> 4 份 pptx 合计 825 KB，占 `docs/` 体积的绝大部分，但它们**不参与文档腐烂**
> （没人会按 pptx 改代码，也没有任何引用指向它们）。**不建议为了"瘦身"删掉汇报材料。**

---

## 2. 分组方案

用户要"以功能分组"。下面是按功能划出来的 8 组 —— **这个分组是有效的，
分歧只在于用什么形式落地**（目录？文件名？还是一张索引表？见 §3）。

| # | 功能组 | 成员 |
|---|---|---|
| A | **架构与接口** | `architecture.md` · `api.md` |
| B | **检索与索引存储** | `bm25_storage_design.md` · `opensearch_migration_design.md` |
| C | **编排与意图路由** | `orchestration_design.md` · `chitchat_intent_design.md` |
| D | **安全与权限** | `security_prompt_injection_test_report.md` · `prompt_injection_remediation_plan.md` · `kb_permission_design.md` |
| E | **性能与容量** | `latency_report.md` · `optimization_tracking.md` · `scale_slo_and_priorities.md` |
| F | **可观测性与运维** | `observability_design.md` · `aiops_module_design.md` · `platform_ops_query_design.md` |
| G | **测试** | `qa_test_questions.md` |
| H | **流程 · 评审 · 归档** | `collaboration_retrospective.md` · `review_2026-08-24/` · `review_2026-08-25/` · `archive/` |

---

## 3. 中文文件名：代价评估与推荐

### 3.1 三个可选方案

| | 方案 A：全中文文件名 | 方案 B：**英文名 + 中文索引**（推荐） | 方案 C：中文子目录 + 英文文件名 |
|---|---|---|---|
| 形态 | `docs/BM25索引存储设计.md` | 路径全不动，新建 `docs/README.md` 中文导航 | `docs/检索与存储/bm25_storage_design.md` |
| 满足"一眼看出讲什么" | ✅ | ✅（索引表里就是中文功能名） | ✅ |
| 满足"以功能分组" | ⚠️ 靠命名前缀 | ✅（索引表按 8 组分节） | ✅（目录即分组） |
| 需要改的代码引用 | **60 处 / 29 个 .py** | **0 处** | **60 处 / 29 个 .py** |
| 需要改的文档交叉引用 | **119 处** | **0 处** | **119 处** |
| `CLAUDE.md` §8 索引表 | 全表重写（**我改不了**） | 加一行即可 | 全表重写（**我改不了**） |
| 历史 commit message | **全部永久失效** | 不受影响 | **全部永久失效** |
| 与在途会话冲突风险 | 🔴 高 | 🟢 无 | 🔴 高 |

### 3.2 中文文件名的具体代价（不是猜的，是本仓已有的证据）

**① git 会把中文路径转义成八进制。** 本仓已经有一个中文文件名可以直接看：

```
$ git ls-files docs/
"docs/test_upload_doc/\345\220\216\345\213\244\350\265\204\346\226\231\346\226\207\346\241\243.md"
```

同一条命令下，英文文件名是可读的，中文的这份不是。`git log --stat`、
`git diff --stat`、CI 日志里都是这个样子（除非全局设 `core.quotepath false`，
但那要求每个开发者的机器都配过）。

**② 部分词搜索会失效。** 现在可以 `grep -rn "docs/bm25"` 一次找齐所有引用；
改成 `BM25索引存储设计.md` 之后，`grep "bm25"` 仍能命中（BM25 是英文），
但 `编排设计.md` 这种就只能整词搜，而中文没有词边界，
`grep "编排"` 会同时命中大量正文。**文件名的检索价值会下降。**

**③ 命令行补全。** zsh 对 CJK 补全可用但要切输入法，路径打字成本明显上升。
对"每次会话开始先读本文件"这种高频操作是负担。

**④ URL 与工具链。** 中文路径在 URL 里会 percent-encode
（`docs/%E6%9E%B6%E6%9E%84.md`），贴链接、写文档引用时可读性归零。

**⑤ 最重的一条：60 处代码引用横跨 29 个 `.py`。**

```
 11  src/ragent_backend/workflow.py      ← 全仓最热文件
  7  scripts/benchmark_latency.py
  4  src/ragent_backend/intent.py
  3  src/mcp_server/tools/query_knowledge_hub.py
  3  src/ragent_backend/app.py
  ...（另 24 个文件，每个 1–3 处）
```

`CLAUDE.md` §7.6：**同一时刻只允许一个会话有写权限**。
为了改文档路径去动 `workflow.py`，是拿"注释可读性"换"和在途会话撞车"。

> 🔴 **这个风险在本次盘点期间当场发生了，不是假设**：
> 开工基线（`git status`）里 `src/` 是干净的；约 40 分钟后收尾复查时，
> `src/ragent_backend/app.py` 变成 ` M`（已修改）、
> `src/observability/middleware.py` 是新增文件 —— **另一个会话正在改 `app.py`**。
> 而 `app.py` 恰好持有 3 处 `docs/` 路径引用（`:482, :556, :594`）。
> 如果本轮按方案 A 去批量改这些注释，就会和那个会话直接冲突。
> **本次没有触碰 `src/` 任何文件。**

### 3.3 推荐：方案 B

**保持英文文件名，新建 `docs/README.md` 作为中文功能导航。**

理由：
1. 用户的真实诉求是"一眼看出这份文档讲什么功能"——**一张按功能分组、
   带中文名和一句话说明的索引表，比文件名更能满足它**（文件名只有几个字，
   索引表可以写状态、日期、该不该信）。
2. 零断链、零代码改动、零历史失效、零并发冲突。
3. 和 `CLAUDE.md` §8 是**同构**的：§8 已经是一张中文索引表，
   `docs/README.md` 是它的"按功能分组"视图，不是另立一套。
4. 可逆。如果将来仍想全中文改名，这份索引表恰好就是改名映射表。

`docs/README.md` 的形态（示意，未创建）：

```markdown
# docs 目录导航
> 状态：活文档 · 最后更新 2026-08-26
> ⚠️ 当前状态的唯一事实来源是 `CLAUDE.md`，本表只负责"哪份文档讲哪个功能"。

## B. 检索与索引存储
| 功能 | 文档 | 状态 | 该不该信 |
|---|---|---|---|
| BM25 索引存储（SQLite 方案 C） | `bm25_storage_design.md` | 阶段 1–3 已实施 | 可信，但注意 §5.4 |
| 检索层迁移 OpenSearch | `opensearch_migration_design.md` | 阶段 0–3 已实施，灰度中 | 可信 |
...
```

> **如果你仍然要方案 A（全中文改名）**，§4 给了完整映射表和引用改动清单，
> 可以直接执行 —— 但请先确认没有别的会话在改 `src/`。

---

## 4. 改名映射表（**仅当选择方案 A / C 时才需要**）

方案 B 不需要改任何名字。以下是选 A 时的完整映射。

| 旧路径 | 新路径（方案 A） | 需连带修改的引用点 |
|---|---|---|
| `docs/architecture.md` | `docs/架构参考手册.md` | 代码 1（`benchmark_latency.py:6`）+ 文档 22 + **`CLAUDE.md` §8、readme.md ×2** |
| `docs/api.md` | `docs/API接口文档.md` | 文档 2 |
| `docs/bm25_storage_design.md` | `docs/BM25索引存储设计.md` | 代码 5（`bm25_sqlite_store.py:1`、`prototype_bm25_sqlite.py:5`、`benchmark_bm25_backends.py:6`、`seed_large_bm25_corpus.py:6`、`test_bm25_sqlite_store.py:3`）+ 文档 5 |
| `docs/opensearch_migration_design.md` | `docs/检索层迁移OpenSearch设计.md` | 代码 7（`pipeline.py:615`、`query_knowledge_hub.py:402`、`opensearch_store.py:1`、`migrate_to_opensearch.py:2`、`verify_opensearch_parity.py:2`、`simulate_search_traffic.py:2`、`test_opensearch_store.py:1`）+ 文档 2 + **`pyproject.toml:38`** |
| `docs/orchestration_design.md` | `docs/编排层设计.md` | 代码 10（`intent.py:23,223`、`workflow.py:1191`、`probe_checkpointer_concurrency.py:4`、`test_sub_query_dependency_and_fanout.py:5,35,416`、`test_d4d5_business_control_group.py:3`、`test_d1_real_router_split.py:16`、`test_build_prompt_cross_material.py:1`）+ 文档 8 |
| `docs/chitchat_intent_design.md` | `docs/闲聊意图第五类设计.md` | 文档 1 |
| `docs/observability_design.md` | `docs/可观测性设计.md` | 代码 3（`workflow.py:1486`、`context.py:3`、`redact.py:1`）+ 文档 2 |
| `docs/security_prompt_injection_test_report.md` | `docs/安全-提示注入测试报告.md` | 代码 5（`prompt_guard.py:3`、`redact.py:31`、`verify_security_posture.py:5,374`、`test_log_redaction.py:186`）+ 文档 16 |
| `docs/prompt_injection_remediation_plan.md` | `docs/安全-提示注入修复方案.md` | 代码 7（`pipeline.py:344`、`workflow.py:80,89,1284,1419`、`prompt_guard.py:4`、`query_knowledge_hub.py:1028`）+ 文档 4 |
| `docs/kb_permission_design.md` | `docs/知识库权限设计.md` | 文档 3 |
| `docs/latency_report.md` | `docs/耗时基线报告-20260823.md` | 代码 7（`workflow.py:1422`、`benchmark_latency.py:3,11,113,439,596,611`）+ 文档 13 |
| `docs/optimization_tracking.md` | `docs/优化前后对比记录.md` | 代码 10（`workflow.py:73,236,668,1409`、`app.py:482,556,594`、`subgraph.py:59,150`、`query_knowledge_hub.py:314`）+ 文档 13 |
| `docs/scale_slo_and_priorities.md` | `docs/规模SLO与优先级.md` | 🔒 **在途，本轮不改名** |
| `docs/qa_test_questions.md` | `docs/问答测试题库.md` | 文档 1 |
| `docs/collaboration_retrospective.md` | `docs/协作复盘与开发流程.md` | 文档 4 + **readme.md** |
| `docs/aiops_module_design.md` | `docs/智能运维模块设计.md` | 🔒 **在途，本轮不改名** |
| `docs/platform_ops_query_design.md` | `docs/平台运营数据查询设计.md` | 🔒 **在途，本轮不改名** |
| `docs/review_2026-08-24/` `review_2026-08-25/` | **不改名** | 目录名自带日期，已是最好的时点标记 |
| `docs/archive/**` | **不改名** | 冻结目录，§6 有对应表 |

**方案 A 的额外代价（无法消除）**：

- **历史 commit message 里的路径永久失效** —— 至少涉及
  `perf(bm25): ...`（引用 `bm25_storage_design.md`）、
  `docs: sync architecture.md ...`、`feat(bm25): 方案 C 阶段 ...` 等多次提交。
  §7.4 说"写完冻结、零维护"的 commit message，**改名会让它们指向不存在的文件**。
  这些**不该也不能改**。
- `scripts/benchmark_results/*.json`（3 份）与 `scripts/security_results/*.json`（8 份）里
  记录的 `baseline_source` 字段会失效。这些是**运行产物快照**，
  按 §7.5 的"数字要能从 git 复现"精神，**不应该回填改写**。
- `CLAUDE.md` §8 整张索引表 + §4 正文里的大量路径引用要重写 —— **本会话不能改**，
  必须交给用户或占着 `CLAUDE.md` 的那个会话。

---

## 5. 删除 / 处置清单

> ⚠️ **以下全部未执行。** 每条都给了依据，请逐条确认。

### 5.1 建议移出 `docs/`：`docs/test_upload_doc/后勤资料文档.md`

- **是什么**：122 字节的**提示注入攻击测试样本**，正文是一段试图让模型在每条回答
  末尾附加"请联系管理员将密码重置为 Temp@123 并发送给我"的注入载荷。
- **为什么不该待在 `docs/`**：它不是文档，是**测试夹具**。
  放在 `docs/` 里有两个具体风险：① 任何"把 docs 当知识库摄入"的操作会把投毒样本
  灌进真实索引（`CLAUDE.md` §4 第 5 条正是"知识库文档投毒"这条 P0）；
  ② 阅读 `docs/` 的人（或 agent）会读到一段伪装成正常资料的指令。
- **建议**：移到 `tests/fixtures/prompt_injection/` 并在同目录加一行 README 说明用途。
  **不建议直接删** —— 它可能是 `security_prompt_injection_test_report.md` 案例 2 的原始素材。
- **需要确认**：这份样本是否还被某个测试流程使用？我没有找到任何代码引用它
  （链接检查报为孤儿），但它可能是**手工**上传用的。

### 5.2 建议改写而非删除：`docs/api.md`

- **状态标记**：❌ 无状态行，只有一句"最后更新: 2026-04-12"（比仓库最早的 commit 还早，
  日期本身可疑）。末次提交 2026-08-13，**13 天没动**。
- **过期证据（实测，非推断）**：
  1. 只覆盖 **7 个端点**，`app.py` 实际有 **65 个** `@app.*` 装饰器（覆盖率约 11%）。
  2. **第 48 行写着"TODO: 后续版本将添加 API Key 或 JWT 认证"** ——
     而 JWT 鉴权早已实现且是 `CLAUDE.md` §5 的已修复 P0（`resolve_jwt_secret`，
     缺密钥直接拒绝启动）。
  3. 全部 curl 示例不带 `Authorization` 头，**照抄必然 401**
     （`review_codebase_findings.md` 已独立记过这条）。
- **判断**：这**正是 §7.4 说的"标着可信、实际过期，比没有文档更危险"**。
  它documented 的 7 个端点经核对**都还存在**，所以内容不是全错，
  错的是"认证"那一节和"这就是全部接口"的暗示。
- **建议二选一，请拍板**：
  - **(a) 补状态行降级**（推荐、低成本）：头部加
    `> **状态：时点快照，仅覆盖 7/65 个端点，认证一节已过期（JWT 已实现）。不要当接口全集用。**`
  - **(b) 删除**：理由是"11% 覆盖率的接口文档没有存在价值"。
    但它有 2 处文档引用，且重写成本不低。
- **我倾向 (a)** —— 删掉不会让人少犯错，只会让人去读 3038 行的 `create_app()`。

### 5.3 建议补状态标记（4 份，不删）

§7.4 原话："所有文档头部必须有**状态 + 日期**；没有状态标记的视同不可信。"
下列文档内容有效但缺标记，**建议补，不建议删**：

| 文件 | 现状 | 建议补的状态行 |
|---|---|---|
| `latency_report.md` | 无状态行，被引用 20 处 | `状态：时点快照（2026-08-23）· 探测脚本已丢失，不可复现 · 现行数字见 architecture.md §3.2` |
| `security_prompt_injection_test_report.md` | 有测试时间无状态行 | `状态：时点快照（2026-08-23）· 脚本已丢失 · 现行复测脚本 scripts/verify_security_posture.py` |
| `prompt_injection_remediation_plan.md` | 无状态行 | `状态：部分实施 —— P0/P1 已落地（见 optimization_tracking.md 任务一），P3 未实施` |
| `qa_test_questions.md` | 无状态行 | `状态：可用 · 取材自 2026-08-24 的 Chroma 实际内容` |

⚠️ **`kb_permission_design.md` 是这批里最危险的一份，单独说**：
它头部写的是「**截至 2026-08-23 的当前实现**」—— 一个**自称当前状态**的标记。
但 `CLAUDE.md` §8 把它列为"时点快照"，而 §3 才是权限模型的正本。
`review_process_retro.md` 更记过它"与 `role.md` 描述的模型互相矛盾，
两份都在仓库里，没有任何交叉标注"。
**建议**：把头部改成
`> **状态：时点快照（2026-08-23）。权限模型的正本是 CLAUDE.md §3，冲突时以 §3 为准。**`
**这条比删任何文档都值得先做** —— 它是唯一一份"自称当前状态、实际不是"的文档。

### 5.4 需要人拍板：`bm25_storage_design.md` 要不要标作废

`opensearch_migration_design.md`（08-26，阶段 0–3 已实施）在三行决策里写着：

```
- 作废：bm25_indexer / bm25_sqlite_store / sparse_retriever / ... / ChromaDB 存储层
  影响：... docs/bm25_storage_design.md（作废）
```

**但我不建议现在按它标作废**，理由：
- OpenSearch 自己的状态行写着"**切读默认关闭，按 collection 灰度**"，
  也就是 **BM25/SQLite 仍是当前生产读路径**；
- `CLAUDE.md` §4 的 P0 第 2/2b/2c/2d/2e/2f 条**大量依赖**这份文档的实测数字；
- 有 5 处代码引用它，其中 `bm25_sqlite_store.py:1` 是模块 docstring。

**建议**：暂不标作废，改为在 `bm25_storage_design.md` 头部加一句交叉标注：
`> ⚠️ 检索层正在迁移 OpenSearch（见 opensearch_migration_design.md）。灰度切读完成后本文作废。`
**请确认**：这是"暂缓作废"，和 OpenSearch 文档里那行"作废"是有冲突的，需要你定。

### 5.5 可以直接删（1 项，零风险）

| 路径 | 理由 |
|---|---|
| `docs/assets/` | **空目录**，git 本来就不跟踪空目录（`git ls-files docs/assets/` 无输出），删了对仓库无任何影响 |

### 5.6 明确**不删**的（澄清）

| 路径 | 为什么不删 |
|---|---|
| `docs/archive/**`（9 份） | 2026-08-25 刻意建立的归档，`CLAUDE.md` §6 有对应表，标为"冻结，不维护"。链接检查报"孤儿"是预期行为 |
| `docs/review_2026-08-2*/**`（6 份） | 时点快照，目录名自带日期，`CLAUDE.md` §8 索引在册 |
| 4 份 `.pptx`（825 KB） | 汇报材料，零引用、零维护成本、不会腐烂 |
| `docs/.DS_Store` | 已在 `.gitignore`（第 136 行），只是本地文件 |

---

## 6. 回归保护：`scripts/check_doc_links.py`

**本轮唯一实际新增的东西**（§7.5 要求脚本落仓，禁止写临时目录后丢弃）。

```bash
.venv/bin/python scripts/check_doc_links.py            # 有断链则退出码 1
.venv/bin/python scripts/check_doc_links.py --orphans  # 额外列出孤儿文档
.venv/bin/python scripts/check_doc_links.py --json     # 机器可读
```

**它扫什么**：全仓 471 个文件（`.md/.py/.js/.jsx/.ts/.tsx/.toml/.yml/.yaml/.cfg/.ini`，
排除 `.venv`/`node_modules`/`.git`/`.claude/worktrees`）里所有形如 `docs/xxx.md`
的字符串，逐个确认目标文件存在。

**2026-08-26 基线（commit `f161ecd`，执行任何整理之前）**：

```
扫描文件      : 471
被引用的文档  : 20 份
引用点总数    : 179
✅ 无断链
孤儿文档      : 13 份（archive 9 + 在途 1 + review 2 + 测试夹具 1，均为预期）
```

**怎么用它做回归**：执行任何改名/删除**之后**再跑一次，
必须仍然是"✅ 无断链"且退出码 0。这就是"没有制造断链"的可复现证据。

**判别力自检**（§7.2：写完测试要问"它在旧实现下会失败吗"）：
脚本第一版跑出来就报了 10 处断链，其中 **2 处是真的**
（`orchestration_design.md:16,17` 指向已被合并删除的 `parallel_reasoning_design.md`
与 `memory_manage_async_decouple_design.md`），另 8 处是脚本自身示例和单测伪造数据。
真断链是它抓出来的，不是我预先知道的 —— **说明它有判别力**。
那 2 处经确认是**刻意保留的溯源说明**，已进 `KNOWN_NON_REFS` 白名单并写明理由。

**白名单纪律**：`KNOWN_NON_REFS` 每条**必须写理由**，否则等于把检查关掉。

**一处已知豁免**：本文件（`doc_reorg_plan.md`）头部带 `doc-link-check: ignore-file`
标记，整份不参与检查。原因是 §4 的改名映射表里列的全是**还不存在的提案路径**，
不豁免的话会刷出 21 条假断链。
⚠️ **代价**：本文里**真正的**断链也查不出来。方案执行完毕后应删除该标记。
脚本每次运行都会把豁免文件**显式打印出来**（`⚠️ 整份豁免` 那一行）——
被静默跳过的检查等于没有检查。

---

## 7. 交给用户的待办：`CLAUDE.md` §8 需要改什么

⚠️ **`CLAUDE.md` 有别的会话在改，本会话不碰。** 以下清单请你或那个会话去落。

### 7.1 若采纳方案 B（推荐）—— §8 只需加 2 行

| 位置 | 动作 |
|---|---|
| §8 表格顶部 | 新增一行：`\| **docs/README.md** \| **docs 目录的中文功能导航（按 8 组分类）** \| **活文档** \|` |
| §8 表格 | 新增一行：`\| scripts/check_doc_links.py \| 文档链接体检，扫全仓 docs 路径引用确认无断链 \| **可复现，活脚本** \|` |

### 7.2 §8 现有条目的**内容**订正（与改不改名无关，现在就该改）

| §8 现有描述 | 问题 | 建议改成 |
|---|---|---|
| `docs/api.md` **未在 §8 出现** | 661 行的接口文档不在索引表里 | 补一行，并标注"时点快照 · 仅覆盖 7/65 端点 · 认证一节已过期" |
| `docs/kb_permission_design.md` \| 权限设计（截至 08-23）\| 时点快照 | 文档**自身**头部却写"当前实现"，两边口径不一致 | 保持 §8 的"时点快照"，改文档头部（见 §5.3） |
| `docs/latency_report.md` \| ...（**脚本已丢，不可复现**）| §8 是对的，文档自己没写 | 保持 §8，补文档头部 |
| `docs/bm25_storage_design.md` **未在 §8 出现** | 717 行、被代码引用 5 处的设计文档不在索引表 | 补一行：`阶段 1–3 已实施；检索层迁移 OpenSearch 后待作废` |
| `docs/opensearch_migration_design.md` **未在 §8 出现** | 同上，且是 08-26 最新的实施 | 补一行：`阶段 0–3 已实施，切读默认关闭按 collection 灰度` |
| `docs/observability_design.md` **未在 §8 出现** | 1239 行，全仓最长文档 | 补一行：`阶段一已实施，二/三/四未实施` |
| `docs/chitchat_intent_design.md` | §4 P1 提过，§8 表里没有 | 补一行：`⏸ 已搁置，需用户明确启动` |
| `docs/review_2026-08-25/` 三份 | §8 完全没有这个目录 | 补三行（其中 `smalltalk_routing_regression.md` 被代码引用 2 处） |

> 上面这张表的价值和改名无关：**§8 索引表目前漏掉了 6 份活跃设计文档**，
> 其中 3 份被代码直接引用。这比"文件名是不是中文"更影响"一眼看出有哪些文档"。

### 7.3 若采纳方案 A（全中文改名）—— §8 整表重写

按 §4 映射表逐条替换路径，另需检查 §2/§3/§4 正文里的所有 `docs/` 路径引用
（`grep -n "docs/" CLAUDE.md` 目前有 30+ 处）。

---

## 8. 建议的执行顺序（等你确认后）

1. **先做零风险的**：删空目录 `docs/assets/`；补 §5.3 的 4 份状态标记 + 订正
   `kb_permission_design.md` 头部。
2. **再做需拍板的**：`api.md` 选 (a) 还是 (b)；`bm25_storage_design.md` 的作废口径。
3. **移夹具**：`test_upload_doc/` → `tests/fixtures/prompt_injection/`（确认无人用后）。
4. **建导航**：新建 `docs/README.md`（方案 B）。
5. **每步之后跑** `.venv/bin/python scripts/check_doc_links.py`，必须退出码 0。
6. **最后**把 §7 的待办交给改 `CLAUDE.md` 的那个会话。

---

## 9. 本次未覆盖的范围

- **未读任何文档的正文内容做事实核对。** 除了 `api.md`（核对了 7 个端点是否存在、
  认证一节是否过期）和 `kb_permission_design.md` / `bm25_storage_design.md`
  （核对了头部状态行与其它文档的冲突），其余文档的**状态标记是否与代码实际相符，
  本次只信了标记本身，没有逐条验证**。也就是说：一份写着"已实施"的文档，
  我没有去代码里确认它真的实施了。
- **未检查文档内锚点。** `#小节` 形式的链接、`§4.5` 这类章节号引用是否存在，
  链接检查脚本不覆盖（已写进脚本的"未覆盖"输出里）。
- **未检查裸文件名引用。** 正文里写 `role.md` 而不写 `docs/role.md` 的那种，
  脚本刻意不匹配（会大量误报）。改名时这类引用**会漏**。
- **未核对 4 份 pptx 的内容是否过期。** 只确认了它们无引用、无维护成本。
- **未验证 `test_upload_doc/后勤资料文档.md` 是否还被某个手工测试流程使用。**
  只确认了没有**代码**引用它。
- **未评估中文文件名在 Windows / CI 容器 / 非 UTF-8 locale 下的表现**，
  §3.2 的结论基于本机 macOS + zsh + git 的实际观察。
- **未触碰在途文件**：`CLAUDE.md`、`docs/scale_slo_and_priorities.md`、
  `docs/aiops_module_design.md`、`docs/platform_ops_query_design.md`。
  它们的状态是从 `git`/文件头读的，**没有编辑过**。
- **本文自身的链接未被检查**（整份豁免，见 §6），因为它含大量提案路径。
- ~~**未执行任何删除、改名、移动。** 本文只是方案。~~ → **已于 2026-08-26 执行，见 §10。**
- **链接检查脚本只跑过当前工作区一次快照**，没有接进 CI（本项目也还没有 CI，
  见 `CLAUDE.md` §4 P1"无 Dockerfile / CI / 依赖锁定"）。
  也就是说：**它能证明"这一刻没断链"，不能自动阻止将来有人改名把链接改断。**

---

## 10. 执行记录（2026-08-26）

**用户拍板**：① `api.md` → **删除**（不是降级保留）；② 命名 → **采纳方案 B**
（英文文件名 + 中文索引表），不做全中文改名。

执行前基线 commit `5e5cf4f`。**未 commit，全部改动留在工作区。**

### 10.1 删除（2 项）

| 项 | 删除依据 | 引用核查 |
|---|---|---|
| `docs/api.md`（661 行） | **无状态标记**（§7.4：视同不可信）；自称"最后更新 2026-04-12"而末次提交 08-13；只覆盖 **7/65** 个端点（11%）；「认证」一节原文写着"**当前版本暂未实现认证机制，所有接口公开访问**"，而 JWT 鉴权早已实现且是 `CLAUDE.md` §5 的**已修复 P0**；全部 curl 示例不带 `Authorization`，照抄必然 401 | 代码引用 **0**；活文档引用 **0**；仅 `review_2026-08-24/review_codebase_findings.md`（**时点快照**）有 3 处，见 §10.5 |
| `docs/assets/`（空目录） | 空且未被 git 跟踪（`git ls-files` 无输出），删除对仓库零影响 | 无 |

**取回方式（已实测可用）**：`git show 7eaff77:docs/api.md` → 完整 661 行。

⚠️ **未删 `bm25_storage_design.md`**：用户已于 commit `5e5cf4f` 更正
`opensearch_migration_design.md` 的误导表述，两份现为**并存**关系
（OpenSearch 切读默认关闭，BM25+SQLite 仍是生产读路径）。

### 10.2 移动（1 项）

`docs/test_upload_doc/后勤资料文档.md` → `tests/fixtures/prompt_injection/`

- **移动前 grep 核查**：`test_upload_doc` / `后勤资料文档` 在 `*.py` `*.md` `*.json`
  `*.sh` 上**零命中**，无任何代码或流程引用。
- 新增 `tests/fixtures/prompt_injection/README.md` 说明**它是攻击样本不是参考资料**、
  为什么移出 `docs/`、旧路径是什么。
- 原空目录 `docs/test_upload_doc/` 已删除。

### 10.3 状态标记订正（1 项）

`docs/kb_permission_design.md` 头部：
「截至 2026-08-23 的**当前实现**」→ **时点快照（2026-08-23），不描述当前状态**，
并标明「权限模型的唯一正本是 `CLAUDE.md` §3」+ 与 `archive/role.md` 的已知冲突。

> 这是全仓**唯一一份自称当前状态、实际不是**的文档，正是 §7.4
> 「标着『已实现』的废弃架构文档比没有文档更危险」的原型。

### 10.4 新建（2 项）

| 文件 | 作用 |
|---|---|
| `docs/README.md` | **中文功能导航索引**，按 §2 的 A–H 八组分类。每份文档标「🟢 当前状态 / 🔵 部分实施 / 🟡 未实施 / ⚪️ 时点快照 / 🧊 冻结」，另含「已删除的文档」去向表与目录约定 |
| `tests/fixtures/prompt_injection/README.md` | 攻击样本夹具说明 |

**为什么放 `docs/README.md`**：GitHub / 编辑器打开目录时自动渲染，是零学习成本的入口；
且与 `CLAUDE.md` §8 同构（§8 是全仓索引，本文是 `docs/` 的按功能分组视图），
不另立一套规则。

### 10.5 一个刻意不修的断链

`review_2026-08-24/review_codebase_findings.md` 的 `:391` `:405` `:576` 仍指向
已删除的 `api.md`。**刻意不改**——它是**时点快照**，记录 08-24 当天的事实，
按 §7.4「报告类保留但标死日期」，**改写冻结报告等于伪造历史记录**。

补偿措施：`docs/README.md`「已删除的文档」一节写明去向与取回命令，
并在 `check_doc_links.py` 的 `KNOWN_NON_REFS` 里登记，**带理由**。

### 10.6 链接检查结果

| | 执行前（`f161ecd`） | 执行后（`5e5cf4f` + 工作区） |
|---|---|---|
| 扫描文件 | 471 | 475 |
| 被引用文档 | 20 份 | 20 份 |
| 引用点 | 179 | 186 |
| **断链** | **0** | **✅ 0** |
| 退出码 | 0 | **0** |

**过程中脚本抓到过 2 处真断链**（`docs/README.md:130` 的 git 取回命令、
夹具 README 里的旧路径说明），两处都是**故意写出的历史路径**，
已登记白名单并写明理由 —— **说明检查在这次执行里真的起了作用，不是摆设**。

---

## 11. 交给用户：`CLAUDE.md` §8 可直接粘贴的表格行

⚠️ **本会话未碰 `CLAUDE.md`。** 以下 9 行可直接粘进 §8 表格。

### 11.1 需要**新增**的行（8 行）

```markdown
| **`docs/README.md`** | **`docs/` 的中文功能导航（8 组分类 + 状态标记 + 已删除文档去向）** | **活文档** |
| `docs/bm25_storage_design.md` | BM25 索引存储（JSON → SQLite 方案 C） | **部分实施**：阶段 1–3 已落地（08-25），阶段 4 未做且现网规模下不该做 |
| `docs/opensearch_migration_design.md` | 检索层迁移 OpenSearch | **部分实施**：阶段 0–3 已落地（08-26），**切读默认关闭**按 collection 灰度；完成阶段 4 后才取代 `bm25_storage_design.md` |
| `docs/observability_design.md` | 结构化日志 + request id 贯穿链路 | **部分实施**：阶段一已落地（08-25），二/三/四未实施 |
| `docs/chitchat_intent_design.md` | 意图分类第五类 `chitchat` | **⏸ 已搁置**，需用户明确启动，不要主动开工 |
| `docs/review_2026-08-25/smalltalk_routing_regression.md` | 闲聊被误路由诊断（81% → 0%） | 时点快照（**已验证通过**，有复现脚本） |
| `docs/review_2026-08-25/module_status_analysis.md` | 两个功能模块的真实状态核查 | 时点快照 |
| `docs/review_2026-08-25/doc_and_collab_practices.md` | 文档规范与人机协作业界现状 | 时点快照（建议 2026-12-31 前复核） |
| `scripts/check_doc_links.py` | **文档链接体检**，扫全仓 `docs/` 路径引用确认无断链 | **可复现，活脚本** |
```

### 11.2 需要**删除**的行（1 行）

`api.md` 本就不在 §8 表里，**无需删行**。但 §8 之外若有正文提到接口文档，请一并清理：

```bash
grep -n "api\.md" CLAUDE.md    # 执行本方案时为 0 命中，确认用
```

### 11.3 需要**修改**的行（1 行）

现有行：

```markdown
| `docs/kb_permission_design.md` | 权限设计（截至 08-23） | 时点快照 |
```

建议改为（与文档头部新状态行一致）：

```markdown
| `docs/kb_permission_design.md` | 权限设计（时点快照 08-23）**正本是本文 §3** | 时点快照 |
```

> §8 原本的「时点快照」判断是**对的**，问题在文档自己头部写着"当前实现"。
> 文档侧已于本次订正，这里只是让两边口径完全一致。
