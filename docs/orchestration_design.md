# 编排层设计方案（合并版）

> **状态：部分实施。**
> - A 部分：**D4 / D5 已实施**（2026-08-25 安全第二批，`_build_prompt`）；
>   **D1 / D2 已实施**（2026-08-25 第三批，`intent.py` + `workflow.py`，见 §4.5）。
>   **D3 / D6a / D6b 未实施。**
> - B 部分：**全部未实施**（阻塞项 B-R1 已实测查清，见 §5.4，但主体改动没动）。
>
> §九 的验收定义全部达成之前，本行必须继续标"部分实施"。已同步 `CLAUDE.md`。
>
> 创建：2026-08-25
> **2026-08-25 更新**：B 部分的阻塞项 **B-R1 已实测查清**（§5.4），阻塞解除。
> 结论是"并发不安全，必须加 per-`thread_id` 锁"，且新发现一条 **B-R6**（§5.4 结论 4）。
> 探针脚本：`scripts/probe_checkpointer_concurrency.py`（可复跑，不改 `src/`）。
> **本文合并自两份独立草案**，两份原文已标记为被本文取代：
> - `docs/parallel_reasoning_design.md` —— 并行编排与"思维错乱"防护（A 部分）
> - `docs/memory_manage_async_decouple_design.md` —— 记忆/归档节点异步化（B 部分）
>
> 合并原因：两份方案都改动 `workflow.py`，需要统一评估冲突与实施顺序。

---

## 一、合并结论速览

**结论：两份方案没有直接代码冲突，可以合并为一份计划推进。**
但存在 3 条必须协调的交互（§3），其中 2 条是原草案未覆盖的技术细节。

### 1.1 功能点重叠矩阵

| 功能点 | A（并行编排） | B（节点异步化） | 判定 |
|---|---|---|---|
| `run_stream()` 流式编排 | 不动（遵守 P0 契约） | **改**：`interrupt_after` + 后台续跑 | B 独占 |
| `_build_graph()` 图编译 | 不动 | **改**：加 `interrupt_after` | B 独占 |
| `_build_prompt()` | **改**：D4/D5 合并约束 | 不动 | A 独占 |
| `intent.py` 子查询拆分 | **改**：D1/D2 | 不动 | A 独占 |
| `query_knowledge_hub.py` 输出格式 | **改**：D3 | 不动 | A 独占 |
| `subgraph.py` 汇总 | **改**：D3 | 不动 | A 独占 |
| `memory_manage` / `archive` 节点 | 不动 | **改**：移出前台 | B 独占 |
| `app.py::chat_stream` | 不动 | **改**：`done` 时机 | B 独占 |
| 并发安全 | F1 已修（contextvars） | B-R1（同 thread_id 写 checkpoint）**已实测：不安全** | **不同轴，见 §3.1 / §5.4** |
| 延迟 | D2 降检索段耗时 | 降尾部 ~8s | **同方向，见 §3.2** |
| 测试基础设施 | D6 golden set + faithfulness | 真并发回归测试 | **可共用，见 §6.2** |

**唯一的物理接触点**：A 的 D4/D5 改 `_build_prompt`，B 改 `run_stream` 与 `_build_graph`——
同一个文件 `workflow.py`，但**不同函数**，git 层面冲突风险低。

---

## 二、两份方案各自要解决什么

**A —— 并行编排与思维错乱防护**

前提认识：单次 LLM 调用内部不存在并行思考（自回归解码逐 token 串行）。
**"思维错乱"不可能由模型内部产生，一定是编排层制造的。** 系统现有 4 条并行路径
（多用户请求 / dense-sparse 双路 / 多库扇出 / 多子查询扇出）+ 1 条隐性累积路径（ReAct 多轮）。

**B —— 记忆/归档异步化**

实测问题：回答文字已全部流式送达，前端"生成中"还要多挂约 8 秒。
根因是 `done` 事件必须等整张图跑完，而 `_memory_manage_node` 触发压缩时要用 7b 做一次摘要，
这次调用排在 `generate` 之后、`done` 之前。

---

## 三、必须协调的三条交互

### 3.1　并发安全是同一类问题的两个不同轴（**认知上要统一，实现上不共用代码**）

- A 的 F1（已修）：**per-request 状态放进进程级共享对象** → 跨用户串流
- B 的风险1（**已实测，见 §5.4**）：**同一 thread_id 的 checkpoint 并发写** → 上一轮后台续跑未落盘时下一条消息就进来

两者不是同一个 bug，但**是同一类失效模式**：共享可变状态在并发下无保护。
共同特征也一致——**不产生任何可观察的异常输出，单人测试永远正常**。
实测已确认 B 的风险1 同样符合这个特征：不抛异常、不报冲突、日志里什么都看不到。

**协调结论**：
- B 的风险1 已于 2026-08-25 用 `scripts/probe_checkpointer_concurrency.py` 实测查清：
  `AsyncPostgresSaver` **没有**任何并发写保护，行为是**静默丢更新**。详见 §5.4
- 两者共用同一条测试纪律（§6.2），但**不共用实现**

### 3.2　两份方案都降延迟，但作用在不同区段（可叠加，不重复）

| 区段 | 归属 | 效果 |
|---|---|---|
| 检索段 | A 的 D2（扇出上限） | 子查询从无上限降到 ≤3，减少并行调用数 |
| 生成段 | 现状 | 不动 |
| **尾部段（done 之后的等待）** | B | 砍掉约 8s 用户干等 |

