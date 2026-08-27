"""并发审批竞态的回归保护——连真实 Postgres（不是 mock），`CLAUDE.md` §7.2：
"并发缺陷必须用并发方式验证"，串行跑 N 次不算。

设计 §6 明确要求："两个有 can_approve 权限的用户同时点批准/拒绝，只能有一个
生效，必须用真并发测试"。这条测试直接对应那条测试设计。

2026-08-26 变异测试（另一会话）实测发现 `approve_action`/`mark_executing`
原来是"先 SELECT 读状态判断、再无条件 UPDATE"，两个并发请求会双双通过状态
检查、后写覆盖先写——两个人都以为自己批准成功了，`approver_user_id` 最终
只记录了后到的那次调用。修复用条件 UPDATE（`WHERE id=... AND status=...`）
把"检查+修改"做成数据库层面的原子操作。

⚠️ **判别力自查踩过两个坑，如实记录**：

**坑一**：最初以为"两个 `asyncio.gather` 出去的调用天然会在
`await self.get_action(...)` 那个真实网络 I/O 点交错"，实测不成立——
`TestConcurrentApproval` 那三条在**本机 localhost Postgres** 下，
`git stash` 回退到修复前的无条件 UPDATE 版本重跑，**依然 3/3 全过**：
不是修复不需要，是本机往返太快，任务 A 经常在任务 B 的 `get_action` 真正
发出网络请求之前就已经读完+写完了，两次调用没有真正交错，天然测不出这条
竞态。

**坑二**：第一次修法是在 `get_action` 里对**每一次**调用无差别加
`await asyncio.sleep(0.05)`，以为"两边都会先撞上这个 sleep 再让出控制权"
就够了。实测**依然测不出来**——用带时间戳的探针脚本抓到：`asyncio.sleep()`
到期后两个协程不保证真正交替执行，常见的情形是协程 A 的 sleep 一醒来，
Python 就把 A 的其余逻辑（读结果 → 判断 → 发 UPDATE）一口气跑完，
中途 `pool.acquire()`/`conn.execute()` 只要能拿到空闲连接就不会真正让出
控制权；等协程 B 的 sleep 终于醒来时，A 已经读完+写完了。
**"两边都 sleep 相同时长"不等于"两边会同时被唤醒并交替执行"**，跟
`test_db_pool.py::TestConcurrentCallsCreateOnlyOnePool` 用
`asyncio.sleep(0.05)` 撑开竞争窗口面对的是同一类问题，但这次连
那个技巧本身都不够用，因为窄的不是"要不要 sleep"而是"sleep 醒来之后
调度器要不要真的切换协程"，无差别加延迟管不到这一层。

**真正管用的技巧**：改用显式的 `asyncio.Event` 屏障——利用"哪个协程都
不可能在自己第一次 `get_action` 返回之前发起第二次调用"这个事实，把
**全局第一、第二次**调用（无论来自哪个协程，一定分别是两个并发
`approve_action` 各自的首次读）分别真读一次数据库后，强制互相等待对方
也读完了才允许继续往下走——这样即使调度器把 A 一路跑到底，A 的第二次
`get_action`（取最终结果）会被算成"第三次调用"，屏障逻辑不认它，
真正被拦住的是 A 想在自己的第一次读之后立刻往下走，但这一步已经在
`_conditional_update` 之前，B 也已经真实读完了同一个旧状态，两者是在
"同一份旧状态"上各自决策、真正交错，不再依赖调度器的运气。
手工用 `git stash` 验证过：撑开窗口后，回退到无条件 UPDATE 版本时，
两个请求都读到 `pending_approval`、都通过 Python 侧的转移前检查、
**都写成功**（`len(successes) == 2`，测试当场失败）；现在的条件 UPDATE
实现下稳定 1 赢 1 输。上面那三条自然并发的测试仍然保留——它们是真实生产
场景下"大概率不会同时点"但"万一撞上了必须正确处理"的基线保护，只是不能
单独当作这条竞态修复的判别力证据来引用。
"""

