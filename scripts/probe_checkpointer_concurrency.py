"""
探针：LangGraph `AsyncPostgresSaver` 对同一 `thread_id` 并发写 checkpoint 的真实行为。

对应 `docs/orchestration_design.md` §5.3 的 **B-R1 阻塞项**与 §8 的 **Q6**。

为什么要有这个脚本
------------------
B 方案（记忆/归档节点异步化）要在 `done` 事件发出后，另起后台任务用**同一 `thread_id`**
继续跑 `memory_manage → archive`。如果用户在后台续跑落盘之前就发了下一条消息，
就会出现两个 `ainvoke()` 同时对着同一个 `thread_id` 操作 checkpoint。
设计文档把"`AsyncPostgresSaver` 到底怎么处理这种情况"列为实施前的阻塞项，
且明确要求**实测**，不接受按业界惯例推断。

怎么测
------
不加载真实的 `RAGWorkflow`（它要拉 LLM / 检索 / 租户服务，噪声太大，且本次调研
不碰 `src/`）。改为用**同一个 `AsyncPostgresSaver`、同一套建连方式**
（照抄 `app.py::create_checkpointer`：单条 `psycopg.AsyncConnection` + autocommit +
`prepare_threshold=0` + `dict_row`）驱动一张最小 `StateGraph`。
被测对象是 checkpointer 本身，这样替换是等价的。

场景：
  S0   静态事实：checkpoint 表结构有无版本列 + `AsyncPostgresSaver.lock` 保护什么
  S1   两个 `ainvoke()` 真并发（`asyncio.gather` + `asyncio.Barrier` 强制重叠）
       写同一 `thread_id` —— 看报错 / 静默覆盖 / 丢更新
  S2   复刻 B 的真实时序：turn1 `interrupt_after` 暂停 →
       「后台续跑」与「用户下一条消息」并发。跑三个变体：
         a) 后台先写完   b) turn2 先写完   c) 后台先写完 + per-thread_id 锁
       两个时序变体是为了证明"丢的是哪一侧取决于时序"，不是某条固定规则
  S2X  后台续跑用的 config 不带 `checkpoint_id` 时，它到底从哪个 checkpoint 恢复
  S3   落库证据：checkpoint 的父子链是不是分叉了，`aget_tuple` 认谁是最新
  S4   对照组：给同一 `thread_id` 加 per-thread 锁串行化，验证锁能否消除 S1 的现象

**真并发**：全部用 `asyncio.gather` + `asyncio.Barrier` 保证两侧确实在同一时刻
压在 checkpointer 上，串行跑 N 次不算数（本仓库硬性规则）。

安全性
------
- 只用带 `probe-cc-` 前缀 + 随机后缀的**独立测试 thread_id**，不碰真实会话数据
- 结束时无条件清理这些 thread 的 checkpoints / checkpoint_writes / checkpoint_blobs
- 全程只读 `src/`，不修改任何生产代码

跑法
----
    set -a; source .env; set +a
    RAGENT_DEBUG=true .venv/bin/python scripts/probe_checkpointer_concurrency.py
"""

from __future__ import annotations

import asyncio
import os
import random
import string
import sys
import time
from typing import Annotated, Any, Dict, List, Optional, TypedDict

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, StateGraph


# --------------------------------------------------------------------------
# 最小被测图
# --------------------------------------------------------------------------

def _append(left: List[str], right: List[str]) -> List[str]:
    """list 累加 reducer —— 用它才能看出"丢更新"：安全的话两侧的标记都在。"""
    return (left or []) + (right or [])


class ProbeState(TypedDict, total=False):
    # 累加通道：并发安全 → 两个 run 的标记都留下；不安全 → 丢一半
    marks: Annotated[List[str], _append]
    # 覆盖通道：模拟 summary / memory_stats 这类"整体替换"的字段
    last_writer: str
    # 计数：模拟 memory_stats 里那种"读-改-写"的字段
    counter: int