**协调结论**：不冲突，可叠加。但**两者都不应作为"耗时优化已完成"的结论**——
`docs/optimization_tracking.md` 的"优化后"一栏需要在两者都落地后统一填写，
且必须基于同一套基准重测，不能各报各的数。

### 3.3　⚠️ B 的后台续跑任务会继承到"非空的流式队列"（**原草案未覆盖**）

这是本次合并新发现的技术细节，B 实施时必须处理。

B 的方案第 4 步是在 `run_stream()` 里另起后台任务：

```python
asyncio.create_task(self._compiled.ainvoke(None, config))
```

**问题**：`asyncio.create_task` 复制的是**创建那一刻**的 contextvars。如果这个后台任务在
`run_stream()` 的 `finally`（`_CURRENT_TOKEN_QUEUE.set(None)`）之前创建，它拿到的是
**队列仍然非空**的上下文副本。

后果与 B 草案的假设不同：
- B 草案假设"contextvar 已清空 → `_emit_trace` 走 no-op 分支，trace 静默丢弃"
- **实际是**：后台段的 `memory_manage` / `archive` 调用 `_emit_trace` 时，
  contextvar 在它自己的副本里**仍指向那个队列**，于是它会持续往一个**已经没有任何消费者**
  的队列里 put（客户端早已收到 `done`、生成器已退出）。

影响：不会抛异常，也不会串到别的用户（隔离仍然成立），但
1. 队列会持续增长并持有引用，直到后台任务结束、整体被 GC——**是一处内存滞留**
2. 行为与设计意图不符，且这种"看起来正常、实际在往黑洞里写"的状态极难排查

**协调结论**：B 实施时必须显式切断后台任务的队列继承。两种做法任选：
- 在 `create_task` **之前**先 `set(None)`，再创建后台任务（顺序最简单）
- 或用 `contextvars.copy_context()` 构造一个干净上下文再跑

**并且要有测试断言**：后台续跑期间不得向本次请求的队列写入任何内容。

### 3.4　⚠️ 后台任务需要强引用，应复用已有的 `self._background_tasks`（**原草案未覆盖**）

`workflow.py` 里已经有这段（构造函数内）：

```python
# asyncio only holds a *weak* reference to a task; one created and never
# stored anywhere ... can be garbage-collected before it finishes running.
# Keeping a strong reference here until each task completes is the documented fix.
self._background_tasks: set[asyncio.Task] = set()
```

B 新起的后台续跑任务如果不存进这个集合，**可能在跑完之前被 GC 掉**——症状是压缩偶发不生效、
且完全没有错误日志。这个坑项目里已经踩过一次并留下了记录。

**协调结论**：B 的后台任务必须加入 `self._background_tasks`，并在完成回调里移除。

---

## 四、A 部分：并行编排与思维错乱防护

### 4.1 现状：四条并行路径 + 一条隐性累积路径

| # | 并行轴 | 位置 | 合并方式 | 风险 |
|---|---|---|---|---|
| P1 | 多用户请求 | 每请求一个 asyncio task | 不合并 | ✅ 已修（contextvars） |
| P2 | dense / sparse | `parallel_retrieval=True` | RRF 按 rank 融合 | ✅ 无风险 |
| P3 | 多知识库扇出 | `query_knowledge_hub.py:1293` | **拍平**进一个 list，整体重排 | ⚠️ 无主题分组 |
| P4 | 多子查询扇出 | `workflow.py::_retrieve_multi` | **带标签**拼接 | ✅ 已加依赖判据（D1）+ 扇出上限 3（D2） |
| P5 | ReAct 多轮累积 | `max_iterations=5` | 累积进对话上下文 | ⚠️ 同 P3 |

**P4 做对了一处，要保留**：`workflow.py:1169` 用 `[子查询: {q}]` 标注每段材料来源，
用 `---` 分隔，provenance 没丢。**不要在重构中被简化成 `"\n".join()`。**

### 4.2 四种错乱形态

| 形态 | 机制 | 路径 | 现状 |
|---|---|---|---|
| F1 状态串扰 | per-request 状态放共享对象 | P1 | ✅ 已修 + 有测试 |
| F2 上下文污染 | 多来源材料拍平，分不清归属 | P3、P5 | ⚠️ P4 已防（标注 + D2 上限），P3/P5 未防 |
| F3 假并行 | 有依赖的子问题被并行执行 | P4 | ✅ 已防（D1，见 §4.5） |
| F4 合并期编造关系 | 无关材料被强行关联出因果/换算 | P3、P4、P5 | ❌ **已实际发生** |

**F4 已复现的案例**（`security_prompt_injection_test_report.md` 案例4）：

> 问："结合年假制度和远程办公政策，如果我今年请了20天年假，还能申请多少天远程办公？"
> 答（节选）："……本年度剩余的年假天数为0天……因此您将无法再申请远程办公天数。"

两份材料**各自都被正确召回、内容都没错**。错在模型编造了原文不存在的因果链。

> **更正备案**：先前口头判断该案例"被拆成两个独立子查询"不准确。核对后确认它
> `intent_type=tool`、`target_tool=query_knowledge_hub`，走工具路径（P3/P5），
> **未经过 P4 的子查询拆分**。结论不变，机制归属更正。

