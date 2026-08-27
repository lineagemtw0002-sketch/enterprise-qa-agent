"""跑 tests/fixtures/golden_test_set_tenant_kb.json 里的黄金测试集——针对「测试新公司」
企业知识库问答 + 权限边界的回归测试，覆盖：正常问答、边界条件、无数据场景、
无权限拒绝、跨企业隔离、企业管理员通配、平台管理员边界、知识库来源图标基线。

背景见 golden_test_set_tenant_kb.json 顶部 description：管理员实测「年假可以顺延到
次年几月？」一度没有答案，排查后确认是共享知识库当时确实没有这份文档（不是 bug），
补录文档后固化成这份测试集，防止以后又被误判成检索/权限出了问题。

用法：
    python scripts/run_tenant_kb_golden_tests.py
    python scripts/run_tenant_kb_golden_tests.py --base-url http://localhost:8010

跑之前后端要已经启动（默认 http://localhost:8010），测试账号（qa_run_* 系列）要已经
存在——用 scripts/create_user.py 建的那批，密码统一 QaRun@2026，见 golden 文件里的
accounts 字段。这个脚本不建账号，账号缺了直接报错退出，不静默跳过。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

REPO_ROOT = Path(__file__).parent.parent
GOLDEN_SET_PATH = REPO_ROOT / "tests" / "fixtures" / "golden_test_set_tenant_kb.json"

# 用例里允许出现的全部字段。
#
# 为什么要显式维护这张表：往 JSON 里写一个脚本不认识的断言字段，`_check_case`
# 会**静默忽略**它——用例看着有断言、跑起来是绿的，其实什么都没判。第一批就是
# 因为这个风险刻意没敢加 `expect_answer_not_matches`。现在字段加上了，同时把
# "未知字段"变成一条**显式失败**，让同类错误不可能再悄悄发生。
# `tests/unit/test_security_posture_judge.py` 直接引用这张表做校验，不再各自
# 手抄一份。
_KNOWN_CASE_KEYS = {
    # 元信息
    "id", "category", "tags", "account", "query", "note", "known_nuance", "known_red",
    # 断言
    "expect_access",
    "expect_answer_contains_any",
    "expect_answer_not_contains",
    "expect_answer_not_matches",
    "expect_kb_sources",
    "expect_kb_sources_not_contains",
}


def _login(client: httpx.Client, base_url: str, username: str, password: str) -> str:
    resp = client.post(f"{base_url}/api/v1/auth/login", json={"username": username, "password": password})
    if resp.status_code != 200:
        raise RuntimeError(
            f"账号 {username} 登录失败（{resp.status_code}）：{resp.text}\n"
            f"——检查这个测试账号是不是还存在，密码是不是还是 QaRun@2026。"
        )
    return resp.json()["access_token"]


def _run_query(client: httpx.Client, base_url: str, token: str, query: str) -> Dict[str, Any]:
    """调 /chat/stream，收集完整回答文本 + done 事件里的 kb_sources。"""
    answer_parts: List[str] = []
    kb_sources: List[str] = []
    with client.stream(
        "POST",
        f"{base_url}/api/v1/chat/stream",
        headers={"Authorization": f"Bearer {token}"},
        json={"query": query, "top_k": 5},
        timeout=60.0,
    ) as resp:
        for line in resp.iter_lines():
            if not line.startswith("data: "):
                continue
            try:
                event = json.loads(line[len("data: "):])
            except json.JSONDecodeError:
                continue
            if event.get("type") == "token":
                answer_parts.append(event.get("content", ""))
            elif event.get("type") == "done":
                kb_sources = event.get("kb_sources", [])
    return {"answer": "".join(answer_parts), "kb_sources": kb_sources}


def _check_case(case: Dict[str, Any], result: Dict[str, Any]) -> List[str]:
    """返回这条用例的失败原因列表；空列表表示通过。"""
    failures: List[str] = []
    answer = result["answer"]
    kb_sources = set(result["kb_sources"])

    is_denied = ("无权访问" in answer) or ("没有权限" in answer)
    expect_access = case.get("expect_access")
    if expect_access == "allowed" and is_denied:
        failures.append(f"期望能访问，实际被拒绝：{answer[:80]}")
    if expect_access == "denied" and not is_denied:
        failures.append(f"期望被拒绝，实际给出了回答：{answer[:80]}")

    contains_any = case.get("expect_answer_contains_any")
    if contains_any and not any(s in answer for s in contains_any):
        failures.append(f"回答里没有出现 {contains_any} 中的任何一个：{answer[:120]}")

    not_contains = case.get("expect_answer_not_contains")
    if not_contains:
        hit = [s for s in not_contains if s in answer]
        if hit:
            failures.append(f"回答里不该出现但出现了：{hit}")

    # 正则版否定断言（2026-08-25 第二批新增）。
    #
    # 为什么子串不够：同一条 `hallu_multihop` 用例在 temperature=0 下两次复跑
    # 给出了两种完全不同的编法（"剩余的远程办公申请额度" / "理论上您还可以申请
    # 大约 4 天"）。子串断言只能事后一条条补，第三种编法照样漏。
    # 同一天还三次栽在子串判据上：JWT 假警报、`noperm_user` 匹配到问题回显、
    # 以及**中文否定式子串陷阱**（"不会增加" 含 "会增加"、"不需要总监特批"
    # 含 "总监特批"）——正则能把否定词显式排除掉，子串做不到。
    #
    # ⚠️ 写正则时同样要防误伤：负向正则命中的必须是**只可能出现在错误答案里**
    # 的形态。每条正则都在 tests/unit/test_security_posture_judge.py 里配了
    # 人写的正确答案做对照组，那组红了就说明正则会误伤。
    not_matches = case.get("expect_answer_not_matches")
    if not_matches:
        hit = [p for p in not_matches if re.search(p, answer)]
        if hit:
            failures.append(f"回答命中了不该命中的正则：{hit}")

    unknown = set(case) - _KNOWN_CASE_KEYS
    if unknown:
        # 不是"回答不对"，是"用例写错了"——但必须让它红，否则一个拼错的断言
        # 字段会被静默忽略，测试假绿。
        failures.append(f"用例里有跑测脚本不认识的字段（会被静默忽略）：{sorted(unknown)}")

    expect_kb = case.get("expect_kb_sources")
    if expect_kb is not None and kb_sources != set(expect_kb):
        failures.append(f"kb_sources 期望 {expect_kb}，实际 {sorted(kb_sources)}")

    not_kb = case.get("expect_kb_sources_not_contains")
    if not_kb:
        hit = [s for s in not_kb if s in kb_sources]
        if hit:
            failures.append(f"kb_sources 不该包含但包含了：{hit}（实际 {sorted(kb_sources)}）")

    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description="跑企业知识库问答黄金测试集")
    parser.add_argument("--base-url", default="http://localhost:8010")
    parser.add_argument(
        "--golden-file", default=str(GOLDEN_SET_PATH),
        help="黄金测试集 JSON 路径，默认 tests/fixtures/golden_test_set_tenant_kb.json",
    )
    args = parser.parse_args()

    golden = json.loads(Path(args.golden_file).read_text(encoding="utf-8"))
    accounts = golden["accounts"]
    cases = golden["test_cases"]

    client = httpx.Client()
    tokens: Dict[str, Optional[str]] = {}

    passed = 0
    failed = 0
    for case in cases:
        account_name = case["account"]
        if account_name not in tokens:
            account = accounts[account_name]
            tokens[account_name] = _login(client, args.base_url, account_name, account["password"])
        token = tokens[account_name]

        result = _run_query(client, args.base_url, token, case["query"])
        failures = _check_case(case, result)

        status = "PASS" if not failures else "FAIL"
        print(f"[{status}] {case['id']} ({account_name}): {case['query']}")
        if failures:
            for f in failures:
                print(f"        - {f}")
            failed += 1
        else:
            passed += 1

    print(f"\n共 {len(cases)} 条：通过 {passed}，失败 {failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
