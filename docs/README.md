# `docs/` 目录导航

> **状态：活文档。** 建立于 2026-08-26（执行 `doc_reorg_plan.md` 的分组方案）。
> **最后更新：2026-08-26**
>
> 🔴 **当前状态的唯一事实来源是 `CLAUDE.md`**（§7.4）。
> 本表**不描述系统长什么样**，只回答两个问题：**哪份文档讲哪个功能** /
> **这份文档还能不能信**。两者冲突时以 `CLAUDE.md` 为准。
>
> 📖 架构图 · 核心链路 · 性能数据 → `architecture.md`（与 `CLAUDE.md` 同属当前状态正本）

## 怎么用这张表

「**该不该信**」一栏是本表的核心价值 —— `CLAUDE.md` §7.4 说
「**标着『已实现』的废弃架构文档，比没有文档更危险**」，所以每份文档都标了
它描述的是**当前状态**还是**某个时点的快照**：

| 标记 | 含义 |
|---|---|
| 🟢 **当前状态** | 随代码同步维护，可以直接照着理解系统 |
| 🔵 **设计（部分实施）** | 设计有效，但**只有标为已实施的部分**对应真实代码 |
| 🟡 **设计（未实施/搁置）** | 纯提案，**代码里没有**。不要照它理解现状 |
| ⚪️ **时点快照** | 那一天的报告，**不描述当前状态**。只能做趋势对照 |
| 🧊 **冻结** | 归档，不维护。只记录"当初为什么这么决定" |

---

## A. 架构与当前状态

| 功能 | 文档 | 该不该信 |
|---|---|---|
| **架构图 · 核心链路 · 双模型 · 性能实测** | `architecture.md` | 🟢 当前状态（08-25 建立，随架构同步） |
| 目录导航（本文） | `README.md` | 🟢 当前状态 |

> ⚠️ **接口文档已于 2026-08-26 删除**（原 `api.md`）—— 见文末「已删除的文档」。

## B. 检索与索引存储

| 功能 | 文档 | 该不该信 |
|---|---|---|
| **BM25 索引存储**（JSON → SQLite，方案 C） | `bm25_storage_design.md` | 🔵 阶段 1–3 已实施（08-25），阶段 4 未做且现在不该做 |
| **检索层迁移 OpenSearch** | `opensearch_migration_design.md` | 🔵 阶段 0–3 已实施（08-26），**切读默认关闭**，按 collection 灰度 · 死期 11-30 |

> ⚠️ **这两份并存，不是谁取代谁。** `RAGENT_OPENSEARCH_READ` 默认 `off`，
> **BM25 + SQLite 仍是生产读路径**。OpenSearch 要到完成阶段 4（删 BM25/Chroma）
> 才取代前者。在那之前**两份各自描述自己那一半的当前状态**。

## C. 编排与意图路由

| 功能 | 文档 | 该不该信 |
|---|---|---|
| **编排层**（并行防护 D1–D6 + 记忆异步化） | `orchestration_design.md` | 🔵 D1/D2/D4/D5 已实施；D3/D6 未实施；**B 部分整体未实施** |
| 意图分类第五类 `chitchat`（闲聊） | `chitchat_intent_design.md` | 🟡 **⏸ 已搁置**，需用户明确启动，**不要主动开工** |

## D. 安全与权限

| 功能 | 文档 | 该不该信 |
|---|---|---|
| 提示注入 / 越狱 / 幻觉 **测试结果** | `security_prompt_injection_test_report.md` | ⚪️ 时点快照（08-23）· **原脚本已丢，不可复现** |
| 提示注入 **修复方案** | `prompt_injection_remediation_plan.md` | 🔵 P0/P1 已落地，P3 未实施 |
| 知识库权限设计 | `kb_permission_design.md` | ⚪️ 时点快照（08-23）· 🔴 **正本是 `CLAUDE.md` §3** |

> 现行安全复测脚本：`scripts/verify_security_posture.py`（可复现，18 用例 6 组）。
> 攻击样本夹具：`tests/fixtures/prompt_injection/`。

## E. 性能与容量

| 功能 | 文档 | 该不该信 |
|---|---|---|
| **优化前后对比**（按轮次追加） | `optimization_tracking.md` | 🟢 活文档 |
| 万人规模 SLO 与优先级重估（12 条 P0） | `scale_slo_and_priorities.md` | 🟡 提案，待讨论确认 |
| 耗时基线报告 | `latency_report.md` | ⚪️ 时点快照（08-23）· **脚本已丢，不可复现** · 现行数字见 `architecture.md` §3.2 |

> 现行耗时基准脚本：`scripts/benchmark_latency.py`（可复现，6 场景 × 3 次）。

## F. 可观测性与运维

| 功能 | 文档 | 该不该信 |
|---|---|---|
| **结构化日志 + request id 贯穿链路** | `observability_design.md` | 🔵 阶段一已实施，二/三/四未实施 |
| 智能运维模块（企业可接入自有系统） | `aiops_module_design.md` | 🟡 设计已确认，**未排期实施** · 死期 09-25 |
| 平台运营数据查询（仪表盘 + 埋点） | `platform_ops_query_design.md` | 🟡 设计待确认，未实施 · 死期 09-26 |

## G. 测试

| 功能 | 文档 | 该不该信 |
|---|---|---|
| 知识库问答测试题库 | `qa_test_questions.md` | ⚪️ 取材自 08-24 的 Chroma 实际内容，可用 |
| **多租户人工测试数据集**（2 企业 × 2 员工，账号 / 角色 / 知识库权限 / 每库问题 / 越权与跨企业拒绝用例 / 跨账号对照表） | `manual_test_dataset.md` | 🟢 当前状态 · 由 `scripts/generate_demo_kb_dataset.py --stage doc` 自动生成，改语料改 `scripts/demo_kb_content/` 后重跑 |