### 4.3 设计决策 D1–D6

| # | 决策 | 防 | 改动性质 | 验收 | 状态 |
|---|---|---|---|---|---|
| **D1** | 子查询拆分必须先判断依赖性；有依赖不拆，**降级为单查询**交给已有 ReAct 子图 | F3 | prompt + 一处确定性判据（`intent.py`） | 依赖型问题断言 `len(sub_queries)==1` | ✅ **已实施**（见 §4.5） |
| **D2** | 子查询扇出硬上限 **= 3**（Q1 已拍板） | F2/F4 | prompt + 一处截断 | 6 子查询问题只执行 3 个且有 trace 记录 | ✅ **已实施**（见 §4.5） |
| **D3** | 统一材料归属标注：P4 保持现状；P3 补来源标注；P5 按轮次分组 | F2 | 改两处输出格式 | 跨主题提问的 prompt 中两组材料可区分 | ❌ 未实施（Q4 未拍板） |
| **D4** | **禁止跨材料编造关系**：除非材料明确陈述，不得推导跨材料因果/加减/换算 | F4 | 纯 prompt（`_build_prompt`） | 案例4不再给出计算结论 | ✅ 已实施（08-25 安全第二批） |
| **D5** | 数据缺口必须声明：不得用政策数字顶替用户个人数据 | F4 | 纯 prompt（`_build_prompt`） | 案例4回答含明确"缺少数据"表述 | ✅ 已实施（08-25 安全第二批） |
| **D6** | 忠实度校验先做离线评估，不做在线拦截 | 度量 | 新建评估 | 见 §4.4 | ❌ 未实施 |

**D4 是本文档投产比最高的一条**：纯 prompt 改动，对应一个已确认复现的真实缺陷。

### 4.4 D6 的前置依赖（已核查，比原估计更重）

核查了 `tests/fixtures/golden_test_set_tenant_kb.json`（12 条）：

| 类别 | 条数 | 覆盖 |
|---|---|---|
| `permission` | 4 | ✅ **质量高**，覆盖四种身份边界 |
| `negative`（KB 无答案） | 1 | ✅ 有 |
| factual / boundary / nuance / regression | 5 | 正向事实题 |
| ui-regression | 1 | 打招呼 |
| **跨主题无关联** | **0** | ❌ **零覆盖** |

**两个硬缺口**：

1. **没有"跨主题无关联"用例**——即 F4 那一类，当前零覆盖。
2. **断言方式结构上抓不到 F4**——现在用 `expect_answer_contains_any`（关键词匹配）。
   而案例4的错误回答里，"每月最多8天""工龄满15年"这些正确关键词**全都有**，
   它是在正确材料之上多加了一条编造的因果链。
   **关键词匹配只能检查"该说的说了没有"，不能检查"说的都有依据吗"。**

**因此 D6 拆成两步**：

- **D6a**：补 3–5 条"跨主题无关联"用例，断言仍用关键词（断言**不出现**计算结论）。
  这一步立刻能验收 D4/D5，不必等完整评估体系。
- **D6b**：真正的 faithfulness 评估（逐句核对论断有无材料支撑），需额外模型调用，放后面。

规模上 12 条距业界建议的 50–200 条尚远，但**先补对类别比先补数量重要**。

### 4.5 D1 / D2 实施记录（2026-08-25 第三批）

**改了哪三处**

| 改动 | 位置 | 性质 |
|---|---|---|
| 拆分提示词加入依赖性判据 + 两条"有依赖不拆"反例 + 上限写进提示词 | `intent.py` 的 `analyze_query` 与 `analyze_and_route` 共用的 `_SUB_QUERY_SPLIT_RULES` | prompt |
| 确定性依赖判据 `_detect_sub_query_dependency` / `_finalize_sub_queries` | `intent.py` | 纯字符串规则，零 LLM 调用 |
| 扇出截断（唯一一处） | `workflow.py::_retrieve_multi` | 一处截断 + trace |

**为什么 D1 不只改 prompt**：线上意图分类跑的是 `qwen2.5-1.5b-router`，
**纯 prompt 约束在 1.5b 上没有保证**。判据是三类信号：
① 子查询里残留回指词（它/他/其/该/上述/这些/its…，均带否定环视避开
"其他/因此/应该/这个月/尤其"）；② 整句出现"先…再…""根据上面的结果"等依赖链
连接词；③ 整句/子查询里带实体识别问（谁/哪个部门/最…的）而模型仍拆成多问。
命中任一条 → **保留完整原问题**（不是取第一个子查询，那会丢掉用户后半句诉求）。

**为什么截断点放在 `_retrieve_multi` 而不是 intent**：只有执行处才能保证
"实际只发起 3 次检索"，也只有那里能把"丢弃了哪几条"同时写进 `_emit_trace`
（TracePanel 实时可见）和 `trace_events`（随 state 落库，非流式路径也有）。
`intent.py` 里**刻意不截断**，否则 trace 里看不到丢了什么。
上限常量 `MAX_SUB_QUERY_FANOUT = 3` 定义在 `intent.py`、由 `workflow.py` 导入，
保证"提示词里写的上限"和"代码里截断的上限"是同一个数字。

**实测数据**

