"""流式队列的并发隔离测试——2026-08-24 代码审计 P0-1 的回归保护。

原始缺陷：`_token_queue` / `_trace_queue` 是 `RAGWorkflow` 的**实例属性**，而
`create_app()` 全进程只构造一个 `RAGWorkflow` 给所有请求共用。并发请求会互相
覆盖对方的队列：请求 A 建了 QA，请求 B 把 `self._token_queue` 覆写成 QB，此后
A 的 `_generate_node` 往 QB 里推 token，两个 SSE 流又都在 await 同一个 QB.get()
——两个用户的回答被随机切碎、交叉投递到对方的连接上；A 先结束时 finally 把
队列置 None，B 剩下的 token 还会被静默丢弃。

修复方式是把队列改成 contextvars 按请求隔离。

**这组测试必须是真并发的**：串行跑两遍在旧实现下也能通过（第二次覆盖第一次时
第一次已经结束了），根本抓不到这个 bug。所以下面每个用例都用
`asyncio.gather` 让两个"请求"真正交错执行，并且用 Event/sleep(0) 强制它们在
对方持有队列期间被调度。

这里直接用真实的 `RAGWorkflow` 实例和真实的 `_token_queue` 属性，只把 store/llm
换成假对象——被测的是"属性怎么解析队列"，不需要跑完整个图。
"""

import asyncio
from typing import Any, Dict, List, Optional

import pytest

from src.ragent_backend.workflow import (
    RAGWorkflow,
    _CURRENT_TOKEN_QUEUE,
    _CURRENT_TRACE_QUEUE,
)


class _FakeStore:
    """RAGWorkflow 只把 store 存起来，构造期不调用它的任何方法。"""


class _FakeLLM:
    """同上；`llm is not None` 会让 _build_graph 走上构建工具子图的分支，
    正好覆盖"子图也要能读到本次请求队列"这条路径。"""


@pytest.fixture
def workflow() -> RAGWorkflow:
    """全进程共享一个实例——刻意复现 create_app() 的真实用法。"""
    return RAGWorkflow(store=_FakeStore(), llm=_FakeLLM())


async def _simulate_request(
    workflow: RAGWorkflow,
    tokens: List[str],
    both_started: asyncio.Event,
    started_count: List[int],
) -> List[str]:
    """模拟一次 run_stream：建自己的队列 → 放进上下文 → 推 token → 收回自己的队列。

    关键点是中间那次 `both_started.wait()`：它保证两个请求**同时**处于"已经
    设置过队列、还没推完 token"的状态。旧实现下第二个请求的 set 会覆盖第一个，
    第一个请求推出去的 token 就会落到第二个的队列里。
    """
    queue: asyncio.Queue[str] = asyncio.Queue()
    _CURRENT_TOKEN_QUEUE.set(queue)

    started_count[0] += 1
    if started_count[0] == 2:
        both_started.set()
    await both_started.wait()

    try:
        for token in tokens:
            # 走真实的属性解析路径，而不是直接用局部变量 queue
            assert workflow._token_queue is not None
            await workflow._token_queue.put(token)
            await asyncio.sleep(0)  # 强制让出，让另一个请求有机会插进来

        received: List[str] = []
        while not queue.empty():
            received.append(queue.get_nowait())
        return received
    finally:
        _CURRENT_TOKEN_QUEUE.set(None)


class TestConcurrentRequestsDoNotCrossStream:
    @pytest.mark.asyncio
    async def test_two_concurrent_requests_keep_their_own_tokens(self, workflow):
        """核心用例：两个请求交错推 token，各自只能收到自己的。

        旧实现下这条必然失败——两个请求的 token 会混进同一个队列。
        """
        both_started = asyncio.Event()
        counter = [0]

        a_tokens = ["A1", "A2", "A3"]
        b_tokens = ["B1", "B2", "B3"]

        a_received, b_received = await asyncio.gather(
            _simulate_request(workflow, a_tokens, both_started, counter),
            _simulate_request(workflow, b_tokens, both_started, counter),
        )

        assert a_received == a_tokens, f"请求 A 收到了不属于它的内容: {a_received}"
        assert b_received == b_tokens, f"请求 B 收到了不属于它的内容: {b_received}"

    @pytest.mark.asyncio
    async def test_no_token_is_lost(self, workflow):
        """除了不能串，也不能丢——旧实现里先结束的请求会把队列置 None，
        导致另一个请求剩余的 token 被静默丢弃。"""
        both_started = asyncio.Event()
        counter = [0]

        a_received, b_received = await asyncio.gather(
            _simulate_request(workflow, [f"A{i}" for i in range(20)], both_started, counter),
            _simulate_request(workflow, [f"B{i}" for i in range(20)], both_started, counter),
        )

        assert len(a_received) == 20
        assert len(b_received) == 20

    @pytest.mark.asyncio
    async def test_many_concurrent_requests(self, workflow):
        """把并发度提到 10，防止"两个刚好错开"式的假通过。"""

        async def one(idx: int) -> List[str]:
            queue: asyncio.Queue[str] = asyncio.Queue()
            _CURRENT_TOKEN_QUEUE.set(queue)
            try:
                expected = [f"r{idx}-t{i}" for i in range(5)]
                for token in expected:
                    await workflow._token_queue.put(token)
                    await asyncio.sleep(0)
                got = []
                while not queue.empty():
                    got.append(queue.get_nowait())
                return got
            finally:
                _CURRENT_TOKEN_QUEUE.set(None)

        results = await asyncio.gather(*(one(i) for i in range(10)))

        for idx, got in enumerate(results):
            assert got == [f"r{idx}-t{i}" for i in range(5)], (
                f"请求 {idx} 的流被污染了: {got}"
            )