> 演示语料生成 + 摄入 + 自检脚本：`scripts/generate_demo_kb_dataset.py`（幂等，带 `--dry-run`）。
> 语料事实来源：`scripts/demo_kb_content/`（`data/demo_kb_corpus/` 在 `.gitignore` 里，不进版本库）。
> ⚠️ 该文档 §0.2 记录了一个**定位到但未修复**的检索粗筛瓶颈（`ingestion.doc_summary.top_docs=5`），
> 照文档提问时相当一部分问题会返回「未找到相关结果」，演示前务必先读。

## H. 流程 · 评审 · 归档

| 功能 | 文档 | 该不该信 |
|---|---|---|
| **协作复盘与开发流程指南**（每周自查只读 §1） | `collaboration_retrospective.md` | 🟢 活文档 |
| 本次文档整理方案 | `doc_reorg_plan.md` | 🔵 已执行（08-26）· 死期 09-26 |
| **代码审计**（带行号证据） | `review_2026-08-24/review_codebase_findings.md` | ⚪️ 时点快照（08-24） |
| 业界基准对标 | `review_2026-08-24/review_industry_baseline.md` | ⚪️ 时点快照（08-24） |
| 开发过程与协作复盘 | `review_2026-08-24/review_process_retro.md` | ⚪️ 时点快照（08-24） |
| 文档规范与人机协作业界现状 | `review_2026-08-25/doc_and_collab_practices.md` | ⚪️ 时点快照（08-25）· 建议 12-31 前复核 |
| 两个模块的真实状态核查 | `review_2026-08-25/module_status_analysis.md` | ⚪️ 时点快照（08-25） |
| **闲聊被误路由诊断**（81%→0%） | `review_2026-08-25/smalltalk_routing_regression.md` | ⚪️ 时点快照（08-25）· 已验证通过，有复现脚本 |
| 历史设计文档（9 份） | `archive/` | 🧊 **冻结，不维护** · 说明见 `archive/README.md` |

> ⚠️ **`review_2026-08-2*/` 目录名自带日期，这本身就是最好的时点标记**，
> 刻意不改名。`archive/` 是 08-25 刻意建立的归档，`CLAUDE.md` §6 有对应表。

---

## 非 markdown 材料

`docs/` 下另有 4 份 `.pptx` 汇报材料（合计约 825 KB），零引用、零维护成本、
不参与文档腐烂，**保留**：

| 文件 | 内容 |
|---|---|
| `dual_model_architecture.pptx` | 双模型架构 |
| `kb_permission_flow.pptx` | 知识库权限流程 |
| `prompt_injection_remediation.pptx` | 提示注入修复 |
| `rag_agent_technical_report.pptx` | 技术总报告 |

---

## 已删除的文档

按 `CLAUDE.md` §7.4「一份设计被取代时**尽快删除**，只标注不删除仍有成本
（grep 会命中）」。**git 保留完整历史，需要时可取回。**

| 文档 | 删除日期 | 依据 | 取回方式 |
|---|---|---|---|
| `api.md` | 2026-08-26 | 661 行接口文档，**无状态标记**；只覆盖 **7/65** 个端点（11%）；「认证」一节写着"当前版本暂未实现认证机制，所有接口公开访问"，而 **JWT 鉴权早已实现且是 `CLAUDE.md` §5 的已修复 P0**；全部 curl 示例不带 `Authorization`，照抄必然 401 | `git show 7eaff77:docs/api.md` |
| `PROJECT_ARCHITECTURE_SUMMARY.md` | 2026-08-25 | 被 `CLAUDE.md` + `architecture.md` 完全取代，无状态标记、落后 8–12 天 | git history |
| `TECHNICAL_OVERVIEW.md` | 2026-08-25 | 同上 | git history |
| `parallel_reasoning_design.md` | 2026-08-25 | 合并进 `orchestration_design.md`（A 部分） | git history |
| `memory_manage_async_decouple_design.md` | 2026-08-25 | 合并进 `orchestration_design.md`（B 部分） | git history |

> ⚠️ **`review_2026-08-24/review_codebase_findings.md` 里仍有指向 `api.md` 的引用**
> （`:391`、`:405`、`:576`）。那是一份**时点快照**，记录的是 08-24 当天的事实，
> 按 §7.4「报告类保留但标死日期」**刻意不修改** —— 改写冻结报告等于伪造历史记录。
> 顺着那条引用找不到文件是**预期行为**，本节就是它的去向说明。

---

## 目录约定

| 约定 | 说明 |
|---|---|
| **文件名保持英文** | `docs/*.md` 路径被**代码引用 60 处、横跨 29 个 `.py` 文件**（`workflow.py` 一个就占 11 处）。改名会连带作废历史 commit message 里的路径，且要动 `src/`。取舍分析见 `doc_reorg_plan.md` §3 |
| **功能分组靠本表**，不靠目录 | 见上面 A–H 八组 |
| **每份文档头部必须有状态 + 日期** | `CLAUDE.md` §7.4：没有状态标记的**视同不可信** |
| **新增文档后** | 在本表加一行，并跑 `.venv/bin/python scripts/check_doc_links.py` |

**改名或删除文档前后必跑**：

```bash
.venv/bin/python scripts/check_doc_links.py            # 有断链则退出码 1
.venv/bin/python scripts/check_doc_links.py --orphans  # 额外列出无人引用的文档
```
