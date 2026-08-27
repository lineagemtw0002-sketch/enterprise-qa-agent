"""路由层测试：`RAGWorkflow._route_after_intent` 对 `intent_type="chitchat"`
的行为（`docs/chitchat_intent_design.md` §4.2 表格 T11~T15）。

可测性关键发现（该文档已核实过，本次复核仍然成立）：
`_route_after_intent(self, state)` 只读 `state` 和 `self._llm`
（`workflow.py::RAGWorkflow._route_after_intent`），所以可以用
`types.SimpleNamespace(_llm=...)` 当 `self`，**未绑定调用**
`RAGWorkflow._route_after_intent(stub, state)`——完全不需要构造
`RAGWorkflow`、不需要 DB、不需要 checkpointer、不需要真实 LLM。

历史记录（T11，Phase 1a 阶段的接缝护栏，已被 Phase 2 落地取代）：
Phase 1a 只改 `intent.py`/`schemas.py`/`chitchat.py`，`workflow.py` 完全
不动，那时 `chitchat` 在 `_route_after_intent` 眼里是"未知值"，四个 `if`
全不命中，落到默认分支 `return "retrieve"`——这是 Phase 1a"零行为变化、
可独立上线"的技术依据（该文档 §2.3-3b 第 4 点），当时的验收测试就是断言
这一点。2026-08-27 Phase 2 给 `workflow.py` 加了真正的 `chitchat →
generate` 路由分支之后，这个"落到默认分支"的行为**不再是 `_route_after_intent`
的真实行为**（现在是被新分支显式处理，不是借道默认分支），继续留一条
断言相同结果的测试会造成"两条测试测的是同一件事、理由却完全不同"的
误导，所以按设计文档 §4.2 T11/T12 的说明整条替换成下面 T12——
**替换动作本身就是 Phase 2 的验收标志**，历史行为记录在本段文字里，
不再保留为可运行的用例。
"""

import types

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


class TestChitchatRoutingPhase2:
    """T12~T15：Phase 2（2026-08-27）真实路由落地后的验收。"""

    def test_chitchat_routes_to_generate(self):
        """T12：Phase 2 核心验收——`self._llm` 存在时，chitchat 现在真的路由到
        "generate"，不再借道默认分支。这条在 Phase 1a 状态下会失败（那时
        默认分支只会返回 "retrieve"），是本条测试存在的意义。"""
        result = RAGWorkflow._route_after_intent(_stub_self(), _state())
        assert result == "generate"

    def test_active_workflow_still_wins_over_chitchat(self):
        """T13：防回归——工作流续填的优先级不许被新分支截胡
        （workflow.py 里 `active_workflow` 检查排在最前面，`workflow.py:325-326`
        一带附近）。"""
        result = RAGWorkflow._route_after_intent(
            _stub_self(), _state(active_workflow={"workflow_type": "leave_request"})
        )
        assert result == "workflow"

    def test_need_clarify_still_wins_over_chitchat(self):
        """T14：防回归——need_clarify 的权威地位不许被削。"""
        result = RAGWorkflow._route_after_intent(_stub_self(), _state(need_clarify=True))
        assert result == "clarify"

    def test_chitchat_degrades_to_retrieve_when_llm_unavailable(self):
        """T15：拍板点 §5-⑧——`self._llm is None` 时降级到 "retrieve"，
        与 tool 分支的降级口径一致，不能路由到一个没有 LLM 就会崩的
        "generate"。这条同时是 Phase 1a 当年那条护栏在字面结果上的延续
        （两个阶段在"无 LLM"这一种情形下确实都返回 "retrieve"），
        但理由已经不同：Phase 1a 是"借道默认分支"，现在是"显式降级判断"，
        本条测试名与 docstring 只描述现在的真实理由，不再混用旧理由。"""
        result = RAGWorkflow._route_after_intent(_stub_self(llm=None), _state())
        assert result == "retrieve"
