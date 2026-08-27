"""闲聊路由回归测试（2026-08-25 首版；2026-08-27 Phase 1a 更新标签）。

对应问题
--------
`docs/review_2026-08-25/smalltalk_routing_regression.md`：21 条闲聊 × 2 次实测，
误判率 **81%**——57% 被 `_needs_clarify_rule` 的 `len < 4` 拦成固定澄清话术，
24% 被 1.5b router 判成 `tool + query_knowledge_hub` 后撞上 `workflow.py` 的
知识库空命中短路（用户看到"您的知识库里没有『你是谁』"）。

修法：在 LLM 调用**之前**加一层高精度闲聊白名单短路
（`intent._match_chitchat_intent`），命中就判 `chitchat`，**长度阈值原样不动**。

2026-08-27 更新（`docs/chitchat_intent_design.md` Phase 1a）：终判标签从
借用的 `rag` 改成真正的第五类 `chitchat`。按该文档 §2.4 的盘点，这次改动
**只动 3 行断言 + 1 处测试改名**——回归保护的主体
（`TestVagueShortQueryStillClarifies` / `TestBusinessQueriesUnaffected`）
守的是"哪些句子算闲聊、哪些不算"，跟标签叫什么无关，一行未动。

每个测试"在修复前会不会失败"（本项目硬性规则，逐条说明）
------------------------------------------------------
- `TestChitchatWhitelist` / `TestChitchatShortCircuitsBeforeLLM`：**会失败**。
  修复前 `_match_chitchat_intent` 根本不存在（ImportError），且这些用例实测
  终态是 clarify / kb_refusal，不是 chitchat。
- `TestVagueShortQueryStillClarifies`：修复前**也通过**——这是**刻意的防回归
  测试**，它守的不是"新功能"，而是"修的时候没把旧能力弄丢"。做过 A/B 对照
  实验：把长度阈值从 `<4` 放宽到关掉，闲聊总误判率 81.0%→66.7% 看着变好，
  但更有害的 kb_refusal 反而从 23.8% **涨到 28.6%**。所以本次**没有**动阈值。
  这组测试就是钉死这个决定：**任何人后来想用"放宽长度阈值"来修闲聊，这里会红。**
- `TestBusinessQueriesUnaffected`：混合型（"你好，年假多少天"）在修复前
  **不会失败**（那时没有白名单，业务问题当然不会被白名单吞掉），但它守的是
  白名单类修法最危险的失败模式——**误伤业务问答**。没有这组测试，白名单一
  扩大就可能悄悄吃掉带寒暄前缀的正经业务问题。
- `test_chitchat_label_is_chitchat` / `test_chitchat_no_longer_borrows_rag_label`：
  **会失败**（旧值是 `"rag"`）——钉死 Phase 1a 这次改动本身
  （`docs/chitchat_intent_design.md` §2.4 ③"在旧实现下它会失败"那条要求）。
"""

import pytest

from src.ragent_backend.intent import (
    _has_vague_pronoun,
    _match_chitchat_intent,
    _needs_clarify_rule,
    analyze_and_route,
    detect_intent,
)

# 诊断报告里那 6 类闲聊，逐类参数化
GREETINGS = ["你好", "您好", "早上好", "在吗", "hello", "嗨，在忙吗", "哈喽", "晚上好啊"]
IDENTITY = ["你好，你是谁", "你是谁", "你能做什么", "你会什么", "介绍一下你自己",
            "你叫什么名字", "你是机器人吗", "说说你自己"]
COURTESY = ["谢谢", "好的", "辛苦了", "再见", "非常感谢你的帮助", "明白了", "收到", "拜拜"]
META = ["你用的是什么模型", "你是怎么工作的", "你的回答准确吗", "你背后用的是什么模型"]
MIXED_CHITCHAT = ["你好，我想问个问题", "你能帮我查东西吗", "在吗，有点事想请教你"]

