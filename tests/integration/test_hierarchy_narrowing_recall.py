"""层次化检索粗筛层的端到端行为（真实 Chroma + BM25 + Ollama embedding + cross-encoder）。

对应缺陷（2026-08-26）：`alice_acme`（org_admin，Acme 名下 6 个知识库）问
「域账号密码多久强制更换一次？」返回"未找到相关结果"，而答案就写在
`acme_it_support_kb` 的 `ACME-IT-001` 正文里；同一批问题换成只有 1 个库权限的
账号则完全正常。根因是粗筛层取**跨全部候选库的全局前 N 篇**，并且拿命中结果的
keys 当"要检索哪几个库"用——候选库越多（= 用户权限越大）被误伤越重。
完整证据与设计见 `docs/hierarchical_narrowing_redesign.md`。

**每条用例在旧实现下会不会失败**（CLAUDE.md §7.2）：

| 用例 | 旧实现下会失败吗 |
|---|---|
| test_six_kb_admin_gets_answers_from_its_own_kb | ✅ 旧实现 30 条里 11 条空结果 |
| test_same_question_works_for_one_kb_and_six_kb_shapes | ✅ **这条是本缺陷的判别式**：旧实现 1 库对、6 库错 |
| test_narrowing_never_drops_a_candidate_collection | ✅ 旧实现 search_collections = narrowed.keys() |
| test_narrowed_pass_falls_back_when_empty | ✅ 旧实现没有兜底 |

依赖真实数据（`scripts/generate_demo_kb_dataset.py` 摄入的 Acme 演示语料）
与本地 Ollama；任一缺失即 skip，不阻断整体测试套件。

⚠️ 判据是"回答里出现预期关键事实"，跟 `docs/manual_test_dataset.md` 同一套口径，
不判断语义正确性——模型把数字答对但语境说反，这套判据抓不到。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio

ACME_KBS = [
    "acme_hr_admin_kb", "acme_finance_kb", "acme_it_support_kb",
    "acme_rd_product_kb", "acme_sales_marketing_kb", "acme_customer_success_kb",
]
IT_KB = "acme_it_support_kb"


@pytest.fixture
def settings():
    try:
        from src.core.settings import load_settings
        return load_settings()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"无法加载配置: {e}")


@pytest.fixture
def questions():
    try:
        from scripts.demo_kb_content.questions import POSITIVE
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"演示问题库不可用: {e}")
    if not POSITIVE.get(IT_KB):
        pytest.skip("演示问题库里没有 IT 支持库的正向问题")
    return POSITIVE


def _make_tool(settings):
    # 每个测试函数一个实例，不跨测试共享——pytest-asyncio 默认每个测试各起一个
    # event loop，class 级共享实例会把上一个 loop 建的连接带到下一个测试里用。
    try:
        from src.mcp_server.tools.query_knowledge_hub import QueryKnowledgeHubTool
        return QueryKnowledgeHubTool(settings=settings)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"无法初始化 QueryKnowledgeHubTool: {e}")


def _with_narrowing_on(settings):
    """强制打开文档级收窄——配置正本里它默认是关的（见 settings.yaml 旁的理由），
    但"收窄开着时也绝不许丢库"这条不变量必须在打开的状态下验证。"""
    override = settings.model_copy(deep=True)
    override.ingestion.doc_summary.setdefault("narrow", {})
    override.ingestion.doc_summary["narrow"].update({"enabled": True, "min_top_score": 0.0})
    return override


async def _run(tool, query, collections):
    from src.core.trace import TraceContext
    trace = TraceContext(trace_type="query")
    resp = await tool._execute_local_multi(query, 5, list(collections), trace)
    return resp, trace


def _stage(trace, name):
    for st in trace.to_dict().get("stages") or []:
        if (st.get("stage") or st.get("name")) == name:
            return st
    return None


class TestRecallForMultiKbUsers:
    async def test_six_kb_admin_gets_answers_from_its_own_kb(self, settings, questions):
        """企业管理员（6 个候选库）问 IT 支持库的问题，必须答得出来。

        旧实现下这 5 条里只有 2 条能命中，其余返回"未找到相关结果"。
        """
        tool = _make_tool(settings)
        misses = []
        for q in questions[IT_KB]:
            resp, _ = await _run(tool, q.query, ACME_KBS)
            if not any(k in resp.content for k in q.keywords):
                misses.append((q.query, q.source))
        assert not misses, f"6 库候选下这些问题没命中预期关键事实: {misses}"

    async def test_same_question_works_for_one_kb_and_six_kb_shapes(self, settings, questions):
        """**本缺陷的判别式**：同一个问题，候选库从 1 个变成 6 个，结论不能变。

        旧实现下 1 库正常、6 库返回空——因为全局 top-N 的预算被别的库瓜分掉了。
        这条把"权限形状影响检索结果"这件事变成断言。
        """
        tool = _make_tool(settings)
        q = questions[IT_KB][0]  # 「域账号密码多久强制更换一次？」

        one_resp, _ = await _run(tool, q.query, [IT_KB])
        six_resp, _ = await _run(tool, q.query, ACME_KBS)

        assert any(k in one_resp.content for k in q.keywords), "单库候选下就答不出来，说明数据或环境有问题"
        assert any(k in six_resp.content for k in q.keywords), (
            "单库能答出、6 库答不出——粗筛又在按候选库数量误伤了"
        )


class TestNarrowingInvariant:
    async def test_narrowing_never_drops_a_candidate_collection(self, settings, questions):
        """收窄**开着**时，被检索的库集合仍必须等于候选库集合。

        粗筛是检索优化，不该拥有"把某个知识库从检索里删掉"的权力——
        那是除 ACL 之外第二个决定"用户能看到什么"的地方。
        """
        tool = _make_tool(_with_narrowing_on(settings))
        q = questions[IT_KB][0]
        _, trace = await _run(tool, q.query, ACME_KBS)

        stage = _stage(trace, "hierarchy_narrow")
        assert stage is not None, "粗筛阶段必须留下 trace，排查'为什么没查到'时要靠它"
        data = stage.get("data") or stage
        assert sorted(data["search_collections"]) == sorted(ACME_KBS)
        assert {d["collection"] for d in data["decisions"]} == set(ACME_KBS), (
            "每个候选库都要有一条决定；缺席意味着调用方拿不到它的状态"
        )

    async def test_narrowing_on_still_answers_or_falls_back(self, settings, questions):
        """收窄开着时，要么直接答对，要么走兜底重查后答对——不允许静默返回空。"""
        tool = _make_tool(_with_narrowing_on(settings))
        q = questions[IT_KB][0]
        resp, trace = await _run(tool, q.query, ACME_KBS)

        assert not resp.is_empty, "收窄开着也不该返回空结果（有兜底重查）"
        assert any(k in resp.content for k in q.keywords)
