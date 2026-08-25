# 记忆管理/归档节点异步化设计（草案，未实施）

> # ⛔ 本文档已被取代，请勿据此实施
> **2026-08-25 起，本文内容已合并进 [`docs/orchestration_design.md`](orchestration_design.md)（B 部分）。**
> 合并版**新增了两条本文未覆盖、且实施时必须处理的风险**：
> - B-R4：后台续跑任务会继承到**非空**的流式队列（`create_task` 复制创建时刻的 contextvars），
>   导致 `_emit_trace` 往一个无消费者的队列持续写入——与本文"contextvar 已清空所以走 no-op"的假设不符
> - B-R5：后台任务需要强引用，必须复用 `self._background_tasks`，否则可能跑完前被 GC
>
> **本文仅作历史留档，不要按它改代码。**

> 状态：设计草案，**零代码改动**。用户已明确指示"先讨论方案，不动代码"。
> 按 CLAUDE.md 规则，本次改动会动到 `workflow.py`（`run_stream`）和 `app.py`（`chat_stream`）两个文件、
> 且是结构性改动，需要先出设计、用户确认后再写代码——本文档就是这一步。

## 背景与问题

用户实测反馈：一次真实对话里，回答文字已经全部通过流式 `token` 事件送达客户端，但前端的"生成中"状态又多挂了约
8 秒才结束。

根因在 `app.py::chat_stream`（约 2701 行起）：`done` 事件只有在 `workflow.run_stream()` 内部的整个
`graph_task`（覆盖 `session → intent → retrieve/tool_subgraph → generate → memory_manage → archive`）
全部跑完之后才会 yield。而 `_memory_manage_node`（`workflow.py:1501`）触发压缩时会调用
`_memory_manager.compact(messages=..., current_summary=..., llm=self._llm)`——用主力 7b 模型做一次历史消息
摘要总结，这次 LLM 调用本身就要数秒，且排在 `generate` 之后、`done` 之前，用户已经看完全部回答内容，还要
干等这次调用跑完。

`archive` 节点本身通常很快（只是把待归档消息写入归档存储），真正的耗时大头是 `memory_manage` 触发压缩时的
那次 LLM 调用；但只要图结构是"一条到底"，`archive` 也一并被拖进等待窗口。

## 必须先知道的现有约束

`workflow.py` 顶部有一套模块级 `contextvars`（`_CURRENT_TOKEN_QUEUE` / `_CURRENT_TRACE_QUEUE`），是这个项目
之前修过的一个 P0（并发请求跨用户串流：`RAGWorkflow` 全进程一个实例，`self._token_queue` 曾是实例属性，
并发请求会互相覆盖对方的流式队列）。修复之后：

- `self._token_queue` / `self._trace_queue` 现在是**只读 property**，读取的是"当前请求"对应的 contextvar 值，
  不能再被赋值（会 `AttributeError`，有专门测试拦这个）。
- `run_stream()` 里的顺序是硬约束：`_CURRENT_TOKEN_QUEUE.set(token_queue)` /
  `_CURRENT_TRACE_QUEUE.set(trace_queue)` **必须在** `asyncio.create_task(self._compiled.ainvoke(...))`
  **之前**执行——子任务（含工具子图）是靠 `create_task` 复制"此刻"的 context 拿到本次请求的队列的，顺序反了
  就会拿到上一个请求或者空队列。
- 回归测试：`tests/unit/test_workflow_stream_isolation.py`（9 条，全部是真并发用例，串行跑抓不出这个 bug）。
  **任何改动 `run_stream()` 的方案都必须过这个测试。**
- `_emit_trace()` 对 `self._trace_queue is None` 的情况是直接 no-op，不报错——这意味着如果某个节点在
  contextvar 已经被清空之后才调用 `_emit_trace`，不会抛异常，只是没人能收到这条 trace（因为客户端此时早已
  拿到 `done`，本来也不会再监听）。这一点对下面的方案很关键。
- `run_stream()` 的 `finally` 块清理 contextvar 时用的是 `_CURRENT_TOKEN_QUEUE.set(None)`，不是
  `ContextVar.reset(token)`——原因是异步生成器清理时所处的上下文未必是当初 `set()` 的那个（生成器被提前
  关闭或 GC 时尤其如此），`reset()` 跨上下文会抛 `ValueError`，反而把真正的退出原因盖掉。**任何调整这段
  `finally` 逻辑的实现都要保持 `set(None)` 的写法，不要改成 `reset(token)`。**

## 方案：LangGraph `interrupt_after` + 后台续跑

### 核心思路

把"用户可见的完成"和"整张图真正跑完"拆成两段：