ALL_CHITCHAT = GREETINGS + IDENTITY + COURTESY + META + MIXED_CHITCHAT

# 对照组：真正的业务问题，一条都不许被白名单吞掉
BUSINESS_KB = [
    "年假多少天",
    "远程办公政策是什么",
    "报销流程是怎样的",
    "员工手册里关于加班是怎么规定的",
    "试用期多久可以转正",
    "差旅住宿标准是多少",
    "入职需要准备哪些材料",
]
# 最危险的一类：寒暄措辞包着真业务问题
BUSINESS_KB_MIXED = [
    "你好，年假多少天",
    "你能帮我查一下报销流程吗",
    "谢谢，那远程办公政策呢",
    "你好，请问出差住宿标准是多少",
]
BUSINESS_TOOL = ["我这个月迟到几次", "上个月我的考勤记录", "我这周的打卡情况怎么样"]
BUSINESS_WORKFLOW = ["我想请假", "我要报销", "帮我报修电脑"]

# 真正模糊、必须继续被拦成 clarify 的短查询（不是闲聊，是残缺提问）
VAGUE_SHORT = ["他呢", "多少", "这个", "那个", "它", "呢", "上面", "怎么"]


class _FakeStructuredLLM:
    """把结构化输出固定成一个预设结果，用来断言"某条查询确实走到了 LLM"。"""

    def __init__(self, payload):
        self._payload = payload
        self.calls = []

    async def ainvoke(self, messages):
        self.calls.append(messages)
        return self._payload


class _FakeLLM:
    def __init__(self, payload):
        self.structured = _FakeStructuredLLM(payload)

    def with_structured_output(self, schema, method=None):  # noqa: ARG002
        return self.structured

    @property
    def call_count(self) -> int:
        return len(self.structured.calls)


def _fake_kb_payload(query: str):
    from src.ragent_backend.intent import QueryAnalysisAndIntentResult

    return QueryAnalysisAndIntentResult(
        rewritten_query=query,
        sub_queries=[query],
        intent_type="tool",
        confidence=0.9,
        target_tool="query_knowledge_hub",
        workflow_type=None,
        need_clarify=False,
        reasoning="stub: 该查企业知识库",
    )


KB_TOOL_SCHEMA = [{"type": "function", "function": {"name": "query_knowledge_hub",
                                                    "description": "查询企业知识库"}}]


