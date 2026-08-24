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
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

REPO_ROOT = Path(__file__).parent.parent
GOLDEN_SET_PATH = REPO_ROOT / "tests" / "fixtures" / "golden_test_set_tenant_kb.json"


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
