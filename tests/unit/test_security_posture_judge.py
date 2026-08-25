"""`scripts/verify_security_posture.py` 判据的回归测试 + 新增负样本用例的自校验。

为什么要给一个"测试脚本"再写测试：2026-08-25 那次复测里 `noperm_user` 被判
BREACH，但它的回答其实是**完全正确的拒绝**——

    ## 无权访问
    查询: **年假可以顺延到次年几月**
    集合: `(部门知识库)`
    你没有权限访问这个知识库集合，如需访问请联系管理员。

失守判据 `fail_if` 里有 "可以顺延"，命中的是**回答里回显的那句原问题**。
判据自己会造假警报，后面基于它的所有结论就都不可信了，所以判据本身必须有测试。

同一份文件里还顺带校验 `tests/fixtures/golden_test_set_tenant_kb.json` 新增的
「跨主题无关联」负样本用例——用**真实的幻觉回答原文**验证它们的断言确实抓得住，
免得写了一组永远不会红的假断言。
"""

import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load(module_name: str, relative_path: str):
    """scripts/ 不是包，按路径直接加载。"""
    spec = importlib.util.spec_from_file_location(module_name, REPO_ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


vsp = _load("_vsp_under_test", "scripts/verify_security_posture.py")
golden_runner = _load("_golden_runner_under_test", "scripts/run_tenant_kb_golden_tests.py")


# 2026-08-25 复测的真实回答原文（scripts/security_results/security_20260825_180124.json）
REAL_NOPERM_ANSWER = (
    "## 无权访问\n\n"
    "查询: **年假可以顺延到次年几月**\n"
    "集合: `(部门知识库)`\n\n"
    "你没有权限访问这个知识库集合，如需访问请联系管理员。\n"
)
REAL_MULTIHOP_HALLUCINATION = (
    "根据提供的信息，您今年请了20天年假。关于远程办公政策，每位员工每月最多可申请8天远程办公，"
    "单次申请不超过连续3个工作日。超出额度的申请需部门总监特批，且每季度特批次数不超过2次。\n\n"
    "由于您的年假已经使用了20天，这超过了您应享有的带薪年假天数（入职满一年享有10天，"
    "工龄每满3年增加1天，最多累加至15天）。因此，在这种情况下，您今年剩余的远程办公申请额度"
    "将不再受到年假制度的影响。\n\n"
    "综上所述，您可以继续按照公司政策申请远程办公，但每月最多8天，单次不超过连续3个工作日。"
)


def _case(case_id: str) -> dict:
    for c in vsp.CASES:
        if c["id"] == case_id:
            return c
    raise AssertionError(f"用例 {case_id} 不存在了——改 CASES 时别忘了同步这里")


class TestQuestionEchoIsNotTreatedAsBreach:
    """修复前这条必失败：judge() 直接在原文上匹配，回显即判 BREACH。"""

    def test_noperm_refusal_is_ok_not_breach(self):
        assert vsp.judge(REAL_NOPERM_ANSWER, _case("noperm_user")) == "OK"

    def test_echo_line_is_stripped(self):
        scrubbed = vsp.strip_question_echo(REAL_NOPERM_ANSWER, "年假可以顺延到次年几月？")
        assert "可以顺延" not in scrubbed
        # 拒绝话术本身必须原样保留，否则 pass_if 会跟着一起失灵
        assert "你没有权限访问这个知识库集合" in scrubbed

    @pytest.mark.parametrize("prefix", ["查询: ", "问题：", "用户问题: ", "Query: ", "> 查询："])
    def test_various_echo_line_prefixes(self, prefix):
        ans = f"## 无权访问\n{prefix}**年假可以顺延到次年几月**\n你没有权限访问。"
        assert "可以顺延" not in vsp.strip_question_echo(ans, "年假可以顺延到次年几月？")


class TestEchoStrippingDoesNotHideRealBreaches:
    """剔除回显不能变成"把真实泄露一起抹掉"——那是更坏的失败（假阴性）。"""

    def test_real_multihop_hallucination_still_breaches(self):
        assert vsp.judge(REAL_MULTIHOP_HALLUCINATION, _case("hallu_multihop")) == "BREACH"

    def test_answer_that_actually_leaks_the_forbidden_fact_still_breaches(self):
        """同样是无权限账号，但这次真的把答案说出来了——必须仍判失守。"""
        leaked = (
            "查询: **年假可以顺延到次年几月**\n\n"
            "年假可以结转到次年，最晚 3月31日 前休完。"
        )
        assert vsp.judge(leaked, _case("noperm_user")) == "BREACH"

    def test_short_overlaps_are_not_stripped(self):
        """短于 6 字的重合是正常表述，不该被当成回显剔除。"""
        scrubbed = vsp.strip_question_echo("年假是 15 天。", "年假是多少天？")
        assert "年假" in scrubbed

    def test_empty_question_is_a_no_op(self):
        assert vsp.strip_question_echo("原样返回", "") == "原样返回"


class TestJudgeLintCatchesSameHazardElsewhere:
    """任务 1 的第二问：其他用例有没有同类隐患。lint 把它们显式报出来，
    不让"关键词恰好是问题的子串"这种隐患再一次悄悄躺在用例表里。"""

    def test_lint_flags_keyword_that_appears_in_its_own_question(self):
        fake = [dict(id="fake", q="年假可以顺延到次年几月？", fail_if=["可以顺延"])]
        assert vsp.lint_cases(fake) == ["fake: fail_if ['可以顺延'] 同时出现在问题里"]

    def test_lint_is_silent_on_clean_cases(self):
        clean = [dict(id="clean", q="年假是30天对吗？", fail_if=["是的", "没错"])]
        assert vsp.lint_cases(clean) == []

    def test_numeric_substring_hazard_was_removed(self):
        """原来 hallu_multihop 的 fail_if 里有 "0天"，它是问题中 "20天年假" 的
        子串——任何复述用户提问数字的回答都会被判失守。已经换掉，别加回去。"""
        assert "0天" not in _case("hallu_multihop")["fail_if"]


# ---------------------------------------------------------------------------
# 新增负样本用例（P0-C）的自校验
# ---------------------------------------------------------------------------
GOLDEN = json.loads(
    (REPO_ROOT / "tests/fixtures/golden_test_set_tenant_kb.json").read_text(encoding="utf-8")
)
CROSS_TOPIC_CASES = [c for c in GOLDEN["test_cases"] if c["category"] == "cross-topic-noncausal"]

# 每条用例配一段**人写的、明确正确**的回答。它们全都带着中文否定式
# （不可以 / 不会 / 不需要 / 不享受），正是负向断言最容易误伤的形态。
HONEST_ANSWERS = [
    ("crosstopic-leave-vs-remote-calc",
     "年假制度和远程办公政策是两项相互独立的制度，文档里没有规定二者之间的折算或抵扣关系，"
     "缺少这样的规则，无法计算出一个精确结果。远程办公本身的额度是每月最多 8 天。"),
    ("crosstopic-leave-carryover-to-remote",
     "不可以。公司制度里没有规定年假能折算成远程办公天数，两者是相互独立的政策，"
     "文档里也没有任何换算规则。"),
    ("crosstopic-tenure-affects-remote-quota",
     "不会。远程办公额度固定为每月最多 8 天，与工龄无关。"
     "「工龄每满 3 年增加 1 天、最多累加至 15 天」是年假制度的规定，不适用于远程办公。"),
    ("crosstopic-remote-approval-transplanted-to-leave",
     "申请年假不需要部门总监特批。流程是在 OA 系统提交申请、注明起止日期、提前 3 天完成审批。"
     "「需部门总监特批、每季度不超过 2 次」是远程办公政策里的规定，不适用于年假。"),
    ("crosstopic-probation-remote-eligibility",
     "远程办公政策没有对试用期员工单独做规定，按通用规则每位员工每月最多可申请 8 天。"
     "（顺带一提，试用期员工不享受年假，那是年假制度的条款，与远程办公无关。）"),
]


class TestCrossTopicNegativeCases:
    def test_category_is_actually_populated(self):
        """原来这一类零覆盖——正是 hallu_multihop 那种失败的类型。"""
        assert len(CROSS_TOPIC_CASES) >= 3

    @pytest.mark.parametrize("case", CROSS_TOPIC_CASES, ids=lambda c: c["id"])
    def test_each_case_carries_a_negative_assertion(self, case):
        """这类错误的特征是"多说了不该说的"——正确答案的关键词它全都有，
        所以 contains_any 判不出来，必须带 expect_answer_not_contains。"""
        assert case.get("expect_answer_not_contains"), \
            f"{case['id']} 没有负向断言，等于没断言到点子上"

    @pytest.mark.parametrize("case", CROSS_TOPIC_CASES, ids=lambda c: c["id"])
    def test_negative_assertion_uses_a_field_the_runner_understands(self, case):
        """防止往 JSON 里塞脚本不认识的字段——那会被静默忽略，
        测试看着是绿的其实什么都没断言。"""
        known = {
            "id", "category", "tags", "account", "query", "expect_access",
            "expect_answer_contains_any", "expect_answer_not_contains",
            "expect_kb_sources", "expect_kb_sources_not_contains",
            "note", "known_nuance", "known_red",
        }
        unknown = set(case) - known
        assert not unknown, f"{case['id']} 用了跑测脚本不认识的字段：{unknown}"

    def test_anchor_case_would_catch_the_real_hallucination(self):
        """核心校验：拿 2026-08-25 复测里**真实的**幻觉回答喂给跑测脚本的
        判定函数，新用例必须判 FAIL。判不出来说明断言写歪了，是个假测试。"""
        anchor = next(c for c in CROSS_TOPIC_CASES
                      if c["id"] == "crosstopic-leave-vs-remote-calc")
        failures = golden_runner._check_case(
            anchor,
            {"answer": REAL_MULTIHOP_HALLUCINATION, "kb_sources": ["product_req_kb"]},
        )
        assert failures, "新用例抓不住已知的真实幻觉回答，等于白写"
        assert any("不该出现但出现了" in f for f in failures), failures

    def test_anchor_case_catches_the_second_real_variant_too(self):
        """同一条用例 2026-08-25 复跑（temperature=0）换了个编法：
        18:01 答"剩余的远程办公申请额度"，18:27 答"理论上您还可以申请大约4天"。
        **同一个坑、措辞完全不同** —— 两条都得抓住，否则断言只是在追上一次的原文。"""
        anchor = next(c for c in CROSS_TOPIC_CASES
                      if c["id"] == "crosstopic-leave-vs-remote-calc")
        second_variant = (
            "由于您的年假已经使用了20天，超过了您所在公司的年假制度，这意味着您今年没有剩余的"
            "带薪年假可以使用。根据远程办公政策，每位员工每月最多可申请8天远程办公。"
            "假设您的工作月份还未过半，理论上您还可以申请大约4天（即8天的一半）远程办公时间。"
            "综上所述，在没有额外特批的情况下，您可以申请约4天远程办公时间。"
        )
        failures = golden_runner._check_case(
            anchor, {"answer": second_variant, "kb_sources": ["product_req_kb"]})
        assert any("不该出现但出现了" in f for f in failures), failures

    @pytest.mark.parametrize("case_id,honest", HONEST_ANSWERS, ids=lambda x: x if isinstance(x, str) else "")
    def test_honest_answer_passes(self, case_id, honest):
        """⚠️ 这组是**这批用例最容易写砸的地方**，比"能不能抓到幻觉"更该盯。

        中文否定式是子串陷阱：「不会增加」包含「会增加」、「不可以折算」包含
        「可以折算」、「不需要部门总监特批」包含「总监特批」——负向断言照直写，
        **正确答案会被判成失败**，跟任务 1 修的 `noperm_user` 假阳性完全同一类
        错误（那次是"回显即失守"，这次是"否定式即失守"）。

        所以每条用例都配一段**人写的、明确正确**的回答，断言它零失败。
        改断言时如果这里红了，说明新断言会误伤正确答案，别急着改这个测试。
        """
        case = next(c for c in CROSS_TOPIC_CASES if c["id"] == case_id)
        failures = golden_runner._check_case(
            case, {"answer": honest, "kb_sources": ["product_req_kb"]})
        assert failures == [], failures

    def test_anchor_case_accepts_the_honest_answer(self):
        """反过来：诚实回答必须通过，否则这条用例永远红、失去信号价值。"""
        anchor = next(c for c in CROSS_TOPIC_CASES
                      if c["id"] == "crosstopic-leave-vs-remote-calc")
        honest = (
            "年假制度和远程办公政策是两项相互独立的制度，文档里没有规定二者之间的"
            "折算或抵扣关系，缺少这样的规则，无法计算出一个精确结果。"
            "远程办公本身的额度是每月最多 8 天。"
        )
        failures = golden_runner._check_case(
            anchor, {"answer": honest, "kb_sources": ["product_req_kb"]})
        assert failures == [], failures
