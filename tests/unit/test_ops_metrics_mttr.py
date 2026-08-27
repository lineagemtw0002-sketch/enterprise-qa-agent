"""`ops_store.py::compute_ops_metrics` 里 MTTR 那段判据的单测覆盖。

## 已经有集成测试了，为什么还要这个

`tests/integration/test_ops_store_metrics.py` 连真实 Postgres，跑得慢、且要求
本机有库。而 MTTR 里真正容易出错的那部分**跟 SQL 没有关系**：SQL 只负责把
"已完成且链接了分析摘要的动作"捞出来，"从 `evidence_refs` 这坨不透明 JSON 里
挑出最早的那条告警起始时间"整段是纯 Python，判据有四条（来源必须是
`alert_correlation`、`detail` 必须是 dict、`started_at` 必须是数字、取 `min`），
四条里错任何一条都不会报错，只会让 MTTR 悄悄变成另一个数字。

这类"不报错、只是算错"的缺陷正是本仓库反复吃亏的形态（CLAUDE.md §5 列了
一串），单测跑 1 秒、集成测试跑几十秒，值得两边都有。

## 两种做法各覆盖什么

1. `TestMttrJudgement` 用 AST 把 `compute_ops_metrics` 里那段 `durations`
   循环**原样抠出来**执行——断言跑的是生产代码本身，不是照抄的副本
   （照抄的副本永远和自己一致，判别力为零）。抠的是 AST，不是
   `inspect.getsource()` 字符串匹配（后者本仓库踩过两次，见 CLAUDE.md §5）。
2. `TestWiring` 用假连接池驱动**真正的** `compute_ops_metrics`，覆盖
   "没有样本时是 `None` 不是 0.0""`sample_sizes.mttr` 数的是真正入选的条数"
   这类循环之外的接线。

## 判别力是怎么确认的

`TestMutantsFailTheseAssertions` 把抠出来的 AST 真的篡改成三种错误实现
（去掉 source 过滤 / 把 min 换成 max / 用 executed_at 给缺证据的动作凑数），
跑同一组输入拿到**不同**的数字。不是"按逻辑推断它会红"。
"""

from __future__ import annotations

import ast
import copy
import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from unittest.mock import AsyncMock

import pytest

from src.ragent_backend.ops_store import STATUS_COMPLETED, OpsStore

_OPS_STORE_PY = Path(__file__).resolve().parents[2] / "src" / "ragent_backend" / "ops_store.py"


# ---------------------------------------------------------------------------
# 把生产代码里那段循环抠出来
# ---------------------------------------------------------------------------

def _extract_durations_loop() -> List[ast.stmt]:
    """取 `compute_ops_metrics` 里从 `durations: List[float] = []` 到那个
    `for row in mttr_rows:` 循环结束的语句。

    锚点是变量名。锚点消失（改名/重构）会让这里 `StopIteration`——那是
    有意义的失败：这个文件跟它测的那段代码是绑在一起的，不该在代码换了
    之后还自顾自地绿着。
    """
    tree = ast.parse(_OPS_STORE_PY.read_text(encoding="utf-8"))
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name == "compute_ops_metrics"
    )
    start = next(
        i for i, stmt in enumerate(fn.body)
        if isinstance(stmt, ast.AnnAssign) and getattr(stmt.target, "id", None) == "durations"
    )
    end = next(
        i for i, stmt in enumerate(fn.body)
        if i > start and isinstance(stmt, ast.For) and getattr(stmt.target, "id", None) == "row"
    )
    return fn.body[start:end + 1]


def _compile_durations(
    mutate: Optional[Callable[[List[ast.stmt]], None]] = None,
) -> Callable[[List[Dict[str, Any]]], List[float]]:
    body = copy.deepcopy(_extract_durations_loop())
    if mutate is not None:
        mutate(body)
    shell = ast.parse("def _durations(mttr_rows):\n    pass\n")
    shell.body[0].body = body + ast.parse("return durations").body
    ast.fix_missing_locations(shell)
    namespace = {"json": json, "List": List}
    exec(compile(shell, filename=str(_OPS_STORE_PY), mode="exec"), namespace)  # noqa: S102
    return namespace["_durations"]


_durations = _compile_durations()


def _row(executed_at: float, refs: Any) -> Dict[str, Any]:
    """一条 mttr 行。字段名照抄那条 SQL 的 SELECT 列表，不是凭语义编的。"""
    return {"executed_at": executed_at, "evidence_refs": refs}