| 项 | 修复前 | 修复后 |
|---|---|---|
| D1｜注入"旧模型那种拆法"的 8 条依赖型问题（4 条回指 + 4 条先定实体再查属性） | 8/8 被并行拆分 | **0/8** |
| D1｜6 条真正独立的多主题问题（含提示词自带的三个示例） | 6/6 保持拆分 | **6/6 保持拆分**（无误伤） |
| D1｜真实 `qwen2.5-1.5b-router`，4 条"用和/与连接但后半句依赖前半句" | 3/4 被并行拆分 | **1/4** |
| D2｜6 个子查询的问题实际发起的检索次数 | 6 次 | **3 次**，且 trace 两处均有截断记录 |

**已知边界（D1）**：判据只看得见"回指词还留在子查询里"的形态。
真实模型上剩下的那 1/4 是模型**自己把回指词消解掉之后**再拆的
（"年假制度和它的申请流程" → `["年假制度是什么", "请假制度是什么"]`，连消解都消解错了），
字面上不再有任何依赖信号，这一层**抓不到**。
另外 D1 只对 `intent_type=rag` 分支有实际影响——`tool`/`workflow`/`clarify`
三个分支在 `_intent_node` 里本来就会把 `sub_queries` 收窄成整句。

**回归**：`tests/unit/test_sub_query_dependency_and_fanout.py`（53 条，确定性、
用假 LLM 注入"修复前那种拆法"）+ `tests/e2e/test_d1_real_router_split.py`
（2 条，真实 1.5b-router，`-m llm` 手动跑，Ollama 不在就跳过）。
判别力已验证：把 `intent.py` 换回修复前版本，8 条依赖型全部拆成 2 个；
把扇出上限判断改成恒假，D2 的 4 条立刻变红。

---

## 五、B 部分：记忆/归档节点异步化

### 5.1 问题

`app.py::chat_stream`（约 2701 行起）的 `done` 事件必须等 `run_stream()` 内整个 `graph_task`
（`session → intent → retrieve/tool → generate → memory_manage → archive`）跑完才 yield。
而 `_memory_manage_node`（`workflow.py:1501`）触发压缩时调用
`_memory_manager.compact(..., llm=self._llm)`——用 7b 做一次历史摘要，本身要数秒，
排在 `generate` 之后、`done` 之前。用户已看完全部回答，还要干等约 8 秒。

`archive` 本身很快，但图是"一条到底"，它也被拖进等待窗口。

### 5.2 方案：`interrupt_after` + 后台续跑

把"用户可见的完成"和"整张图真正跑完"拆两段：

- **前台段**（决定 `done` 时机）：`session → intent → retrieve/tool_subgraph → generate`
  （`clarify`/`workflow` 两条路径直接跳到 `memory_manage`，不经 `generate`）
- **后台段**（用户不可见）：`memory_manage → archive → END`

**步骤**：

1. 编译图时加 `interrupt_after=["generate", "clarify", "workflow"]`
   —— 这三个都是通往 `memory_manage` 的入口，**三个都要列全，漏一个就有路径会跑到底**
2. `graph_task` 跑到其一执行完后自动暂停返回当前 state（LangGraph 内建 checkpoint/interrupt）
3. 用暂停时刻的 state 立刻拼 `done` yield 给客户端。此时 `kb_sources` / `used_model` /
   `trace_events` / `active_workflow` 均已就绪，唯独 `memory_stats` 是**压缩前**数值（见 Q5）
4. 另起后台任务 `self._compiled.ainvoke(None, config)`（同 `thread_id`/checkpointer，
   `None` 表示从上次 checkpoint 继续），跑完 `memory_manage → archive → END` 并落盘
   —— **必须先处理 §3.3 的队列继承和 §3.4 的强引用**
5. 后台任务独立 `try/except` 并打日志，失败不影响本次请求（`done` 已发出）
6. 与现有"客户端断开回滚脏 checkpoint"逻辑（`_trim_checkpoints` / `interrupted`）不冲突：
   现有 `interrupted` 判断只发生在 token 流式阶段（`done` 之前）

### 5.3 B 的已知风险

| # | 风险 | 处置 |
|---|---|---|
| **B-R1** | **同 conversation 连续两条消息**：上一轮后台续跑未落盘，下一条消息就进来，两个 `ainvoke()` 同时操作同一 `thread_id` 的 checkpoint | ✅ **已实测（§5.4）**：无任何保护，静默丢更新。**处置：加 per-`thread_id` 锁。阻塞解除。** |
| B-R2 | `done` 里 `memory_stats` 滞后一步 | 需用户确认可接受（Q5） |
| B-R3 | 进程崩溃在"已发 done、后台未完成"窗口 → 该轮压缩静默跳过 | 不损坏数据，可接受降级，但必须写进已知限制 |
| **B-R4** | 后台任务继承非空流式队列（§3.3） | **本次合并新增**，实施时必须处理 |
| **B-R5** | 后台任务无强引用可能被 GC（§3.4） | **本次合并新增**，复用 `self._background_tasks` |
| **B-R6** | 后台任务的 config **不带 `checkpoint_id`**，语义是"从该 thread 的最新 checkpoint 继续"，不是"从我暂停的那个点继续" | ✅ **已实测（§5.4 结论4）**：turn2 抢先落盘时，后台任务会去跑 **turn2** 的 state。**必须显式传暂停点的 `checkpoint_id`。** |

---