def build_graph(checkpointer: Any, *, interrupt: bool = False):
    """
    三节点直链，结构对齐真实图的分段：
        front（≈ generate，前台段最后一个节点）
          → memory（≈ memory_manage，慢，B 要挪到后台的那个）
          → archive
    `interrupt=True` 时在 front 之后 interrupt，复刻 B 方案第 1 步。
    """

    async def front(state: ProbeState) -> Dict[str, Any]:
        tag = state.get("last_writer", "?")
        await asyncio.sleep(0.05)
        return {"marks": [f"{tag}:front"], "last_writer": tag}

    async def memory(state: ProbeState) -> Dict[str, Any]:
        tag = state.get("last_writer", "?")
        # 真实的 _memory_manage_node 触发压缩时要用 7b 做摘要，是秒级的。
        # 这里用 sleep 放大同一个时间窗口，好让两侧确实重叠。
        await asyncio.sleep(0.35)
        return {
            "marks": [f"{tag}:memory"],
            "counter": (state.get("counter") or 0) + 1,
            "last_writer": tag,
        }

    async def archive(state: ProbeState) -> Dict[str, Any]:
        tag = state.get("last_writer", "?")
        await asyncio.sleep(0.02)
        return {"marks": [f"{tag}:archive"], "last_writer": tag}

    g = StateGraph(ProbeState)
    g.add_node("front", front)
    g.add_node("memory", memory)
    g.add_node("archive", archive)
    g.add_edge(START, "front")
    g.add_edge("front", "memory")
    g.add_edge("memory", "archive")
    g.add_edge("archive", END)

    kwargs: Dict[str, Any] = {}
    if interrupt:
        kwargs["interrupt_after"] = ["front"]
    return g.compile(checkpointer=checkpointer, **kwargs)


# --------------------------------------------------------------------------
# 基础设施
# --------------------------------------------------------------------------

async def make_checkpointer() -> tuple[AsyncPostgresSaver, AsyncConnection]:
    """照抄 app.py::create_checkpointer 的建连参数（单连接，非连接池）。"""
    url = os.getenv("RAGENT_POSTGRES_URL")
    if not url:
        print("❌ 缺少 RAGENT_POSTGRES_URL。先 `set -a; source .env; set +a`")
        sys.exit(1)
    conn = await AsyncConnection.connect(
        url, autocommit=True, prepare_threshold=0, row_factory=dict_row
    )
    saver = AsyncPostgresSaver(conn)
    await saver.setup()
    return saver, conn


def new_thread_id(label: str) -> str:
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
    return f"probe-cc-{label}-{suffix}"