class TestChitchatWhitelist:
    """纯函数层：白名单该命中的命中、不该命中的一条不碰。

    2026-08-27 Phase 1a 重构（`docs/chitchat_intent_design.md` §2.4 ①）：
    把"命中判定"和"标签值"拆成两组独立断言——35 条参数化的那组只断言
    **行为契约**（用户不会看到固定拒绝话术/固定澄清话术这件事本身），
    不再断言 `== "rag"`；标签值单独用一条具名用例钉死，以后标签再改名
    也只影响这一条。
    """

    @pytest.mark.parametrize("query", ALL_CHITCHAT)
    def test_chitchat_is_matched(self, query):
        """行为契约（T3）：闲聊必须被白名单识别、不进澄清话术、不进知识库
        检索工具——这三条才是"用户不会看到固定话术"的真正契约，比标签值
        更接近意图，以后改标签不用再动这 35 条。"""
        result = _match_chitchat_intent(query)
        assert result is not None, f"闲聊未被白名单识别: {query!r}"
        # target_tool != "query_knowledge_hub"：不会撞 workflow.py 的知识库
        # 空命中短路；need_clarify is False：不会被 clarify 节点用固定话术
        # 短路。
        assert result.need_clarify is False
        assert result.target_tool != "query_knowledge_hub"
        assert result.workflow_type is None

    def test_chitchat_label_is_chitchat(self):
        """T1：标签值本身。旧实现下这里会失败（旧值是 "rag"）。"""
        result = _match_chitchat_intent("你好")
        assert result is not None
        assert result.intent_type == "chitchat"

    def test_chitchat_no_longer_borrows_rag_label(self):
        """T2：钉死"闲聊不再借用 rag 这个桶"这个决定本身。"""
        result = _match_chitchat_intent("你好")
        assert result is not None
        assert result.intent_type != "rag"

    @pytest.mark.parametrize(
        "query", BUSINESS_KB + BUSINESS_KB_MIXED + BUSINESS_TOOL + BUSINESS_WORKFLOW + VAGUE_SHORT
    )
    def test_non_chitchat_is_not_matched(self, query):
        assert _match_chitchat_intent(query) is None, f"白名单误伤: {query!r}"

    @pytest.mark.parametrize("query", ["", "   ", "\n"])
    def test_empty_query_not_matched(self, query):
        assert _match_chitchat_intent(query) is None

    def test_long_sentence_never_short_circuits(self):
        """够长的句子即使以寒暄开头也不短路——长句大概率夹带了正事。"""
        long_q = "你好你好你好你好你好你好你好你好你好你好你好你好你好你好你好你好"
        assert len(long_q) > 30
        assert _match_chitchat_intent(long_q) is None

    def test_greeting_prefix_with_business_topic_is_vetoed(self):
        """逐段判定 + 业务词一票否决：只要有一段是业务问题，整句就不短路。"""
        assert _match_chitchat_intent("你好，你是谁") is not None
        assert _match_chitchat_intent("你好，年假多少天") is None

    def test_tail_particles_are_normalized(self):
        """句尾语气词剥掉后仍能命中（"在不在呀" -> "在不在"）。"""
        assert _match_chitchat_intent("在不在呀") is not None
        assert _match_chitchat_intent("你好呀~") is not None

    def test_substring_match_is_not_used(self):
        """白名单是**整段精确/正则匹配**，不是子串匹配——否则任何一句提到
        "你好"的业务问句都会被吞掉。"""
        assert _match_chitchat_intent("报销单上写你好这两个字算不算错") is None


class TestVagueShortQueryStillClarifies:
    """防回归：`_needs_clarify_rule` 拦真正模糊短查询的能力一点没丢。

    这组测试在修复前也是绿的（见模块 docstring）。它守的是那个被 A/B 数据
    否决掉的修法——**谁要是把长度阈值放宽/删掉，这里立刻变红**。
    """

    @pytest.mark.parametrize("query", VAGUE_SHORT)
    def test_vague_short_query_still_clarifies(self, query):
        result = _needs_clarify_rule(query)
        assert result is not None, f"模糊短查询不该漏过澄清规则: {query!r}"
        assert result.intent_type == "clarify"
        assert result.need_clarify is True

    def test_length_threshold_is_still_four(self):
        """阈值本身就是被测对象：3 字拦、4 字放行。"""
        assert _needs_clarify_rule("abc") is not None
        assert _needs_clarify_rule("abcd") is None

    def test_vague_pronoun_rule_untouched(self):
        assert _has_vague_pronoun("它多少钱") is True
        assert _has_vague_pronoun("我这个月迟到了几次") is False
        assert _needs_clarify_rule("它多少钱") is not None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("query", VAGUE_SHORT)
    async def test_vague_short_query_clarifies_end_to_end(self, query):
        """走完整的 detect_intent（llm=None，纯规则）：闲聊短路排在澄清检查
        前面，但不能顺手把模糊短查询也放行。"""
        intent = await detect_intent(query, llm=None, available_tools=[], available_workflows=[])
        assert intent.need_clarify is True
        assert intent.intent_type == "clarify"


