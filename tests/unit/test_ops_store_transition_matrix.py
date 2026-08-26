"""智能运维审批状态机 —— 全量跃迁矩阵 + 非法跃迁补充覆盖。

配套 `tests/unit/test_ops_store_status_machine.py`（逐条核对设计图里画出来的箭头 +
`mark_executing` 的审批人字段不变量）。本文件补它没覆盖的三类：

1. **全量矩阵**：10 个状态两两组合共 90 条有向边，逐条断言"设计图里有的必须允许、
   没有的必须拒绝"。现有那份是"正向逐条点名 + 终态只试了一个目标状态"，
   漏掉的组合（自跃迁、终态→其它终态、`expired`→`approved` 这类）都在这里补上。
2. **终态之后不能再变**，包括"超时后又被批准""已完成又被重新执行"这两条最危险的路径。
3. **`mark_executing` 审批人字段校验的"半损坏"变体**：只缺 `approved_at`、
   或只缺 `approver_user_id`。

全程不连 Postgres：`_assert_transition_allowed` 本身是纯函数；需要走
`OpsStore` 方法的用例用 monkeypatch 换掉 `get_action` / `_get_pool`，
只验证状态判断逻辑，不产生任何 IO。

## 判别力自查（`CLAUDE.md` §7.2）

- `TestFullTransitionMatrix` —— **判别式，且是本文件的主要价值**：`_STATUS_TRANSITIONS`
  里任何一条边被增、被删、被改指向，这组必然变红。现有测试做不到这点：它只对
  "画出来的箭头"逐条 assert 允许，**多出来一条没画的箭头它发现不了**
  （例如有人为了"修一个卡住的工单"给 `expired` 加一条回 `pending_approval` 的边）。
- `TestTerminalStatesAreReallyTerminal` —— **判别式**，终态出边集合改成非空即变红。
- `TestApproveActionRejectsNonPendingStates` —— **判别式**，`approve_action` 里那句
  `_assert_transition_allowed` 删掉就变红（尤其 `expired` 那条：超时之后又被批准，
  正是 §3.3 明写"不能静默转 approved"要防的事故）。
- `TestMarkExecutingApproverFieldsPartialCorruption` —— **判别式**，且比现有测试更强：
  现有用例把 `approver_user_id` 和 `approved_at` **同时**置 None，所以把实现里的
  `or` 手滑写成 `and` 它照样绿；这里两个字段各缺一个，`and` 立刻变红。
- `TestMarkResultTargetValidation` 的 `rolled_back`/`expired` 两条 —— **判别式**
  （白名单从 `(completed, failed)` 放宽即变红）。
- `TestAdvanceStatusLegalPathWritesTargetStatus` —— **回归保护**，钉住"合法跃迁确实
  把 target_status 写进了 UPDATE 语句"，防止将来重构时把参数顺序搞反。
"""

from __future__ import annotations

import itertools
import time
from unittest.mock import AsyncMock

import pytest

from src.ragent_backend.ops_store import (
    STATUS_APPROVED,
    STATUS_COMPLETED,
    STATUS_EXECUTING,
    STATUS_EXPIRED,
    STATUS_FAILED,
    STATUS_PENDING_APPROVAL,
    STATUS_PROPOSED,
    STATUS_REJECTED,
    STATUS_REJECTED_PRE,
    STATUS_ROLLED_BACK,
    IllegalStatusTransition,
    OpsStore,
    RemediationAction,
    _assert_transition_allowed,
)

ALL_STATUSES = [
    STATUS_PROPOSED,
    STATUS_REJECTED_PRE,
    STATUS_PENDING_APPROVAL,
    STATUS_APPROVED,
    STATUS_REJECTED,
    STATUS_EXPIRED,
    STATUS_EXECUTING,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_ROLLED_BACK,
]

# `docs/aiops_module_design.md` §3.3 状态图里画出来的全部箭头，在这里**独立于
# 生产代码重新誊写一遍**——刻意不 import `_STATUS_TRANSITIONS` 来生成期望值，
# 否则测试会跟着实现一起错（拿被测对象当预言机）。
DESIGNED_EDGES = {
    (STATUS_PROPOSED, STATUS_PENDING_APPROVAL),
    (STATUS_PROPOSED, STATUS_REJECTED_PRE),
    (STATUS_PENDING_APPROVAL, STATUS_APPROVED),
    (STATUS_PENDING_APPROVAL, STATUS_REJECTED),
    (STATUS_PENDING_APPROVAL, STATUS_EXPIRED),
    (STATUS_APPROVED, STATUS_EXECUTING),
    (STATUS_EXECUTING, STATUS_COMPLETED),
    (STATUS_EXECUTING, STATUS_FAILED),
    (STATUS_FAILED, STATUS_ROLLED_BACK),
}

