"""验证 tests/unit/test_workflow_stream_isolation.py 的场景确实能抓到旧 bug。

做法：用旧设计（队列 = 实例属性，全进程共享一个实例）跑同一套并发场景，
如果测试是有效的，这里应该出现串流。
"""
import asyncio
from typing import List, Optional


class OldStyleWorkflow:
    """复刻修复前的设计：队列挂在实例上。"""

    def __init__(self):
        self._token_queue: Optional[asyncio.Queue] = None


async def simulate_request(
    wf: OldStyleWorkflow,
    tokens: List[str],
    both_started: asyncio.Event,
    started: List[int],
) -> List[str]:
    # 旧实现：run_stream 直接覆写实例属性
    queue: asyncio.Queue = asyncio.Queue()
    wf._token_queue = queue

    started[0] += 1
    if started[0] == 2:
        both_started.set()
    await both_started.wait()

    try:
        for t in tokens:
            if wf._token_queue is not None:
                await wf._token_queue.put(t)
            await asyncio.sleep(0)
        got = []
        while not queue.empty():
            got.append(queue.get_nowait())
        return got
    finally:
        wf._token_queue = None  # 旧实现的 finally


async def main():
    wf = OldStyleWorkflow()  # 全进程一个实例，跟 create_app() 一样
    ev = asyncio.Event()
    counter = [0]

    a_expected = ["A1", "A2", "A3"]
    b_expected = ["B1", "B2", "B3"]

    a, b = await asyncio.gather(
        simulate_request(wf, a_expected, ev, counter),
        simulate_request(wf, b_expected, ev, counter),
    )

    print(f"请求 A 期望 {a_expected}")
    print(f"请求 A 实收 {a}")
    print(f"请求 B 期望 {b_expected}")
    print(f"请求 B 实收 {b}")
    print()

    a_ok = a == a_expected
    b_ok = b == b_expected
    if a_ok and b_ok:
        print("❌ 旧实现下测试也通过了 —— 说明这组测试抓不到该 bug，测试无效")
    else:
        print("✅ 旧实现下测试失败 —— 说明这组测试确实能抓到并发串流")
        leaked = [t for t in a if t.startswith("B")] + [t for t in b if t.startswith("A")]
        lost = [t for t in a_expected + b_expected if t not in a + b]
        if leaked:
            print(f"   串流(收到对方的 token): {leaked}")
        if lost:
            print(f"   丢失(谁都没收到的 token): {lost}")


asyncio.run(main())