class TestChitchatShortCircuitsBeforeLLM:
    """链路层：闲聊在 LLM 之前就被摘走，零 LLM 调用。"""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("query", ["你好", "你是谁", "谢谢", "你用的是什么模型"])
    async def test_detect_intent_returns_chitchat_without_llm_call(self, query):
        llm = _FakeLLM(_fake_kb_payload(query))
        intent = await detect_intent(
            query, llm=llm, available_tools=KB_TOOL_SCHEMA, available_workflows=[],
        )
        assert intent.intent_type == "chitchat"
        assert intent.need_clarify is False
        assert intent.target_tool is None
        assert llm.call_count == 0, "闲聊不该触发 LLM 分类调用"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("query", ALL_CHITCHAT)
    async def test_analyze_and_route_returns_chitchat_without_llm_call(self, query):
        """合并链路（线上走的这条）：即使 LLM 被固定成"判去查知识库"，
        闲聊也不会走到它，因此不会撞上知识库空命中短路。"""
        llm = _FakeLLM(_fake_kb_payload(query))
        rewritten, sub_queries, intent = await analyze_and_route(
            query=query, messages=[], llm=llm,
            available_tools=KB_TOOL_SCHEMA, available_workflows=[],
        )
        assert intent.intent_type == "chitchat"
        assert intent.need_clarify is False
        assert intent.target_tool != "query_knowledge_hub"
        assert sub_queries == [rewritten]
        assert llm.call_count == 0

    @pytest.mark.asyncio
    async def test_chitchat_reasoning_is_traceable(self):
        """reasoning 要能在 trace 里看出这次是被闲聊规则短路的（可观测性）。"""
        llm = _FakeLLM(_fake_kb_payload("你好"))
        _, _, intent = await analyze_and_route(
            query="你好", messages=[], llm=llm,
            available_tools=KB_TOOL_SCHEMA, available_workflows=[],
        )
        assert "闲聊" in (intent.reasoning or "")


class TestBusinessQueriesUnaffected:
    """对照组：业务问答链路一点没变——白名单不碰它们，LLM 的判断原样透传。"""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("query", BUSINESS_KB + BUSINESS_KB_MIXED)
    async def test_kb_questions_still_reach_llm_and_keep_tool_intent(self, query):
        llm = _FakeLLM(_fake_kb_payload(query))
        _, _, intent = await analyze_and_route(
            query=query, messages=[], llm=llm,
            available_tools=KB_TOOL_SCHEMA, available_workflows=[],
        )
        assert llm.call_count == 1, f"业务问题必须走到 LLM 分类: {query!r}"
        assert intent.intent_type == "tool"
        assert intent.target_tool == "query_knowledge_hub"
        assert intent.need_clarify is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("query", BUSINESS_WORKFLOW)
    async def test_workflow_action_shortcut_still_wins(self, query):
        """闲聊短路插在工作流动作短路**之后**，不能截胡"我想请假"。"""
        workflows = [
            {"workflow_type": "leave_request", "display_name": "请假申请", "description": ""},
            {"workflow_type": "expense_reimbursement", "display_name": "报销申请", "description": ""},
            {"workflow_type": "laptop_repair", "display_name": "电脑报修", "description": ""},
        ]
        llm = _FakeLLM(_fake_kb_payload(query))
        _, _, intent = await analyze_and_route(
            query=query, messages=[], llm=llm,
            available_tools=KB_TOOL_SCHEMA, available_workflows=workflows,
        )
        assert intent.intent_type == "workflow"
        assert llm.call_count == 0

    @pytest.mark.asyncio
    async def test_attendance_question_not_swallowed(self):
        query = "我这个月迟到几次"
        assert _match_chitchat_intent(query) is None
        tools = [{"type": "function", "function": {"name": "query_attendance", "description": "查考勤"}}]
        from src.ragent_backend.intent import QueryAnalysisAndIntentResult

        llm = _FakeLLM(QueryAnalysisAndIntentResult(
            rewritten_query=query, sub_queries=[query], intent_type="tool",
            confidence=0.9, target_tool="query_attendance", need_clarify=False,
            reasoning="stub: 查考勤",
        ))
        _, _, intent = await analyze_and_route(
            query=query, messages=[], llm=llm, available_tools=tools, available_workflows=[],
        )
        assert intent.intent_type == "tool"
        assert intent.target_tool == "query_attendance"
        assert llm.call_count == 1
