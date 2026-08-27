"""子查询拆分的依赖性判据（D1）与扇出硬上限（D2）回归测试（2026-08-25）。

对应设计
--------
`docs/orchestration_design.md` §4.3 决策 D1 / D2，防的是 §4.2 的 F3（假并行）
和 F2/F4（多来源材料拍进同一个 prompt 后被强行关联）。

修复前的实际行为
----------------
- **D1**：拆分提示词只有"多个并列主题就拆"一条判据，三个示例（北京/上海/杭州
  天气、华为/苹果对比、单一主题）全是天然独立的，**没有任何一条规则要求判断
  依赖关系**。于是"销售额最高的部门是哪个，该部门的招聘预算是多少"这种第二问
  必须用第一问答案的问题，也会被拆成两个子查询并行检索。
- **D2**：`workflow.py::_retrieve_multi` 用 `asyncio.gather` 并行跑
  `sub_queries` 里的**每一个**，全仓没有任何扇出上限。

每个测试"在修复前会不会失败"（本项目硬性规则 §7.2，逐条说明）
----------------------------------------------------------------
- `TestDependencyDetector`：**会失败**——`_detect_sub_query_dependency`
  修复前不存在（ImportError）。
- `TestDependentQueriesCollapseToSingleQuery`：**会失败**。修复前 LLM 返回
  几个子查询就原样透传几个（只做 strip 清洗），断言 `len(sub_queries) == 1`
  必红。这两个类（回指型 / 先定实体再查属性型）正是 D1 要挡的两种形态。
- `TestIndependentQueriesStillSplit`：修复前**也通过**——**刻意的防回归测试**。
  D1 最危险的失败模式不是"漏挡"，而是**把真正独立的多主题问题也一并收成
  单查询**，那等于把 P4 这条并行路径整个废掉、且没有任何报错。
  这组用例（含拆分提示词自带的三个示例）钉死"独立的照拆不误"。
- `TestFanoutHardLimit`：**会失败**——修复前 6 个子查询会真的发起 6 次检索，
  断言 `execute` 只被调用 3 次必红；`sub_query_fanout_truncated` 这条 trace
  修复前也不存在。
- `TestChitchatPathUnaffected`：修复前**也通过**——防的是 D1 误伤闲聊短路
  （`_match_chitchat_intent`，2026-08-25 把闲聊误判从 81% 降到 0%）。
  依赖判据只在 `len(sub_queries) > 1` 时才运行，而闲聊路径返回的永远是单元素，
  这组用例把这条"结构上不可能误伤"的性质钉住。
- `TestSubQueryProvenanceLabelsSurvive`：修复前**也通过**——`docs/orchestration_design.md`
  §4.1 明确要求 `[子查询: {q}]` 归属标注**不许在重构中被简化成 `"\\n".join()`**。
  D2 动的正是这个函数，这组用例守住那条约束。
"""

import asyncio
from typing import Any, Dict, List, Optional

import pytest

from src.ragent_backend.intent import (
    MAX_SUB_QUERY_FANOUT,
    QueryAnalysisAndIntentResult,
    QueryAnalysisResult,
    _detect_sub_query_dependency,
    _fallback_split,
    _finalize_sub_queries,
    _match_chitchat_intent,
    analyze_and_route,
    analyze_query,
)
from src.ragent_backend.workflow import (
    _CURRENT_TRACE_QUEUE,
    RAGWorkflow,
)


# ---------------------------------------------------------------- 依赖型样本
# 每条都是 (整句, 修复前 LLM 会拆出来的子查询列表)。
# 第一类：回指型——后一问用"它/他/其/该/上述"指代前一问的答案。
ANAPHORIC_CASES = [
    ("报销政策是什么，其适用范围是什么",
     ["报销政策是什么", "其适用范围是什么"]),
    ("年假制度是什么，上述制度的审批流程是什么",
     ["年假制度是什么", "上述制度的审批流程是什么"]),
    ("公司有哪些培训课程，这些课程怎么报名",
     ["公司有哪些培训课程", "这些课程怎么报名"]),
    ("What is the reimbursement policy and what are its limits",
     ["What is the reimbursement policy", "What are its limits"]),
]

