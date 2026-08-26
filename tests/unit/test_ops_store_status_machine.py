"""智能运维模块审批状态机的回归保护 —— `docs/aiops_module_design.md` §3.3
"硬性不变量"：`executing` 状态只能从 `approved` 转移而来，且必须有
`approver_user_id` + `approved_at`，这条不允许任何调试开关绕过。

不碰真实 Postgres（本仓库 `conftest.py` 无 DB fixture，`RAGENT_POSTGRES_URL`
默认指向跨会话共用的本地库），用 monkeypatch 直接换掉 `get_action`/`_get_pool`
接住调用，只验证状态机判断逻辑本身。

判别力自查（`CLAUDE.md` §7.2）：`TestApprovedAndExecutingRejectAdvanceStatus`
和 `TestMarkExecutingRequiresApproverFields` 两组如果把 `_STATUS_TRANSITIONS`
或 `mark_executing` 里的校验删掉，会直接变红——不是摆设断言。
"""

from __future__ import annotations

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
        action_id="remact_1", org_id="org-1", connection_id="opsconn_1",
        proposed_by="u1", intent="重启卡死的服务", plan={"target": "order-service"},
        impact_radius=None, status=STATUS_PROPOSED, approver_user_id=None,
        approved_at=None, executed_at=None, result=None, rollback_plan=None,
        outcome_effective=None, created_at=time.time(),
    )
    base.update(overrides)
    return RemediationAction(**base)


class TestTransitionTableMatchesDesignedStateMachine:
    """`docs/aiops_module_design.md` §3.3 的状态图逐条核对——每条设计文档里
    画出来的箭头都应该在这里能找到，反过来也一样（没画出来的箭头不该存在）。
    """

    def test_proposed_can_go_to_pending_approval_or_rejected_pre(self):
        _assert_transition_allowed(STATUS_PROPOSED, STATUS_PENDING_APPROVAL)
        _assert_transition_allowed(STATUS_PROPOSED, STATUS_REJECTED_PRE)

    def test_pending_approval_can_go_to_approved_rejected_or_expired(self):
        _assert_transition_allowed(STATUS_PENDING_APPROVAL, STATUS_APPROVED)
        _assert_transition_allowed(STATUS_PENDING_APPROVAL, STATUS_REJECTED)
        _assert_transition_allowed(STATUS_PENDING_APPROVAL, STATUS_EXPIRED)

    def test_approved_can_only_go_to_executing(self):
        _assert_transition_allowed(STATUS_APPROVED, STATUS_EXECUTING)
        with pytest.raises(IllegalStatusTransition):
            _assert_transition_allowed(STATUS_APPROVED, STATUS_COMPLETED)

    def test_executing_can_go_to_completed_or_failed(self):
        _assert_transition_allowed(STATUS_EXECUTING, STATUS_COMPLETED)
        _assert_transition_allowed(STATUS_EXECUTING, STATUS_FAILED)

    def test_failed_can_go_to_rolled_back(self):
        _assert_transition_allowed(STATUS_FAILED, STATUS_ROLLED_BACK)

    @pytest.mark.parametrize("terminal", [
        STATUS_REJECTED_PRE, STATUS_REJECTED, STATUS_EXPIRED,
        STATUS_COMPLETED, STATUS_ROLLED_BACK,
    ])
    def test_terminal_states_have_no_outgoing_transitions(self, terminal):
        with pytest.raises(IllegalStatusTransition):
            _assert_transition_allowed(terminal, STATUS_PROPOSED)

    def test_cannot_skip_pending_approval_straight_to_approved(self):
        """不能从 proposed 直接跳到 approved——必须先过 pending_approval，
        这是"越界判定必须在进入审批前发生"这条设计要求的状态机体现。"""
        with pytest.raises(IllegalStatusTransition):
            _assert_transition_allowed(STATUS_PROPOSED, STATUS_APPROVED)

    def test_cannot_skip_approval_straight_to_executing(self):
        with pytest.raises(IllegalStatusTransition):
            _assert_transition_allowed(STATUS_PROPOSED, STATUS_EXECUTING)