TERMINAL_STATUSES = [
    STATUS_REJECTED_PRE,
    STATUS_REJECTED,
    STATUS_EXPIRED,
    STATUS_COMPLETED,
    STATUS_ROLLED_BACK,
]


class _FakeAcquireCtx:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _FakeAcquireCtx(self._conn)


def _action(**overrides) -> RemediationAction:
    base = dict(
        action_id="remact_1",
        org_id="org-1",
        connection_id="opsconn_1",
        proposed_by="u1",
        intent="重启卡死的服务",
        plan={"target": "order-service"},
        impact_radius=None,
        status=STATUS_PROPOSED,
        approver_user_id=None,
        approved_at=None,
        executed_at=None,
        result=None,
        rollback_plan=None,
        outcome_effective=None,
        created_at=time.time(),
    )
    base.update(overrides)
    return RemediationAction(**base)


# ==================== 全量跃迁矩阵 ====================


class TestFullTransitionMatrix:
    """10×10 = 100 条有向边逐条核对（含自跃迁）。

    设计图里有的 9 条必须允许，其余 91 条必须抛 `IllegalStatusTransition`。
    """

    @pytest.mark.parametrize("current,target", sorted(DESIGNED_EDGES))
    def test_designed_edge_is_allowed(self, current, target):
        _assert_transition_allowed(current, target)  # 不抛异常即通过

    @pytest.mark.parametrize(
        "current,target",
        [
            pair
            for pair in itertools.product(ALL_STATUSES, ALL_STATUSES)
            if pair not in DESIGNED_EDGES
        ],
    )
    def test_undesigned_edge_is_rejected(self, current, target):
        """设计图里没画的箭头一律不许存在——包括自跃迁（`X → X`）。

        自跃迁看着无害，其实是"重复提交/重放"的入口：`pending_approval →
        pending_approval` 会让一次审批请求被重复处理，`executing → executing`
        会让一个修复动作被重复下发到客户环境。
        """
        with pytest.raises(IllegalStatusTransition):
            _assert_transition_allowed(current, target)

    def test_unknown_current_status_rejects_everything(self):
        """状态字段被写进了一个表里没有的值（脏数据/未来新增状态漏登记）时，
        必须拒绝而不是放行——`.get(current, frozenset())` 的默认值是 fail-closed。
        """
        for target in ALL_STATUSES:
            with pytest.raises(IllegalStatusTransition):
                _assert_transition_allowed("some_status_that_does_not_exist", target)


class TestTerminalStatesAreReallyTerminal:
    """终态之后不能再变——现有测试只试了 `终态 → proposed` 一个目标。"""

    @pytest.mark.parametrize("terminal", TERMINAL_STATUSES)
    @pytest.mark.parametrize("target", ALL_STATUSES)
    def test_no_outgoing_edge_from_terminal(self, terminal, target):
        with pytest.raises(IllegalStatusTransition):
            _assert_transition_allowed(terminal, target)

    def test_expired_cannot_become_approved(self):
        """§3.3 原文：超时后自动转 `expired`，"而不是静默转 `approved` 或无限期挂起，
        两者都是事故源"。这条把"超时之后还能被补批准"这条路堵死。"""
        with pytest.raises(IllegalStatusTransition):
            _assert_transition_allowed(STATUS_EXPIRED, STATUS_APPROVED)

    def test_expired_cannot_return_to_pending_approval(self):
        # "把过期工单重新打开"是一个很自然的产品需求，但它不在 V1 状态图里；
        # 真要做必须先改设计文档（并回答"重新打开后超时怎么算"）。
        with pytest.raises(IllegalStatusTransition):
            _assert_transition_allowed(STATUS_EXPIRED, STATUS_PENDING_APPROVAL)

    def test_completed_cannot_be_executed_again(self):
        with pytest.raises(IllegalStatusTransition):
            _assert_transition_allowed(STATUS_COMPLETED, STATUS_EXECUTING)

    def test_rejected_cannot_be_approved_later(self):
        with pytest.raises(IllegalStatusTransition):
            _assert_transition_allowed(STATUS_REJECTED, STATUS_APPROVED)

    def test_rejected_pre_cannot_reenter_the_flow(self):
        """`rejected_pre` = 越界被拦在审批之前（§3.3.1）。它必须是死路，
        否则"越界拦截"就能靠改状态绕过去。"""
        for target in (STATUS_PENDING_APPROVAL, STATUS_APPROVED, STATUS_EXECUTING):
            with pytest.raises(IllegalStatusTransition):
                _assert_transition_allowed(STATUS_REJECTED_PRE, target)