from __future__ import annotations

import asyncio
import os

import pytest

pytestmark = pytest.mark.asyncio

os.environ.setdefault("RAGENT_DEBUG", "true")

from src.ragent_backend import db_pool
from src.ragent_backend.ops_store import (
    STATUS_APPROVED,
    STATUS_PENDING_APPROVAL,
    IllegalStatusTransition,
    OpsStore,
)
from src.ragent_backend.org_store import ORG_PLATFORM_ID


@pytest.fixture(autouse=True)
def _reset_pool_between_tests():
    """`pytest-asyncio`（STRICT 模式）默认每个测试函数一个新的事件循环，
    但 `OpsStore._pool`/`db_pool._POOL_CACHE` 是**类级别/模块级别**缓存
    （P1-2），第一个测试建的池绑定着它自己的事件循环，第二个测试的新事件
    循环去用这个绑定旧循环的池会报 `InterfaceError: another operation is
    in progress`（真实踩过，不是猜的）——每个测试开始前清一次缓存，逼下一次
    `_get_pool()` 在当前事件循环里重新建池。"""
    db_pool._POOL_CACHE.clear()
    OpsStore._pool = None
    yield


async def _make_pending_action(ops: OpsStore, name_suffix: str):
    conn = await ops.register_connector(
        ORG_PLATFORM_ID, f"concurrency-test-{name_suffix}", "prometheus", "test",
    )
    action = await ops.create_proposed_action(
        ORG_PLATFORM_ID, conn.connection_id, "test", "并发审批测试", {"target": "x"},
    )
    action = await ops.advance_status(action.action_id, STATUS_PENDING_APPROVAL)
    return conn, action


async def _cleanup(ops: OpsStore, conn, action):
    pool = await ops._get_pool()
    async with pool.acquire() as c:
        await c.execute("DELETE FROM remediation_actions WHERE id = $1", action.action_id)
        await c.execute("DELETE FROM ops_system_connections WHERE id = $1", conn.connection_id)


class TestConcurrentApproval:
    async def test_two_concurrent_approvals_only_one_wins(self):
        ops_store = OpsStore()
        await ops_store._get_pool()
        conn, action = await _make_pending_action(ops_store, "approve")
        try:
            results = await asyncio.gather(
                ops_store.approve_action(action.action_id, approver_user_id="admin-A"),
                ops_store.approve_action(action.action_id, approver_user_id="admin-B"),
                return_exceptions=True,
            )
            successes = [r for r in results if not isinstance(r, Exception)]
            failures = [r for r in results if isinstance(r, Exception)]

            assert len(successes) == 1, f"应该恰好一个成功，实际 {len(successes)}"
            assert len(failures) == 1, f"应该恰好一个失败，实际 {len(failures)}"
            assert isinstance(failures[0], IllegalStatusTransition)

            # 最终落库的状态必须自洽：approver_user_id 是那个真正赢了的请求，
            # 不是"两边各写一半"的混合状态。
            final = await ops_store.get_action(action.action_id)
            assert final.status == STATUS_APPROVED
            assert final.approver_user_id == successes[0].approver_user_id
            assert final.approved_at is not None
        finally:
            await _cleanup(ops_store, conn, action)

    async def test_ten_concurrent_approvals_exactly_one_wins(self):
        """把并发度拉高到 10——两个请求"恰好"错开、没有真正竞争的可能性
        随并发数升高而下降，10 个能更可靠地暴露竞态（如果修复不完整的话）。
        """
        ops_store = OpsStore()
        await ops_store._get_pool()
        conn, action = await _make_pending_action(ops_store, "approve10")
        try:
            results = await asyncio.gather(
                *[
                    ops_store.approve_action(action.action_id, approver_user_id=f"admin-{i}")
                    for i in range(10)
                ],
                return_exceptions=True,
            )
            successes = [r for r in results if not isinstance(r, Exception)]
            failures = [r for r in results if isinstance(r, Exception)]
            assert len(successes) == 1
            assert len(failures) == 9
            assert all(isinstance(f, IllegalStatusTransition) for f in failures)
        finally:
            await _cleanup(ops_store, conn, action)

    async def test_concurrent_mark_executing_only_one_wins(self):
        ops_store = OpsStore()
        await ops_store._get_pool()
        conn, action = await _make_pending_action(ops_store, "exec")
        try:
            action = await ops_store.approve_action(action.action_id, approver_user_id="admin-A")
            results = await asyncio.gather(
                ops_store.mark_executing(action.action_id),
                ops_store.mark_executing(action.action_id),
                return_exceptions=True,
            )
            successes = [r for r in results if not isinstance(r, Exception)]
            failures = [r for r in results if isinstance(r, Exception)]
            assert len(successes) == 1
            assert len(failures) == 1
            assert isinstance(failures[0], IllegalStatusTransition)
        finally:
            await _cleanup(ops_store, conn, action)


