"""`app.py::admin_analyze_ops_incident` 的响应映射回归保护。

## 这条测试为什么存在

这段映射刚踩过一次真实的坑：第一版按语义猜了 `anomaly_count` 这个字段名，
而 `OpsToolset.analyze_ops_incident` 实际返回的是 `anomaly_targets`（列表）。
**猜错的后果不是报错**——`data.get("anomaly_count")` 恒为 `None`，
`has_findings` 恒为 `False`，塔台上永远显示"未发现异常"，界面照常渲染、
日志一个字都没有。这是本产品线第四次栽在猜字段名上（前三次见 CLAUDE.md §5：
§10.5 指标的四个键、`/auth/me` 的 roles 形状、`Segmented` 白屏），
共同点都是"构建通过、不报错、只是某条判据悄悄恒假"。

## 为什么用 AST 把生产代码那段映射抠出来执行，而不是照抄一份

端点是 `create_app()` 里的闭包，要直接调它得起整个 app + 连 Postgres，
而本仓库 `conftest.py` 没有 DB fixture（CLAUDE.md §4 P1）。

那就只剩两条路：
1. **在测试里照抄一份映射逻辑**——判别力为零。抄的那份永远和自己一致，
   `app.py` 改成 `anomaly_count` 它照样绿，正好放过这个测试要防的那个 bug。
2. **把 `app.py` 里那几行真的抠出来执行**（本文件的做法）。断言跑的是
   生产代码本身，改错字段名当场变红。

⚠️ 抠的方式是 **AST**，不是 `inspect.getsource()` + 字符串匹配。
后者本仓库踩过两次（CLAUDE.md §5：`assert "compare_digest" in getsource(...)`
命中的是 docstring 里的那句话，把实现换成 `==` 测试照样绿）。
这里不做任何字符串断言——是把 AST 节点编译成真的函数**跑一遍看输出**。

## 判别力是怎么确认的

`TestMutantsFailTheseAssertions` 不是按逻辑推断，是**真的把生产 AST 篡改成
旧实现再跑一遍**：把 `anomaly_targets` 换回 `anomaly_count`、把去重用的
集合推导换成列表推导，然后断言篡改版在同一组输入下给出**不同**的结果。
既然上面的断言钉的是正确结果，篡改版必然让它们变红。
"""

from __future__ import annotations

import ast
import copy
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, List, Optional

import pytest

from src.ragent_backend.schemas import AnalyzeOpsIncidentResponse

# ⚠️ **2026-08-27 端点搬家了**：分层批次 1 把 `admin_analyze_ops_incident` 连同
# 其余 22 个 `/admin/ops` 端点从 `app.py` 搬进了 `api/ops_router.py`
# （见 docs/app_layering_design.md）。这个文件靠 AST 锚点定位那段代码，锚点
# 一失效就整体 `StopIteration` —— 而这**正是当初刻意选的失效方式**：宁可
# 收集阶段就吵着报错，也不要静默地测了个空气。这次它按预期吵了，改锚点即可。
_APP_PY = Path(__file__).resolve().parents[2] / "src" / "ragent_backend" / "api" / "ops_router.py"


def _extract_mapping_tail() -> List[ast.stmt]:
    """取 `admin_analyze_ops_incident` 从 `data = outcome.data or {}` 到
    `return AnalyzeOpsIncidentResponse(...)` 的那几条语句。

    起点刻意锚在"给 `data` 赋值"这条语句上，而不是"倒数第二条"——
    端点前面那段（权限校验 / 调 toolset / 写审计）全都要 IO，抠出来没法跑；
    从 `data = ...` 开始的这一段是纯粹的字典 → 响应模型映射，零 IO。
    锚点消失（比如有人把 `data` 改名）会让这里直接 `StopIteration`，
    那也是一种有意义的失败：这个测试跟它测的那段代码绑在一起。
    """
    tree = ast.parse(_APP_PY.read_text(encoding="utf-8"))
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name == "admin_analyze_ops_incident"
    )
    start = next(
        i for i, stmt in enumerate(fn.body)
        if isinstance(stmt, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "data" for t in stmt.targets)
    )
    return fn.body[start:]


