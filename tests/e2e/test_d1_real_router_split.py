"""D1 在**真实 1.5b-router 模型**上的效果实测（2026-08-25 第三批）。

为什么单独有这一组：`tests/unit/test_sub_query_dependency_and_fanout.py` 用假
LLM **注入**"修复前那种拆法"，验证的是后处理层兜不兜得住——那是判别力最强的
写法，但它证明不了"真实模型到底会不会那样拆"。这一组补的正是那半边：拿线上
实际用的 `qwen2.5-1.5b-router` 跑，量"D1 到底改善了多少"。

⚠️ 要真实调本地 Ollama，默认不在 `tests/unit` 里跑。用法：

    set -a; source .env; set +a
    RAGENT_DEBUG=true .venv/bin/python -m pytest \
        tests/e2e/test_d1_real_router_split.py -m llm -s

Ollama 没起 / 模型没 `ollama create` 出来时**跳过**而不是失败。

2026-08-25 实测结论（`docs/orchestration_design.md` §4.5 那张表的来源）：

- 依赖型（用"和/与"连接、后半句依赖前半句）4 条：
  **修复前 3/4 被并行拆分 → 修复后 1/4**。
  剩下那 1 条是模型**自己先把回指词消解掉**（还消解错了：
  "年假制度和它的申请流程" → `["年假制度是什么", "请假制度是什么"]`），
  字面上不再有任何依赖信号，确定性判据抓不到——**这是 D1 的已知边界**。
- 另有 6 条"两个完整分句、后半句带回指"的措辞，1.5b router **压根不拆**，
  它直接把后半句丢掉只返回前半句；但 `rewritten_query` 保留了整句，且这类
  问题被判成 `intent_type=tool`（`_intent_node` 本来就会把 sub_queries 收窄成
  整句），**线上不会因此丢信息**。

本文件断言的是"修复后不比修复前差"，**不硬编码 1/4 这个数字**——本地模型
输出有方差，把实测数字写成断言只会得到一个隔三差五变红的假警报（这条教训见
CLAUDE.md §4 里"LLM 输出方差足以让同一条用例在两天之间换色"）。
精确的行为断言在 `tests/unit/` 那份里，那份是确定性的。
"""

from __future__ import annotations

import os
from typing import List, Tuple

import pytest

MODEL = os.getenv("RAGENT_INTENT_MODEL", "qwen2.5-1.5b-router")
BASE_URL = os.getenv("RAGENT_OLLAMA_BASE_URL", "http://localhost:11434")

# 用"和/与"连接的依赖型问句——1.5b router 对这类连词句拆分最积极，
# 是 D1 唯一真正能观察到效果的形态。
DEPENDENT_QUERIES: List[str] = [
    "年假制度和它的申请流程分别是什么",
    "远程办公政策和该政策的审批人分别是什么",
    "培训课程列表和这些课程的报名方式分别是什么",
    "差旅标准和其适用的职级范围分别是什么",
]

# 对照组：真正独立的多主题问题，D1 之后必须继续拆
INDEPENDENT_QUERIES: List[str] = [
    "北京上海杭州的天气怎么样",
    "年假制度和报销流程分别是什么",
    "差旅住宿标准和报销周期分别是什么",
]


@pytest.fixture(scope="module")
def router_llm():
    pytest.importorskip("langchain_openai")
    import httpx
    from langchain_openai import ChatOpenAI

    try:
        resp = httpx.get(f"{BASE_URL}/api/tags", timeout=3.0)
        names = {m.get("name", "").split(":")[0] for m in resp.json().get("models", [])}
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"Ollama {BASE_URL} 连不上，跳过 D1 真实模型实测：{e}")
    if MODEL.split(":")[0] not in names:
        pytest.skip(f"模型 {MODEL} 未在本机 Ollama 中，跳过 D1 真实模型实测")

    return ChatOpenAI(
        model=MODEL, temperature=0.0, max_tokens=1024,
        base_url=f"{BASE_URL}/v1", api_key="ollama",
    )


async def _split_counts(queries: List[str], llm) -> List[Tuple[str, int, List[str]]]:
    from src.ragent_backend.intent import analyze_and_route

    out: List[Tuple[str, int, List[str]]] = []
    for q in queries:
        _rewritten, subs, _intent = await analyze_and_route(
            query=q, messages=[], llm=llm, available_tools=[], available_workflows=[],
        )
        out.append((q, len(subs), subs))
    return out


@pytest.mark.llm
@pytest.mark.e2e
@pytest.mark.asyncio
async def test_dependent_queries_are_mostly_not_split(router_llm):
    """依赖型问句被并行拆分的条数必须**明显少于半数**。

    用"少于半数"而不是精确条数：修复前实测 3/4（75%），修复后 1/4（25%），
    中间隔着一个足够宽的带，本地模型的输出方差撑不破它。
    """
    results = await _split_counts(DEPENDENT_QUERIES, router_llm)
    for q, n, subs in results:
        print(f"[依赖型] {n} 个  {q} -> {subs}")
    split_count = sum(1 for _q, n, _s in results if n > 1)
    assert split_count * 2 < len(DEPENDENT_QUERIES), (
        f"依赖型问句仍有 {split_count}/{len(DEPENDENT_QUERIES)} 被并行拆分："
        f"{[(q, s) for q, n, s in results if n > 1]}"
    )


@pytest.mark.llm
@pytest.mark.e2e
@pytest.mark.asyncio
async def test_independent_queries_are_still_split(router_llm):
    """对照组：D1 不许把真正独立的多主题问题一起收成单查询。"""
    results = await _split_counts(INDEPENDENT_QUERIES, router_llm)
    for q, n, subs in results:
        print(f"[独立型] {n} 个  {q} -> {subs}")
    for q, n, subs in results:
        assert n > 1, f"独立的多主题问题被误收成单查询: {q!r} -> {subs}"


# 刻意**没有**在这里放"真实模型不会吐超过 3 个子查询"的用例：
# 那条既拦不住什么（真吐多了 `_retrieve_multi` 也会截断），又只能写成观察式的
# 软断言——按 CLAUDE.md §7.2，写不出"旧实现下会失败"的测试就是废测试。
# D2 的硬断言在 `tests/unit/test_sub_query_dependency_and_fanout.py::TestFanoutHardLimit`。