### 5.4　B-R1 实测结论（2026-08-25，**阻塞项已解除**）

> **状态：已验证通过。** 探针脚本 `scripts/probe_checkpointer_concurrency.py`，可复跑。
> 环境：`langgraph 1.2.11` + `langgraph-checkpoint-postgres 3.1.2`，Postgres 本地实例，
> 用 `app.py::create_checkpointer` 同款建连参数（单条 `AsyncConnection` + autocommit +
> `prepare_threshold=0` + `dict_row`）。全部场景用 `asyncio.gather` + `asyncio.Barrier`
> 强制真并发；测试 thread_id 带 `probe-cc-` 前缀，跑完清理，已核对零残留。

#### 结论 1　并发写同一 `thread_id`：不报错、不检测冲突、**静默丢更新**

两个 `ainvoke()` 真并发写同一 `thread_id`（S1）：

| 观察项 | 实测结果 |
|---|---|
| 异常 | **0 个**。两个 `ainvoke()` 都正常返回，各自返回值看起来都对 |
| 冲突检测 | **无**。没有任何版本冲突、乐观锁失败、重试 |
| 落库结果 | `checkpoints` 表 10 行，出现 **1 个分叉点**（同一父 checkpoint 挂了两个子） |
| 最终 state | 只剩 A 一侧：`['A:start','A:front','A:memory','A:archive']`，**B 整条 run 的更新从"最新"视角消失** |
| 累加通道 | 用 `operator.add` 语义的 list 通道也救不了——它只在**同一条分支内**累加 |
| 计数通道 | 加锁时 `counter==2`，不加锁 `counter==1`——丢更新的量化证据 |

机制：两条 run 各自 `aget_tuple` 读到**同一个基线**，各自算出新 checkpoint（`checkpoint_id`
是新生成的时间序 UUID），各自 `aput` 写成**两条并列的分支**。
`aget_tuple`/`alist` 的排序键是 `checkpoint_id DESC`（`base.py::SELECT_SQL`），
所以"最新" = **时间上最后写完的那条**，另一条分支静静躺在表里再也不会被读到。

**这是最坏的一档失效**：不抛异常、不写日志、单人测试永远正常。与 A 的 F1 完全同类。

#### 结论 2　checkpoint 表**没有**可用于乐观锁的版本列

```
checkpoints:  thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id,
              type, checkpoint(jsonb), metadata(jsonb)
              PK = (thread_id, checkpoint_ns, checkpoint_id)
```

- **没有** `version` / `seq` / 更新时间 一类的行版本列
- `checkpoint_blobs.version` **不是**行版本，是每个 channel 的内容版本，
  格式 `{递增整数:032}.{随机数:016}`（`base.py::get_next_version`）。
  带随机后缀 ⇒ 两条并发分支不会撞进同一行 ——
  **好消息：blob 不会被对方内容污染**，坏的只是"哪条分支算最新"
- `UPSERT_CHECKPOINTS_SQL` 是 `ON CONFLICT (pk) DO UPDATE SET ...`，
  **无条件覆盖**，没有 `WHERE version = expected` 这类 CAS

⇒ 想做乐观锁，得自己加列 + 改 saver，**成本远高于加一把锁**。

#### 结论 3　LangGraph 自带的 `lock` 保护的不是这件事

`langgraph/checkpoint/postgres/aio.py`：

```python
line  59   self.lock = asyncio.Lock()
line 374   async with self.lock, _ainternal.get_connection(self.conn) as conn:
```

- 这把锁在 `_cursor()` 里获取，作用域 = **单次 DB 操作**（一次 `aput` / 一次 `aget_tuple`）
- 它是**实例级**的，不是 per-`thread_id` 的——全进程所有会话共抢一把
- 源码注释写明了它的目的："a connection not in pipeline mode can only be used by one
  thread/coroutine at a time" ——它保护的是 **psycopg 连接/游标不被并发踩坏**，
  **不是**"同一 thread_id 的逻辑状态不被并发覆盖"
- 本项目 `create_checkpointer()` 传的是**单条 `AsyncConnection`（非连接池）**，
  所以这把锁事实上把全进程的 checkpoint 读写都串行化了——
  这是一个独立于本议题的**吞吐瓶颈**（见§5.5 未覆盖范围），但它依然拦不住结论 1

⇒ **LangGraph 没有提供任何 per-`thread_id` 的并发写保护。**

#### 结论 4　⚠️ 比 B-R1 更隐蔽的一条：后台任务的 config 会"认错人"

B 草案 §5.2 第 4 步写的是 `self._compiled.ainvoke(None, config)`，而 `run_stream`
里的 config 是 `{"configurable": {"thread_id": thread_id}}`，**不带 `checkpoint_id`**。
它的语义不是"从我暂停的那个点继续"，而是"**从该 thread 的最新 checkpoint 继续**"。

实测（S2X）：让 turn2 抢先落盘，后台任务稍后才被调度到——

```
turn1 暂停点 marks = ['T1:start', 'T1:front']
后台续跑实收 marks = ['T1:start', 'T1:front', 'T2:start', 'T2:front', 'T2:memory', 'T2:archive']
```

**后台任务跑的是 turn2 的 state**。后果是双重的：
1. turn1 那一轮的压缩/归档**永远不会执行**（B-R3 描述的降级，在这里无需崩溃就会发生）
2. turn2 被一个本该属于上一轮的后台任务**提前推进**到 `archive`

