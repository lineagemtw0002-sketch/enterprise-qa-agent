# 编排层设计方案（合并版）

> **状态：设计草案，未实施。零代码改动。**
> 落地后必须回来更新本行状态与 `CLAUDE.md`。
>
> 创建：2026-08-25
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
| 并发安全 | F1 已修（contextvars） | 风险1（同 thread_id 写 checkpoint） | **不同轴，见 §3.1** |
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
- B 的风险1（未解决）：**同一 thread_id 的 checkpoint 并发写** → 上一轮后台续跑未落盘时下一条消息就进来

两者不是同一个 bug，但**是同一类失效模式**：共享可变状态在并发下无保护。
共同特征也一致——**不产生任何可观察的异常输出，单人测试永远正常**。

**协调结论**：
- B 的风险1 必须在实施前查清 `AsyncPostgresSaver` 对同一 `thread_id` 并发写入的实际行为
  （报错 / 静默覆盖 / 有版本冲突检测），或直接加 per-`thread_id` 锁
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
| P4 | 多子查询扇出 | `workflow.py:1159` | **带标签**拼接 | ⚠️ 拆分不判依赖、无扇出上限 |
| P5 | ReAct 多轮累积 | `max_iterations=5` | 累积进对话上下文 | ⚠️ 同 P3 |

**P4 做对了一处，要保留**：`workflow.py:1169` 用 `[子查询: {q}]` 标注每段材料来源，
用 `---` 分隔，provenance 没丢。**不要在重构中被简化成 `"\n".join()`。**

### 4.2 四种错乱形态

| 形态 | 机制 | 路径 | 现状 |
|---|---|---|---|
| F1 状态串扰 | per-request 状态放共享对象 | P1 | ✅ 已修 + 有测试 |
| F2 上下文污染 | 多来源材料拍平，分不清归属 | P3、P5 | ⚠️ P4 已防，P3/P5 未防 |
| F3 假并行 | 有依赖的子问题被并行执行 | P4 | ❌ 未防 |
| F4 合并期编造关系 | 无关材料被强行关联出因果/换算 | P3、P4、P5 | ❌ **已实际发生** |

**F4 已复现的案例**（`security_prompt_injection_test_report.md` 案例4）：

> 问："结合年假制度和远程办公政策，如果我今年请了20天年假，还能申请多少天远程办公？"
> 答（节选）："……本年度剩余的年假天数为0天……因此您将无法再申请远程办公天数。"

两份材料**各自都被正确召回、内容都没错**。错在模型编造了原文不存在的因果链。

> **更正备案**：先前口头判断该案例"被拆成两个独立子查询"不准确。核对后确认它
> `intent_type=tool`、`target_tool=query_knowledge_hub`，走工具路径（P3/P5），
> **未经过 P4 的子查询拆分**。结论不变，机制归属更正。

### 4.3 设计决策 D1–D6

| # | 决策 | 防 | 改动性质 | 验收 |
|---|---|---|---|---|
| **D1** | 子查询拆分必须先判断依赖性；有依赖不拆，交后续多轮处理 | F3 | 纯 prompt（`intent.py`） | 依赖型问题断言 `len(sub_queries)==1` |
| **D2** | 子查询扇出设硬上限（建议 3，见 Q1） | F2/F4 | prompt + 一处截断 | 6 子查询问题只执行 3 个且有 trace 记录 |
| **D3** | 统一材料归属标注：P4 保持现状；P3 补来源标注；P5 按轮次分组 | F2 | 改两处输出格式 | 跨主题提问的 prompt 中两组材料可区分 |
| **D4** | **禁止跨材料编造关系**：除非材料明确陈述，不得推导跨材料因果/加减/换算 | F4 | 纯 prompt（`_build_prompt`） | 案例4不再给出计算结论 |
| **D5** | 数据缺口必须声明：不得用政策数字顶替用户个人数据 | F4 | 纯 prompt（`_build_prompt`） | 案例4回答含明确"缺少数据"表述 |
| **D6** | 忠实度校验先做离线评估，不做在线拦截 | 度量 | 新建评估 | 见 §4.4 |

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
| **B-R1** | **同 conversation 连续两条消息**：上一轮后台续跑未落盘，下一条消息就进来，两个 `ainvoke()` 同时操作同一 `thread_id` 的 checkpoint | **实施前必须查清 `AsyncPostgresSaver` 的并发写行为，或加 per-`thread_id` 锁。这是阻塞项。** |
| B-R2 | `done` 里 `memory_stats` 滞后一步 | 需用户确认可接受（Q5） |
| B-R3 | 进程崩溃在"已发 done、后台未完成"窗口 → 该轮压缩静默跳过 | 不损坏数据，可接受降级，但必须写进已知限制 |
| **B-R4** | 后台任务继承非空流式队列（§3.3） | **本次合并新增**，实施时必须处理 |
| **B-R5** | 后台任务无强引用可能被 GC（§3.4） | **本次合并新增**，复用 `self._background_tasks` |

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
| 3 | D1 + D2 | prompt + 一处截断 | Q1 拍板 | A |
| 4 | **B-R1 并发行为查清** | 只读调研 | 无 | B |
| 5 | B 主体（`interrupt_after` + 后台续跑） | **结构性改动** | 阶段 4 结论 + Q5 | B |
| 6 | D3 归属标注 | 改两处输出格式 | Q4 拍板 | A |
| 7 | D6b faithfulness 评估 | 新建评估链路 | 阶段 2 | A |

