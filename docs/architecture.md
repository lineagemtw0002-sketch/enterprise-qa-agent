# 架构参考手册

> **状态：当前实现，随架构变更同步更新（2026-08-25 建立）。**
> 本文与 `CLAUDE.md` 同属"当前状态"的正本，两者**不重复内容**：
> - `CLAUDE.md` —— 每个会话都要读的部分（定位 · 未闭环 · 硬性规则）
> - **本文** —— 需要时才查的部分（架构图 · 核心链路 · 性能数据）
>
> 拆分原因：CLAUDE.md 每会话必读，而架构细节绝大多数会话用不到。
> 实测这三章占 CLAUDE.md 约 48% 的 token，拆出后每会话固定成本减半。
>
> **改架构时这两份都要更新。** 除这两份外，任何文档都不得描述"当前系统长什么样"。

## 目录

| # | 章节 |
|---|---|
| 1 | [系统架构](#1-系统架构)：分层图 · 问答全流程 · 功能模块 · 代码模块 · 内置工具 |
| 2 | [核心链路](#2-核心链路)：**双模型系统** · 检索链路 · 工具调用 · 摄入流水线 |
| 3 | [性能现状与目标](#3-性能现状与目标)：SLO · **性能测试** · 容量测算 |

---

## 1. 系统架构

### 1.1 分层架构

```mermaid
flowchart TB
    subgraph FE["前端 · React + Vite（33 文件 / 5.9K 行）"]
        UI["对话界面 · 管理后台（15 个 admin 组件）· TracePanel"]
    end

    subgraph API["接入层 · FastAPI（app.py 3038 行 / 72 端点）"]
        AUTH["auth.py<br/>JWT 鉴权 + 角色守卫"]
        EP["REST 端点 + SSE 流式 + trace WebSocket"]
    end

    subgraph ORCH["编排层 · LangGraph（ragent_backend 23 文件 / 10.4K 行）"]
        WF["workflow.py 1672 行<br/>RAGWorkflow StateGraph"]
        INT["intent.py 812 行<br/>analyze_and_route"]
        SUB["tool_agent 8 文件 / 2.1K 行<br/>ReAct 子图"]
    end

    subgraph CAP["能力层"]
        MCP["mcp_server 7 文件 / 3.1K 行<br/>query_knowledge_hub"]
        ING["ingestion 23 文件 / 4.9K 行<br/>摄入流水线"]
        CORE["core 17 文件 / 4.1K 行<br/>检索引擎 · 配置"]
        SEC["security 95 行<br/>prompt_guard"]
    end

    subgraph LIB["基础库 · libs（42 文件 / 7.5K 行）"]
        FAC["factory 统一：llm / embedding / reranker / vector_store / splitter"]
    end

    subgraph DATA["存储"]
        PG[("PostgreSQL<br/>会话 · 用户 · 角色 · 组织")]
        CH[("ChromaDB<br/>向量，按 collection 隔离")]
        BM[("BM25 索引<br/>data/db/bm25/{collection}")]
        SQ[("SQLite<br/>摄入历史 · 去重")]
    end

    subgraph MODEL["模型 · 本地 Ollama"]
        M7["qwen2.5:7b<br/>生成 / 工具决策 / 抽取 / 摘要"]
        M15["qwen2.5-1.5b-router<br/>意图分类 + query 改写"]
    end

    UI --> EP
    EP --> AUTH
    EP --> WF
    WF --> INT
    WF --> SUB
    WF --> MCP
    SUB --> MCP
    MCP --> CORE
    ING --> CORE
    CORE --> LIB
    LIB --> CH
    LIB --> BM
    ING --> SQ
    WF --> PG
    INT --> M15
    WF --> M7
    SUB --> M7

    style MODEL fill:#f0f4ff
    style DATA fill:#f5f5f5
    style SEC fill:#fff4f0
```

### 1.2 一次问答的完整流程

```mermaid
flowchart LR
    START([用户提问]) --> S["session<br/>会话初始化<br/>召回长期记忆"]
    S --> I{"intent<br/>analyze_and_route<br/><b>1.5b router</b>"}

    I -->|clarify| C["clarify<br/>反问澄清"]
    I -->|rag| R["retrieve<br/>会话内上传文件"]
    I -->|tool| T["tool_subgraph<br/>ReAct 循环 ≤5 轮"]
    I -->|workflow| W["workflow<br/>请假/报销字段抽取"]

    R --> G["generate<br/><b>7b</b> 流式生成<br/>+ 提示词泄露检测"]
    T --> G
    C --> M
    W --> M
    G --> M["memory_manage<br/>滑动窗口 + 摘要压缩"]
    M --> A["archive<br/>后台归档"]
    A --> DONE([done 事件])

    style I fill:#e8f0ff
    style G fill:#fff0e8
```

> **关键点**：`done` 事件必须等整张图跑完才发出，而 `memory_manage` 触发压缩时要用 7b
> 做一次摘要——**用户已看完全部回答，还要干等约 8 秒**。
> 异步化方案见 `docs/orchestration_design.md` B 部分（**未实施**）。

### 1.3 功能模块

```mermaid
flowchart TB
    subgraph U["面向员工"]
        F1["💬 智能问答<br/>流式回答 · 来源引用 · TracePanel"]
        F2["📁 知识库上传<br/>PDF/Word/Excel/PPT/MD"]
        F3["📝 工作流发起<br/>请假 · 报销（自然语言填单）"]
        F4["🕐 考勤查询"]
    end

    subgraph A["面向企业管理员"]
        F5["🗂️ 知识库管理<br/>创建 · 删除 · 分页查看 chunk"]
        F6["👥 角色与权限<br/>角色 CRUD · 角色↔知识库授权"]
        F7["✅ 工作流审批<br/>模板管理 · 审批人配置"]
    end

    subgraph P["面向平台管理员"]
        F8["🏢 组织管理<br/>企业 CRUD · 用户归属"]
        F9["📊 运维仪表盘<br/>网关监控 · 连接器健康 · token 趋势"]
        F10["🔧 知识库测试查询<br/>⚠️ 绕过 ACL，上线前必删"]
    end

    style F10 fill:#ffe0e0
```

| 模块 | 状态 | 关键位置 |
|---|---|---|
| 智能问答（RAG + 工具 + 工作流四分支） | ✅ 可用 | `workflow.py` |
| 知识库上传与摄入 | ⚠️ 可用，但**同名文档更新后新旧版本并存**；残留分布在 **5 处存储**，且产品面只有"整库删"一档、无按文档删除入口 | `ingestion/pipeline.py` |
| 工作流（请假/报销） | ✅ 可用 | `workflow_store.py` 784 行 |
| 考勤查询（含租户联邦路由） | ✅ 可用 | `builtin_tools.py:108` |
| 角色与知识库权限 | ✅ 可用 | `role_store.py` |
| 组织/企业管理 | ✅ 可用 | `org_store.py` |
| 运维仪表盘 | ⚠️ **仅监控展示**，自动运维内核未实现 | `OperationsDashboard.jsx` |
| 长期记忆（LTM） | ✅ 可用 | `ltm_store.py` |
| 提示注入防护 | ⚠️ **已接入三处主链路**（摄入拒收 / 检索剔除 / 生成短路），但**委托模式链路完全未覆盖**、零单测 | `security/prompt_guard.py`、`query_knowledge_hub.py`、`workflow.py` |

### 1.4 代码模块清单

| 模块 | 规模 | 职责 | 最大文件 |
|---|---|---|---|
| `src/ragent_backend/` | 23 文件 / **10,396 行** | FastAPI 应用 · LangGraph 编排 · 各类 Store | `app.py` 3038 · `workflow.py` 1672 |
| `src/libs/` | 42 文件 / 7,461 行 | 基础能力（factory 模式 + 多 provider） | — |
| `src/ingestion/` | 23 文件 / 4,881 行 | 摄入流水线（8 阶段） | `pipeline.py` |
| `src/core/` | 17 文件 / 4,089 行 | 检索引擎 · 配置层 | `hybrid_search.py` 813 |
| `src/observability/` | 20 文件 / 3,946 行 | 评估 · 追踪 | — |
| `src/mcp_server/` | 7 文件 / 3,121 行 | 知识库检索工具 | `query_knowledge_hub.py` 1528 |
| `src/tool_agent/` | 8 文件 / 2,138 行 | ReAct 子图 · 工具注册表 | `subgraph.py` |
| `src/security/` | 2 文件 / 95 行 | 提示注入规则检测（行数少是因为**处理策略刻意留给调用方**，不是未完成） | `prompt_guard.py` |
| `frontend/src/` | 33 文件 / 5,875 行 | React 前端 | `App.jsx` 1334 |
| `tests/` | 79 文件 / 27,179 行 | ⚠️ **几乎全在老 RAG 库层**，见 §6 | — |

### 1.5 内置工具（6 个）

`query_knowledge_hub` · `list_collections` · `get_document_summary` ·
`query_attendance` · `check_workflow_status` · `resubmit_workflow`

---

## 2. 核心链路

### 2.1 双模型系统

系统同时跑两个本地模型，**按"任务是否需要综合推理/自由生成"分工**，不是按大小分。

```mermaid
flowchart TB
    Q["用户提问"] --> IN["_intent_node"]

    IN -->|"唯一使用点"| R15["<b>qwen2.5-1.5b-router</b><br/>LoRA 微调<br/>━━━━━━━━<br/>· query 改写（指代消解）<br/>· 子查询拆分<br/>· 四分类<br/><b>一次调用全做完</b>"]

    R15 --> ROUTE{路由结果}
    ROUTE --> G["_generate_node<br/>生成最终回答"]
    ROUTE --> T["think_node<br/>ReAct 工具决策"]
    ROUTE --> W["_extract_workflow_fields<br/>工作流字段抽取"]
    G --> MM["_memory_manage_node<br/>记忆摘要压缩"]

    G -.->|调用| M7["<b>qwen2.5:7b</b><br/>通用生成模型"]
    T -.->|调用| M7
    W -.->|调用| M7
    MM -.->|调用| M7

    style R15 fill:#e8f0ff
    style M7 fill:#fff0e8
```

| | `qwen2.5-1.5b-router` | `qwen2.5:7b` |
|---|---|---|
| **来源** | LoRA 微调（窄任务蒸馏） | 通用开源模型 |
| **使用点** | **只有 `_intent_node` 一处** | 生成 / ReAct 决策 / 字段抽取 / 记忆摘要 |
| **任务性质** | 输出结构固定（严格 JSON schema），四选一 | 开放任务，需综合判断与自由生成 |
| **实测耗时** | **约 3.2s** | 同一合并任务约 8.7s |
| **准确率** | **不输 7b**，部分边界案例反而更准 | — |
| **回退** | `RAGENT_INTENT_MODEL` 可覆盖；模型不存在时**自动回退 7b** | — |

**为什么这个任务适合小模型**：输出是严格 JSON schema（`intent_type` / `confidence` /
`target_tool`…），不需要自由发挥，属于"窄任务 + 固定输出结构"，正是微调小模型的适用区间。

> ⚠️ **该结论只对这一个合并任务成立。** ReAct 决策（`think_node`）和工作流字段抽取
> **没有验证过**，继续用 7b，不受 `RAGENT_INTENT_MODEL` 影响。
>
> ⚠️ **业界视角的提醒**：业界基准把"微调专用小模型（如果 embedding 分类器够用）"
> 列在**"大厂特有、小团队不必跟"**——理由是"训练是最便宜的一步，维护是最贵的一步"
> （registry、回归集、漂移监控、基座升级兼容性都是长期负债）。
> 本项目是 4 分类、边界清晰，落在"embedding 分类器可能够用"的区间。
> **已做完的不必推翻，但不要再往上加维护负担。**

### 2.2 检索链路

```mermaid
flowchart TB
    Q["查询"] --> ACL{"ACL 收敛<br/>_org_owned_collections(org)<br/>∩ 角色 allowed_collections"}
    ACL -->|为空| DENY["直接拒绝"]
    ACL -->|候选库列表| FAN["并行扇出<br/>每库一路"]

    FAN --> D["Dense<br/>Chroma 向量"]
    FAN --> S["Sparse<br/>BM25"]
    D --> RRF["RRF 融合<br/>k=60"]
    S --> RRF
    RRF --> INJ["注入过滤<br/>_filter_injected_chunks"]
    INJ --> RR["Cross-Encoder 重排<br/>bge-reranker-base"]
    RR --> TH{"MIN_RELEVANCE_SCORE<br/>= 0.1"}
    TH -->|低于阈值| EMPTY["固定模板：未检索到内容<br/>（跳过 LLM，不让小模型自由发挥）"]
    TH -->|通过| OUT["带来源标注的上下文"]

    style ACL fill:#ffe8e8
    style TH fill:#fff8e0
```

**阈值说明**：`MIN_RELEVANCE_SCORE = 0.1` 打在 **cross-encoder 重排分**上，**不是 RRF 融合分**。
依据是实测（`query_knowledge_hub.py:56-65`）：不相关问题的噪音稳定在 0.03 以下、
真相关的稳定在 0.13 以上，两簇间有明显断层。**reranker 降级 fallback 时不适用这个阈值。**

### 2.3 工具调用（ReAct 子图）

`think_node → tool_node → summarize_node`，`max_iterations=5`。
每步起止都推送 trace。工具执行有审计回调（`_audit_log`）。

### 2.4 摄入流水线（8 阶段）

```mermaid
flowchart LR
    F["文件"] --> I1["① 完整性检查<br/>SHA256 增量跳过"]
    I1 --> I2["② 加载<br/>PDF/DOCX/XLSX/PPTX/MD"]
    I2 --> I3["③ 切块"]
    I3 --> I4["④ 变换增强<br/>chunk_refiner · metadata"]
    I4 --> I5["⑤ 片段去重<br/>内容指纹"]
    I5 --> I6["⑥ Embedding"]
    I6 --> I7["⑦ 写入<br/>Chroma + BM25"]
    I7 --> I8["⑧ 层级索引<br/>{collection}__summary"]

    style I1 fill:#ffe8e8
    style I5 fill:#ffe8e8
```

> ⚠️ **①和⑤都是"跳过"语义，没有"替换"语义** —— `doc_id` 就是文件 SHA256，
> 内容一变即被当作全新文档，**旧版本片段永远不会被删除**。见 [§6](#6-已知未闭环) 第 1 条。

---

## 3. 性能现状与目标

### 3.1 最小 SLO（2026-08-25 确认接受）

| 类别 | 指标 | 目标 | 当前 |
|---|---|---|---|
| **正确性** | 跨组织/越权泄露 | **0（无错误预算）** | 隔离已验证 |
| | 身份不可伪造 | 0 已知路径 | ✅ 已修 |
| | 并发请求内容不串 | 0 | ✅ 已修 |
| **质量** | 忠实度 / 拒答率 / 不编造跨主题关联 | ≥95% | ⚠️ **全部无法度量** |
| **性能** | **首字延迟 TTFT** | **≤ 3s** | ⚠️ **1.7 – 9.2s（部分场景达标）**〔优化前 5.1 – 20.1s〕 |
| | 完整回答 P95 | ≤ 15s | ✅ **14.3s**〔优化前 46.8s〕 |
| | 完整回答 P50 | ≤ 8s | ✅ **3.0s**〔优化前 24.2s〕 |
| | 峰值承载 | ≥ 1.5 QPS | ❓ **本轮未测**（串行单用户，无并发测量） |

> **"当前"列的性能数字**：2026-08-25 实测，`scripts/benchmark_latency.py`，
> 每场景 3 次取中位数，详见 [§3.2](#32-性能测试方法与结果)。〔方括号〕内为
> 2026-08-23 优化前基线（`docs/latency_report.md`）。
>
> ⚠️ **P50/P95 口径已变，不能与优化前直接比大小**：优化前那两个数字来自
> 303 条真实对话消息的分布；本次是 **6 个场景等权重的合成分布（n=18）**，
> 场景配比是人为选的，不代表真实流量。两者都写在这里是为了看量级变化，
> 不是同口径统计。**峰值承载那一行的 0.067 QPS 已删除** —— 那是从旧 P50
> 反推的估算值，本轮既没重测并发也没重算，留着会被当成实测。
>
> **TTFT 仍是最重要的一条，且仍未全面达标**：6 个场景里 3 个（检索命中 3.6s、
> 6 库并行 3.9s、长回答 9.2s）热态中位数仍超过 3s。
> **但性质已经变了**——优化前是"用户等 12–20 秒看不到任何字"，
> 现在最坏也是 9.2s、多数场景 2–4s。
>
> ⚠️ **短回答场景下"流式"目前等于非流式**：`_generate_node` 为了做系统提示词
> 泄露检测，会先攒够 `_PROMPT_LEAK_CHECK_WINDOW = 200` 个字符再放行第一批 token
> （`workflow.py:105`）。回答不足 200 字时整段生成完才出第一个字，所以本次实测里
> 短回答场景的 **TTFT 与总耗时相差不到 20ms**。这是安全与 TTFT 的真实取舍，
> 不是测量误差；**要把 TTFT 压到 3s 以内，这个窗口是绕不开的一环**。
>
> **质量类全部无法度量**：golden set 仅 12 条，且关键词断言结构上抓不到"编造"。
> 这是后续所有优化的前置。

### 3.2 性能测试：方法与结果

#### 3.2.1 当前基准（2026-08-25，**已验证通过**）

**测试脚本**：`scripts/benchmark_latency.py` —— **可复现**，跑法见脚本顶部 docstring。
原始结果 JSON 落在 `scripts/benchmark_results/`。

**方法**
- 脚本自己用 `create_app()` + uvicorn 在本进程内起真实 HTTP 服务（真实 SSE 链路，
  真实 socket；lifespan 完整执行，含启动预热），再用 httpx 消费 `/api/v1/chat/stream`
- **TTFT** = 请求发出 → 收到第一个 `token` 事件；**总耗时** = 请求发出 → `done` 事件
- **分阶段**取 workflow 自身的 trace 时间戳（`_emit_trace` 的 node_start/node_end 配对）
- 每场景 **3 次取中位数**；第 1 次单独标 `cold`，第 2 次起标 `warm`
- 账号：`bob_acme`（单库 `acme_hr_admin_kb`，22 块）/ `alice_acme`（org_admin，6 库）

> **这批数字对应的代码状态**：`main` 分叉分支 `fix/p0-security-and-project-conventions`
> @ **`97e6d88`**，且**工作区带未提交改动**：
> `M docs/optimization_tracking.md` · `M src/mcp_server/tools/query_knowledge_hub.py` ·
> `M src/ragent_backend/app.py` · `M src/ragent_backend/workflow.py` ·
> `?? docs/review_2026-08-25/` · `?? scripts/benchmark_latency.py` ·
> `?? scripts/benchmark_mlx_vs_ollama.py` · `?? scripts/benchmark_results/`
> —— 其中三个 `src/` 文件正是本轮性能优化的载体。
> **也就是说这批数字无法只靠 commit hash 复现，对应的是"含上述未提交改动的工作区状态"。**
> 脚本每次运行都会把当时的 commit + 脏文件清单写进结果 JSON 的 `code_state` 字段。

**分场景实测（热态中位数，括号内为冷态即该场景第 1 次）**

| 场景 | **TTFT** | 总耗时 | intent | 检索(tool_subgraph) | generate | 输出 |
|---|---|---|---|---|---|---|
| 闲聊（"你好，你是谁"） | **1.93s**（2.48s） | 1.94s | 1.81s | 0.19s | **0**（未调 LLM ⚠️见下） | 56 字 |
| 检索命中 | **3.57s**（4.06s） | 3.58s | 1.50s | 0.28s | 1.85s | 65 字 |
| 检索未命中 | **2.13s**（1.75s） | 2.14s | 1.76s | 0.14s | **0**（短路，不调 LLM） | 56 字 |
| 发起工作流 | **1.71s**（1.88s） | 1.71s | 0.001s | —（走 workflow 1.69s） | — | 10 字 |
| 长回答 | **9.24s**（8.85s） | 14.43s | 1.75s | 0.28s | **12.01s** | 354 字 |
| 6 库并行检索 | **3.94s**（4.09s） | 3.95s | 1.92s | 0.39s | 1.93s | 65 字 |

**与优化前对照（2026-08-23，见 [§3.2.2](#322-优化前基线2026-08-23保留对照)）**

| 场景 | TTFT 优化前 → 现在 | 总耗时 优化前 → 现在 | 可比性 |
|---|---|---|---|
| 闲聊 | 20.1s → **1.9s** | 22.1s → **1.9s** | ⚠️ **不可比**，行为变了（见下方结论 5） |
| 检索命中 | 16.0s → **3.6s** | 20.4s → **3.6s** | ✅ 可比（输出 158 → 65 字，部分归功于输出变短） |
| 检索未命中 | 12.9s → **2.1s** | 12.9s → **2.1s** | ✅ 可比（两次都是 56 字固定短路话术） |
| 发起工作流 | 5.1s → **1.7s** | 5.1s → **1.7s** | ✅ 可比（同为 10 字） |
| 长回答 | 17.8s → **9.2s** | 39.0s → **14.4s** | ⚠️ 部分可比（输出 682 → 354 字） |
| 6 库并行 | 13.5s → **3.9s** | 17.9s → **4.0s** | ✅ 可比 |

**全场景合成分布**（n=18，6 场景等权重，**非真实流量分布**）：
总耗时 P50 **2.98s** / P95 **14.30s** / max 14.79s；TTFT P50 **2.97s** / P95 **8.98s**。

**结论**

1. **固定开销已经塌掉了**——优化前"最快一档也要 8.4s 起步"，现在最快 1.7s。
   剩下的固定开销主要是 **intent 节点，稳定 1.5–2.2s**，在非长回答场景里
   已经占到 TTFT 的一半左右，是下一个最值得压的目标。
2. **生成阶段耗时仍然几乎完全由输出长度决定**：65 字 → 1.85s，354 字 → 12.0s。
   这一条与优化前一致，是模型推理速度的物理上限，不是代码逻辑问题。
3. **当前数据量下检索不是瓶颈，6 库并行与单库基本持平**——同一个问题，
   单库 tool_subgraph 0.28s vs 6 库 0.39s。检索链路从优化前的 8.65s
   降到零点几秒（启动预加载 reranker/embedding 的效果）。
   ⚠️ **但这仍然是在测试数据量（每库约 20 块）下测的**，与优化前那次的前提完全相同；
   `CLAUDE.md` §4 P0 第 2 条推算真实数据量（几个 G 文档 → 143K–716K 块）下
   BM25 索引全量加载会变成主要瓶颈。**两者不矛盾，是不同数据量下的两个结论；
   本次实测对"真实数据量下检索快不快"这个问题没有提供任何证据。**
4. **冷启动几乎被消灭**——冷/热态差距 ≤ 0.5s（长回答场景冷态甚至更快，在噪声内）。
   代价是**后端启动本身要 ~20s**（`lifespan` 里 `_preload_retrieval_models()` +
   `_warm_llms_at_startup()` 同步跑完才开始接受请求），这笔钱从"运气不好的第一个
   用户"挪到了"部署时"。
5. ⚠️ **闲聊场景出现行为回归，不是性能改善**：`"你好，你是谁"` 现在被路由进
   `tool_subgraph` 查知识库，然后走"未命中"短路，返回固定话术
   `"抱歉，在您当前可访问的知识库范围内，未检索到相关内容…"`（56 字，`model_id`
   显示 `n/a (empty kb hit, no LLM call)`）。优化前该场景返回的是 73 字的真实
   LLM 回答。**它变快是因为不再调用 LLM，而不是同样的事做得更快，而且答案是错的。**
   这条已登记为 `CLAUDE.md` §4 的 P1。
6. ⚠️ **短回答场景的"流式"名存实亡**：见 [§3.1](#31-最小-slo2026-08-25-确认接受)
   关于 `_PROMPT_LEAK_CHECK_WINDOW = 200` 的说明。实测里 5 个短回答场景
   TTFT 与总耗时相差不到 20ms。

**本次未覆盖**
- **并发 / 多用户 / 排队行为** —— 全部为串行单用户测量，SLO 的"峰值承载 ≥ 1.5 QPS"
  这一条本轮**没有任何实测数据**
- **真实数据量下的表现** —— 每库仅约 20 块测试数据，见结论 3
- 关闭启动预加载的对照（没有量化"预加载省了多少"，只观察到冷热差已经很小）
- 非本机硬件（Apple M4 16GB + 本地 Ollama）
- 前端渲染耗时（只测到 SSE 事件）；真实网络往返（本机 loopback）
- 回答质量 —— 本次只测耗时，除了上面结论 5 那条明显回归外没做正确性判定

#### 3.2.2 优化前基线（2026-08-23，保留对照）

**测试方法**（`docs/latency_report.md`）
- **样本统计**：303 条真实对话消息的端到端耗时分布 —— 平均 26.4s，P50 24.2s，P95 46.8s
- **受控探测**：trace 时间戳按场景各测**一次**（无重复、无中位数）

> ⚠️ 当时的探测脚本 `latency_probe.py` **已丢失**，**这批数字不可复现**。
> 它们只作为量级对照保留，不要再往上叠新结论。
> 教训已固化成硬性规则：脚本一律落 `scripts/`（`CLAUDE.md` §7.2）。

| 场景 | 总耗时 | **TTFT** | 意图分类 | 检索+生成 | 输出长度 |
|---|---|---|---|---|---|
| 闲聊（"你好，你是谁"） | 22.1s | **20.1s** | **13.8s** | 8.3s | 73 字 |
| 检索命中 | 20.4s | 16.0s | 6.6s | 13.7s | 158 字 |
| 检索未命中 | 12.9s | 12.9s | 6.4s | 6.6s | 56 字 |
| 发起工作流 | **5.1s** | 5.1s | 1.7s | 3.4s | 10 字 |
| 长回答 | 39.0s | 17.8s | 6.1s | **32.9s** | 682 字 |
| 6 库并行检索 | 17.9s | 13.5s | 5.7s | 12.2s | 183 字 |

**当时的四个结论**：① 存在与输出长度无关的固定开销，最快一档也要 8.4s 起步 ·
② 生成阶段耗时几乎完全由输出长度决定 · ③ 当前数据量下检索不是瓶颈，
6 库并行与单库基本持平 · ④ 工作流路径明显更快。
**③ 和 ④ 在 2026-08-25 复测中仍然成立；① 已被推翻（固定开销从 8.4s 降到 1.7s）。**

#### 3.2.3 中间过程记录

单场景的逐轮优化记录（think 节点跳过 · 启动预加载 reranker/embedding ·
reranker 推理预热 · MLX vs Ollama 实测）见 `docs/optimization_tracking.md` 任务二。
那是**单场景、手工执行**的定点验证（"已跑通"档），
本节 §3.2.1 才是覆盖全部 6 个场景的可复现基准（"已验证通过"档）。

### 3.3 容量测算（单客户口径）

假设：日活 30% · 每人每日 4 次 · 峰值小时占 20% · 突发系数 2.0

```
日提问量 12,000  →  峰值小时 2,400  →  峰值 1.33 QPS
```

**并发在飞请求数 = 峰值 QPS × 停留时间**（Little's Law）：

| 延迟 | 并发在飞 |
|---|---|
| 优化前 P50 24.2s（2026-08-23 真实分布） | **32.3** |
| 若达成 10s | 13.3 |
| 若达成 5s | 6.7 |
| 2026-08-25 合成分布 P50 3.0s ⚠️见下 | 4.0 |

> **推论：并发压力是延迟的函数。压延迟本身就是在降容量需求——两件事是一件事。**
>
> ⚠️ **最后一行不能当成"容量需求已经降到 4 了"**：3.0s 来自 §3.2.1 的
> **6 场景等权重合成分布**，真实流量里长回答的占比几乎肯定高于 1/6，
> 而长回答一条就 14.4s。而且 Little's Law 假设服务端能真正并行处理这些在飞请求，
> 当前 `OLLAMA_NUM_PARALLEL` 默认 1（`CLAUDE.md` §4 P0 第 3 条），
> **并发本轮完全没测**。要拿这一行做容量决策，先补真实流量分布 + 并发压测。

**多租户按客户数倍乘**：1–3 家客户 = 1.3–4.0 QPS，所需模型并发度约 20–60。
这个量级**单台 GPU 服务器 + vLLM 即可满足**，不需要多机集群。

### 3.4 硬件解决不了的那部分

| 类别 | 换硬件能解决吗 |
|---|---|
| 模型服务吞吐 | ✅ **能** —— GPU + vLLM |
| 每查询重建检索组件 / 全链路无缓存 | ❌ **不能** —— 浪费按负载等比放大 |
| 管理端 N+1 | ❌ 不能 —— 上万次串行往返 |
| 连接池分散（累计上限约 68） | ❌ 不能 |
| 无结构化日志 / request id | ❌ 不能 |

---