# 第二类：先确定实体、再查该实体属性——不先跑完第一问，第二问根本没法执行。
ENTITY_FIRST_CASES = [
    ("销售额最高的部门是哪个，该部门今年的招聘预算是多少",
     ["销售额最高的部门是哪个", "该部门今年的招聘预算是多少"]),
    ("上个月报销金额最高的员工是谁，他的报销明细是什么",
     ["上个月报销金额最高的员工是谁", "他的报销明细是什么"]),
    ("哪个部门的加班时长最多，加班补贴标准是多少",
     ["哪个部门的加班时长最多", "加班补贴标准是多少"]),
    ("先查出年假天数最多的岗位，再看这个岗位的转正条件",
     ["年假天数最多的岗位是什么", "转正条件是什么"]),
]

DEPENDENT_CASES = ANAPHORIC_CASES + ENTITY_FIRST_CASES

# ---------------------------------------------------------------- 独立型样本
# 前三条就是拆分提示词自带的示例，必须原样继续拆。
INDEPENDENT_CASES = [
    ("北京、上海、杭州的天气怎么样",
     ["北京的天气怎么样", "上海的天气怎么样", "杭州的天气怎么样"]),
    ("华为和苹果的旗舰手机对比",
     ["华为旗舰手机", "苹果旗舰手机"]),
    ("年假制度和报销流程分别是什么",
     ["年假制度是什么", "报销流程是什么"]),
    # 下面几条专门覆盖"字面上带依赖词、语义上其实独立"的误伤风险：
    # 其他/因此/应该/这个月/尤其 分别对应 其/此/该/这个 的否定环视。
    ("远程办公政策和其他公司有什么不同",
     ["远程办公政策是什么", "其他公司的远程办公政策是什么"]),
    ("这个月和上个月的考勤情况",
     ["这个月的考勤情况", "上个月的考勤情况"]),
    ("报销应该怎么办，请假应该怎么办",
     ["报销应该怎么办", "请假应该怎么办"]),
]


class TestDependencyDetector:
    """纯函数层：该判有依赖的判出来，该判独立的一条不碰。"""

    @pytest.mark.parametrize("query,sub_queries", DEPENDENT_CASES)
    def test_dependent_split_is_detected(self, query, sub_queries):
        reason = _detect_sub_query_dependency(query, sub_queries)
        assert reason is not None, f"有依赖却被判成独立: {query!r} -> {sub_queries}"

    @pytest.mark.parametrize("query,sub_queries", INDEPENDENT_CASES)
    def test_independent_split_is_untouched(self, query, sub_queries):
        reason = _detect_sub_query_dependency(query, sub_queries)
        assert reason is None, f"独立拆分被误判成有依赖: {query!r} -> {reason}"

    def test_single_sub_query_never_triggers(self):
        """只有一个子查询时无所谓依赖——判据必须直接短路返回 None。
        这条同时是"不可能误伤闲聊"的结构性保证（闲聊路径永远只有一个元素）。"""
        assert _detect_sub_query_dependency("它是什么", ["它是什么"]) is None
        assert _detect_sub_query_dependency("上述内容", ["上述内容"]) is None

    def test_finalize_collapses_to_the_whole_question(self):
        """降级不是"取第一个子查询"，而是**保留完整原问题**——
        取第一个会把用户后半句的诉求整段丢掉。"""
        whole = "销售额最高的部门是哪个，该部门今年的招聘预算是多少"
        out = _finalize_sub_queries(whole, ["销售额最高的部门是哪个", "该部门今年的招聘预算是多少"])
        assert out == [whole]

    def test_finalize_dedupes_and_strips(self):
        out = _finalize_sub_queries("年假和病假各多少天", ["年假多少天。", " 年假多少天 ", "病假多少天？"])
        assert out == ["年假多少天", "病假多少天"]

    def test_finalize_does_not_truncate(self):
        """扇出上限只在 workflow 的检索节点截断（那里才有 trace），
        intent 这一层不许偷偷截——否则 trace 里看不到丢了什么。"""
        subs = [f"主题{i}是什么" for i in range(6)]
        assert len(_finalize_sub_queries("六个主题", subs)) == 6

    def test_rule_based_fallback_also_applies_dependency_check(self):
        """LLM 不可用时走的规则拆分（按"和/与"切）更容易切出带回指的碎片，
        这条路径同样要过 D1 判据。"""
        assert _fallback_split("年假制度和它的申请流程是什么") == ["年假制度和它的申请流程是什么"]
        # 对照：规则拆分本身没坏
        assert len(_fallback_split("年假制度和报销流程分别是什么")) == 2


