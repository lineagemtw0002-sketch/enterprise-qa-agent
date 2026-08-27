"""验证 T-6 / T-7 的判别力：确认它们在"旧式实现"下确实会红。

`CLAUDE.md` §7.2：**并发缺陷必须用并发方式验证**，而"这条测试真的能抓到 bug 吗"
本身也要验证——否则写出来的可能是一条永远绿的废测试。

做法：用一个**共享可变状态版**的 request_id 实现（把它挂在一个全进程共享的对象上，
这正是 P0-1 的成因——`create_app()` 全进程只构造一个 `RAGWorkflow`），跑
`tests/unit/test_observability_context.py` 里同一套并发场景。

预期输出：
- 旧式实现（共享属性）→ **FAIL**（id 串了）
- 现行实现（contextvars）→ **PASS**

同时验证一件反直觉的事：**串行跑同样次数，旧式实现照样全绿**。
这就是"必须真并发"的直接证据。

用法：
    .venv/bin/python scripts/verify_request_id_propagation.py
"""

from __future__ import annotations

import asyncio
import sys
from typing import List, Optional

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from src.observability.context import (  # noqa: E402
    bind_request_context,
    clear_request_context,
    get_request_context,
)

CONCURRENCY = 10
NODES = (
    "session", "intent", "retrieve", "tool_subgraph", "workflow",
    "clarify", "generate", "memory_manage", "archive",
)


class SharedStateHolder:
    """旧式实现：request_id 挂在一个全进程共享的对象上。"""

    def __init__(self) -> None:
        self.request_id: Optional[str] = None


# ── 旧式实现 ────────────────────────────────────────────────────────


async def _old_style_request(
    holder: SharedStateHolder,
    request_id: str,
    all_started: asyncio.Event,
    started: List[int],
) -> List[str]:
    holder.request_id = request_id  # ← 覆写共享状态

    started[0] += 1
    if started[0] == CONCURRENCY:
        all_started.set()
    await all_started.wait()

    seen: List[str] = []
    try:
        for _ in NODES:
            seen.append(holder.request_id or "MISSING")
            await asyncio.sleep(0)  # 强制让出，让别的请求插进来
        return seen
    finally:
        holder.request_id = None  # 旧实现的 finally


# ── 现行实现 ────────────────────────────────────────────────────────


async def _contextvar_request(
    request_id: str,
    all_started: asyncio.Event,
    started: List[int],
) -> List[str]:
    bind_request_context(request_id=request_id)

    started[0] += 1
    if started[0] == CONCURRENCY:
        all_started.set()
    await all_started.wait()

    seen: List[str] = []
    try:
        for _ in NODES:
            ctx = get_request_context()
            seen.append(ctx.request_id if ctx else "MISSING")
            await asyncio.sleep(0)
        return seen
    finally:
        clear_request_context()


# ── 断言 ────────────────────────────────────────────────────────────


def _check(results: List[List[str]]) -> tuple[bool, str]:
    for i, seen in enumerate(results):
        expected = f"r-{i}"
        if seen != [expected] * len(NODES):
            wrong = sorted({s for s in seen if s != expected})
            return False, f"请求 {i} 读到了别人的 id：{wrong}（应全部是 {expected}）"
    return True, "每个请求全程只读到自己的 id"


async def _run_concurrent(old_style: bool) -> tuple[bool, str]:
    all_started = asyncio.Event()
    started = [0]
    holder = SharedStateHolder()
    if old_style:
        coros = [
            _old_style_request(holder, f"r-{i}", all_started, started)
            for i in range(CONCURRENCY)
        ]
    else:
        coros = [
            _contextvar_request(f"r-{i}", all_started, started)
            for i in range(CONCURRENCY)
        ]
    return _check(await asyncio.gather(*coros))


async def _run_serial_old_style() -> tuple[bool, str]:
    """同样次数、串行执行——旧式实现在这里**照样全绿**。"""
    holder = SharedStateHolder()
    results: List[List[str]] = []
    for i in range(CONCURRENCY):
        event = asyncio.Event()
        event.set()  # 串行：不等别人
        started = [CONCURRENCY]
        results.append(await _old_style_request(holder, f"r-{i}", event, started))
    return _check(results)


async def main() -> int:
    print("=" * 72)
    print("T-6 / T-7 判别力验证")
    print("=" * 72)

    ok_old, msg_old = await _run_concurrent(old_style=True)
    print(f"\n[1] 旧式实现（共享属性）+ 真并发   → {'PASS' if ok_old else 'FAIL'}")
    print(f"    {msg_old}")

    ok_serial, msg_serial = await _run_serial_old_style()
    print(f"\n[2] 旧式实现（共享属性）+ 串行     → {'PASS' if ok_serial else 'FAIL'}")
    print(f"    {msg_serial}")

    ok_new, msg_new = await _run_concurrent(old_style=False)
    print(f"\n[3] 现行实现（contextvars）+ 真并发 → {'PASS' if ok_new else 'FAIL'}")
    print(f"    {msg_new}")

    print("\n" + "-" * 72)
    verdict_ok = (not ok_old) and ok_serial and ok_new
    if verdict_ok:
        print("结论：测试有判别力。")
        print("  · [1] 红 → T-6/T-7 确实能抓到「id 存在共享位置」这个 bug")
        print("  · [2] 绿 → 串行跑同样次数抓不到，**必须真并发**")
        print("  · [3] 绿 → 现行 contextvars 实现下不串")
    else:
        print("结论：验证本身不成立，请检查脚本。")
        print(f"  期望 [1]=FAIL [2]=PASS [3]=PASS，实际 "
              f"[1]={'PASS' if ok_old else 'FAIL'} "
              f"[2]={'PASS' if ok_serial else 'FAIL'} "
              f"[3]={'PASS' if ok_new else 'FAIL'}")
    print("-" * 72)
    return 0 if verdict_ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