class TestContextPropagatesIntoChildTasks:
    """`_generate_node` 是在 `asyncio.create_task(compiled.ainvoke(...))` 里跑的，
    工具子图更是嵌套更深的调用。隔离方案要成立，上下文必须能传进子任务。"""

    @pytest.mark.asyncio
    async def test_child_task_sees_the_request_queue(self, workflow):
        queue: asyncio.Queue[str] = asyncio.Queue()
        _CURRENT_TOKEN_QUEUE.set(queue)

        async def child_like_generate_node():
            # 子任务里读到的必须是本次请求的队列
            assert workflow._token_queue is queue
            await workflow._token_queue.put("from-child")

        # create_task 复制创建时的上下文——这正是 run_stream 依赖的机制
        await asyncio.create_task(child_like_generate_node())

        assert queue.get_nowait() == "from-child"
        _CURRENT_TOKEN_QUEUE.set(None)

    @pytest.mark.asyncio
    async def test_two_requests_child_tasks_stay_separate(self, workflow):
        """子任务层面同样不能串。"""

        async def request(tag: str) -> str:
            queue: asyncio.Queue[str] = asyncio.Queue()
            _CURRENT_TOKEN_QUEUE.set(queue)

            async def child():
                await asyncio.sleep(0)
                await workflow._token_queue.put(tag)

            await asyncio.create_task(child())
            await asyncio.sleep(0)
            result = queue.get_nowait()
            _CURRENT_TOKEN_QUEUE.set(None)
            return result

        a, b = await asyncio.gather(request("A"), request("B"))
        assert a == "A"
        assert b == "B"


class TestNonStreamingPath:
    """`run()`（非流式）不设置队列，推送必须被安全跳过而不是抛异常。"""

    def test_queues_are_none_without_a_request_context(self, workflow):
        assert workflow._token_queue is None
        assert workflow._trace_queue is None

    def test_queue_is_not_instance_state(self):
        """两个实例、同一个上下文——队列跟着请求走，不跟着实例走。

        这条是对根因的直接断言：只要队列还是实例属性，它就一定失败。
        """
        wf_a = RAGWorkflow(store=_FakeStore(), llm=_FakeLLM())
        wf_b = RAGWorkflow(store=_FakeStore(), llm=_FakeLLM())

        queue: asyncio.Queue[str] = asyncio.Queue()
        _CURRENT_TOKEN_QUEUE.set(queue)
        try:
            assert wf_a._token_queue is queue
            assert wf_b._token_queue is queue
        finally:
            _CURRENT_TOKEN_QUEUE.set(None)

    def test_queue_is_read_only_attribute(self, workflow):
        """防回归：不允许有人再把 per-request 队列写回实例属性。"""
        with pytest.raises(AttributeError):
            workflow._token_queue = asyncio.Queue()
        with pytest.raises(AttributeError):
            workflow._trace_queue = asyncio.Queue()


class TestTraceQueueIsolation:
    """trace 队列和 token 队列是同一个问题的两半，一起锁住。"""

    @pytest.mark.asyncio
    async def test_concurrent_trace_queues_do_not_cross(self, workflow):
        async def request(tag: str) -> List[Dict[str, Any]]:
            queue: asyncio.Queue[Dict[str, Any]] = asyncio.Queue()
            _CURRENT_TRACE_QUEUE.set(queue)
            try:
                for i in range(3):
                    await workflow._trace_queue.put({"tag": tag, "seq": i})
                    await asyncio.sleep(0)
                got = []
                while not queue.empty():
                    got.append(queue.get_nowait())
                return got
            finally:
                _CURRENT_TRACE_QUEUE.set(None)

        a, b = await asyncio.gather(request("A"), request("B"))

        assert all(e["tag"] == "A" for e in a), f"A 的 trace 被污染: {a}"
        assert all(e["tag"] == "B" for e in b), f"B 的 trace 被污染: {b}"