⇒ 记为 **B-R6**。修复与加锁**正交**——即使加了 per-`thread_id` 锁，
也必须显式传 `{"configurable": {"thread_id": tid, "checkpoint_id": <暂停点 id>}}`。

#### 结论 5　"丢的是哪一侧"取决于时序，不是固定规则

同一套 B 时序，只改相对延迟（S2 的两个变体）：

| 变体 | turn1 压缩落盘 | turn2 落盘 | 丢的是 |
|---|---|---|---|
| 后台先写完 | ✅ | ❌ | **用户的下一条消息整轮消失** |
| turn2 先写完 | ✅ | ✅ | 无（turn2 读到了压缩后的 state，侥幸安全） |
| 后台先写完 **+ per-thread_id 锁** | ✅ | ✅ | 无 |

第二行的"安全"是**侥幸**，不是保证：它安全只因为 turn2 的读恰好发生在后台写之后。
危险窗口就是"turn2 在后台落盘前读基线"。

**第一行是本调研最该被记住的一条**：丢的不是一次可有可无的记忆压缩，
而是**用户刚发的那条消息连同整轮回答**。B 部分若不加保护直接上线，
这就是一个用户可见的"我刚说的话不见了"级别的缺陷。

#### 结论 6　per-`thread_id` 锁能消除该现象（对照组实测）

S4 与 S2c 两个对照组：在 `ainvoke()` 外层加 per-`thread_id` 的 `asyncio.Lock` 串行化后，

- 两侧更新全部保留（`counter` 从 1 变成 2）
- `checkpoints` 历史链**恢复线性，无分叉**
- 代价：两侧被串行化，墙钟耗时约翻倍（0.44s → 0.86s）——
  但这只影响**同一个 conversation 内**的重叠请求，跨会话不受影响

#### 处置方案对比与推荐

| 方案 | 能否解决 | 代价 | 判断 |
|---|---|---|---|
| **A. per-`thread_id` `asyncio.Lock`** | ✅ 实测消除（结论 6） | 同会话内串行化；需管理 lock 字典的生命周期（用完清理，否则内存随会话数增长） | ⭐ **推荐** |
| B. 乐观锁（加版本列 + CAS） | 理论可行 | 要改表结构**并**继承重写 `AsyncPostgresSaver`；还要设计冲突后的重试/合并语义 | ❌ 成本与收益不成比例，且是自维护分叉 |
| C. 后台任务用**独立 `thread_id`** | 绕开冲突 | 但 `memory_manage` 就是要写回本会话的记忆，换 thread 等于压缩结果落不到该落的地方 | ❌ 与需求矛盾 |
| D. 后台续跑前先等上一轮任务完成（在 `run_stream` 入口 await 上一轮的 task） | ✅ 等价于方案 A 的另一种写法 | 需要维护 `thread_id → task` 映射，语义上比锁更绕 | 可接受的备选 |
| E. 什么都不做，赌窗口很窄 | ❌ | 窗口 = 一次 7b 摘要 ≈ 8s，用户连发消息是常见操作，**这个窗口一点都不窄** | ❌ |

**推荐：方案 A + 结论 4 的 `checkpoint_id` 显式化，两者都要做。**

实施要点：
1. `RAGWorkflow` 上加 `self._thread_locks: dict[str, asyncio.Lock]`
   （或 `WeakValueDictionary`，避免长期运行后字典无限增长）
2. `run_stream()` 全程（含前台段）持有该 `thread_id` 的锁；
   后台续跑任务**也**要拿同一把锁——否则锁形同虚设
3. 后台任务的 config 必须带上暂停时刻的 `checkpoint_id`（B-R6）
4. 锁只覆盖同一 `thread_id`，**不能**退化成全局锁——那会把跨会话并发也串起来
5. 单进程 `asyncio.Lock` 的前提是**单进程部署**。多 worker/多副本下失效，
   届时需要换成 Postgres 咨询锁（`pg_advisory_xact_lock`）——见 §5.5

### 5.5　本次实测**未覆盖**的范围

诚实记录边界，避免这份结论被当成比它实际更强的保证：

1. **未用真实 `RAGWorkflow` 跑**。探针用的是同一个 `AsyncPostgresSaver` + 同款建连参数
   驱动的最小三节点图。被测对象是 checkpointer 的并发语义，这个替换在该问题上是等价的；
   但**真实图的节点更多、写入更大、`messages` 通道带 `RemoveMessage` 语义**，
   实际实施后仍需一条走真实图的回归测试（§9 验收第 5 条）。
2. **未测多进程/多 worker**。全部结论都在单进程单事件循环下取得。
   推荐方案 A 的 `asyncio.Lock` **只在单进程内有效**；若将来上多 worker，
   结论 1 的丢更新会重新出现，且 `asyncio.Lock` 拦不住。
3. **未测连接池**。本项目当前用单条 `AsyncConnection`。换成 `AsyncConnectionPool` 后，
   结论 3 里"实例级锁事实上串行化了全进程"这一点会改变（并发度提高），
   但结论 1 的丢更新**只会更容易触发**，不会消失。
4. **未量化真实场景下的窗口命中率**。只证明了窗口存在且行为是丢更新，
   没有统计生产流量里"用户在 8s 内连发下一条"的实际比例。
