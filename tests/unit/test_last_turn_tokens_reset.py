"""`last_turn_tokens` 每轮必须重置 —— token 用量重复计数的回归保护。

## 缺陷

`last_turn_tokens`（`schemas.py:533`）是普通字段，没有 `Annotated` 累加 reducer，
所以 checkpointer 会把它原样带到下一轮。而全仓只有三处引用：
声明、`_generate_node:1509` 写、`_archive_node:1713` 读 —— **没有任何重置点**。

`tool_execution_trace` 是**完全相同的形态**，它在 `_session_node` 里被显式清空，
旁边还有一整段注释解释为什么必须清空。两者的区别只是一个被发现了、一个没有。

## 后果不是多占内存，是用量被重复计数

三条短路路径都不经过 `_generate_node`，返回的 dict 里没有这个字段：
- 越权话术拦截（`detect_privilege_claim` 命中）
- 无权访问该知识库
- 检索空命中的固定拒绝话术

于是 `_archive_node` 把**上一轮的 token 数**原样盖在这一轮的消息上。
同一份用量进两次库，而且**不会报错**。

已在真实库上确认它正在造成影响：994 条 assistant 消息里 417 条有 `total_tokens`，
`dashboard_stats.py` 的成本面板显示 562,004 —— 那个数字里含重复计数，
而 `COALESCE(SUM(total_tokens), 0)` 又会跳过 NULL，所以 58% 的缺失也是隐形的。

## 为什么这组测试不去起真实 workflow

`RAGWorkflow` 要 LLM、Postgres checkpointer、store。而本仓库
`grep -rln "asyncpg\|POSTGRES" tests/` 命中为零 —— 从没有测试碰过 Postgres
（`CLAUDE.md` §7.1 说的"现有结构测不了"）。

所以这里测的是**不变量本身**：`_session_node` 跑完之后，凡是"没有 reducer、
会被 checkpointer 带过轮次、且只在部分路径写入"的字段，都必须被重置。
这个判据比"跑一遍真实流程"更稳，也更能挡住"以后又新增一个同类字段却忘了清空"。
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Dict

import pytest

_WORKFLOW = Path(__file__).resolve().parents[2] / "src" / "ragent_backend" / "workflow.py"


def _session_node() -> ast.AST:
    src = _WORKFLOW.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "_session_node":
            return node
    raise AssertionError("找不到 _session_node —— 它被改名了？重置逻辑要跟着走")


def _state_assignments() -> Dict[str, ast.AST]:
    """`_session_node` 里所有 `state["X"] = ...` 的目标键 -> 右值 AST。

    ⚠️ **必须走 AST，不能用字符串包含判断。**
    第一版写的是 `assert "last_turn_tokens" in 函数源码`，结果撤掉修复后
    测试照样绿 —— 因为**重置那行旁边的注释里就反复出现这个字段名**，
    子串匹配命中的是注释不是代码。测试在测自己写的注释。
    这是今晚反复出现的那类假绿，判别力检查（把修复撤掉看红不红）才抓出来。
    """
    out: Dict[str, ast.AST] = {}
    for node in ast.walk(_session_node()):
        if not isinstance(node, ast.Assign):
            continue
        for t in node.targets:
            if (
                isinstance(t, ast.Subscript)
                and isinstance(t.value, ast.Name)
                and t.value.id == "state"
                and isinstance(t.slice, ast.Constant)
                and isinstance(t.slice.value, str)
            ):
                out[t.slice.value] = node.value
    return out


class TestLastTurnTokensIsResetEveryTurn:
    def test_session_node_resets_it(self):
        """核心断言：`_session_node` 里必须有 `state["last_turn_tokens"] = ...`。

        **修复前这条会红** —— 全仓只有声明/写/读三处，没有重置。
        """
        assert "last_turn_tokens" in _state_assignments(), (
            "_session_node 没有对 state['last_turn_tokens'] 赋值 —— 短路轮次会"
            "沿用上一轮的 token 数，导致同一份用量被记两次"
        )

    def test_it_is_reset_alongside_tool_execution_trace(self):
        """两者形态相同，必须在同一处一起清。

        分开清没有技术问题，但放在一起才能让下一个人看见"这类字段有一组"，
        而不是又漏掉第三个。
        """
        asg = _state_assignments()
        assert "tool_execution_trace" in asg, "前提失效：tool_execution_trace 的重置没了"
        assert "last_turn_tokens" in asg, "last_turn_tokens 没有被重置"
        gap = abs(asg["tool_execution_trace"].lineno - asg["last_turn_tokens"].lineno)
        assert gap <= 25, (
            f"两处重置相隔 {gap} 行 —— 它们是同一类不变量，应该挨着，"
            "否则以后新增同类字段时看不到这是一组"
        )

    def test_reset_value_is_none_not_empty_dict(self):
        """必须重置成 `None`，不能是 `{}`。

        `_archive_node:1713` 写的是 `state.get("last_turn_tokens") or {}`，
        两者对它等价。但字段声明是 `Optional[Dict[str, Any]]`，
        `None` 才是"本轮还没有用量"的语义；`{}` 是"本轮用量为空"，
        将来若有人区分这两者（比如统计"有多少轮真的调过 LLM"），`{}` 会说谎。
        """
        asg = _state_assignments()
        assert "last_turn_tokens" in asg, "last_turn_tokens 没有被重置"
        value = asg["last_turn_tokens"]
        assert isinstance(value, ast.Constant) and value.value is None, (
            f"重置值不是 None，而是 {ast.dump(value)[:60]}"
        )


class TestTheFieldStillHasNoReducer:
    """如果哪天给它加了 reducer，这组测试的前提就变了。

    加 reducer（比如按轮累加）会让"每轮重置"这件事从必要变成有害 ——
    那时应该删掉重置和这组测试，而不是让两者打架。
    这条断言在那种情况下会红，是提醒而不是阻碍。
    """

    def test_no_annotated_reducer(self):
        schemas = (_WORKFLOW.parent / "schemas.py").read_text(encoding="utf-8")
        line = next(
            l for l in schemas.splitlines() if l.strip().startswith("last_turn_tokens")
        )
        assert "Annotated" not in line, (
            "last_turn_tokens 现在有 reducer 了 —— 那么 _session_node 里的重置"
            "可能已经变成有害的，请一并复核 workflow.py 与本测试文件"
        )
