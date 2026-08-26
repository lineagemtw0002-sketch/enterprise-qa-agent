"""智能运维工具层（`src/ops/tools.py`）——设计见 `docs/aiops_module_design.md` §3.6。

**这个文件的重点全部集中在一句设计要求上**：

> 执行类工具必须在工具层强制检查审批状态，不能只依赖上游节点"应该已经检查过"
> 这种隐式假设。

所以下面每一条"拒绝"用例都同时断言两件事：**拒绝了** + **执行通道一次都没被调用**。
只断言返回值是不够的——"发出去了但结果被判定为拒绝"和"根本没发出去"，
在生产环境里是"服务被重启了"和"服务没被重启"的区别。

判别力（§7.2）：全新代码没有旧实现可对照。但其中几条是**去掉对应检查就会红**的：
- `test_execution_is_refused_when_not_approved`：删掉状态检查就红
- `test_execution_rechecks_scope_after_approval`：删掉白名单复查就红
  （而且这一道在下游**完全没有**对应检查，删了没有任何别的地方会拦住）
- `test_cross_org_execution_is_refused`：删掉 org 校验就红
- `test_dispatch_failure_does_not_leave_action_stuck_in_executing`：不 catch 就红

全假件，不连 DB / WebSocket，毫秒级。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Dict, List, Optional

import pytest

from src.ops.federation import ConnectionRef, FederatedQueryEngine
from src.ops.tools import OpsToolset
from src.ops.types import (
    ERROR_OFFLINE,
    ConnectorUnavailable,
    DataPoint,
    ExecutionOutcome,
    QueryResult,
)

ORG = "org_acme"
OTHER_ORG = "org_globex"
CONN = "conn_prom"


@dataclass
class FakeAction:
    action_id: str
    org_id: str
    connection_id: str
    plan: Dict[str, Any]
    status: str
    approver_user_id: Optional[str] = "u_boss"


@dataclass
class FakeScope:
    scope_config: Dict[str, Any]


@dataclass
class FakeSummary:
    summary_id: str
    org_id: str


class FakeStore:
    def __init__(
        self, action: Optional[FakeAction] = None, scope: Optional[FakeScope] = None,
        summaries: Optional[Dict[str, FakeSummary]] = None,
    ) -> None:
        self.action = action
        self.scope = scope
        self.summaries = summaries or {}
        self.created: List[Dict[str, Any]] = []
        self.advanced: List[tuple] = []
        self.marked_executing: List[str] = []
        self.results: List[tuple] = []

    async def get_action(self, action_id):
        return self.action if self.action and self.action.action_id == action_id else None

    async def get_remediation_scope(self, connection_id, action_type):
        return self.scope

    async def get_analysis_summary(self, summary_id):
        return self.summaries.get(summary_id)

    async def create_proposed_action(self, **kw):
        self.created.append(kw)
        return FakeAction("remact_new", kw["org_id"], kw["connection_id"], kw["plan"], "proposed")

    async def advance_status(self, action_id, target_status):
        self.advanced.append((action_id, target_status))
        return self.action

    async def mark_executing(self, action_id):
        self.marked_executing.append(action_id)
        self.action = replace(self.action, status="executing")
        return self.action

    async def mark_result(self, action_id, target_status, result):
        self.results.append((action_id, target_status, result))
        self.action = replace(self.action, status=target_status)
        return self.action


class FakeDispatcher:
    def __init__(self, succeeded: bool = True, raises: Optional[Exception] = None) -> None:
        self.calls: List[Dict[str, Any]] = []
        self._succeeded = succeeded
        self._raises = raises

    async def execute(self, connection_id, org_id, action_id, plan, timeout_s):
        self.calls.append({"connection_id": connection_id, "action_id": action_id, "plan": plan})
        if self._raises:
            raise self._raises
        return ExecutionOutcome(succeeded=self._succeeded, detail="已重启 order-service")


class FakeDirectory:
    async def list_for_org(self, org_id):
        return [ConnectionRef(CONN, "Prometheus")]


class FakeTransport:
    def __init__(self, raises=None, points=1):
        self._raises, self._points = raises, points

    async def query(self, connection_id, org_id, request, timeout_s):
        if self._raises:
            raise self._raises
        return QueryResult(connection_id, "Prometheus",
                           [DataPoint(ts=float(i), value=1.0) for i in range(self._points)])

    async def online_status(self, connection_ids):
        return {c: True for c in connection_ids}


def _toolset(store, dispatcher=None, transport=None) -> OpsToolset:
    engine = FederatedQueryEngine(transport or FakeTransport(), FakeDirectory())
    return OpsToolset(engine, store, dispatcher)


IN_SCOPE = FakeScope({"allowed_targets": ["order-service"]})
OUT_OF_SCOPE = FakeScope({"allowed_targets": ["payment-gateway"]})
PLAN = {"target": "order-service"}


class TestExecutionGate:
    """执行类工具的四道检查。每条都断言"执行通道一次都没被调用"。"""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["proposed", "pending_approval", "rejected", "executing", "completed"])
    async def test_execution_is_refused_when_not_approved(self, status):
        """人工审批是硬性前置依赖（§3.3），不是"建议先审批"。"""
        store = FakeStore(FakeAction("a1", ORG, CONN, PLAN, status), IN_SCOPE)
        d = FakeDispatcher()
        out = await _toolset(store, d).execute_approved_remediation(ORG, "a1", "restart_service")

        assert out.refused is True and out.ok is False
        assert d.calls == [], f"状态是 {status} 却把动作发下去了"
        assert store.marked_executing == []

    @pytest.mark.asyncio
    async def test_cross_org_execution_is_refused(self):
        """工具入参是 LLM 给的，端点侧的 ACL 管不到这里。"""
        store = FakeStore(FakeAction("a1", OTHER_ORG, CONN, PLAN, "approved"), IN_SCOPE)
        d = FakeDispatcher()
        out = await _toolset(store, d).execute_approved_remediation(ORG, "a1", "restart_service")

        assert out.refused is True and d.calls == []
        assert "找不到" in out.message, "跨 org 时不该泄露'这条动作存在但你无权'"

    @pytest.mark.asyncio
    async def test_execution_rechecks_scope_after_approval(self):
        """⚠️ 四道检查里唯一在下游完全没有对应检查的一道。

        提议时目标在白名单内、审批通过，但审批到执行之间管理员把它从白名单摘掉了
        （§10.4 默认审批超时 30 分钟，这半小时足够改配置）。删掉这道复查，
        修复就会打在一个已被明令禁止的目标上，而且没有任何别的地方会拦住。
        """
        store = FakeStore(FakeAction("a1", ORG, CONN, PLAN, "approved"), OUT_OF_SCOPE)
        d = FakeDispatcher()
        out = await _toolset(store, d).execute_approved_remediation(ORG, "a1", "restart_service")

        assert out.refused is True and d.calls == []
        assert "白名单被改过" in out.message or "超出" in out.message

    @pytest.mark.asyncio
    async def test_scope_removed_entirely_also_refuses(self):
        store = FakeStore(FakeAction("a1", ORG, CONN, PLAN, "approved"), None)
        d = FakeDispatcher()
        out = await _toolset(store, d).execute_approved_remediation(ORG, "a1", "restart_service")
        assert out.refused is True and d.calls == []

    @pytest.mark.asyncio
    async def test_missing_action_is_refused(self):
        d = FakeDispatcher()
        out = await _toolset(FakeStore(None, IN_SCOPE), d).execute_approved_remediation(ORG, "nope")
        assert out.refused is True and d.calls == []

    @pytest.mark.asyncio
    async def test_no_dispatcher_configured_fails_safely(self):
        store = FakeStore(FakeAction("a1", ORG, CONN, PLAN, "approved"), IN_SCOPE)
        out = await _toolset(store, None).execute_approved_remediation(ORG, "a1", "restart_service")
        assert out.ok is False and store.marked_executing == []


class TestExecutionHappyPathAndFailure:
    @pytest.mark.asyncio
    async def test_approved_and_in_scope_is_dispatched(self):
        store = FakeStore(FakeAction("a1", ORG, CONN, PLAN, "approved"), IN_SCOPE)
        d = FakeDispatcher(succeeded=True)
        out = await _toolset(store, d).execute_approved_remediation(ORG, "a1", "restart_service")

        assert out.ok is True and out.refused is False
        assert d.calls[0]["plan"] == PLAN
        assert store.results == [("a1", "completed", {"detail": "已重启 order-service"})]

    @pytest.mark.asyncio
    async def test_connector_reports_failure_is_not_a_refusal(self):
        """"被规则挡下"和"执行失败"必须分开——压成一个布尔值会让
        "我们拦住了一次危险操作"和"我们搞砸了一次操作"在日志里长得一样。"""
        store = FakeStore(FakeAction("a1", ORG, CONN, PLAN, "approved"), IN_SCOPE)
        out = await _toolset(store, FakeDispatcher(succeeded=False)).execute_approved_remediation(
            ORG, "a1", "restart_service")
        assert out.ok is False and out.refused is False
        assert store.results[0][1] == "failed"

    @pytest.mark.asyncio
    async def test_dispatch_failure_does_not_leave_action_stuck_in_executing(self):
        """下发炸了也必须落到终态——留在 executing 的记录既不会超时也不会被重试，
        只能人工去数据库里改。"""
        store = FakeStore(FakeAction("a1", ORG, CONN, PLAN, "approved"), IN_SCOPE)
        d = FakeDispatcher(raises=RuntimeError("WebSocket 断了"))
        out = await _toolset(store, d).execute_approved_remediation(ORG, "a1", "restart_service")

        assert out.ok is False
        assert store.results and store.results[0][1] == "failed"
        assert "WebSocket" in store.results[0][2]["error"]

    @pytest.mark.asyncio
    async def test_scope_recheck_is_skipped_when_action_type_unknown(self):
        """拿不到 action_type 时跳过复查而不是假装查过——给人"已经查过"的错觉
        比不查更危险。其余三道检查仍然生效。"""
        store = FakeStore(FakeAction("a1", ORG, CONN, PLAN, "approved"), OUT_OF_SCOPE)
        d = FakeDispatcher()
        out = await _toolset(store, d).execute_approved_remediation(ORG, "a1", action_type=None)
        assert out.ok is True and len(d.calls) == 1


class TestPropose:
    @pytest.mark.asyncio
    async def test_out_of_scope_never_becomes_a_pending_approval(self):
        """§3.3.1：越界在**进入 pending_approval 之前**就挡掉，
        不允许流到审批人那一步再靠人肉发现。"""
        store = FakeStore(scope=OUT_OF_SCOPE)
        out = await _toolset(store).propose_remediation(
            ORG, CONN, "u1", "restart_service", "重启一下", PLAN)

        assert out.refused is True
        assert store.created == [], "越界的提议被写进库了"
        assert store.advanced == []

    @pytest.mark.asyncio
    async def test_missing_scope_means_refuse_not_allow(self):
        """没登记白名单 = 一律不允许。**不能是"没配置就放行"**——
        那个默认值会让"忘了配"变成"全放开"。"""
        store = FakeStore(scope=None)
        out = await _toolset(store).propose_remediation(
            ORG, CONN, "u1", "restart_service", "重启", PLAN)
        assert out.refused is True and store.created == []

    @pytest.mark.asyncio
    async def test_in_scope_creates_and_advances_to_pending_approval(self):
        store = FakeStore(scope=IN_SCOPE)
        out = await _toolset(store).propose_remediation(
            ORG, CONN, "u1", "restart_service", "重启 order-service", PLAN)

        assert out.ok is True
        assert store.advanced == [("remact_new", "pending_approval")]
        assert "尚未执行" in out.message, "必须让用户知道这只是建议、还没动手"

    @pytest.mark.asyncio
    async def test_valid_same_org_summary_id_is_linked(self):
        """§9.2 事后复盘视图的链路——LLM 紧接着一次 analyze_ops_incident 提议
        修复时，传回来的 summary_id 应该原样存进 create_proposed_action。"""
        store = FakeStore(
            scope=IN_SCOPE, summaries={"opssum_1": FakeSummary("opssum_1", ORG)},
        )
        out = await _toolset(store).propose_remediation(
            ORG, CONN, "u1", "restart_service", "重启 order-service", PLAN,
            summary_id="opssum_1",
        )
        assert out.ok is True
        assert store.created[0]["summary_id"] == "opssum_1"

    @pytest.mark.asyncio
    async def test_unknown_summary_id_is_silently_dropped_not_refused(self):
        """判别式：不存在的 summary_id 不该拒掉整条修复提议——链接只是复盘
        视图的辅助信息，不是这次提议正确性的前提。"""
        store = FakeStore(scope=IN_SCOPE, summaries={})
        out = await _toolset(store).propose_remediation(
            ORG, CONN, "u1", "restart_service", "重启 order-service", PLAN,
            summary_id="opssum_does_not_exist",
        )
        assert out.ok is True
        assert store.created[0]["summary_id"] is None

    @pytest.mark.asyncio
    async def test_cross_org_summary_id_is_silently_dropped(self):
        """判别式：另一家企业的 summary_id 同样不该被链接——即使 id 本身存在，
        跨 org 引用会在复盘视图里泄露"这个企业知道另一家企业发生过什么分析"
        这件事，哪怕只是一个不透明的 id 字符串。"""
        store = FakeStore(
            scope=IN_SCOPE, summaries={"opssum_1": FakeSummary("opssum_1", "org_other")},
        )
        out = await _toolset(store).propose_remediation(
            ORG, CONN, "u1", "restart_service", "重启 order-service", PLAN,
            summary_id="opssum_1",
        )
        assert out.ok is True
        assert store.created[0]["summary_id"] is None

    @pytest.mark.asyncio
    async def test_bad_scope_config_is_not_reported_as_ai_overreach(self):
        """管理员配错了 ≠ AI 提议越界。两种失败混为一谈会让管理员去骂 AI，
        而真正该改的是他自己的配置。"""
        store = FakeStore(scope=FakeScope({"wrong_key": []}))
        out = await _toolset(store).propose_remediation(
            ORG, CONN, "u1", "restart_service", "重启", PLAN)
        assert out.ok is False and out.refused is False
        assert "管理员" in out.message

    @pytest.mark.asyncio
    async def test_unknown_action_type_is_refused(self):
        store = FakeStore(scope=IN_SCOPE)
        out = await _toolset(store).propose_remediation(
            ORG, CONN, "u1", "delete_database", "删库跑路", PLAN)
        assert out.refused is True and store.created == []


class TestQueryTool:
    @pytest.mark.asyncio
    async def test_unavailable_systems_appear_in_the_text_not_only_in_data(self):
        """部分失败要出现在**给 LLM 看的文本**里——只放进 structured_data 的话，
        LLM 会把残缺数据当成全部数据来推理。"""
        transport = FakeTransport(raises=ConnectorUnavailable(ERROR_OFFLINE, "心跳超时"))
        out = await _toolset(FakeStore(), transport=transport).query_ops_system(ORG, "order-service")

        assert out.ok is False
        assert "不可用" in out.message and "Prometheus" in out.message
        assert out.data["unavailable"][0]["reason"] == ERROR_OFFLINE

    @pytest.mark.asyncio
    async def test_successful_query_reports_point_count(self):
        out = await _toolset(FakeStore(), transport=FakeTransport(points=5)).query_ops_system(
            ORG, "order-service")
        assert out.ok is True and out.data["point_count"] == 5


class _AnalysisStore(FakeStore):
    """在 FakeStore 上补出分析/开关这两条能力。"""

    def __init__(self, enabled=True, enabled_raises=None, save_raises=None):
        super().__init__()
        self._enabled, self._enabled_raises, self._save_raises = enabled, enabled_raises, save_raises
        self.saved = []

    async def is_module_enabled(self, org_id):
        if self._enabled_raises:
            raise self._enabled_raises
        return self._enabled

    async def save_analysis_summary(self, org_id, connection_id, summary, evidence_refs):
        if self._save_raises:
            raise self._save_raises
        self.saved.append({"org_id": org_id, "connection_id": connection_id,
                           "summary": summary, "evidence_refs": evidence_refs})
        return type("S", (), {"summary_id": "opssum_test"})()


class _MetricAlertTransport:
    """按查询类别返回不同数据：metric 给一条带尖峰的序列，alert 给一串同源告警。"""

    def __init__(self, metric_values=None, alert_count=5, raises=None):
        self._metric_values = metric_values if metric_values is not None else [10.0] * 20 + [900.0]
        self._alert_count = alert_count
        self._raises = raises
        self.kinds = []

    async def query(self, connection_id, org_id, request, timeout_s):
        self.kinds.append(request.kind)
        if self._raises:
            raise self._raises
        if request.kind == "metric":
            pts = [DataPoint(ts=float(i), value=v) for i, v in enumerate(self._metric_values)]
        else:
            pts = [DataPoint(ts=1000.0 + i * 10, text="5xx 比例升高",
                             labels={"env": "prod", "severity": "error"})
                   for i in range(self._alert_count)]
        return QueryResult(connection_id, "Prometheus", pts)

    async def online_status(self, connection_ids):
        return {c: True for c in connection_ids}


class TestModuleSwitchIsHonest:
    """未开通模块时要**明说**，不能静默返回空结果让 LLM 自己编解释。"""

    @pytest.mark.asyncio
    async def test_disabled_module_is_refused_with_a_clear_message(self):
        store = _AnalysisStore(enabled=False)
        transport = _MetricAlertTransport()
        out = await _toolset(store, transport=transport).query_ops_system(ORG, "order-service")

        assert out.refused is True
        assert "未开通" in out.message
        assert transport.kinds == [], "未开通时不该发出任何查询"

    @pytest.mark.asyncio
    async def test_switch_lookup_failure_fails_open(self):
        """读开关失败时放行——宁可放行也不要因为一次数据库抖动，就让管理员
        去找平台管理员开一个本来就开着的开关。"""
        store = _AnalysisStore(enabled_raises=RuntimeError("db down"))
        transport = _MetricAlertTransport()
        out = await _toolset(store, transport=transport).query_ops_system(ORG, "order-service")
        assert out.refused is False and transport.kinds != []


class _JsonLLM:
    def __init__(self, reply):
        self._reply = reply

    async def ainvoke(self, prompt):
        return self._reply


class TestAnalyzeOpsIncident:
    @pytest.mark.asyncio
    async def test_runs_the_whole_chain_and_persists_summary(self):
        store = _AnalysisStore()
        toolset = OpsToolset(
            FederatedQueryEngine(_MetricAlertTransport(), FakeDirectory()), store,
            llm=_JsonLLM('{"summary":"错误率突增","likely_causes":["上游超时"],"next_steps":["查连接池"]}'))
        out = await toolset.analyze_ops_incident(ORG, "order-service")

        assert out.ok is True
        assert out.data["incident_count"] == 1 and out.data["alert_count"] == 5
        assert out.data["anomaly_targets"], "带尖峰的指标应该被检出异常"
        assert store.saved, "非降级结果应当落库"
        # ⚠️ 落库的只有摘要 + 依据，没有原始运维数据（§3.1 BYOC）
        assert set(store.saved[0]) == {"org_id", "connection_id", "summary", "evidence_refs"}
        # §10.5 验收指标"告警合并率"要求这份统计量真的落库，不能只在这一次
        # ToolOutcome.data 里回给调用方看一眼就丢——判别式：把持久化那行的
        # correlation_stats_ref 删掉，这条会失败。
        stats_refs = [r for r in store.saved[0]["evidence_refs"] if r.get("source") == "alert_correlation_stats"]
        assert len(stats_refs) == 1
        assert stats_refs[0]["detail"] == {"alert_count": 5, "incident_count": 1, "noise_reduction": 0.8}

    @pytest.mark.asyncio
    async def test_degraded_result_is_not_persisted(self):
        """ops_analysis_summaries 是给审批人看数据血缘的。存一条"其实只是数据复述"
        的记录会稀释它的意义。"""
        store = _AnalysisStore()
        toolset = OpsToolset(
            FederatedQueryEngine(_MetricAlertTransport(), FakeDirectory()), store, llm=None)
        out = await toolset.analyze_ops_incident(ORG, "order-service")
        assert out.data["degraded"] is True and store.saved == []

    @pytest.mark.asyncio
    async def test_partial_data_failure_is_surfaced_in_the_conclusion(self):
        """结论建立在残缺数据上时用户必须知道——否则"没发现异常"会被当成"一切正常"。"""
        store = _AnalysisStore()
        toolset = OpsToolset(
            FederatedQueryEngine(
                _MetricAlertTransport(raises=ConnectorUnavailable(ERROR_OFFLINE, "心跳超时")),
                FakeDirectory()),
            store, llm=None)
        out = await toolset.analyze_ops_incident(ORG, "order-service")
        assert "不可用" in out.message and out.data["unavailable"]

    @pytest.mark.asyncio
    async def test_save_failure_does_not_lose_the_analysis(self):
        """落库失败不该把已经算出来的结论一起丢掉。"""
        store = _AnalysisStore(save_raises=RuntimeError("db down"))
        toolset = OpsToolset(
            FederatedQueryEngine(_MetricAlertTransport(), FakeDirectory()), store,
            llm=_JsonLLM('{"summary":"s"}'))
        out = await toolset.analyze_ops_incident(ORG, "order-service")
        assert out.ok is True and out.data["summary_id"] is None and out.message