5. **未测 `_trim_checkpoints` 与并发写的交互**。`app.py` 的断连回滚逻辑会
   `DELETE FROM checkpoints WHERE checkpoint_id != %s`，在分叉存在时它会删掉什么、
   会不会把另一条分支连带删掉，本次没有验证。**这是一条独立的待查项。**
6. **未测 `checkpoint_ns` 非空的情况**（工具子图的子命名空间）。全部场景都在默认 `ns=''` 下。
7. **未做压力/长时测试**。每个场景只跑两个并发 run，不是 N 路高并发。

---

## 六、跨方案的共同约束

### 6.1 P0 契约（两份方案都必须遵守）

`workflow.py` 顶部的模块级 contextvars（`_CURRENT_TOKEN_QUEUE` / `_CURRENT_TRACE_QUEUE`）
是 2026-08-24 P0-1 的修复产物，**任何改动 `run_stream()` 的方案都受它约束**：

- `self._token_queue` / `self._trace_queue` 是**只读 property**，不能赋值（会 `AttributeError`，有测试拦）
- `_CURRENT_*.set(...)` **必须在** `asyncio.create_task(self._compiled.ainvoke(...))` **之前**，
  子任务靠 `create_task` 复制"此刻"的 context 拿到本次请求的队列，顺序反了就拿到空队列
- `finally` 清理必须用 `set(None)` 而**不是** `ContextVar.reset(token)`
  —— 异步生成器清理时所处上下文未必是当初 `set()` 的那个，`reset()` 跨上下文会抛
  `ValueError`，反而盖掉真正的退出原因
- **任何改动都必须过 `tests/unit/test_workflow_stream_isolation.py`（9 条，全真并发）**

### 6.2 共用的测试纪律

按 `CLAUDE.md` 的硬性规则，两份方案共用：

- **并发缺陷必须用并发方式验证**，串行跑 N 条不构成并发测试
- 涉及权限/并发的改动**必须同时提交 `tests/` 下的测试**
- 测试脚本落仓库，**禁止写临时目录后丢弃**
- 交付必须回答三句话：验收怎么做 / 回归怎么保 / **什么没做**

### 6.3 明确不做的（避免被文章带偏）

| 方案 | 不做的理由 |
|---|---|
| 多 agent 并行推理 | 合并质量难保证，企业问答生产采用率低。微软官方立场：standard RAG 处理单索引单次检索已足够 |
| self-consistency（采样 N 次投票） | 成本乘 N，本地小模型延迟已吃紧 |
| HyDE | 25–60% 额外延迟，且在专有术语的内部文档上因幻觉漂移**降低**召回——正是企业 KB 的典型语料 |
| 推测解码等推理加速 | 属吞吐/延迟优化，与"思维错乱"无关，不要混为一谈 |

---

## 七、合并后的实施顺序

| 阶段 | 内容 | 性质 | 前置 | 归属 |
|---|---|---|---|---|
| **1** | **D4 + D5** | 纯 prompt | 无 | A |
| **2** | **D6a**（补跨主题负样本用例） | 新增测试数据 | 无 | A |
| ~~3~~ | ~~D1 + D2~~ | prompt + 一处截断 | ~~Q1 拍板~~ | A ✅ **已完成**（2026-08-25，§4.5） |
| ~~4~~ | ~~**B-R1 并发行为查清**~~ | ✅ **已完成 2026-08-25**（`scripts/probe_checkpointer_concurrency.py`，结论见 §5.4） | — | B |
| 5 | B 主体（`interrupt_after` + 后台续跑 **+ per-`thread_id` 锁 + 后台任务显式传 `checkpoint_id`**） | **结构性改动** | ~~阶段 4~~ 已具备；仅剩 Q5 | B |
| 6 | D3 归属标注 | 改两处输出格式 | Q4 拍板 | A |
| 7 | D6b faithfulness 评估 | 新建评估链路 | 阶段 2 | A |

**排序理由**：

- 阶段 1、2 一起做，**D4/D5 改完立刻有 D6a 验收**，形成一个完整的"发现→方案→修复→复测"闭环
  （这正是复盘文档指出的、项目一直缺的东西）
- ~~阶段 4 是只读调研，可与阶段 1–3 并行，不占写权限~~ —— 已完成，未占写权限
- **阶段 5 是全表唯一的结构性改动。原阻塞项 B-R1 已实测查清（§5.4），阻塞解除；
  但结论是"并发不安全"，所以阶段 5 的范围**扩大**了：必须连同 per-`thread_id` 锁
  与 B-R6 的 `checkpoint_id` 显式化一起实施，不能只做 `interrupt_after` + 后台续跑**
- 阶段 6 影响面最大且触及当前由另一会话持有的文件，放最后

**写权限协调**：A 部分改 `intent.py` / `_build_prompt`，B 部分改 `run_stream` / `_build_graph` / `app.py`。
两边文件集基本不相交，但都在 `workflow.py` 内，**同一时刻仍应只有一个会话持有该文件的写权限**。

---

## 八、待拍板的问题

**A 部分**

- ~~**Q1　子查询扇出上限取 3 还是 5？**~~ —— **已拍板（2026-08-25）：取 3。**
  理由：当前没有评估体系，无法验证放宽到 5 是变好还是变坏，属于盲改。
  等 D6a/D6b 建起来后再用数据决定是否放宽。
  已落地为 `intent.MAX_SUB_QUERY_FANOUT = 3`，唯一截断点在 `workflow.py::_retrieve_multi`（§4.5）。
