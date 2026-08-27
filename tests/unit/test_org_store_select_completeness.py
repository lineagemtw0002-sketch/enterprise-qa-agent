"""`org_store` 里每一条构造 Organization 的 SELECT 都必须带上全部列。

## 这条测试在防什么

`_row_to_org` 对缺失的列有兜底（`"seat_limit" in row.keys()` 否则 None），
那个兜底是必要的——不同查询 SELECT 的列不同。但它有个代价：
**忘了在某条 SELECT 里加新列时，不会报错，字段会静默变成 None。**

2026-08-26 加 `seat_limit` 时就踩了：`get_seat_limit()` 读得对，
但 `get_organization()` 的 SELECT 没带这一列，于是 `org.seat_limit` 恒为 None
—— **"这家企业有 5 个席位上限"被读成"不限"，席位校验形同虚设，
而且不抛任何异常**。

单元测试抓不到（它们不连库），`create_app()` 能不能构造起来也抓不到，
是 `scripts/verify_account_lifecycle.py` 连真库跑才发现的。
这条测试把它固化成一个不需要数据库的检查。

## 为什么走 AST 而不是字符串搜索

`ast.Constant` 只包含真正的字符串字面量，**不包含注释和 docstring**。
本仓库已经两次因为"断言命中的是自己写的注释"而假绿
（`test_last_turn_tokens_reset.py`、`test_activation_code.py`），
这里从一开始就不给那个机会。
"""

from __future__ import annotations

import ast
from pathlib import Path

_ORG_STORE = Path(__file__).resolve().parents[2] / "src" / "ragent_backend" / "org_store.py"

# Organization dataclass 里所有从库里读的列。加字段时这里也要加，
# 而这正是本测试想强制发生的那次"想一想还有哪些 SELECT 要改"。
_COLUMNS = ("id", "name", "is_platform", "created_at", "seat_limit")


def _org_selects() -> list[str]:
    """所有从 organizations 取整行的 SQL 字面量。

    判据是"这条 SQL 里出现了 is_platform" —— 那是 Organization 独有的列，
    出现它就说明这条查询在构造 Organization。
    """
    tree = ast.parse(_ORG_STORE.read_text(encoding="utf-8"))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and "SELECT" in node.value.upper()
        and "is_platform" in node.value
    ]


class TestEveryOrganizationSelectIsComplete:
    def test_there_are_selects_to_check(self):
        """前提检查：如果一条都找不到，说明判据失效了（比如 SQL 被挪进了
        f-string 或常量表），后面所有断言会**空转变绿**。"""
        assert len(_org_selects()) >= 3, "没找到足够多的 organizations 查询，判据可能已失效"

    def test_all_columns_present_in_every_select(self):
        missing: list[tuple[str, str]] = []
        for sql in _org_selects():
            for col in _COLUMNS:
                if col not in sql:
                    missing.append((col, " ".join(sql.split())[:80]))
        assert not missing, (
            "有 SELECT 漏了 Organization 的列，`_row_to_org` 会把它静默填成 None：\n"
            + "\n".join(f"  缺 {c}：{q}" for c, q in missing)
        )

    def test_seat_limit_specifically(self):
        """单独再钉一次 seat_limit —— 它漏掉的后果是席位校验失效，
        比其他列更严重，值得一条自己的断言和自己的报错信息。"""
        bad = [s for s in _org_selects() if "seat_limit" not in s]
        assert not bad, (
            f"{len(bad)} 条查询没有 SELECT seat_limit —— "
            "org.seat_limit 会恒为 None，「有上限」被读成「不限」"
        )