# ==================== 「未审批直接执行」的各条路径 ====================


class TestNoPathReachesExecutingWithoutApproval:
    """§6 测试设计点名："`proposed`/`pending_approval` 状态下**任何路径**都不能
    进入 `executing`，必须显式测试"跳过审批"这条路径被拒绝"。
    """

    @pytest.mark.parametrize(
        "current",
        [s for s in ALL_STATUSES if s != STATUS_APPROVED],
    )
    def test_executing_only_reachable_from_approved(self, current):
        with pytest.raises(IllegalStatusTransition):
            _assert_transition_allowed(current, STATUS_EXECUTING)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "current",
        [STATUS_PROPOSED, STATUS_PENDING_APPROVAL, STATUS_REJECTED, STATUS_EXPIRED],
    )
    async def test_mark_executing_rejects_unapproved_states(self, monkeypatch, current):
        store = OpsStore()
        monkeypatch.setattr(store, "get_action", AsyncMock(return_value=_action(status=current)))
        with pytest.raises(IllegalStatusTransition):
            await store.mark_executing("remact_1")

    @pytest.mark.asyncio
    async def test_mark_executing_rejects_already_executing(self, monkeypatch):
        # 重复下发同一个修复动作 —— 状态机必须挡住。
        store = OpsStore()
        already = _action(
            status=STATUS_EXECUTING, approver_user_id="admin-1", approved_at=time.time()
        )
        monkeypatch.setattr(store, "get_action", AsyncMock(return_value=already))
        with pytest.raises(IllegalStatusTransition):
            await store.mark_executing("remact_1")


class TestMarkExecutingApproverFieldsPartialCorruption:
    """§3.3 硬性不变量的"半损坏"变体。

    现有 `test_rejects_approved_state_missing_approver_fields` 把两个字段同时
    置 None，所以实现里 `approver_user_id is None or approved_at is None` 的 `or`
    即便被手滑写成 `and`，那条测试仍然是绿的。这里各缺一个字段，`and` 会当场变红。
    """

    @pytest.mark.asyncio
    async def test_rejects_when_only_approved_at_is_missing(self, monkeypatch):
        store = OpsStore()
        half = _action(status=STATUS_APPROVED, approver_user_id="admin-1", approved_at=None)
        monkeypatch.setattr(store, "get_action", AsyncMock(return_value=half))
        with pytest.raises(IllegalStatusTransition):
            await store.mark_executing("remact_1")

    @pytest.mark.asyncio
    async def test_rejects_when_only_approver_user_id_is_missing(self, monkeypatch):
        store = OpsStore()
        half = _action(status=STATUS_APPROVED, approver_user_id=None, approved_at=time.time())
        monkeypatch.setattr(store, "get_action", AsyncMock(return_value=half))
        with pytest.raises(IllegalStatusTransition):
            await store.mark_executing("remact_1")


# ==================== approve_action / advance_status / mark_result ====================


class TestApproveActionRejectsNonPendingStates:
    """只有 `pending_approval` 能被批准。"""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "current",
        [s for s in ALL_STATUSES if s != STATUS_PENDING_APPROVAL],
    )
    async def test_rejects_every_non_pending_state(self, monkeypatch, current):
        store = OpsStore()
        monkeypatch.setattr(store, "get_action", AsyncMock(return_value=_action(status=current)))
        with pytest.raises(IllegalStatusTransition):
            await store.approve_action("remact_1", approver_user_id="admin-1")

    @pytest.mark.asyncio
    async def test_rejects_double_approval(self, monkeypatch):
        """已经批准过的工单再批一次必须被挡——否则第二个审批人的 id 会覆盖第一个，
        审批记录变成"最后点的人"而不是"批准它的人"。

        注意：这条只覆盖**串行**的重复批准。两个审批人**并发**点批准的竞态
        （§6 点名要求、且必须用真并发验证）当前实现挡不住，见交付报告的 bug 清单。
        """
        store = OpsStore()
        approved = _action(
            status=STATUS_APPROVED, approver_user_id="admin-1", approved_at=time.time()
        )
        monkeypatch.setattr(store, "get_action", AsyncMock(return_value=approved))
        with pytest.raises(IllegalStatusTransition):
            await store.approve_action("remact_1", approver_user_id="admin-2")


