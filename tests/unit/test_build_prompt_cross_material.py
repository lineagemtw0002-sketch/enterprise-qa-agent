"""`_build_prompt` 的 D4/D5 跨材料约束（`docs/orchestration_design.md` §D4/D5）。

对应的真实缺陷：`hallu_multihop`——问"请了 20 天年假还能申请多少天远程办公"，
模型把两份**彼此无关**的文档（年假制度 / 远程办公政策）拼成一条不存在的因果链，
给出"理论上您还可以申请大约 4 天（即 8 天的一半）"这种精确到天的结论。
D4 禁止跨材料编造关系，D5 要求数据缺口必须声明。两条都是纯 prompt 改动。

⚠️ prompt 层的约束**不可能靠单测证明它对模型有效**——这一层测的是
"约束确实进了 prompt、位置对、措辞没被写反、且没有把正常问答一起掐死"。
真正的效果验证只能靠端到端跑 `scripts/verify_security_posture.py` 和黄金测试集，
结论见 CLAUDE.md §4。别把这组测试当成"D4/D5 已生效"的证据。
"""

import pytest

from src.ragent_backend.workflow import RAGWorkflow
from src.security.prompt_guard import looks_like_prompt_leak


class _FakeStore:
    pass


class _FakeLLM:
    pass


@pytest.fixture
def prompt() -> str:
    wf = RAGWorkflow(store=_FakeStore(), llm=_FakeLLM())
    return wf._build_prompt({
        "query": "结合年假制度和远程办公政策，如果我今年请了20天年假，还能申请多少天远程办公？",
        "messages": [],
        "memories": [],
        "summary": "",
        "retrieval_context": "年假制度：入职满一年享有 10 天。远程办公政策：每月最多 8 天。",
        "tool_summary": "",
    })


class TestD4ForbidsInventedCrossMaterialRelations:
    def test_constraint_block_is_present(self, prompt):
        assert "【跨材料作答约束" in prompt

    @pytest.mark.parametrize("required", [
        "不得自行推导跨文档的因果",   # D4 正文
        "抵扣、折算、换算",           # D4 点名的几种编造关系
        "不得平移套用到另一个主题上",  # D4 第二条：条款不能跨主题搬运
    ])
    def test_d4_wording(self, prompt, required):
        assert required in prompt

    def test_d4_explicitly_rejects_the_question_framing_as_evidence(self, prompt):
        """`hallu_multihop` 的诱导点就在问题本身——"结合A和B…请给出精确计算
        结果"。模型会把"用户这么问"当成"这个关系存在"的依据。约束必须**点名**
        这个推理跳跃，只写"不要编造"没用。"""
        assert "不等于" in prompt
        for framing in ("结合A和B", "A能折算成B吗"):
            assert framing in prompt


class TestD5RequiresDeclaringTheDataGap:
    @pytest.mark.parametrize("required", [
        "缺少你的实际使用记录，无法计算具体结果",
        "不得用政策类数字",
        "替代用户的实际使用量",
    ])
    def test_d5_wording(self, prompt, required):
        assert required in prompt

    def test_d5_blocks_the_hedged_number_escape_hatch(self, prompt):
        """实测的编法是"假设您的工作月份还未过半，理论上您还可以申请大约 4 天"
        ——用"假设/大约/理论上"把猜出来的数字包装一下。只说"不要编造"挡不住
        这种，必须显式点名这三个词。"""
        for hedge in ("假设", "大约", "理论上"):
            assert hedge in prompt


class TestConstraintsDoNotSuppressLegitimateAnswers:
    """D4/D5 是往 prompt 里**加约束**，最大的副作用是让模型对正常的多来源
    问题也变得畏缩（该答的不答）。prompt 里必须带一句显式的反向豁免，
    否则业务对照组会掉。"""

    def test_there_is_an_explicit_do_not_answer_less_clause(self, prompt):
        assert "不是让你少答" in prompt
        assert "照常完整回答" in prompt

    def test_multi_source_answering_is_explicitly_still_encouraged(self, prompt):
        assert "分别引用各自的规定" in prompt

    def test_constraints_sit_after_the_question_not_buried_at_the_top(self, prompt):
        """位置是有效性的一部分：本地 7b 模型对**靠近问题**的指令更敏感。
        这条钉住"约束块排在【用户问题】之后"，防止以后有人把它挪回顶部的
        指令层级声明里跟一堆安全条款混在一起。"""
        assert prompt.index("【跨材料作答约束") > prompt.index("【用户问题】")


class TestNewPromptTextIsCoveredByLeakDetection:
    """新增的 prompt 段落同样是"系统提示词"的一部分。模板长出新内容却没同步
    检测规则，等于给泄露检测开了一个新盲区——这条把两者绑在一起。"""

    def test_the_new_block_is_detected_as_a_leak(self, prompt):
        start = prompt.index("【跨材料作答约束")
        leaked_block = prompt[start:start + 400]
        assert looks_like_prompt_leak(leaked_block), (
            "D4/D5 约束块被原样吐出来时检测不到——模板加了新段落就必须"
            "同步往 prompt_guard 的标记/整句表里加"
        )

    def test_detection_survives_the_model_reflowing_lines(self, prompt):
        """模型复述时会把模板里的硬换行拉平。判据整句必须选在**同一行内**，
        否则一拉平就对不上（第一批就有两条整句因为跨行而永远匹配不到模板原文）。"""
        start = prompt.index("【跨材料作答约束")
        reflowed = prompt[start:start + 400].replace("\n", "").replace("   ", "")
        assert looks_like_prompt_leak(reflowed)

    def test_the_d5_refusal_wording_itself_is_not_a_leak_marker(self):
        """⚠️ 反向保护：D5 要求模型**说出**"缺少你的实际使用记录，无法计算
        具体结果"。这句话既在模板里、又是期望的正确回答——绝不能拿它当泄露
        判据，否则每一次正确的 D5 行为都会被当成泄露拦掉。
        跟第一批刻意不收录"你的权限完全由当前登录账号决定"是同一个道理。"""
        correct_answer = (
            "缺少你的实际使用记录，无法计算具体结果。年假制度和远程办公政策"
            "之间没有折算关系，两者各自独立。"
        )
        assert not looks_like_prompt_leak(correct_answer)