def _compile_mapping(
    mutate: Optional[Callable[[List[ast.stmt]], None]] = None,
) -> Callable[[object], AnalyzeOpsIncidentResponse]:
    """把抠出来的语句包进 `def _mapping(outcome)` 编译成真函数。

    `mutate` 用来构造"旧实现/错误实现"的对照组（见文件头"判别力"一节），
    它拿到的是一份深拷贝，篡改不会污染其它测试。
    """
    tail = copy.deepcopy(_extract_mapping_tail())
    if mutate is not None:
        mutate(tail)
    # 用 parse 造壳子而不是手搓 ast.FunctionDef：后者要逐个填对
    # posonlyargs/type_params 这些随 Python 版本变动的字段，parse 不会漏。
    shell = ast.parse("def _mapping(outcome):\n    pass\n")
    shell.body[0].body = tail
    ast.fix_missing_locations(shell)
    namespace = {"AnalyzeOpsIncidentResponse": AnalyzeOpsIncidentResponse}
    exec(compile(shell, filename=str(_APP_PY), mode="exec"), namespace)  # noqa: S102
    return namespace["_mapping"]


_mapping = _compile_mapping()


def _outcome(ok: bool = True, message: str = "分析完成", **data) -> SimpleNamespace:
    """假的 `ToolOutcome`。字段名照抄 `src/ops/tools.py::analyze_ops_incident`
    真实返回的 `data`，不是这里凭语义编的——本文件防的就是编字段名。"""
    return SimpleNamespace(ok=ok, message=message, data=dict(data))


class TestHasFindings:
    """`has_findings` 是这段映射里唯一一处"由多个字段推导出来"的判据，
    也正是踩过坑的那一处。三条输入各钉一个分支。
    """

    def test_anomaly_targets_alone_makes_findings_true(self):
        # 这条就是当初 `anomaly_count` 那个 bug 的现场：检出了异常但一条告警
        # 都没合并，旧实现在这里恒给 False。
        resp = _mapping(_outcome(anomaly_targets=["order-service@prod"], incident_count=0))
        assert resp.has_findings is True
        assert resp.anomaly_targets == ["order-service@prod"]

    def test_incident_count_alone_makes_findings_true(self):
        # 反过来：没检出指标异常，但告警合并出了事件，同样算"有发现"。
        resp = _mapping(_outcome(anomaly_targets=[], alert_count=19, incident_count=1))
        assert resp.has_findings is True

    def test_nothing_found_is_not_a_failure(self):
        """"跑通了但一切正常"必须是 `ok=True, has_findings=False`。

        把它折成 `ok=False` 会让界面把"这个服务是健康的"显示成"分析失败了"
        ——一个每天都会出现的正常结果被报成故障，人很快就不看这个提示了。
        """
        resp = _mapping(_outcome(ok=True, anomaly_targets=[], alert_count=0, incident_count=0))
        assert resp.ok is True
        assert resp.has_findings is False

    def test_missing_data_keys_do_not_crash(self):
        # 上游降级/异常路径可能只回一个 message，`data` 是 None。
        # 这段映射必须能吃下它并给出全默认值，而不是抛 AttributeError
        # 把一次"分析没结果"变成一次 500。
        resp = _mapping(SimpleNamespace(ok=False, message="模块未开通", data=None))
        assert resp.ok is False
        assert resp.has_findings is False
        assert resp.anomaly_targets == []
        assert resp.alert_count == 0 and resp.incident_count == 0
        assert resp.unavailable == []