# ------------------------------------------------------------------ 链路层
class _FakeStructuredLLM:
    def __init__(self, payload):
        self._payload = payload
        self.calls: List[Any] = []

    async def ainvoke(self, messages):
        self.calls.append(messages)
        return self._payload


class _FakeLLM:
    """固定返回一个"修复前那种拆法"的结构化结果，用来验证后处理层是否兜住。"""

    def __init__(self, payload):
        self.structured = _FakeStructuredLLM(payload)

    def with_structured_output(self, schema, method=None):  # noqa: ARG002
        return self.structured

    @property
    def last_prompt(self) -> str:
        return self.structured.calls[-1][0].content


def _merged_payload(rewritten: str, sub_queries: List[str]) -> QueryAnalysisAndIntentResult:
    return QueryAnalysisAndIntentResult(
        rewritten_query=rewritten,
        sub_queries=sub_queries,
        intent_type="rag",
        confidence=0.9,
        target_tool=None,
        workflow_type=None,
        need_clarify=False,
        reasoning="stub",
    )


class TestDependentQueriesCollapseToSingleQuery:
    """D1 主判据：LLM 照旧拆成多个，后处理必须收成一个。"""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("query,split", DEPENDENT_CASES)
    async def test_analyze_and_route_collapses(self, query, split):
        llm = _FakeLLM(_merged_payload(query, split))
        rewritten, sub_queries, _intent = await analyze_and_route(
            query=query, messages=[], llm=llm, available_tools=[], available_workflows=[],
        )
        assert len(sub_queries) == 1, f"依赖型问题被拆成了 {sub_queries}"
        assert sub_queries == [rewritten]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("query,split", DEPENDENT_CASES)
    async def test_analyze_query_collapses(self, query, split):
        """降级路径（合并调用失败时走的 analyze_query）行为必须一致，
        否则一旦降级就又开始并行拆依赖型问题了。"""
        llm = _FakeLLM(QueryAnalysisResult(rewritten_query=query, sub_queries=split))
        result = await analyze_query(query=query, messages=[], llm=llm)
        assert result.sub_queries == [query]

    @pytest.mark.asyncio
    async def test_prompt_states_the_dependency_criterion(self):
        """D1 的第一层是 prompt 本身（确定性判据只是 1.5b 兜底），
        提示词里必须同时有判据和反例，删掉任何一半这条会红。"""
        query = "年假制度是什么"
        llm = _FakeLLM(_merged_payload(query, [query]))
        await analyze_and_route(
            query=query, messages=[], llm=llm, available_tools=[], available_workflows=[],
        )
        prompt = llm.last_prompt
        assert "有没有依赖" in prompt
        assert "只要存在依赖，就不要拆分" in prompt
        assert "不拆" in prompt  # 反例
        assert str(MAX_SUB_QUERY_FANOUT) in prompt  # D2 上限也写进了提示词


class TestIndependentQueriesStillSplit:
    """防回归：真正独立的多主题问题必须继续拆（D1 最危险的误伤方向）。"""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("query,split", INDEPENDENT_CASES)
    async def test_independent_query_still_splits(self, query, split):
        llm = _FakeLLM(_merged_payload(query, split))
        _rewritten, sub_queries, _intent = await analyze_and_route(
            query=query, messages=[], llm=llm, available_tools=[], available_workflows=[],
        )
        assert sub_queries == split, f"独立的多主题问题被误收成单查询: {query!r}"


