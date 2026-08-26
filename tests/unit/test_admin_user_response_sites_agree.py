"""`AdminUserResponse` 的两个构造点必须给同一组字段。

## 这条测试在防什么

`AdminUserResponse` 在 `app.py` 里被构造**两次**：

1. `_build_admin_user_response(user)` —— 单个用户。建号、改角色、停用的响应走它。
2. `admin_list_users` 里的批量版 —— 用户列表页走它（2026-08-26 修 N+1 时
   从前者展开成内联构造，两者从此不再共享代码）。

pydantic 的字段都有默认值，所以**漏填一处不会报错**，只会让那条路径静默返回
默认值。2026-08-26 就漏过一次：加 `disabled_at` / `activated_at` /
`pending_activation` 时只落到了批量版（更准确地说，是 `str.replace(..., 1)`
命中了文件里更靠前的 `get_me`），结果——

- 用户列表页显示正常（走批量版）；
- **停用接口返回的 `disabled_at` 恒为 null**，前端开关点完状态不刷新；
- `get_me` 那边多传了三个 `MeResponse` 根本没有的字段，pydantic 默认静默忽略，
  连个警告都没有。

单测抓不到，`create_app()` 能不能构造起来也抓不到。
是 `scripts/verify_account_endpoints_e2e.py` 真发 HTTP 请求才发现的。
这条测试把它固化成不需要数据库、不需要起服务的检查。

## 判据

不比对"填了哪些字段名"（两处的变量名不同：一个是 `user.x`，一个是 `u.x`），
而是比对**关键字参数名的集合**。
"""

from __future__ import annotations

import ast
from pathlib import Path

_APP = Path(__file__).resolve().parents[2] / "src" / "ragent_backend" / "app.py"


def _admin_user_response_kwargs() -> list[set[str]]:
    """所有 `AdminUserResponse(...)` 调用各自用到的关键字参数名。"""
    tree = ast.parse(_APP.read_text(encoding="utf-8"))
    out: list[set[str]] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "AdminUserResponse"
        ):
            out.append({kw.arg for kw in node.keywords if kw.arg})
    return out


class TestBothConstructionSitesAgree:
    def test_there_are_exactly_two_sites(self):
        """前提检查。

        变成 1 个说明有人把它们合回去了（那更好，这时该删掉本文件）；
        变成 3 个说明又多了一条路径，也要纳入下面的比对。
        任何一种情况都该有人看一眼，而不是让断言空转。
        """
        sites = _admin_user_response_kwargs()
        assert len(sites) == 2, (
            f"AdminUserResponse 现在有 {len(sites)} 个构造点（原本 2 个）。"
            "合并了就删掉本文件；新增了就确认它也填齐了字段。"
        )

    def test_same_field_set(self):
        a, b = _admin_user_response_kwargs()
        assert a == b, (
            "两个构造点填的字段不一致，漏填的那条路径会静默返回默认值：\n"
            f"  只在其中一处：{sorted(a ^ b)}"
        )

    def test_lifecycle_fields_present(self):
        """单独再钉一次生命周期三个字段。

        它们漏掉的后果是"停用了但界面看不出来"，比其他字段更容易被误判成
        前端 bug，值得一条自己的断言和自己的报错信息。
        """
        required = {"disabled_at", "activated_at", "pending_activation"}
        for i, site in enumerate(_admin_user_response_kwargs()):
            assert required <= site, (
                f"第 {i + 1} 个 AdminUserResponse 构造点缺少 {sorted(required - site)}"
            )


class TestMeResponseDoesNotGetStrayFields:
    """`MeResponse(...)` 只能传它真有的字段。

    pydantic v2 默认忽略多余的关键字参数——传错了不报错、不警告，
    值就没了。2026-08-26 真的往这里塞进过三个它没有的字段。
    """

    def test_no_unknown_kwargs(self):
        from src.ragent_backend.schemas import MeResponse

        known = set(MeResponse.model_fields)
        tree = ast.parse(_APP.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "MeResponse"
            ):
                passed = {kw.arg for kw in node.keywords if kw.arg}
                assert passed <= known, (
                    f"MeResponse 收到了它没有的字段 {sorted(passed - known)}，"
                    "pydantic 会静默丢弃"
                )