class TestDegradedIsPassedThrough:
    def test_degraded_true_is_reported(self):
        """降级 = RCA 那一步没有模型参与，结论只是数据复述。
        吞掉这个标志，用户会把一段复述当成分析结论。"""
        resp = _mapping(_outcome(degraded=True, anomaly_targets=["a"]))
        assert resp.degraded is True

    def test_degraded_absent_defaults_to_false(self):
        assert _mapping(_outcome(anomaly_targets=["a"])).degraded is False


class TestUnavailableDedup:
    """指标查询和告警查询是**两次** fan-out，同一个连接器挂掉会被报两次。
    不去重的话界面上同一个系统列两遍，看起来像"有两个系统出问题了"——
    而这个数字正是用户判断"这次结论有多可信"的依据。
    """

    def test_same_system_reported_twice_collapses_to_one(self):
        resp = _mapping(_outcome(unavailable=[
            {"system": "prometheus-a", "reason": "连接器离线"},
            {"system": "prometheus-a", "reason": "连接器离线"},
        ]))
        assert resp.unavailable == ["prometheus-a"]

    def test_different_systems_all_kept_and_sorted(self):
        # 排序是为了让同一批失败每次显示顺序一致（集合本身无序，
        # 不排序的话界面会无缘无故地跳来跳去）。
        resp = _mapping(_outcome(unavailable=[
            {"system": "zabbix-b", "reason": "超时"},
            {"system": "prometheus-a", "reason": "离线"},
            {"system": "zabbix-b", "reason": "超时"},
        ]))
        assert resp.unavailable == ["prometheus-a", "zabbix-b"]

    def test_entries_without_system_name_are_dropped(self):
        # 空名字在界面上是一个什么都不写的空条目，比不显示更让人困惑。
        resp = _mapping(_outcome(unavailable=[
            {"system": "", "reason": "未知"},
            {"reason": "连 system 都没有"},
            {"system": "prometheus-a", "reason": "离线"},
        ]))
        assert resp.unavailable == ["prometheus-a"]


class TestPassThroughFields:
    def test_counts_and_summary_id(self):
        resp = _mapping(_outcome(
            summary_id="opssum_1", alert_count=19, incident_count=1,
            anomaly_targets=["a@x", "b@x"],
        ))
        assert resp.summary_id == "opssum_1"
        assert resp.alert_count == 19 and resp.incident_count == 1
        assert resp.anomaly_targets == ["a@x", "b@x"]

    def test_message_is_not_rewritten(self):
        # message 里带着"⚠️ 以下数据源本次不可用"这类降级提示，
        # 改写/截断它等于把警告吃掉。
        resp = _mapping(_outcome(message="根因：磁盘写满\n⚠️ 数据源不可用"))
        assert resp.message == "根因：磁盘写满\n⚠️ 数据源不可用"


# ---------------------------------------------------------------------------
# 判别力自查（CLAUDE.md §7.2：写完测试要问"它在旧实现下会失败吗"）
# ---------------------------------------------------------------------------

def _mutate_to_anomaly_count(tail: List[ast.stmt]) -> None:
    """把 `has_findings` 里的 `"anomaly_targets"` 换回踩坑时的 `"anomaly_count"`。

    只改 `has_findings=` 那个关键字实参，不动 `anomaly_targets=` 那个
    ——当初的 bug 就长这样：透出字段是对的，只有判据用错了名字，
    所以界面上"异常列表"有内容、"是否有发现"却是 False。
    """
    call = next(
        n for n in ast.walk(ast.Module(body=tail, type_ignores=[]))
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        and n.func.id == "AnalyzeOpsIncidentResponse"
    )
    kw = next(k for k in call.keywords if k.arg == "has_findings")
    for node in ast.walk(kw.value):
        if isinstance(node, ast.Constant) and node.value == "anomaly_targets":
            node.value = "anomaly_count"