def _alert_ref(started_at: Any) -> Dict[str, Any]:
    """`analyze_ops_incident` 真实写进 `evidence_refs` 的告警关联依据形状。"""
    return {"source": "alert_correlation", "description": "19 条告警合并为 1 个事件",
            "detail": {"started_at": started_at, "alert_count": 19}}


# ---------------------------------------------------------------------------
# 判据本身
# ---------------------------------------------------------------------------

class TestMttrJudgement:
    def test_takes_the_earliest_alert_as_the_clock_start(self):
        """MTTR 的起点是**最早**那条告警，不是最后一条、也不是随便哪条。

        一次事件里同一批告警会陆续冒出来，用后来的那条当起点等于把
        "我们花了多久才发现"这段时间悄悄从统计里扣掉，MTTR 会系统性偏小。
        """
        rows = [_row(400.0, [_alert_ref(300.0), _alert_ref(100.0), _alert_ref(250.0)])]
        assert _durations(rows) == [300.0]

    def test_only_alert_correlation_evidence_counts(self):
        """`evidence_refs` 是一坨不透明 JSON，里面混着别的来源。

        异常检测那条依据也可能带 `started_at`（指标窗口的起点），
        而指标窗口通常比告警早得多——不按 `source` 过滤的话，MTTR 会被
        "我们从多早开始看这个指标"污染，那不是修复耗时。
        """
        rows = [_row(400.0, [
            {"source": "anomaly_detection", "detail": {"started_at": 50.0}},
            _alert_ref(300.0),
        ])]
        assert _durations(rows) == [100.0]

    def test_action_without_alert_evidence_is_skipped_not_backfilled(self):
        """只做了异常检测、没有告警关联的动作 —— **跳过，不拿别的时间戳凑数**。

        拿 `created_at` 顶替会把"平台内部处理时长"混进 MTTR，那是另一个口径
        的数字。宁可样本少一条，也不要一个解释不清的平均值。
        """
        rows = [_row(400.0, [{"source": "anomaly_detection", "detail": {"started_at": 50.0}}])]
        assert _durations(rows) == []

    def test_non_numeric_started_at_is_ignored(self):
        # 连接器回 ISO 字符串（"2026-08-27T10:00:00Z"）而不是 epoch 时，
        # 减法会直接抛 TypeError 炸掉整个指标接口。这里选择当它不存在。
        rows = [_row(400.0, [{"source": "alert_correlation",
                              "detail": {"started_at": "2026-08-27T10:00:00Z"}}])]
        assert _durations(rows) == []

    def test_detail_must_be_a_dict(self):
        # 依据条目的形状由分析层自己定，Store 层当不透明 JSON 存取——
        # 也就是说这里随时可能读到形状不对的东西，不能假设它一定是 dict。
        rows = [_row(400.0, [{"source": "alert_correlation", "detail": None},
                             {"source": "alert_correlation", "detail": "started at noon"}])]
        assert _durations(rows) == []

    def test_non_dict_entries_do_not_crash(self):
        rows = [_row(400.0, ["一条裸字符串", None, _alert_ref(100.0)])]
        assert _durations(rows) == [300.0]

    def test_negative_elapsed_is_dropped(self):
        """告警比执行还晚 = 数据不对（时钟漂移、或者链错了摘要）。

        留下来会变成一个负的耗时，被中位数一平均，MTTR 直接失去意义；
        而负数在界面上又不会像 `None` 那样显眼。
        """
        rows = [_row(100.0, [_alert_ref(400.0)])]
        assert _durations(rows) == []

    def test_zero_elapsed_is_kept(self):
        # 边界：`>= 0` 不是 `> 0`。同一秒内完成是可能的（尤其是演示环境），
        # 把它当异常丢掉会莫名其妙地少一个样本。
        assert _durations([_row(400.0, [_alert_ref(400.0)])]) == [0.0]

    def test_evidence_refs_may_arrive_as_json_string(self):
        """asyncpg 取 jsonb 时可能给回字符串（取决于有没有装 codec）。

        当成 list 直接迭代的话会逐字符迭代，每个字符都不是 dict，
        于是**一条样本都取不到、还不报错**——MTTR 静默恒为 None。
        """
        rows = [_row(400.0, json.dumps([_alert_ref(100.0)]))]
        assert _durations(rows) == [300.0]

    def test_null_evidence_refs(self):
        assert _durations([_row(400.0, None)]) == []

    def test_multiple_actions_each_contribute_one_duration(self):
        rows = [
            _row(400.0, [_alert_ref(100.0)]),   # 300
            _row(1000.0, [_alert_ref(700.0)]),  # 300
            _row(50.0, [_alert_ref(20.0)]),     # 30
        ]
        assert sorted(_durations(rows)) == [30.0, 300.0, 300.0]


