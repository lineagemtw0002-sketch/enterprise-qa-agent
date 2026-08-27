"""服务健康网格的判定逻辑（`src/ops/service_health.py`）。

判别力说明：下面每一条都对应设计稿（`docs/design_reference/aiops_console_mockup.html`）
里一格具体的服务，而**第一版实现有三条是错的**，这些测试在那一版下会失败：

1. `notification-worker`（队列延迟 3s）第一版被判 `critical` ——因为队列延迟
   用了 HTTP P95 的阈值。设计稿上它是"正常"。
2. `search-index`（一个指标都查不到）第一版被判 `warning`，混进了"观察中"。
   设计稿上它是"数据中断"，那是完全不同的一件事。
3. 只报 `discovered` 的服务第一版**根本不会出现在网格里**（没有指标点就不建桶），
   而设计稿要求它出现、并显示成数据中断。
"""

import pytest

from src.ops.service_health import (
    DEFAULT_THRESHOLDS, STATUS_CRITICAL, STATUS_OK, STATUS_STALE, STATUS_WARNING,
    InvalidThresholds, classify, median_seconds, points_to_services,
    resolve_thresholds, validate_thresholds,
)


def _pt(service, metric, value):
    return {"ts": 0.0, "value": value, "text": "", "labels": {"service": service, "metric": metric}}


class TestClassify:
    def test_http_latency_and_error_rate_take_the_worse_side(self):
        # 延迟正常但错误率 8% —— 这个服务就是坏的，不能被平均成"观察中"
        assert classify(0.081, 120.0) == STATUS_CRITICAL
        assert classify(0.0001, 2400.0) == STATUS_CRITICAL

    def test_queue_latency_is_far_more_tolerant_than_http(self):
        """设计稿上 notification-worker 队列延迟 3s 标的是"正常"。"""
        assert classify(0.001, None, 3000.0) == STATUS_OK
        # 同一个数字放在 HTTP 延迟上就是严重故障
        assert classify(0.001, 3000.0, None) == STATUS_CRITICAL
        assert classify(0.001, None, 35000.0) == STATUS_CRITICAL

    def test_no_metrics_at_all_is_stale_not_ok_and_not_warning(self):
        """"查不到数据"跟"很健康"和"有点小问题"都不是一回事。"""
        assert classify(None, None) == STATUS_STALE
        assert classify(None, None, None) == STATUS_STALE

    @pytest.mark.parametrize("err,expected", [
        (0.0002, STATUS_OK), (0.012, STATUS_WARNING), (0.081, STATUS_CRITICAL),
    ])
    def test_error_rate_bands(self, err, expected):
        assert classify(err, 88.0) == expected


class TestPointsToServices:
    def test_matches_the_design_mockup_service_by_service(self):
        from services.ops_probe_demo.fake_ops_data import points_for
        got = {s.service: s.status for s in points_to_services(
            points_for("service_health", target="", start_ts=0, end_ts=0))}
        assert got == {
            "order-service": STATUS_CRITICAL,       # 设计稿：异常
            "payment-gateway": STATUS_WARNING,      # 设计稿：观察中
            "auth-service": STATUS_OK,              # 设计稿：正常
            "inventory-api": STATUS_OK,             # 设计稿：正常
            "notification-worker": STATUS_OK,       # 设计稿：正常（队列延迟 3s）
            "search-index": STATUS_STALE,           # 设计稿：数据中断
        }

    def test_discovered_only_service_still_appears(self):
        out = points_to_services([_pt("search-index", "discovered", 1.0)])
        assert [s.service for s in out] == ["search-index"]
        assert out[0].status == STATUS_STALE

    def test_points_without_service_label_are_skipped_not_guessed(self):
        out = points_to_services([
            {"ts": 0.0, "value": 9.9, "labels": {"metric": "error_rate"}},   # 没有 service
            _pt("auth-service", "error_rate", 0.0002),
        ])
        assert [s.service for s in out] == ["auth-service"]
        assert out[0].error_rate == 0.0002

    def test_broken_services_sort_first(self):
        out = points_to_services([
            _pt("z-ok", "error_rate", 0.0), _pt("a-bad", "error_rate", 0.5),
            _pt("m-warn", "error_rate", 0.02),
        ])
        assert [s.service for s in out] == ["a-bad", "m-warn", "z-ok"]

    def test_non_numeric_values_do_not_crash_or_count(self):
        out = points_to_services([
            _pt("svc", "error_rate", "8.1%"), _pt("svc", "p95_latency_ms", True),
        ])
        assert out[0].status == STATUS_STALE      # 一个有效指标都没有


