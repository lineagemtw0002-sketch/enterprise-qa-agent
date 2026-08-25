"""请求上下文贯穿与并发隔离测试 —— 可观测性方案 T-2/T-4/T-5/T-6/T-7/T-9。

**这组测试的结构照抄 `tests/unit/test_workflow_stream_isolation.py`**（P0-1 的
回归保护）：用 `asyncio.gather` 让多个"请求"**真正交错执行**，并用
`asyncio.Event` + `sleep(0)` 强制它们在对方持有上下文期间被调度。

为什么必须真并发（`CLAUDE.md` §7.2：并发缺陷必须用并发方式验证）：
如果 `request_id` 被写成某个共享对象的实例属性——这正是 P0-1 的成因，
`create_app()` 全进程只有一个 `RAGWorkflow`——**串行跑 10 遍照样全绿**
（第二次覆盖第一次时第一次已经结束了），只有交错执行才抓得到。
`scripts/verify_request_id_propagation.py` 用一个"实例属性版"的假实现
实际验证过 T-6/T-7 会红。

T-6 与 T-7 是**两个不同的失败模式**，缺一不可：
- T-6 断言的是 contextvar 本身的读值
- T-7 断言的是**渲染出来的 JSON 字符串**——注入发生在 handler 阶段，
  与 contextvar 的读取可能差一拍（比如某天有人把注入改到 logger 层缓存起来）
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Dict, List

import pytest

from src.observability.context import (
    CONTEXT_FIELDS,
    RequestContext,
    bind_request_context,
    clear_request_context,
    context_as_dict,
    get_request_context,
    new_request_id,
    request_context,
    sanitize_request_id,
)


# ── 基础语义 ────────────────────────────────────────────────────────


class TestBindAndRead:
    def test_bind_then_read(self) -> None:
        bind_request_context(request_id="r-1", org_id="org_acme")
        ctx = get_request_context()
        assert ctx is not None
        assert ctx.request_id == "r-1"
        assert ctx.org_id == "org_acme"

    def test_bind_merges_instead_of_replacing(self) -> None:
        """中间件先绑 request_id，端点认完用户再补 org_id——不该把前者冲掉。"""
        bind_request_context(request_id="r-1")
        bind_request_context(org_id="org_acme", user_id="u-1")
        ctx = get_request_context()
        assert ctx.request_id == "r-1"
        assert ctx.org_id == "org_acme"

    def test_auto_generates_request_id(self) -> None:
        """保证"日志里永远有 id"：只绑 org 也会自动补一个 request_id。"""
        ctx = bind_request_context(org_id="org_acme")
        assert ctx.request_id
        assert len(ctx.request_id) == 16

    def test_context_is_frozen(self) -> None:
        """不可变是刻意的：避免"某个节点悄悄改了别人的上下文"这类问题。"""
        ctx = bind_request_context(request_id="r-1")
        with pytest.raises(Exception):
            ctx.request_id = "r-2"  # type: ignore[misc]

    def test_unknown_field_rejected(self) -> None:
        """打错字段名要当场报错，而不是静默丢掉——否则日志里会永远缺那个字段。"""
        with pytest.raises(TypeError):
            bind_request_context(reqeust_id="r-1")

    def test_clear_sets_none(self) -> None:
        bind_request_context(request_id="r-1")
        clear_request_context()
        assert get_request_context() is None

    def test_context_manager_restores_previous(self) -> None:
        bind_request_context(request_id="outer")
        with request_context(request_id="inner"):
            assert get_request_context().request_id == "inner"
        assert get_request_context().request_id == "outer"


class TestRequestIdHygiene:
    """T-3 的第三条断言（**日志注入面**）。中间件本身是阶段二的事，
    但入站 id 的校验规则属于本模块，先钉死。"""

    def test_generated_id_is_16_hex(self) -> None:
        rid = new_request_id()
        assert len(rid) == 16
        int(rid, 16)  # 必须是合法十六进制

    def test_valid_inbound_id_kept(self) -> None:
        assert sanitize_request_id("abc-123_XYZ") == "abc-123_XYZ"

    @pytest.mark.parametrize(
        "bad",
        [
            None,
            "",
            "   ",
            "has space",
            "line\nbreak",          # ← 换行能伪造出一整行假日志
            "semi;colon",
            "a" * 129,              # ← 超长撑爆日志行
            "中文",
        ],
    )
    def test_illegal_inbound_id_rejected(self, bad) -> None:
        """入站 `X-Request-Id` 是**外部可控字符串**，不校验就是日志注入面。"""
        assert sanitize_request_id(bad) is None


# ── T-9：无上下文不崩 ──────────────────────────────────────────────


class TestNoContext:
    def test_context_as_dict_all_none(self) -> None:
        clear_request_context()
        d = context_as_dict()
        assert set(d) == set(CONTEXT_FIELDS)
        assert all(v is None for v in d.values())

    def test_log_without_context_has_null_fields_not_missing(
        self, capture_json_logs
    ) -> None:
        """T-9：常驻任务 / `/health` 没有请求上下文。

        字段必须**存在且为 null**，不能干脆不出现——"缺字段"会让
        `select(.request_id == null)` 之类的查询直接漏掉这些行，
        而那恰恰是排查"哪些日志没挂上链路"时要看的。
        """
        clear_request_context()
        logs = capture_json_logs()
        logging.getLogger("test.ctx.none").info("keep-warm tick", extra={"event": "warm"})
        record = logs.records[-1]
        for name in CONTEXT_FIELDS:
            assert name in record, f"字段 {name} 缺失（应为 null）"
            assert record[name] is None

    def test_explicit_extra_wins_over_injection(self, capture_json_logs) -> None:
        """调用点显式传的值优先——后台重放任务要能标注"这是在补哪一次请求"。"""
        logs = capture_json_logs()
        bind_request_context(request_id="ctx-id")
        logging.getLogger("test.ctx.override").info("x", extra={"request_id": "explicit"})
        assert logs.records[-1]["request_id"] == "explicit"


# ── T-4 / T-5：继承 ────────────────────────────────────────────────


class TestPropagation:
    @pytest.mark.asyncio
    async def test_create_task_inherits(self) -> None:
        bind_request_context(request_id="r-parent")

        async def child() -> str:
            return get_request_context().request_id

        task = asyncio.create_task(child())
        assert await task == "r-parent"

    @pytest.mark.asyncio
    async def test_to_thread_inherits(self) -> None:
        """T-4：钉死一个"文档里声称的不变量"。

        `query_knowledge_hub.py` 的检索走 `asyncio.to_thread`。CPython 的
        `to_thread` 内部用 `contextvars.copy_context()`，所以线程里读得到——
        但这是**别人的实现细节**，不是我们的保证。若某天版本升级导致不继承，
        整条检索链路的 request_id 会**静默丢失**（不报错、日志照出，就是查不到）。
        `CLAUDE.md` §7.2：注释里声称的不变量要么验证、要么标为未证实假设。
        """
        bind_request_context(request_id="r-thread")

        def in_thread() -> str:
            ctx = get_request_context()
            return ctx.request_id if ctx else "MISSING"

        assert await asyncio.to_thread(in_thread) == "r-thread"

    @pytest.mark.asyncio
    async def test_background_task_survives_parent_cleanup(self) -> None:
        """T-5：主协程清理上下文**不会**影响已经 create_task 出去的后台任务。

        这是 `workflow.py` 里归档 / LTM 抽取那两个 `create_task` 的真实形态：
        SSE 流结束、`finally` 里 `set(None)` 之后，后台任务还在跑，
        它们的日志**必须仍然带着那次请求的 id**。

        反直觉但正确——子任务持有的是创建时刻的上下文**副本**。
        这条专门拦"清理把子任务也清了"这个错误。
        """
        released = asyncio.Event()
        seen: List[str] = []

        async def background() -> None:
            await released.wait()  # 确保主协程已经清理完才读
            ctx = get_request_context()
            seen.append(ctx.request_id if ctx else "MISSING")

        bind_request_context(request_id="r-bg")
        task = asyncio.create_task(background())   # ← 必须在 clear 之前创建
        clear_request_context()
        assert get_request_context() is None       # 主协程这边确实清了
        released.set()
        await task

        assert seen == ["r-bg"]

    @pytest.mark.asyncio
    async def test_child_binding_does_not_leak_to_parent(self) -> None:
        """子任务改自己的上下文不该污染父协程——contextvars 的隔离方向。"""
        bind_request_context(request_id="r-parent")

        async def child() -> None:
            bind_request_context(request_id="r-child")

        await asyncio.create_task(child())
        assert get_request_context().request_id == "r-parent"


# ── T-2：贯穿全链路 ────────────────────────────────────────────────


#: 本项目 LangGraph 的 9 个节点（`workflow.py` 的实际链路）
NODES = (
    "session", "intent", "retrieve", "tool_subgraph", "workflow",
    "clarify", "generate", "memory_manage", "archive",
)


class TestEndToEndChain:
    @pytest.mark.asyncio
    async def test_single_request_id_across_all_nodes(self, capture_json_logs) -> None:
        """T-2：一次请求的所有事件共享同一个 request_id，**且覆盖全部 9 个节点**。

        "覆盖 9 个节点"这半条是关键——只断言 id 唯一的话，
        "只有前两个节点带 id、后七个根本没打日志"也能通过。

        判别力：旧实现里 request_id 这个概念**根本不存在**，日志里零命中，
        这条必然失败。
        """
        logs = capture_json_logs()
        logger = logging.getLogger("test.ctx.chain")

        async def node(name: str) -> None:
            # 每个节点模拟成一个独立 task —— 这正是 workflow 里 create_task 的形态
            logger.info(f"{name} done", extra={"event": f"{name}.step.success"})

        bind_request_context(request_id="r-chain", org_id="org_acme")
        for name in NODES:
            await asyncio.create_task(node(name))

        records = [r for r in logs.records if r["logger"] == "test.ctx.chain"]
        assert len({r["request_id"] for r in records}) == 1
        assert records[0]["request_id"] == "r-chain"
        covered = {r["event"].split(".")[0] for r in records}
        assert covered == set(NODES), f"未覆盖的节点：{set(NODES) - covered}"

    @pytest.mark.asyncio
    async def test_org_id_rides_along(self, capture_json_logs) -> None:
        """D-6：`org_id` 走 contextvars。它是"按 org 分文件"的前置依赖。"""
        logs = capture_json_logs()
        bind_request_context(request_id="r-1", org_id="org_globex")
        logging.getLogger("test.ctx.org").info("x", extra={"event": "retrieve.x"})
        assert logs.records[-1]["org_id"] == "org_globex"


# ── T-6 / T-7：真并发 ──────────────────────────────────────────────


async def _simulate_request(
    request_id: str,
    node_names: tuple,
    all_started: asyncio.Event,
    started: List[int],
    concurrency: int,
    logger: logging.Logger,
) -> List[str]:
    """模拟一次请求：绑上下文 → 等所有请求都绑完 → 交错打日志 → 收自己看到的 id。

    中间那次 `all_started.wait()` 是关键：它保证**所有请求同时**处于
    "已经绑过上下文、还没打完日志"的状态。如果 request_id 存在任何共享位置
    （实例属性、模块全局），后绑的会覆盖先绑的，先绑的那个请求打出来的日志
    就会带上别人的 id。
    """
    bind_request_context(request_id=request_id, org_id=f"org-{request_id}")

    started[0] += 1
    if started[0] == concurrency:
        all_started.set()
    await all_started.wait()

    seen: List[str] = []
    for name in node_names:
        ctx = get_request_context()
        seen.append(ctx.request_id if ctx else "MISSING")
        logger.info(
            f"{name} done",
            extra={"event": f"{name}.step.success", "expected_request_id": request_id},
        )
        await asyncio.sleep(0)  # 强制让出，让别的请求插进来
    return seen


class TestConcurrentIsolation:
    @pytest.mark.asyncio
    async def test_request_ids_do_not_cross_under_concurrency(self) -> None:
        """T-6：10 个并发请求，每个读到的永远是自己的 id。

        判别力：把 request_id 换成共享实例属性的实现下**必然失败**；
        而串行跑 10 遍在那个坏实现下**照样全绿**——这就是必须真并发的原因。
        """
        concurrency = 10
        all_started = asyncio.Event()
        started = [0]
        logger = logging.getLogger("test.ctx.concurrent")

        results = await asyncio.gather(*[
            _simulate_request(f"r-{i}", NODES, all_started, started, concurrency, logger)
            for i in range(concurrency)
        ])

        for i, seen in enumerate(results):
            assert seen == [f"r-{i}"] * len(NODES), f"请求 {i} 读到了别人的 id：{seen}"

    @pytest.mark.asyncio
    async def test_rendered_json_lines_do_not_cross(self, capture_json_logs) -> None:
        """T-7：断言**渲染出来的 JSON 字符串**里 id 也不串。

        T-6 抓不到这一条：上下文注入发生在 handler 阶段，与 contextvar 的读取
        可能差一拍（例如有人为了省开销把注入结果缓存到 logger 上）。
        两条是**两个不同的失败模式**。

        每条日志自带 `expected_request_id`（来自闭包里的局部变量，绝不可能串），
        注入进来的 `request_id` 必须与它逐条相等。
        """
        concurrency = 10
        all_started = asyncio.Event()
        started = [0]
        logs = capture_json_logs()
        logger = logging.getLogger("test.ctx.concurrent.json")

        await asyncio.gather(*[
            _simulate_request(f"r-{i}", NODES, all_started, started, concurrency, logger)
            for i in range(concurrency)
        ])

        records = [
            r for r in logs.records if r["logger"] == "test.ctx.concurrent.json"
        ]
        assert len(records) == concurrency * len(NODES)
        for r in records:
            assert r["request_id"] == r["expected_request_id"], (
                f"日志行串了：注入 {r['request_id']} vs 实际 {r['expected_request_id']}"
            )
        # org_id 也走同一套机制，一并钉死（按 org 分文件依赖它）
        for r in records:
            assert r["org_id"] == f"org-{r['expected_request_id']}"

    @pytest.mark.asyncio
    async def test_one_request_cleanup_does_not_clear_others(self) -> None:
        """一个请求结束时 `set(None)`，不能把并发中的其他请求也清掉。

        这是 P0-1 的另一半：旧实现里 A 先结束、finally 把共享队列置 None，
        B 剩下的 token 就被静默丢弃了。
        """
        a_bound = asyncio.Event()
        b_checked = asyncio.Event()
        result: Dict[str, str] = {}

        async def request_a() -> None:
            bind_request_context(request_id="r-a")
            a_bound.set()
            clear_request_context()      # A 结束，清理自己的上下文
            await b_checked.wait()

        async def request_b() -> None:
            bind_request_context(request_id="r-b")
            await a_bound.wait()
            await asyncio.sleep(0)       # 让 A 的 clear 先跑
            ctx = get_request_context()
            result["b"] = ctx.request_id if ctx else "MISSING"
            b_checked.set()

        await asyncio.gather(request_a(), request_b())
        assert result["b"] == "r-b"