class TestChitchatPathUnaffected:
    """闲聊白名单短路（132 条测试守着）必须一点不受影响。"""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("query", ["你好", "你是谁", "谢谢", "你用的是什么模型", "在吗"])
    async def test_chitchat_still_short_circuits_with_single_sub_query(self, query):
        llm = _FakeLLM(_merged_payload(query, [query, query + "？"]))
        rewritten, sub_queries, intent = await analyze_and_route(
            query=query, messages=[], llm=llm, available_tools=[], available_workflows=[],
        )
        # 2026-08-27 Phase 1a（docs/chitchat_intent_design.md）：闲聊标签从
        # 借用的 "rag" 改成真正的第五类 "chitchat"，本条同步更新，见
        # tests/unit/test_intent_chitchat_routing.py 顶部说明。
        assert intent.intent_type == "chitchat"
        assert intent.need_clarify is False
        assert sub_queries == [query]
        assert rewritten == query
        # 闲聊在 LLM 之前就被摘走：零 LLM 调用
        assert llm.structured.calls == []
        assert _match_chitchat_intent(query) is not None


# ------------------------------------------------------------------ D2 扇出
class _FakeStore:
    """RAGWorkflow 构造期不调用 store 的任何方法。"""


class _GraphOnlyLLM:
    """`llm is not None` 才会构建工具子图，而 `_route_after_intent` 里
    "tool_subgraph" 这个分支目标必须存在，否则 `_build_graph()` 直接编译失败。
    被测的 `_retrieve_node` 完全不碰它。"""


class _FakeRetrievalResult:
    def __init__(self, query: str):
        self.content = f"关于「{query}」的检索结果正文"
        self.metadata = {"result_count": 1}


class _RecordingRetrievalTool:
    """记录每一次真实发起的检索——D2 断言的是"实际执行了几次"，
    不是"列表里剩几个"。"""

    def __init__(self):
        self.calls: List[str] = []

    async def execute(  # noqa: ARG002
        self,
        query: str,
        collection: str,
        top_k: int,
        user_id: str | None = None,
        org_id: str | None = None,
    ):
        """⚠️ 签名要跟真实 `QueryKnowledgeHubTool.execute` 保持一致。

        `_retrieve_multi` 的 `_run_one` 用 `except Exception` 兜住每个子查询
        （单个失败不影响其他），所以假工具**少一个参数不会报错**——
        TypeError 被吞掉，表现为 `calls` 为空、检索"什么都没做"。
        2026-08-26 给 execute 加 user_id 时这 4 条就是这么红的，
        而红出来的信息是"断言 [] == [...]"，跟真实原因差很远。
        """
        self.calls.append(query)
        self.last_user_id = user_id
        await asyncio.sleep(0)
        return _FakeRetrievalResult(query)


@pytest.fixture
def workflow_with_recording_tool():
    wf = RAGWorkflow(store=_FakeStore(), llm=_GraphOnlyLLM())
    tool = _RecordingRetrievalTool()
    wf._retrieval_tool = tool
    return wf, tool


def _state(sub_queries: List[str]) -> Dict[str, Any]:
    return {
        "conversation_id": "conv-d2",
        "query": "六个主题的问题",
        "rewritten_query": "六个主题的问题",
        "sub_queries": sub_queries,
        "top_k": 5,
        "trace_events": [],
    }


async def _drain_trace_queue(queue: "asyncio.Queue[Dict[str, Any]]") -> List[Dict[str, Any]]:
    """_emit_trace 是 create_task 推送的，先让出一轮再取。"""
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    events: List[Dict[str, Any]] = []
    while not queue.empty():
        events.append(queue.get_nowait())
    return events


SIX_SUB_QUERIES = [
    "年假制度是什么",
    "报销流程是什么",
    "远程办公政策是什么",
    "差旅住宿标准是多少",
    "加班补贴怎么算",
    "试用期多久转正",
]


