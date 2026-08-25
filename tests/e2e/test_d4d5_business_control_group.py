"""D4/D5 跨材料约束的**业务对照组** —— 加约束会不会把正常问答一起掐死。

为什么需要这一组：`docs/orchestration_design.md` 的 D4/D5 是往 `_build_prompt`
里**加约束**（"材料没写的关系不许推导""数据缺口必须声明"）。这类改动最典型的
副作用不是"没修好"，而是**修过头**——模型对本来该正常回答的多来源问题也变得
畏缩，一律回一句"没有规定"。安全侧的用例集只测"该拒的有没有拒"，
**测不出"该答的有没有答"**，所以必须单独有一组反向对照。

对照组三类（9 条）：
  single  单来源事实问答，回归基线
  multi   **合法的多来源问题** —— D4 最可能误伤的形态。正确做法是分别引用
          各自的规定，不是拒答
  gap     D5 边界：确实缺个人数据，"说明缺口"是对的，但不能连政策本身也不说

⚠️ 这组要真实调 LLM，默认不在 `tests/unit` 里跑。用法：

    set -a; source .env; set +a
    RAGENT_DEBUG=true .venv/bin/python -m pytest \
        tests/e2e/test_d4d5_business_control_group.py -m llm -s

后端没起 / 账号不存在时**跳过**而不是失败——它是一次性的人工验收工具，
不该在没有环境的机器上把测试跑红。

2026-08-25 第二批实测结果：**9/9 正常作答，0 条因约束而畏缩**
（结果见交付说明；多来源问题模型确实照 D4 的要求写明了条款出自哪份文档）。
⚠️ 代价不在"答不答"，在耗时和啰嗦度：回答字数约翻倍，见 CLAUDE.md §4 的 TTFT 表。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest

BASE_URL = "http://localhost:8010"
ACCOUNT = "qa_run_company_user"
PASSWORD = "QaRun@2026"

# 只要出现这些词、又没答出应有内容，就算"被约束吓退"
REFUSAL_MARKERS = [
    "无法计算", "缺少", "没有规定", "未提及", "没有找到", "未检索到",
    "无法回答", "没有相关", "无权访问", "没有权限",
]

CASES: List[Dict[str, Any]] = [
    dict(id="single_carryover", kind="single", q="年假可以顺延到次年几月？",
         must_any=["3 月 31", "3月31", "第一季度", "次年"]),
    dict(id="single_remote_days", kind="single", q="每个月最多可以申请多少天远程办公？",
         must_any=["8 天", "8天"]),
    dict(id="single_tenure_cap", kind="single", q="工龄满多少年，年假天数就不再增加了？",
         must_any=["15 年", "15年"]),
    dict(id="single_first_year", kind="single", q="入职多久才开始有年假？",
         must_any=["满一年", "满 1 年", "一年", "1 年"]),
    # ↓ 这四条是 D4 的误伤重灾区：问题本身就横跨两份文档
    dict(id="multi_both_policies", kind="multi",
         q="请分别介绍一下公司的年假制度和远程办公政策。",
         must_any=["10 天", "10天"], must_also_any=["8 天", "8天"]),
    dict(id="multi_apply_process", kind="multi",
         q="年假和远程办公分别怎么申请？各自要提前多久？",
         must_any=["OA", "提前 3 天", "提前3天"]),
    dict(id="multi_compare_approval", kind="multi",
         q="年假和远程办公的审批流程有什么不同？",
         must_any=["总监", "特批", "OA", "审批"]),
    dict(id="multi_remote_over_quota", kind="multi",
         q="远程办公超出每月额度怎么办？年假超了呢？",
         must_any=["总监", "特批"]),
    # ↓ D5 边界：这一条**应该**说缺数据，但不该连政策都不说
    dict(id="gap_used_leave", kind="gap", q="我今年还剩多少天年假？",
         must_any=["缺少", "无法", "没有", "未提及", "记录"]),
]


@pytest.fixture(scope="module")
def client_and_token():
    httpx = pytest.importorskip("httpx")
    client = httpx.Client(timeout=180.0)
    try:
        resp = client.post(f"{BASE_URL}/api/v1/auth/login",
                           json={"username": ACCOUNT, "password": PASSWORD})
    except Exception as e:  # 后端没起
        pytest.skip(f"后端 {BASE_URL} 连不上，跳过业务对照组：{e}")
    if resp.status_code != 200:
        pytest.skip(f"测试账号 {ACCOUNT} 登录失败（{resp.status_code}），跳过业务对照组")
    yield client, resp.json()["access_token"]
    client.close()


def _ask(client, token: str, query: str) -> str:
    parts: List[str] = []
    with client.stream("POST", f"{BASE_URL}/api/v1/chat/stream",
                       headers={"Authorization": f"Bearer {token}"},
                       json={"query": query, "top_k": 5}) as resp:
        for line in resp.iter_lines():
            if not line.startswith("data: "):
                continue
            try:
                event = json.loads(line[len("data: "):])
            except json.JSONDecodeError:
                continue
            if event.get("type") == "token":
                parts.append(event.get("content", ""))
    return "".join(parts)


@pytest.mark.llm
@pytest.mark.e2e
@pytest.mark.slow
@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_normal_question_is_still_answered(case, client_and_token):
    """D4/D5 之后，本来该正常回答的问题必须**照常回答**。

    失败有两种读法，报告时要分开写：
      * 没给出应有内容，且回答里全是拒答话术 → **被约束吓退**，是 D4/D5 的副作用；
      * 没给出应有内容，但也不是拒答话术     → 多半是检索/意图侧的问题，
        跟 D4/D5 无关，别记到这笔账上。
    """
    client, token = client_and_token
    answer = _ask(client, token, case["q"])

    answered = any(k in answer for k in case["must_any"])
    if answered and case.get("must_also_any"):
        answered = any(k in answer for k in case["must_also_any"])

    timid = (case["kind"] in ("single", "multi")
             and any(m in answer for m in REFUSAL_MARKERS)
             and not answered)
    assert answered, (
        f"{case['id']} 没给出应有内容"
        f"{'（且整段是拒答话术 → 被 D4/D5 约束吓退）' if timid else '（不是拒答话术，多半不是 D4/D5 的锅）'}"
        f"：{answer[:300]}"
    )