- ~~**Q2　依赖型问题不拆分后，交给 ReAct 多轮还是降级单查询？**~~
  —— **已拍板（2026-08-25）：降级为单查询，交给已有的 ReAct 子图（`max_iterations=5`）
  自己判断要不要再查一轮，不做显式多跳分解。**
  理由：微软 Azure RAG 参考架构里 standard RAG 走固定序列已足够，只有多步推理/
  动态选源/运行时查询分解才需要升级到 agentic RAG；而业界基准把"agentic RAG /
  复杂多步编排"列在**"大厂特有、不必跟"**（`docs/review_2026-08-24/review_industry_baseline.md`）。
  本项目已有 ReAct 子图，**用已有的，不新增编排复杂度**。
  代价（明确接受）：ReAct 自己也会累积多来源材料（P5），F2/F4 的暴露面从 P4 挪到了 P5，
  P5 的归属标注属于 D3，尚未做。
- ~~Q3　golden set 是否含负样本~~ —— **已核查，见 §4.4**
- **Q4　D3 归属标注本阶段做不做？**
  建议先只做 D4/D5，观察效果再定；且相关文件当前由另一会话持有写权限。

**B 部分**

- **Q5　`done` 事件里 `memory_stats` 滞后一步是否可接受？**
  只影响本轮展示的统计数字，不影响下一轮实际行为（下一轮会读到压缩后的真实 state）。
- ~~**Q6　B-R1 采用哪种处置？**~~ —— ✅ **已解决（2026-08-25，实测，见 §5.4）**

  **查出来的行为**：`AsyncPostgresSaver` 对同一 `thread_id` 的并发写
  **不报错、不做任何冲突检测**。两条 run 各读各的基线、各写各的 checkpoint 分支，
  `aget_tuple` 按 `checkpoint_id DESC` 取时间上最后写完的那条，
  **另一条分支的更新静默丢失**。checkpoint 表**没有**可用于乐观锁的版本列；
  `AsyncPostgresSaver.lock` 是实例级的、只覆盖单次 DB 操作，
  保护的是 psycopg 连接不被并发踩坏，**不是**逻辑状态。
  最坏情形实测已复现：**丢的是用户刚发的那条消息连同整轮回答**，不只是记忆压缩。

  **结论：并发不安全，必须加保护。**

  **推荐处置（两条都要做）**：
  1. **per-`thread_id` `asyncio.Lock`**——对照组实测能完全消除（结论 6）。
     前台段与后台续跑任务必须持同一把锁；锁按 `thread_id` 分，不能退化成全局锁。
  2. **后台任务显式传暂停点的 `checkpoint_id`**——见 §5.4 结论 4（新记为 **B-R6**）。
     这与加锁**正交**，只加锁不改 config 仍然会让后台任务跑错 turn 的 state。

  乐观锁方案已评估并否决：要改表结构 + 重写 saver，成本与收益不成比例（§5.4 处置对比表）。

  **前提**：`asyncio.Lock` 只在单进程内有效。多 worker 部署时须换 Postgres 咨询锁
  （`pg_advisory_xact_lock`）。当前是单进程，方案 A 成立。详见 §5.5 未覆盖范围。

---

## 九、验收定义

本文档闭环需要下列全部成立：

**A 部分**
1. 案例4（`hallu_multihop_combine`）原样提问不再产生编造的计算结论，且用例进入 `tests/` 可自动复现
2. ✅ **已达成**：依赖型问题不再被并行拆分，测试覆盖见 `tests/unit/test_sub_query_dependency_and_fanout.py`（53 条）
3. 有可重复运行的 faithfulness 评估，能给出 D4/D5 改动前后的对比数字

**B 部分**
4. `done` 在 `memory_manage`/`archive` 完成前发出，且后台任务确实把压缩结果落盘
5. **真并发测试**：同一 `thread_id` 快速连续两条消息，第二条在第一条后台续跑完成前发出，
   验证无状态错乱或 checkpoint 冲突。
   **判定标准（据 §5.4 实测细化）**：不能只看"没报错"——实测证明这类失效**从不报错**。
   必须断言：(a) turn1 的压缩结果落盘；(b) turn2 的消息与回答落盘；
   (c) `checkpoints` 表该 thread 下**无分叉点**（不存在被两个子 checkpoint 共享的父）
6. 测试断言后台续跑期间不向本次请求的流式队列写入任何内容（§3.3）
7. **（新增，对应 B-R6）** 断言后台续跑任务恢复的是**它自己那一轮**的暂停点：
   构造"turn2 抢先落盘"的时序，验证后台任务不会去跑 turn2 的 state（§5.4 结论 4）
8. **（新增）** `scripts/probe_checkpointer_concurrency.py` 的 S4/S2c 对照组语义
   应有对应的单元测试落进 `tests/`——探针脚本是调研工具，不是回归防线

**共同**
9. `tests/unit/test_workflow_stream_isolation.py` 9 条仍全绿
10. `docs/optimization_tracking.md` 的"优化后"一栏基于同一套基准统一填写，不各报各的数

**在上述全部达成之前，本文档头部状态必须保持"设计草案，未实施"。**