class TestAdvanceStatusGuards:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("target", [STATUS_APPROVED, STATUS_EXECUTING])
    async def test_reserved_targets_rejected_before_any_db_access(self, monkeypatch, target):
        """`approved`/`executing` 必须走专用方法。这里额外验证：拒绝发生在
        **查库之前**——`get_action` 被换成会炸的 mock，如果实现顺序反了就会
        抛 AssertionError 而不是 IllegalStatusTransition。
        """
        store = OpsStore()

        async def _explode(*args, **kwargs):
            raise AssertionError("advance_status 不应在拒绝保留目标状态前访问数据库")

        monkeypatch.setattr(store, "get_action", _explode)
        with pytest.raises(IllegalStatusTransition):
            await store.advance_status("remact_1", target)

    @pytest.mark.asyncio
    async def test_missing_action_raises_value_error_not_transition_error(self, monkeypatch):
        """"工单不存在"和"跃迁非法"是两种不同的失败，调用方要能区分
        （一个是 404，一个是 409）。"""
        store = OpsStore()
        monkeypatch.setattr(store, "get_action", AsyncMock(return_value=None))
        with pytest.raises(ValueError) as exc:
            await store.advance_status("remact_missing", STATUS_PENDING_APPROVAL)
        assert not isinstance(exc.value, IllegalStatusTransition)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "current,target",
        [
            (STATUS_EXPIRED, STATUS_PENDING_APPROVAL),
            (STATUS_COMPLETED, STATUS_FAILED),
            (STATUS_REJECTED, STATUS_PENDING_APPROVAL),
            (STATUS_ROLLED_BACK, STATUS_EXECUTING),
            (STATUS_PROPOSED, STATUS_COMPLETED),
        ],
    )
    async def test_illegal_transitions_rejected_through_the_generic_entry(
        self, monkeypatch, current, target
    ):
        # 状态机校验必须真的接在 advance_status 上，不能只存在于纯函数里。
        store = OpsStore()
        monkeypatch.setattr(store, "get_action", AsyncMock(return_value=_action(status=current)))
        with pytest.raises(IllegalStatusTransition):
            await store.advance_status("remact_1", target)


class TestAdvanceStatusLegalPathWritesTargetStatus:
    """回归保护：合法跃迁确实把 `target_status` 作为第一个参数写进 UPDATE。"""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "current,target",
        [
            (STATUS_PROPOSED, STATUS_PENDING_APPROVAL),
            (STATUS_PROPOSED, STATUS_REJECTED_PRE),
            (STATUS_PENDING_APPROVAL, STATUS_REJECTED),
            (STATUS_PENDING_APPROVAL, STATUS_EXPIRED),
            (STATUS_FAILED, STATUS_ROLLED_BACK),
        ],
    )
    async def test_legal_transition_executes_update_with_target(
        self, monkeypatch, current, target
    ):
        store = OpsStore()
        monkeypatch.setattr(
            store,
            "get_action",
            AsyncMock(side_effect=[_action(status=current), _action(status=target)]),
        )
        fake_conn = AsyncMock()
        monkeypatch.setattr(store, "_get_pool", AsyncMock(return_value=_FakePool(fake_conn)))

        result = await store.advance_status("remact_1", target)

        assert result.status == target
        fake_conn.execute.assert_awaited_once()
        args = fake_conn.execute.await_args.args
        assert args[1] == target, f"UPDATE 的状态参数应是 {target}，实际 {args[1]}"
        assert args[2] == "remact_1"


class TestMarkResultTargetValidation:
    """`mark_result` 只接受 `completed`/`failed`。"""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "target",
        [
            STATUS_ROLLED_BACK,
            STATUS_EXPIRED,
            STATUS_PENDING_APPROVAL,
            STATUS_EXECUTING,
            STATUS_PROPOSED,
            "not_a_status",
        ],
    )
    async def test_rejects_targets_outside_the_whitelist(self, target):
        store = OpsStore()
        with pytest.raises(IllegalStatusTransition):
            await store.mark_result("remact_1", target, result={})

    @pytest.mark.asyncio
    @pytest.mark.parametrize("target", [STATUS_COMPLETED, STATUS_FAILED])
    async def test_rejects_whitelisted_target_from_illegal_current_state(
        self, monkeypatch, target
    ):
        """目标状态在白名单里，但当前状态不是 `executing`——比如有人想把一个
        刚 `approved`、还没执行的工单直接标成 `completed`（跳过执行）。
        白名单校验通过之后，状态机校验必须仍然拦住它。
        """
        store = OpsStore()
        monkeypatch.setattr(
            store, "get_action", AsyncMock(return_value=_action(status=STATUS_APPROVED))
        )
        with pytest.raises(IllegalStatusTransition):
            await store.mark_result("remact_1", target, result={})