# ---------------------------------------------------------------------------
# 循环之外的接线：假连接池驱动真正的 compute_ops_metrics
# ---------------------------------------------------------------------------

class _FakeAcquireCtx:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _FakeAcquireCtx(self._conn)


class _FakeConn:
    """按 SQL 内容分派三次 `fetch`。

    ⚠️ 不用 `AsyncMock` 一律返回同一份数据糊弄过去——那样"MTTR 查询有没有
    真的被发出来"这件事就测不出来了，随便返回什么都能凑出一个数字。
    """

    def __init__(self, status_rows=None, summary_rows=None, mttr_rows=None):
        self._status_rows = status_rows or []
        self._summary_rows = summary_rows or []
        self._mttr_rows = mttr_rows or []
        self.queries: List[str] = []

    async def fetch(self, sql: str, *args):
        self.queries.append(sql)
        if "JOIN ops_analysis_summaries" in sql:
            return self._mttr_rows
        if "FROM ops_analysis_summaries" in sql:
            return self._summary_rows
        return self._status_rows


def _store_with(monkeypatch, conn: _FakeConn) -> OpsStore:
    store = OpsStore()
    monkeypatch.setattr(store, "_get_pool", AsyncMock(return_value=_FakePool(conn)))
    return store


class TestWiring:
    @pytest.mark.asyncio
    async def test_no_samples_gives_none_not_zero(self, monkeypatch):
        """"还没有样本"和"耗时恰好是 0"是两件不同的事。

        糊成 0 会让一家刚接入、还没执行过任何修复的企业，在大屏上看到
        一个漂亮的"MTTR 0 秒"——一个假的好成绩比没有数字更糟。
        """
        store = _store_with(monkeypatch, _FakeConn())
        metrics = await store.compute_ops_metrics("org-1")
        assert metrics["mttr_seconds"] is None
        assert metrics["sample_sizes"]["mttr"] == 0

    @pytest.mark.asyncio
    async def test_median_not_mean(self, monkeypatch):
        # 中位数抗离群值：一次异常拖了很久的修复不该把整体指标带偏。
        conn = _FakeConn(mttr_rows=[
            _row(400.0, [_alert_ref(300.0)]),      # 100
            _row(400.0, [_alert_ref(200.0)]),      # 200
            _row(100000.0, [_alert_ref(0.0)]),     # 100000（离群）
        ])
        metrics = await _store_with(monkeypatch, conn).compute_ops_metrics("org-1")
        assert metrics["mttr_seconds"] == 200.0
        assert metrics["sample_sizes"]["mttr"] == 3

    @pytest.mark.asyncio
    async def test_sample_size_counts_only_qualified_rows(self, monkeypatch):
        """SQL 捞回来 3 条，只有 1 条真的带告警依据 —— 样本量必须报 1。

        报 3 会让人以为"这个中位数有 3 个样本撑着"，而 CLAUDE.md 记的
        设计意图正是"1 个样本的数字和 200 个样本的数字在决策上不是一回事"。
        """
        conn = _FakeConn(mttr_rows=[
            _row(400.0, [_alert_ref(100.0)]),
            _row(400.0, [{"source": "anomaly_detection", "detail": {"started_at": 100.0}}]),
            _row(400.0, None),
        ])
        metrics = await _store_with(monkeypatch, conn).compute_ops_metrics("org-1")
        assert metrics["sample_sizes"]["mttr"] == 1
        assert metrics["mttr_seconds"] == 300.0

    @pytest.mark.asyncio
    async def test_mttr_query_is_actually_issued_and_filters_completed(self, monkeypatch):
        # 光看返回值分不出"查询发对了"还是"假件恰好返回了想要的东西"。
        conn = _FakeConn()
        await _store_with(monkeypatch, conn).compute_ops_metrics("org-1")
        mttr_sql = [q for q in conn.queries if "JOIN ops_analysis_summaries" in q]
        assert len(mttr_sql) == 1, "MTTR 那次查询没发出来"
        assert STATUS_COMPLETED in mttr_sql[0], "MTTR 只该统计已完成的动作"
        assert "executed_at IS NOT NULL" in mttr_sql[0]

    @pytest.mark.asyncio
    async def test_connection_ids_filter_reaches_the_mttr_query(self, monkeypatch):
        """按连接器过滤是权限隔离的一部分（非 org_admin 只看得到自己被授权的）。

        漏在 MTTR 这一支上，别人连接器的修复耗时就会混进他的大屏。
        """
        conn = _FakeConn()
        await _store_with(monkeypatch, conn).compute_ops_metrics("org-1", connection_ids=["opsconn_1"])
        mttr_sql = next(q for q in conn.queries if "JOIN ops_analysis_summaries" in q)
        assert "connection_id = ANY" in mttr_sql