class TestFanoutHardLimit:
    """D2：扇出硬上限 = 3，超出截断并留痕。"""

    @pytest.mark.asyncio
    async def test_only_limit_many_retrievals_are_executed(self, workflow_with_recording_tool):
        wf, tool = workflow_with_recording_tool
        assert len(SIX_SUB_QUERIES) > MAX_SUB_QUERY_FANOUT

        await wf._retrieve_node(_state(list(SIX_SUB_QUERIES)))

        assert len(tool.calls) == MAX_SUB_QUERY_FANOUT, (
            f"扇出没有被截断，实际发起了 {len(tool.calls)} 次检索: {tool.calls}"
        )
        assert tool.calls == SIX_SUB_QUERIES[:MAX_SUB_QUERY_FANOUT]

    @pytest.mark.asyncio
    async def test_truncation_is_recorded_in_state_trace_events(self, workflow_with_recording_tool):
        wf, _tool = workflow_with_recording_tool
        update = await wf._retrieve_node(_state(list(SIX_SUB_QUERIES)))

        events = [e for e in update["trace_events"] if e.get("sub_query_fanout_truncated")]
        assert len(events) == 1, f"trace_events 里没有截断记录: {update['trace_events']}"
        event = events[0]
        assert event["sub_query_fanout_limit"] == MAX_SUB_QUERY_FANOUT
        assert event["dropped_sub_queries"] == SIX_SUB_QUERIES[MAX_SUB_QUERY_FANOUT:]
        assert event["sub_query_count"] == MAX_SUB_QUERY_FANOUT

    @pytest.mark.asyncio
    async def test_truncation_is_emitted_to_trace_panel(self, workflow_with_recording_tool):
        """流式路径（TracePanel）也要看得见截断，不能只落在 state 里。"""
        wf, _tool = workflow_with_recording_tool
        queue: asyncio.Queue = asyncio.Queue()
        _CURRENT_TRACE_QUEUE.set(queue)
        try:
            await wf._retrieve_node(_state(list(SIX_SUB_QUERIES)))
            events = await _drain_trace_queue(queue)
        finally:
            _CURRENT_TRACE_QUEUE.set(None)

        truncated = [e for e in events if e["step"] == "sub_query_fanout_truncated"]
        assert len(truncated) == 1, f"TracePanel 收不到截断事件: {[e['step'] for e in events]}"
        payload = truncated[0]["payload"]
        assert payload["limit"] == MAX_SUB_QUERY_FANOUT
        assert payload["dropped_count"] == len(SIX_SUB_QUERIES) - MAX_SUB_QUERY_FANOUT
        assert payload["dropped_sub_queries"] == SIX_SUB_QUERIES[MAX_SUB_QUERY_FANOUT:]

    @pytest.mark.asyncio
    async def test_at_limit_is_not_truncated(self, workflow_with_recording_tool):
        """边界：正好 3 个不算超限，不许误截、也不许留假的截断记录。"""
        wf, tool = workflow_with_recording_tool
        exactly = SIX_SUB_QUERIES[:MAX_SUB_QUERY_FANOUT]

        update = await wf._retrieve_node(_state(list(exactly)))

        assert tool.calls == exactly
        assert not [e for e in update["trace_events"] if e.get("sub_query_fanout_truncated")]

    @pytest.mark.asyncio
    async def test_single_sub_query_still_uses_single_query_path(self, workflow_with_recording_tool):
        """单子查询走的是原来的单查询路径，行为不变：用 `rewritten_query`
        整句检索一次、结果不带 `[子查询: ]` 标签。"""
        wf, tool = workflow_with_recording_tool
        state = _state(["年假制度是什么"])
        update = await wf._retrieve_node(state)
        assert tool.calls == [state["rewritten_query"]]
        assert "[子查询:" not in update["retrieval_context"]


class TestSubQueryProvenanceLabelsSurvive:
    """`docs/orchestration_design.md` §4.1：`[子查询: X]` 归属标注是 P4
    唯一做对的地方，明确要求不许在重构中被简化掉。D2 改的正是这个函数。"""

    @pytest.mark.asyncio
    async def test_each_kept_sub_query_is_labelled(self, workflow_with_recording_tool):
        wf, _tool = workflow_with_recording_tool
        update = await wf._retrieve_node(_state(list(SIX_SUB_QUERIES)))
        context = update["retrieval_context"]

        for q in SIX_SUB_QUERIES[:MAX_SUB_QUERY_FANOUT]:
            assert f"[子查询: {q}]" in context
        for q in SIX_SUB_QUERIES[MAX_SUB_QUERY_FANOUT:]:
            assert q not in context, f"被截断的子查询仍出现在上下文里: {q}"
        assert context.count("---") >= MAX_SUB_QUERY_FANOUT - 1
