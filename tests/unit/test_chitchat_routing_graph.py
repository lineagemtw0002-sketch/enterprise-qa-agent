"""路由层测试：`RAGWorkflow._route_after_intent` 对 `intent_type="chitchat"`
的行为（`docs/chitchat_intent_design.md` §4.2 表格 T11~T15）。

可测性关键发现（该文档已核实过，本次复核仍然成立）：
`_route_after_intent(self, state)` 只读 `state` 和 `self._llm`
（`workflow.py::RAGWorkflow._route_after_intent`），所以可以用
`types.SimpleNamespace(_llm=...)` 当 `self`，**未绑定调用**
`RAGWorkflow._route_after_intent(stub, state)`——完全不需要构造
`RAGWorkflow`、不需要 DB、不需要 checkpointer、不需要真实 LLM。

T11 属于 Phase 1a（`intent.py`/`schemas.py`/`chitchat.py`，workflow.py 完全
不动）：`chitchat` 在 `_route_after_intent` 眼里是"未知值"，四个 `if` 全不
命中，落到默认分支 `return "retrieve"`——这与今天 `rag` 的走法逐字相同，
是 Phase 1a"零行为变化、可独立上线"的技术依据（该文档 §2.3-3b 第 4 点）。

T12~T15 属于 Phase 2（`workflow.py` 加 `chitchat → generate` 的真实路由），
在 Phase 2 提交时一并补上——本文件在 Phase 1a 阶段只包含 T11。
"""

import types

import pytest

from src.ragent_backend.workflow import RAGWorkflow


def _stub_self(llm=object()):
    return types.SimpleNamespace(_llm=llm)


def _state(**overrides):
    base = {
        "intent_type": "chitchat",
        "need_clarify": False,
        "active_workflow": None,
    }
    base.update(overrides)
    return base


class TestChitchatRoutingPhase1a:
    def test_chitchat_routes_to_retrieve_before_phase2(self):
        """T11：接缝护栏——证明 Phase 1a 单独上线时用户可见行为零变化。
        旧实现下（`intent_type` 的 Literal 里根本没有 "chitchat"，但
        `_route_after_intent` 本身只读裸字符串，不做 Literal 校验）这条也
        通过，因为默认分支本来就返回 "retrieve"——这正是本条测试存在的
        意义：钉住"新标签在 Phase 2 之前完全借道默认分支"这件事本身，
        Phase 2 落地后这条会被替换成断言 "generate"（替换动作本身就是
        Phase 2 的验收，见该文件顶部说明）。"""
        result = RAGWorkflow._route_after_intent(_stub_self(), _state())
        assert result == "retrieve"

    def test_active_workflow_still_wins_over_chitchat(self):
        """防回归：工作流续填的优先级不许被 chitchat 标签截胡。"""
        result = RAGWorkflow._route_after_intent(
            _stub_self(), _state(active_workflow={"workflow_type": "leave_request"})
        )
        assert result == "workflow"

    def test_need_clarify_still_wins_over_chitchat(self):
        """防回归：need_clarify 的权威地位不许被削。"""
        result = RAGWorkflow._route_after_intent(_stub_self(), _state(need_clarify=True))
        assert result == "clarify"