# ---------------------------------------------------------------------------
# 判别力自查（CLAUDE.md §7.2）
# ---------------------------------------------------------------------------

def _mutate_drop_source_filter(body: List[ast.stmt]) -> None:
    """去掉 `ref.get("source") == "alert_correlation"` 这条判据。"""
    comp = next(
        n for n in ast.walk(ast.Module(body=body, type_ignores=[]))
        if isinstance(n, ast.ListComp)
    )
    gen = comp.generators[0]
    gen.ifs = [
        cond for cond in gen.ifs
        if not any(
            isinstance(c, ast.Constant) and c.value == "alert_correlation"
            for c in ast.walk(cond)
        )
    ]


def _mutate_min_to_max(body: List[ast.stmt]) -> None:
    """把 `min(starts)` 换成 `max(starts)` —— 用最后一条告警当起点。"""
    for node in ast.walk(ast.Module(body=body, type_ignores=[])):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "min":
            node.func.id = "max"


def _mutate_backfill_instead_of_skip(body: List[ast.stmt]) -> None:
    """把"没有告警依据就跳过"换成"用 0 顶上"——即"拿别的时间戳凑数"那类错误。"""
    for node in ast.walk(ast.Module(body=body, type_ignores=[])):
        if isinstance(node, ast.If) and any(isinstance(s, ast.Continue) for s in node.body):
            node.body = ast.parse("starts = [0.0]").body


class TestMutantsFailTheseAssertions:
    """把生产 AST 真的改成错误实现跑一遍，证明上面的断言不是摆设。

    每条对照的是 `TestMttrJudgement` 里同名判据的那条测试。
    """

    def test_without_source_filter_the_metric_window_pollutes_mttr(self):
        rows = [_row(400.0, [
            {"source": "anomaly_detection", "detail": {"started_at": 50.0}},
            _alert_ref(300.0),
        ])]
        assert _durations(rows) == [100.0]
        assert _compile_durations(_mutate_drop_source_filter)(rows) == [350.0], (
            "去掉 source 过滤后指标窗口起点被当成了告警时间——"
            "test_only_alert_correlation_evidence_counts 会因此变红"
        )

    def test_max_instead_of_min_shrinks_the_duration(self):
        rows = [_row(400.0, [_alert_ref(300.0), _alert_ref(100.0), _alert_ref(250.0)])]
        assert _durations(rows) == [300.0]
        assert _compile_durations(_mutate_min_to_max)(rows) == [100.0], (
            "取最后一条告警当起点会把发现耗时扣掉——"
            "test_takes_the_earliest_alert_as_the_clock_start 会因此变红"
        )

    def test_backfilling_invents_a_sample_out_of_nothing(self):
        rows = [_row(400.0, [{"source": "anomaly_detection", "detail": {"started_at": 50.0}}])]
        assert _durations(rows) == []
        assert _compile_durations(_mutate_backfill_instead_of_skip)(rows) == [400.0], (
            "给没有告警依据的动作凑一个起点，会平白多出一个样本——"
            "test_action_without_alert_evidence_is_skipped_not_backfilled 会因此变红"
        )


class TestExtractionItselfIsWiredToProduction:
    def test_loop_was_really_found(self):
        body = _extract_durations_loop()
        assert any(isinstance(n, ast.For) for n in body), (
            "没在 compute_ops_metrics 里找到那个 durations 循环，本文件测的是空气"
        )