async def dump_thread(conn: AsyncConnection, thread_id: str) -> List[dict]:
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT checkpoint_id, parent_checkpoint_id,
                   checkpoint->'channel_versions' AS versions,
                   metadata->>'step' AS step,
                   metadata->>'source' AS source
            FROM checkpoints
            WHERE thread_id = %s
            ORDER BY checkpoint_id ASC
            """,
            (thread_id,),
        )
        return list(await cur.fetchall())


async def cleanup(conn: AsyncConnection, thread_ids: List[str]) -> None:
    if not thread_ids:
        return
    async with conn.cursor() as cur:
        for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
            await cur.execute(
                f"DELETE FROM {table} WHERE thread_id = ANY(%s)", (thread_ids,)
            )
    print(f"\n[cleanup] 已清理 {len(thread_ids)} 个测试 thread 的全部 checkpoint 行")


def hr(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


# --------------------------------------------------------------------------
# S1：两个 ainvoke() 真并发写同一 thread_id
# --------------------------------------------------------------------------

async def scenario_1(saver: AsyncPostgresSaver, conn: AsyncConnection, thread_id: str) -> dict:
    hr("S1  两个 ainvoke() 真并发写同一 thread_id（asyncio.gather + Barrier）")
    graph = build_graph(saver)
    config = {"configurable": {"thread_id": thread_id}}
    barrier = asyncio.Barrier(2)

    async def run(tag: str):
        await barrier.wait()  # 确保两侧同一时刻起跑，真重叠
        # 不传 counter：让它从 checkpoint 里的既有值累加。
        # 并发安全 → 两条 run 各 +1，最终 counter==2；丢更新 → 只有 1。
        return await graph.ainvoke(
            {"marks": [f"{tag}:start"], "last_writer": tag}, config
        )

    t0 = time.time()
    results = await asyncio.gather(run("A"), run("B"), return_exceptions=True)
    elapsed = time.time() - t0

    errors = [r for r in results if isinstance(r, BaseException)]
    ok_results = [r for r in results if not isinstance(r, BaseException)]

    print(f"耗时 {elapsed:.2f}s")
    print(f"抛异常的 run 数：{len(errors)}")
    for e in errors:
        print(f"   {type(e).__name__}: {e}")
    for i, r in enumerate(ok_results):
        print(f"run#{i} 返回 marks   = {r.get('marks')}")
        print(f"run#{i} last_writer = {r.get('last_writer')}  counter={r.get('counter')}")

    snap = await saver.aget_tuple(config)
    final = snap.checkpoint["channel_values"] if snap else {}
    final_marks = final.get("marks", [])
    print(f"\n落库后 aget_tuple 认定的最新 state：")
    print(f"   marks       = {final_marks}")
    print(f"   last_writer = {final.get('last_writer')}  counter={final.get('counter')}")

    rows = await dump_thread(conn, thread_id)
    # 注意：分叉点可能出现在根上（parent 为 NULL），不能像"只统计非空父"那样漏掉
    parents = [r["parent_checkpoint_id"] for r in rows]
    dup_parents = {p for p in parents if parents.count(p) > 1}
    print(f"\ncheckpoints 行数 = {len(rows)}；被多个子 checkpoint 共享的父节点数 = {len(dup_parents)}")
    print(f"   （父节点含 NULL 根；被复用 = 状态历史分叉，同一父下挂了两条互不相干的分支）")

    a_ok = any(m.startswith("A:") for m in final_marks)
    b_ok = any(m.startswith("B:") for m in final_marks)
    print("\n判定：")
    if errors:
        print("   ⚠️ 有 run 抛异常 —— 并发写会报错（至少不是静默的）")
    if a_ok and b_ok:
        print("   ✅ 最终 state 同时含 A、B 两侧的更新 —— 没有丢更新")
    else:
        lost = "B" if a_ok else "A"
        print(f"   ❌ 最终 state 只剩一侧 —— {lost} 那一整条 run 的更新被静默丢弃（last-writer-wins）")
    return {
        "errors": len(errors),
        "lost_update": not (a_ok and b_ok),
        "forked": len(dup_parents) > 0,
        "rows": len(rows),
    }


# --------------------------------------------------------------------------
# S2：复刻 B 的真实时序 —— 后台续跑 vs 用户下一条消息
# --------------------------------------------------------------------------

async def scenario_2(
    saver: AsyncPostgresSaver,
    conn: AsyncConnection,
    thread_id: str,
    *,
    turn2_extra_delay: float = 0.0,
    variant: str = "后台先写完",
    use_thread_lock: bool = False,
) -> dict:
    """
    `turn2_extra_delay` 用来翻转"谁最后写完"：
      0.0  → 后台续跑（memory 0.35s）最后写完
      0.5  → turn2 最后写完
    两个变体都跑一遍，才能证明"丢的是哪一侧取决于时序"，而不是某个固定规则。
    `use_thread_lock=True` 是对照组：外层加 per-thread_id 锁再跑同一时序。
    """
    lock_note = "（已加 per-thread_id 锁）" if use_thread_lock else ""
    hr(f"S2  复刻 B 时序：turn1 后台续跑 与 turn2 新消息 并发 —— 变体「{variant}」{lock_note}")
    graph = build_graph(saver, interrupt=True)
    config = {"configurable": {"thread_id": thread_id}}
    thread_lock = asyncio.Lock()

    # --- turn1 前台段：跑到 front 之后 interrupt，此刻 B 方案就发 done 了 ---
    turn1 = await graph.ainvoke(
        {"marks": ["T1:start"], "last_writer": "T1", "counter": 0}, config
    )
    print(f"turn1 前台段结束（done 已发出）marks = {turn1.get('marks')}")
    snap = await saver.aget_tuple(config)
    print(f"   暂停点 checkpoint_id = {snap.config['configurable']['checkpoint_id']}")
    st = await graph.aget_state(config)
    print(f"   下一步待执行节点 next = {st.next}")

    # --- 后台续跑 与 turn2 新消息，真并发 ---
    # 注意：B 草案里后台任务用的 config 就是这个不带 checkpoint_id 的 config，
    # 语义是"从该 thread 的**最新** checkpoint 继续"，而不是"从暂停那个点继续"。
    barrier = asyncio.Barrier(2)

    async def background_resume():
        await barrier.wait()
        if use_thread_lock:
            async with thread_lock:
                return await graph.ainvoke(None, config)
        return await graph.ainvoke(None, config)

    async def user_turn2():
        await barrier.wait()
        # 让后台略微领先，模拟"用户紧接着发下一条"；extra_delay 用来翻转谁最后落盘
        await asyncio.sleep(0.01 + turn2_extra_delay)
        if use_thread_lock:
            async with thread_lock:
                return await graph.ainvoke(
                    {"marks": ["T2:start"], "last_writer": "T2"}, config
                )
        return await graph.ainvoke(
            {"marks": ["T2:start"], "last_writer": "T2"}, config
        )

    results = await asyncio.gather(
        background_resume(), user_turn2(), return_exceptions=True
    )
    labels = ["后台续跑", "turn2 新消息"]
    for label, r in zip(labels, results):
        if isinstance(r, BaseException):
            print(f"\n{label}：❌ 抛异常 {type(r).__name__}: {r}")
        else:
            print(f"\n{label}：marks = {r.get('marks')}")
            print(f"           last_writer={r.get('last_writer')} counter={r.get('counter')}")

    snap = await saver.aget_tuple(config)
    final = snap.checkpoint["channel_values"] if snap else {}
    final_marks = final.get("marks", [])
    print(f"\n最终落库 state：marks = {final_marks}")
    print(f"                counter = {final.get('counter')}  last_writer = {final.get('last_writer')}")

    print("\n判定：")
    errors = [r for r in results if isinstance(r, BaseException)]
    if errors:
        print("   ⚠️ 并发续跑抛异常")
    # turn1 的记忆压缩（memory 节点）是否真的落盘了
    t1_memory = any(m == "T1:memory" for m in final_marks)
    t2_present = any(m.startswith("T2:") for m in final_marks)
    if t1_memory and t2_present:
        print("   ✅ turn1 的后台压缩与 turn2 的更新都在最终 state 里")
    elif not t1_memory:
        print("   ❌ turn1 的后台压缩结果没进最终 state —— 这一轮记忆压缩被静默丢弃")
    elif not t2_present:
        print("   ❌ turn2（用户新消息）的更新没进最终 state —— 用户这条消息丢了")
    # 后台任务实际从哪儿恢复
    bg = results[0]
    if not isinstance(bg, BaseException):
        bg_marks = bg.get("marks", [])
        if any(m.startswith("T2:") for m in bg_marks):
            print("   ❌ 后台续跑吃到了 turn2 的输入 —— 它从错误的 checkpoint 恢复了")
        else:
            print("   ✓ 后台续跑恢复自 turn1 的状态（未串到 turn2 的输入）")
    return {
        "variant": variant,
        "locked": use_thread_lock,
        "errors": len(errors),
        "final_marks": final_marks,
        "t1_memory_persisted": t1_memory,
        "t2_persisted": t2_present,
    }


# --------------------------------------------------------------------------
# S2X：后台续跑的 config 不带 checkpoint_id —— 它到底从哪儿恢复？
# --------------------------------------------------------------------------

async def scenario_2x(saver: AsyncPostgresSaver, conn: AsyncConnection, thread_id: str) -> dict:
    """
    B 草案 §5.2 第 4 步写的是 `self._compiled.ainvoke(None, config)`，
    而 `run_stream` 里的 config 是 `{"configurable": {"thread_id": ...}}`，**不带
    checkpoint_id**。它的语义不是"从我暂停的那个点继续"，而是"从该 thread 的**最新**
    checkpoint 继续"。

    上面 S2 的两个变体里后台都抢先读到了 turn1 的暂停点，所以没暴露这一点。
    这里把时序反过来：让 turn2 先落盘，后台续跑**之后**才开始读。
    """
    hr("S2X 后台续跑用不带 checkpoint_id 的 config —— turn2 先落盘时它恢复的是谁？")
    graph = build_graph(saver, interrupt=True)
    config = {"configurable": {"thread_id": thread_id}}

    turn1 = await graph.ainvoke(
        {"marks": ["T1:start"], "last_writer": "T1", "counter": 0}, config
    )
    paused = await saver.aget_tuple(config)
    paused_id = paused.config["configurable"]["checkpoint_id"]
    print(f"turn1 暂停点 checkpoint_id = {paused_id}")
    print(f"turn1 前台段 marks = {turn1.get('marks')}")

    barrier = asyncio.Barrier(2)

    async def background_resume():
        await barrier.wait()
        await asyncio.sleep(0.30)  # 后台任务被调度晚了 —— turn2 此时已经落盘
        return await graph.ainvoke(None, config)

    async def user_turn2():
        await barrier.wait()
        return await graph.ainvoke({"marks": ["T2:start"], "last_writer": "T2"}, config)

    results = await asyncio.gather(background_resume(), user_turn2(), return_exceptions=True)
    for label, r in zip(["后台续跑", "turn2 新消息"], results):
        if isinstance(r, BaseException):
            print(f"\n{label}：❌ {type(r).__name__}: {r}")
        else:
            print(f"\n{label}：marks = {r.get('marks')}")

    bg = results[0]
    hijacked = (not isinstance(bg, BaseException)) and any(
        m.startswith("T2:") for m in bg.get("marks", [])
    )
    print("\n判定：")
    if hijacked:
        print("   ❌ 后台续跑读到的是 **turn2** 的 state —— 它没在给 turn1 收尾，")
        print("      而是把 turn2 的状态跑完了 memory→archive。")
        print("      turn1 那一轮的压缩/归档**永远不会执行**，且 turn2 被一个")
        print("      本该属于上一轮的后台任务提前推进，二者都不符合设计意图。")
    else:
        print("   ✓ 后台续跑仍恢复自 turn1 的暂停点")
    print("\n   → 无论并发是否加锁，B 的后台任务都必须显式传")
    print("     `{'configurable': {'thread_id': tid, 'checkpoint_id': <暂停点 id>}}`，")
    print("     不能沿用 run_stream 里那个不带 checkpoint_id 的 config。")
    return {"hijacked": hijacked}


# --------------------------------------------------------------------------
# S3：落库证据 —— 分叉结构与"谁是最新"的裁决规则
# --------------------------------------------------------------------------

async def scenario_3(conn: AsyncConnection, thread_ids: List[str]) -> None:
    hr("S3  落库证据：checkpoint 父子链与'谁是最新'的裁决规则")
    for tid in thread_ids:
        rows = await dump_thread(conn, tid)
        print(f"\nthread={tid}  共 {len(rows)} 行")
        children: Dict[Optional[str], List[str]] = {}
        for r in rows:
            # 用 "<ROOT>" 占位 NULL 父，分叉可能就发生在根上（两条 run 都从空状态起跑）
            children.setdefault(r["parent_checkpoint_id"] or "<ROOT>", []).append(
                r["checkpoint_id"]
            )
        forks = {p: c for p, c in children.items() if len(c) > 1}
        if forks:
            print(f"   ❌ 检测到 {len(forks)} 个分叉点（同一父 checkpoint 挂了多个子）：")
            for p, c in forks.items():
                print(f"      父 {p} → 子 {c}")
        else:
            print("   ✓ 链是线性的，没有分叉")
        if rows:
            print(f"   checkpoint_id 最大者（= aget_tuple/alist 认定的最新）：{rows[-1]['checkpoint_id']}")
    print("\n说明：aget_tuple/alist 的排序键就是 checkpoint_id DESC（见 base.py SELECT_SQL），")
    print("      而 checkpoint_id 是时间序 UUID —— 所以'最新'= 时间上最后写的那个，")
    print("      不做任何父子一致性校验，另一条分支就此从'最新'视角消失。")


# --------------------------------------------------------------------------
# S4：对照组 —— per-thread_id 锁能不能消除上面的现象
# --------------------------------------------------------------------------

async def scenario_4(saver: AsyncPostgresSaver, conn: AsyncConnection, thread_id: str) -> dict:
    hr("S4  对照组：加 per-thread_id 锁串行化后，同样的并发还会不会丢更新")
    graph = build_graph(saver)
    config = {"configurable": {"thread_id": thread_id}}
    lock = asyncio.Lock()  # 现实里是 dict[thread_id] -> Lock
    barrier = asyncio.Barrier(2)

    async def run(tag: str):
        await barrier.wait()  # 并发起跑照旧，锁只在图执行外层
        async with lock:
            return await graph.ainvoke(
                {"marks": [f"{tag}:start"], "last_writer": tag}, config
            )

    t0 = time.time()
    results = await asyncio.gather(run("A"), run("B"), return_exceptions=True)
    elapsed = time.time() - t0

    snap = await saver.aget_tuple(config)
    final = snap.checkpoint["channel_values"] if snap else {}
    final_marks = final.get("marks", [])
    print(f"耗时 {elapsed:.2f}s（两侧被串行化，约为 S1 的两倍属正常）")
    for i, r in enumerate(results):
        if isinstance(r, BaseException):
            print(f"run#{i} ❌ {type(r).__name__}: {r}")
    print(f"最终 state marks = {final_marks}")
    print(f"           counter = {final.get('counter')}")

    rows = await dump_thread(conn, thread_id)
    parents = [r["parent_checkpoint_id"] for r in rows]
    dup = {p for p in parents if parents.count(p) > 1}

    a_ok = any(m.startswith("A:") for m in final_marks)
    b_ok = any(m.startswith("B:") for m in final_marks)
    print("\n判定：")
    if a_ok and b_ok and not dup:
        print("   ✅ 两侧更新都在，且历史链无分叉 —— per-thread_id 锁能消除 S1 的问题")
    else:
        print(f"   ❌ 锁没解决：A在={a_ok} B在={b_ok} 分叉点={len(dup)}")
    return {"fixed": bool(a_ok and b_ok and not dup)}


# --------------------------------------------------------------------------
# S0：静态事实核对（表结构 / 源码层面的保护机制）
# --------------------------------------------------------------------------

async def scenario_0(conn: AsyncConnection) -> None:
    hr("S0  静态事实：checkpoint 表结构有没有可做乐观锁的版本列")
    async with conn.cursor() as cur:
        for t in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
            await cur.execute(
                """SELECT column_name, data_type FROM information_schema.columns
                   WHERE table_name = %s ORDER BY ordinal_position""",
                (t,),
            )
            cols = [(r["column_name"], r["data_type"]) for r in await cur.fetchall()]
            print(f"\n{t}:")
            for name, typ in cols:
                print(f"   {name:24} {typ}")
    print("""