class TestApprovedAndExecutingRejectAdvanceStatus:
    """`advance_status` 是通用转移入口，但 approved/executing 两个目标状态
    必须走专用方法（`approve_action`/`mark_executing`），不能从这个口子进——
    专用方法各自要求额外字段（approver_user_id/approved_at），通用口子写不了
    这些字段，如果被绕过去写，会产生"状态是 approved 但没有审批人"的坏数据。
    """

    @pytest.mark.asyncio
    async def test_advance_status_rejects_approved_as_target(self):
        store = OpsStore()
        with pytest.raises(IllegalStatusTransition):
            await store.advance_status("remact_1", STATUS_APPROVED)

    @pytest.mark.asyncio
    async def test_advance_status_rejects_executing_as_target(self):
        store = OpsStore()
        with pytest.raises(IllegalStatusTransition):
            await store.advance_status("remact_1", STATUS_EXECUTING)


class TestApproveActionRequiresPendingApproval:
    @pytest.mark.asyncio
    async def test_rejects_when_not_pending_approval(self, monkeypatch):
        store = OpsStore()
        monkeypatch.setattr(store, "get_action", AsyncMock(return_value=_action(status=STATUS_PROPOSED)))
        with pytest.raises(IllegalStatusTransition):
            await store.approve_action("remact_1", approver_user_id="admin-1")

    @pytest.mark.asyncio
    async def test_allows_when_pending_approval(self, monkeypatch):
        store = OpsStore()
        pending = _action(status=STATUS_PENDING_APPROVAL)
        approved = _action(status=STATUS_APPROVED, approver_user_id="admin-1", approved_at=time.time())

        get_action_mock = AsyncMock(side_effect=[pending, approved])
        monkeypatch.setattr(store, "get_action", get_action_mock)

        fake_conn = AsyncMock()
        # asyncpg 的 execute() 对 UPDATE 返回 "UPDATE <行数>" 这样的命令标签
        # 字符串（不是行数本身）——_conditional_update 靠 rsplit 解析它，
        # 假件必须如实模拟这个返回形状，不能让 AsyncMock 默认返回一个
        # MagicMock 糊弄过去。
        fake_conn.execute = AsyncMock(return_value="UPDATE 1")
        monkeypatch.setattr(store, "_get_pool", AsyncMock(return_value=_FakePool(fake_conn)))

        result = await store.approve_action("remact_1", approver_user_id="admin-1")
        assert result.status == STATUS_APPROVED
        assert result.approver_user_id == "admin-1"


class TestMarkExecutingRequiresApproverFields:
    """§3.3 硬性不变量的核心测试：即使状态字段本身已经是 approved
    （比如未来某个新代码路径不小心通过别的方式把状态改成了 approved 但漏填
    审批人），`mark_executing` 也必须再校验一次 approver_user_id/approved_at
    是否存在，这是"双重保险"，不是信任状态字段就够了。
    """

    @pytest.mark.asyncio
    async def test_rejects_approved_state_missing_approver_fields(self, monkeypatch):
        store = OpsStore()
        # 故意构造一个"状态是 approved 但没有审批人字段"的坏数据，
        # 模拟不变量被绕过的场景。
        corrupted = _action(status=STATUS_APPROVED, approver_user_id=None, approved_at=None)
        monkeypatch.setattr(store, "get_action", AsyncMock(return_value=corrupted))

        with pytest.raises(IllegalStatusTransition):
            await store.mark_executing("remact_1")

    @pytest.mark.asyncio
    async def test_rejects_when_not_approved(self, monkeypatch):
        store = OpsStore()
        monkeypatch.setattr(
            store, "get_action",
            AsyncMock(return_value=_action(status=STATUS_PENDING_APPROVAL)),
        )
        with pytest.raises(IllegalStatusTransition):
            await store.mark_executing("remact_1")

    @pytest.mark.asyncio
    async def test_allows_when_properly_approved(self, monkeypatch):
        store = OpsStore()
        approved = _action(status=STATUS_APPROVED, approver_user_id="admin-1", approved_at=time.time())
        executing = _action(status=STATUS_EXECUTING, approver_user_id="admin-1", approved_at=approved.approved_at)

        get_action_mock = AsyncMock(side_effect=[approved, executing])
        monkeypatch.setattr(store, "get_action", get_action_mock)

        fake_conn = AsyncMock()
        fake_conn.execute = AsyncMock(return_value="UPDATE 1")
        monkeypatch.setattr(store, "_get_pool", AsyncMock(return_value=_FakePool(fake_conn)))

        result = await store.mark_executing("remact_1")
        assert result.status == STATUS_EXECUTING


class TestMarkResultOnlyAcceptsCompletedOrFailed:
    @pytest.mark.asyncio
    async def test_rejects_arbitrary_target_status(self):
        store = OpsStore()
        with pytest.raises(IllegalStatusTransition):
            await store.mark_result("remact_1", STATUS_APPROVED, result={})