1. **前台段**（决定 `done` 何时发出）：`session → intent → retrieve/tool_subgraph → generate`（或
   `clarify`/`workflow`，这两条路径直接跳到 `memory_manage`，不经过 `generate`）。
2. **后台段**（用户不可见，做记账）：`memory_manage → archive → END`。

### 具体步骤

1. 编译图时加 `interrupt_after=["generate", "clarify", "workflow"]`——这三个节点都是通往
   `memory_manage` 的入口，三个都要列全，漏一个就会有一条路径不在该暂停的地方继续跑到底。
2. `run_stream()` 里，`graph_task`（即 `self._compiled.ainvoke(initial_state, config)`）跑到这三个节点
   之一执行完后会自动暂停，返回当前 state——这是 LangGraph 内建的 checkpoint/interrupt 行为，不需要额外
   代码触发。
3. 用这个"暂停时刻"的 state 立刻拼 `done` 事件 yield 给客户端。此时 `kb_sources` / `used_model` /
   `trace_events` / `active_workflow` 等字段都已经就绪，唯独 `memory_stats`（读的是 `summary`/消息数）会是
   **压缩前**的数值——这一点需要用户确认是否可接受；个人倾向可接受，因为只影响这一轮展示的统计数字，不影响
   下一轮请求的实际行为（下一轮请求会读到压缩后的真实 state）。
4. 紧接着另起一个**不跟随本次 HTTP 请求生命周期**的后台任务：
   `asyncio.create_task(self._compiled.ainvoke(None, config))`——用同一个 `thread_id`/checkpointer，
   传 `None` 作为输入表示"不带新输入、从上次 checkpoint 继续"，把 `memory_manage → archive → END` 跑完并
   落盘。这是 LangGraph 官方支持的暂停-恢复用法，不需要手工维护第二套图结构。
5. 这个后台任务要独立 `try/except` 并打日志，失败不能影响本次请求（因为 `done` 已经发出去了）。
6. 与现有的"客户端断开就回滚脏 checkpoint"逻辑（`_trim_checkpoints`，`interrupted` 标记）天然不冲突：现有
   `interrupted` 判断只发生在 token 流式阶段（`done` 之前），`done` 发出之后的后台续跑完全走另一条路径，
   不会被这条回滚逻辑误伤，不需要额外改造。

## 风险与未决问题（动手前必须想清楚）

1. **同一 conversation 连续两条消息的并发风险——这是最关键、必须先解决的一条。**
   如果上一轮的后台续跑（`memory_manage`/`archive`）还没落盘，用户就发了下一条消息，两个 `ainvoke()` 会
   同时对着同一个 `thread_id` 操作 checkpoint。这正是这个项目已经出过 P0 的那一类问题（并发共享状态）。
   动手前必须先搞清楚 `AsyncPostgresSaver` 对同一 `thread_id` 并发写入的实际行为（报错？静默覆盖？有版本
   冲突检测？），或者直接加一个 per-`thread_id` 的信号量/锁，保证后台续跑必须在下一轮真正开始前完成。
   按 CLAUDE.md 的硬性规则："并发缺陷必须用并发方式验证"——这条不能只串行跑一遍就认为没问题，必须补一个
   真并发的回归测试（模拟"上一轮续跑还没完成、下一条消息就进来"这个场景），类比
   `tests/unit/test_workflow_stream_isolation.py` 的做法。
2. **`done` 事件里 `memory_stats` 数值滞后一步**——如上所述，需要用户确认是否接受这个展示层面的偏差。
3. **进程崩溃在"已发 done、后台续跑还没完成"这个窗口**——那一轮的压缩会被静默跳过，不会损坏数据，但也没有
   自动补跑机制。这是可接受的降级，但要写进已知限制里，不能默认忽略。
4. 需要新增的回归测试至少覆盖：
   - 单请求场景：`done` 在 `memory_manage`/`archive` 完成前发出，且后台任务确实把压缩结果落盘了。
   - 并发场景：同一 `thread_id` 快速连续两条消息，第二条在第一条的后台续跑完成前发出，验证不会出现状态
     错乱或 checkpoint 冲突。

## 结论

方案本身（`interrupt_after` + 后台续跑）技术上成立，且与现有的 contextvars 流式隔离修复不冲突。但风险 1
（同 thread_id 并发写入 checkpoint）在验证清楚、且补上对应的并发回归测试之前，不能实施。这是一个结构性
改动，按 CLAUDE.md 规则需要用户确认后才能动代码——目前用户已选择"先出设计文档"，本文档即为该设计，
**尚未实施，`workflow.py`/`app.py` 没有任何相关代码改动**。