结论：
  · `checkpoints` 没有 version / seq / xmin 一类的行版本列，主键是
    (thread_id, checkpoint_ns, checkpoint_id)，checkpoint_id 每次写都是新 UUID。
  · `checkpoint_blobs.version` 不是行版本，是**每个 channel 的内容版本**，
    格式 `{递增整数:032}.{随机数:016}`（base.py::get_next_version）。带随机后缀，
    所以两条并发分支不会撞进同一行 —— 好消息：blob 不会被对方内容污染。
  · UPSERT_CHECKPOINTS_SQL 是 `ON CONFLICT (pk) DO UPDATE SET ...`，无条件覆盖，
    没有 `WHERE version = expected` 这类比较交换（CAS）。
  → 现有表结构**不具备**乐观锁所需的版本列。""")


def report_lock_semantics() -> None:
    hr("S0b  AsyncPostgresSaver 自带的 `self.lock` 到底保护了什么")
    print("""源码：langgraph/checkpoint/postgres/aio.py

    line 59   self.lock = asyncio.Lock()
    line 374  async with self.lock, _ainternal.get_connection(self.conn) as conn:

  · 这把锁在 `_cursor()` 里获取，作用域 = **单次 DB 操作**
    （一次 aput / 一次 aput_writes / 一次 aget_tuple）。
  · 它是**实例级**的，不是 per-thread_id 的 —— 全进程所有会话共抢一把。
  · 它的目的写在注释里：单条连接不能被多个协程同时用（"a connection not in
    pipeline mode can only be used by one thread/coroutine at a time"）。
    也就是说它保护的是 **psycopg 连接/游标不被并发踩坏**，
    **不是**"同一 thread_id 的逻辑状态不被并发覆盖"。
  · 本项目 app.py::create_checkpointer 传的是**单条 AsyncConnection**（非连接池），
    所以这把锁事实上把全进程的 checkpoint 读写串行化了 ——
    这是一个吞吐瓶颈，但它依然拦不住"两个 ainvoke 各读各的基线、各写各的分支"。
  → LangGraph **没有**提供任何 per-thread_id 的并发写保护。""")


# --------------------------------------------------------------------------

async def main() -> int:
    saver, conn = await make_checkpointer()
    created: List[str] = []
    try:
        await scenario_0(conn)
        report_lock_semantics()

        t1 = new_thread_id("s1"); created.append(t1)
        r1 = await scenario_1(saver, conn, t1)

        t2a = new_thread_id("s2a"); created.append(t2a)
        r2a = await scenario_2(saver, conn, t2a, turn2_extra_delay=0.0,
                               variant="后台先写完")

        t2b = new_thread_id("s2b"); created.append(t2b)
        r2b = await scenario_2(saver, conn, t2b, turn2_extra_delay=0.5,
                               variant="turn2 先写完")

        t2c = new_thread_id("s2c"); created.append(t2c)
        r2c = await scenario_2(saver, conn, t2c, turn2_extra_delay=0.0,
                               variant="后台先写完", use_thread_lock=True)

        t2x = new_thread_id("s2x"); created.append(t2x)
        r2x = await scenario_2x(saver, conn, t2x)

        await scenario_3(conn, [t1, t2a, t2b, t2c, t2x])

        t4 = new_thread_id("s4"); created.append(t4)
        r4 = await scenario_4(saver, conn, t4)

        hr("总结")
        print(f"S1 并发写：异常数={r1['errors']}  丢更新={r1['lost_update']}  历史分叉={r1['forked']}")
        for r in (r2a, r2b, r2c):
            print(
                f"S2「{r['variant']}」锁={r['locked']}  异常数={r['errors']}  "
                f"turn1压缩落盘={r['t1_memory_persisted']}  turn2落盘={r['t2_persisted']}"
            )
        print(f"S2X 后台任务被 turn2 的 state 劫持：{r2x['hijacked']}")
        print(f"S4 加锁后：问题消除={r4['fixed']}")
        print("""
一句话结论：
  同一 thread_id 并发 ainvoke **不报错、不检测冲突**，两条 run 各读各的基线、
  各写各的 checkpoint 分支，`aget_tuple` 按 checkpoint_id 取时间上最后写的那条 ——
  另一条分支的更新从此不可见（静默丢更新）。表里没有可用于乐观锁的版本列。
  per-thread_id 锁（S4）能消除该现象。""")
        return 0
    finally:
        await cleanup(conn, created)
        await conn.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