class TestForcedRaceWindow:
    """真正的判别力证据（见模块 docstring 的"判别力自查踩过两个坑"）——
    用 `monkeypatch` + `asyncio.Event` 屏障拦住两个并发调用里最先发生的
    两次 `get_action`（一定分别是两个协程各自的首次读），逼它们真正同时
    读到同一个"读时刻"的旧状态、且谁都不能抢先往下走，不依赖本机网络往返
    恰好交错的运气，也不依赖"sleep 醒来就会真正让出调度权"这个不成立的假设。"""

    async def test_conditional_update_wins_even_with_widened_race_window(self, monkeypatch):
        ops_store = OpsStore()
        await ops_store._get_pool()
        conn, action = await _make_pending_action(ops_store, "forced-race")
        try:
            # 单纯加个 sleep 撑不开窗口——手工验证过（见模块 docstring）：
            # `asyncio.sleep()` 到期后两个协程不保证真正交替执行，经常是
            # 协程 A 一路跑到把 UPDATE 都发出去了，协程 B 的 sleep 才醒。
            # 改用显式的 `asyncio.Event` 屏障：**前两次**调用（一定分别是两个
            # 并发 `approve_action` 各自的"第一次读状态"——因为哪个协程都
            # 不可能在第一次读返回之前发起第二次调用）各自真读一次数据库，
            # 但都必须等对方也读完了才能继续网下走，从而保证两边真正是在
            # 同一个旧状态上决策，不留任何"其实是顺序执行"的空子。第三、
            # 四次调用（取最终结果那次）不受影响，直接放行。
            original_get_action = ops_store.get_action
            entry_count = {"n": 0}
            barrier = asyncio.Event()

            async def synchronized_get_action(action_id: str):
                entry_count["n"] += 1
                slot = entry_count["n"]
                result = await original_get_action(action_id)
                if slot == 1:
                    await asyncio.wait_for(barrier.wait(), timeout=2.0)
                elif slot == 2:
                    barrier.set()
                return result

            monkeypatch.setattr(ops_store, "get_action", synchronized_get_action)

            results = await asyncio.gather(
                ops_store.approve_action(action.action_id, approver_user_id="admin-A"),
                ops_store.approve_action(action.action_id, approver_user_id="admin-B"),
                return_exceptions=True,
            )
            successes = [r for r in results if not isinstance(r, Exception)]
            failures = [r for r in results if isinstance(r, Exception)]

            assert len(successes) == 1, (
                f"两个请求的『读状态』都被人为延迟到几乎同一时刻，"
                f"两边都会读到 pending_approval、都会通过 Python 侧的转移前检查——"
                f"能不能只让一个真正生效，完全取决于条件 UPDATE 本身，"
                f"不是 Python 判断。预期恰好 1 个成功，实际 {len(successes)}"
            )
            assert len(failures) == 1
            assert isinstance(failures[0], IllegalStatusTransition)
        finally:
            await _cleanup(ops_store, conn, action)