def _mutate_dedup_to_list(tail: List[ast.stmt]) -> None:
    """把 `unavailable` 那个集合推导换成列表推导 = 去重被拿掉。"""
    call = next(
        n for n in ast.walk(ast.Module(body=tail, type_ignores=[]))
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        and n.func.id == "AnalyzeOpsIncidentResponse"
    )
    kw = next(k for k in call.keywords if k.arg == "unavailable")
    for node in ast.walk(kw.value):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "sorted":
            setcomp = node.args[0]
            assert isinstance(setcomp, ast.SetComp), "去重靠的是集合推导，形状变了这条对照组就该重写"
            node.args[0] = ast.ListComp(elt=setcomp.elt, generators=setcomp.generators)


class TestMutantsFailTheseAssertions:
    """把生产 AST 真的篡改成旧实现跑一遍，证明上面的断言不是摆设。

    这不是"按逻辑推断它会红"——是当场跑出**不同的输出**。
    """

    def test_anomaly_count_mutant_reports_no_findings(self):
        mutant = _compile_mapping(_mutate_to_anomaly_count)
        payload = _outcome(anomaly_targets=["order-service@prod"], incident_count=0)

        assert _mapping(payload).has_findings is True, "当前实现应当判为有发现"
        assert mutant(payload).has_findings is False, (
            "旧实现（猜 anomaly_count）在同一份数据上恒判无发现——"
            "这正是 TestHasFindings 第一条要挡住的回归"
        )

    def test_mutant_still_fills_anomaly_targets(self):
        # 佐证"这个 bug 为什么没被任何人发现"：异常列表本身是对的，
        # 页面看起来完全正常，只有那一个布尔判据是错的。
        mutant = _compile_mapping(_mutate_to_anomaly_count)
        resp = mutant(_outcome(anomaly_targets=["order-service@prod"]))
        assert resp.anomaly_targets == ["order-service@prod"]
        assert resp.has_findings is False

    def test_list_mutant_reports_the_same_system_twice(self):
        mutant = _compile_mapping(_mutate_dedup_to_list)
        payload = _outcome(unavailable=[
            {"system": "prometheus-a", "reason": "离线"},
            {"system": "prometheus-a", "reason": "离线"},
        ])

        assert _mapping(payload).unavailable == ["prometheus-a"]
        assert mutant(payload).unavailable == ["prometheus-a", "prometheus-a"], (
            "去掉去重后同一个系统会被列两遍——TestUnavailableDedup 会因此变红"
        )


class TestExtractionItselfIsWiredToProduction:
    """如果哪天有人重写了这个端点、锚点没了，上面所有断言都会静默失去意义
    （抠不到东西 → 没东西可跑）。这条让"抠不到"变成一次显式失败。
    """

    def test_tail_contains_the_response_construction(self):
        tail = _extract_mapping_tail()
        assert any(
            isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            and n.func.id == "AnalyzeOpsIncidentResponse"
            for n in ast.walk(ast.Module(body=tail, type_ignores=[]))
        ), "没在 admin_analyze_ops_incident 尾部找到响应构造，本文件测的是空气"

    def test_mapping_needs_no_io(self):
        # 抠出来的那段一旦混进 await（比如有人把某个字段改成现查数据库），
        # 编译出来的就是协程函数，直接调用拿不到结果——与其在断言里
        # 得到一个奇怪的 coroutine，不如在这里说清楚是怎么回事。
        import inspect
        assert not inspect.iscoroutinefunction(_mapping), (
            "这段映射现在含 await 了，说明它不再是纯映射，本文件的做法要重新设计"
        )


@pytest.mark.parametrize("targets,incidents,expected", [
    ([], 0, False),
    (["a"], 0, True),
    ([], 1, True),
    (["a"], 1, True),
])
def test_has_findings_truth_table(targets, incidents, expected):
    """把"或"的四种组合摆全。单看某一条容易漏掉"两个都空才算没发现"。"""
    assert _mapping(_outcome(anomaly_targets=targets, incident_count=incidents)).has_findings is expected