class TestMedianSeconds:
    def test_empty_is_none_not_zero(self):
        """"还没有样本"和"耗时是 0"是两件不同的事。"""
        assert median_seconds([]) is None

    def test_even_and_odd(self):
        assert median_seconds([10, 20, 30]) == 20.0
        assert median_seconds([10, 20, 30, 40]) == 25.0

    def test_negative_durations_are_dropped(self):
        # 执行时间早于告警时间说明数据有问题，不能让它把中位数拉低
        assert median_seconds([-5, 10, 20, 30]) == 20.0


class TestThresholdValidation:
    """阈值校验。**非法配置必须当场报错，不能夹紧成一个看起来正常的值。**"""

    def test_unknown_field_is_rejected_not_ignored(self):
        """静默忽略的后果是管理员以为改了、界面也显示保存成功，实际一点没生效。"""
        with pytest.raises(InvalidThresholds, match="未知的阈值字段"):
            validate_thresholds({"err_rate": 0.5})

    @pytest.mark.parametrize("bad", [
        {"error_rate_warning": -1}, {"error_rate_warning": 0},
        {"p95_warning_ms": "500"}, {"p95_warning_ms": True},
    ])
    def test_non_positive_or_non_numeric_rejected(self, bad):
        with pytest.raises(InvalidThresholds):
            validate_thresholds(bad)

    def test_warning_above_critical_is_rejected_even_when_only_one_is_given(self):
        """只改 warning 把它顶到默认 critical 之上，那个指标就**永远跳不到
        warning 档**——一个永远不会出现的状态比配错更难被发现，所以要跟已经
        生效的另一半合起来判，不能只看这次传了什么。"""
        with pytest.raises(InvalidThresholds, match="不能大于"):
            validate_thresholds({"error_rate_warning": 0.5})
        # 两个一起改成自洽的一对就该通过
        assert validate_thresholds({"error_rate_warning": 0.1, "error_rate_critical": 0.2})

    def test_not_a_dict(self):
        with pytest.raises(InvalidThresholds):
            validate_thresholds(["error_rate_warning", 0.1])


class TestThresholdResolution:
    def test_field_level_fallback_not_whole_block(self):
        """只覆盖一个字段时，其余五个必须仍然跟随平台默认。

        整块替换的话，管理员改一个错误率就等于把另外五个冻结在填写那天的值上，
        以后平台默认改了它们也不会跟着动——而他完全不知道自己做了这件事。
        """
        th = resolve_thresholds(None, {"error_rate_critical": 0.2})
        assert th.error_rate_critical == 0.2
        assert th.p95_critical_ms == DEFAULT_THRESHOLDS.p95_critical_ms
        assert th.queue_warning_ms == DEFAULT_THRESHOLDS.queue_warning_ms

    def test_later_overrides_win(self):
        th = resolve_thresholds({"p95_warning_ms": 1000}, {"p95_warning_ms": 50})
        assert th.p95_warning_ms == 50

    def test_none_and_empty_layers_are_skipped(self):
        assert resolve_thresholds(None, {}, None) == DEFAULT_THRESHOLDS

    def test_unknown_keys_in_stored_overrides_are_ignored(self):
        """解析时对历史脏数据要宽容——校验发生在写入端，读取端再抛异常
        会让一条坏数据把整个大屏打挂。"""
        assert resolve_thresholds({"legacy_field": 1, "p95_warning_ms": 700}).p95_warning_ms == 700


class TestClassifyWithCustomThresholds:
    def test_relaxed_thresholds_flip_a_critical_service_to_ok(self):
        loose = resolve_thresholds({"error_rate_warning": 0.1, "error_rate_critical": 0.2,
                                    "p95_warning_ms": 3000, "p95_critical_ms": 5000})
        assert classify(0.081, 2400.0) == STATUS_CRITICAL          # 平台默认下是异常
        assert classify(0.081, 2400.0, thresholds=loose) == STATUS_OK

    def test_per_service_override_beats_connector_default(self):
        out = points_to_services(
            [_pt("auth-service", "p95_latency_ms", 88.0)],
            overrides={"*": {"p95_warning_ms": 1000}, "auth-service": {"p95_warning_ms": 50}},
        )
        assert out[0].status == STATUS_WARNING   # 服务级 50ms 生效，不是连接器默认的 1000ms

    def test_override_for_one_service_does_not_touch_another(self):
        out = {s.service: s.status for s in points_to_services(
            [_pt("a", "error_rate", 0.081), _pt("b", "error_rate", 0.081)],
            overrides={"a": {"error_rate_warning": 0.1, "error_rate_critical": 0.2}},
        )}
        assert out == {"a": STATUS_OK, "b": STATUS_CRITICAL}