**排序理由**：

- 阶段 1、2 一起做，**D4/D5 改完立刻有 D6a 验收**，形成一个完整的"发现→方案→修复→复测"闭环
  （这正是复盘文档指出的、项目一直缺的东西）
- 阶段 4 是只读调研，可与阶段 1–3 并行，不占写权限
- **阶段 5 是全表唯一的结构性改动，且有阻塞项（B-R1），放在验证清楚之后**
- 阶段 6 影响面最大且触及当前由另一会话持有的文件，放最后

**写权限协调**：A 部分改 `intent.py` / `_build_prompt`，B 部分改 `run_stream` / `_build_graph` / `app.py`。
两边文件集基本不相交，但都在 `workflow.py` 内，**同一时刻仍应只有一个会话持有该文件的写权限**。

---

## 八、待拍板的问题

**A 部分**

- **Q1　子查询扇出上限取 3 还是 5？**
  建议 3——当前没有评估体系，无法验证放宽到 5 是变好还是变坏，属于盲改。
  等 D6a/D6b 建起来后再用数据决定是否放宽。
- **Q2　依赖型问题不拆分后，交给 ReAct 多轮还是降级单查询？**
  ReAct 能力更强（可多跳）但更慢，且它自己也会累积多来源材料（P5），等于把问题从一处挪到另一处；
  降级简单但损失多跳能力。**这条取决于目标用户是否真会问多跳问题——回到那个尚未回答的
  "交付给谁 / 怎么用"。**
- ~~Q3　golden set 是否含负样本~~ —— **已核查，见 §4.4**
- **Q4　D3 归属标注本阶段做不做？**
  建议先只做 D4/D5，观察效果再定；且相关文件当前由另一会话持有写权限。

**B 部分**

- **Q5　`done` 事件里 `memory_stats` 滞后一步是否可接受？**
  只影响本轮展示的统计数字，不影响下一轮实际行为（下一轮会读到压缩后的真实 state）。
- **Q6　B-R1 采用哪种处置？**
  先查清 `AsyncPostgresSaver` 实际行为再定，还是直接加 per-`thread_id` 锁兜底？

---

## 九、验收定义

本文档闭环需要下列全部成立：

**A 部分**
1. 案例4（`hallu_multihop_combine`）原样提问不再产生编造的计算结论，且用例进入 `tests/` 可自动复现
2. 依赖型问题不再被并行拆分，有测试覆盖
3. 有可重复运行的 faithfulness 评估，能给出 D4/D5 改动前后的对比数字

**B 部分**
4. `done` 在 `memory_manage`/`archive` 完成前发出，且后台任务确实把压缩结果落盘
5. **真并发测试**：同一 `thread_id` 快速连续两条消息，第二条在第一条后台续跑完成前发出，
   验证无状态错乱或 checkpoint 冲突
6. 测试断言后台续跑期间不向本次请求的流式队列写入任何内容（§3.3）

**共同**
7. `tests/unit/test_workflow_stream_isolation.py` 9 条仍全绿
8. `docs/optimization_tracking.md` 的"优化后"一栏基于同一套基准统一填写，不各报各的数

**在上述全部达成之前，本文档头部状态必须保持"设计草案，未实施"。**
