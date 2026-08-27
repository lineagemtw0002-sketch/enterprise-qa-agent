"""演示探针的故障注入模型（`services/ops_probe_demo/`）。

这个文件测的是**演示件**，不是产品代码——但它有真实价值：演示环境一旦
"注入了故障却什么都没发生"，排查方向几乎必然被带到平台侧（检测器？联邦查询？
权限？），而真实原因只是模拟数据不够典型。把每一种故障的预期效果钉在这里，
下次改数据时立刻会知道破坏了哪一种。

⚠️ **刻意用真实的检测/关联函数做断言**，不 mock：这里要回答的问题就是
"这份数据能不能被真实的检测器发现"，用假件回答等于没回答。
"""

import time

import pytest

from services.ops_probe_demo.environments import (
    ENVIRONMENTS, FAULT_KINDS, effective_spec, resolve_environment,
)
from services.ops_probe_demo.fake_ops_data import points_for
from src.ops.analysis import Alert, correlate_alerts, detect_anomalies
from src.ops.service_health import STATUS_OK, STATUS_STALE, points_to_services
from src.ops.types import DataPoint


def _alerts(points):
    return [
        Alert(f"a{i}", ts=p["ts"], target=p["labels"]["target"], labels=p["labels"],
              text=p["text"], severity=p["labels"].get("severity", "error"))
        for i, p in enumerate(points)
    ]


class TestHealthyByDefault:
    """**健康是默认状态。** 一个永远有告警的演示环境，证明不了"没问题时不误报"。"""

    @pytest.mark.parametrize("env_key", sorted(ENVIRONMENTS))
    def test_no_fault_means_no_alerts_and_no_anomaly(self, env_key):
        now = time.time()
        assert points_for("alert", target="", start_ts=now - 3600, end_ts=now,
                          environment=env_key) == []
        env = resolve_environment(env_key)
        for service, spec in env.services.items():
            if not spec.reports_metrics:
                continue
            series = points_for("metric", target=service, start_ts=now - 3600, end_ts=now,
                                environment=env_key)
            report = detect_anomalies(
                [DataPoint(ts=p["ts"], value=p["value"]) for p in series], target=service)
            assert not report.has_anomaly, f"{env_key}/{service} 没注入故障却被判异常"

    @pytest.mark.parametrize("env_key", sorted(ENVIRONMENTS))
    def test_no_fault_means_every_reporting_service_is_ok(self, env_key):
        got = {s.service: s.status for s in points_to_services(
            points_for("service_health", target="", start_ts=0, end_ts=0, environment=env_key))}
        env = resolve_environment(env_key)
        for service, spec in env.services.items():
            expected = STATUS_OK if spec.reports_metrics else STATUS_STALE
            assert got[service] == expected, f"{env_key}/{service} 正常态却是 {got[service]}"


class TestEveryFaultKindIsTriggerable:
    """**每一种故障都要能被触发、且效果可验证。**

    这是这组测试的核心：`FAULT_KINDS` 里加了一种新故障却忘了让它真正影响数据，
    演示时只会表现为"注入了但大屏没反应"，没有任何地方会报错。
    """

    @pytest.mark.parametrize("fault_key", sorted(FAULT_KINDS))
    def test_fault_changes_the_service_status_as_declared(self, fault_key):
        kind = FAULT_KINDS[fault_key]
        target = "order-service"
        got = {s.service: s.status for s in points_to_services(
            points_for("service_health", target="", start_ts=0, end_ts=0,
                       faults={target: fault_key}))}
        assert got[target] == kind.expected_status, (
            f"{fault_key} 声明预期状态 {kind.expected_status}，实际 {got[target]}")

    @pytest.mark.parametrize("fault_key", sorted(FAULT_KINDS))
    def test_fault_produces_alerts_that_collapse_into_one_incident(self, fault_key):
        now = time.time()
        points = points_for("alert", target="order-service", start_ts=now - 3600, end_ts=now,
                            faults={"order-service": fault_key})
        assert points, f"{fault_key} 没有产生任何告警"
        corr = correlate_alerts(_alerts(points))
        assert len(corr.incidents) == 1, (
            f"{fault_key} 的 {corr.original_count} 条同源告警被切成了 {len(corr.incidents)} 个事件")
        assert corr.noise_reduction > 0.8

    @pytest.mark.parametrize("fault_key", sorted(FAULT_KINDS))
    def test_fault_only_touches_the_service_it_names(self, fault_key):
        """注入一个服务的故障，不该把别的服务也染色——否则界面上分辨不出
        "到底是什么坏了"，而这正是这个模块要帮人回答的问题。"""
        got = {s.service: s.status for s in points_to_services(
            points_for("service_health", target="", start_ts=0, end_ts=0,
                       faults={"order-service": fault_key}))}
        assert got["payment-gateway"] == STATUS_OK
        assert got["auth-service"] == STATUS_OK

    @pytest.mark.parametrize("fault_key", sorted(FAULT_KINDS))
    def test_healing_restores_the_baseline(self, fault_key):
        """恢复之后必须真的回到正常态——演示的价值有一半在"修完变绿"这一下。"""
        got = {s.service: s.status for s in points_to_services(
            points_for("service_health", target="", start_ts=0, end_ts=0, faults={}))}
        assert got["order-service"] == STATUS_OK


class TestFaultOverlaySemantics:
    def test_fault_only_overrides_the_metrics_it_affects(self):
        """只影响错误率的故障不该把延迟也改掉。"""
        env = resolve_environment("ecommerce")
        base = env.services["order-service"]
        hurt = effective_spec(base, "error_spike")
        assert hurt.error_rate != base.error_rate
        assert hurt.p95_latency_ms == base.p95_latency_ms

    def test_down_stops_reporting_entirely(self):
        env = resolve_environment("ecommerce")
        hurt = effective_spec(env.services["order-service"], "down")
        assert hurt.reports_metrics is False

    def test_unknown_fault_is_rejected_not_ignored(self):
        env = resolve_environment("ecommerce")
        with pytest.raises(KeyError):
            effective_spec(env.services["order-service"], "not_a_real_fault")

    def test_unknown_environment_is_rejected(self):
        with pytest.raises(KeyError):
            resolve_environment("nope")


class TestEveryRemediationActionHasATriggerableFault:
    def test_all_four_action_types_are_covered(self):
        """平台支持四类修复动作，每一类都要有一种故障能把它演出来——
        否则 `clean_disk`/`rollback_deployment` 在演示里永远只能空跑。"""
        covered = {k.suggested_action for k in FAULT_KINDS.values()}
        assert covered == {"restart_service", "scale_instances",
                           "clean_disk", "rollback_deployment"}
