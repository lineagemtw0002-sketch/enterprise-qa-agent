"""扩缩容基线实测（`src/ops/measured_baseline.py`）。

这个模块存在的唯一理由是：**不采信 AI 自报的基线**。所以这里的每条测试
都在回答"某种情况下它会不会退回去信 AI"——退回去就等于这个修复失效。
"""

import pytest

from src.ops.measured_baseline import MEASURED_KEY, measure_instance_count, with_measured_baseline
from src.ops.types import DataPoint, FederatedResult, QueryResult


class _FakeEngine:
    """按需返回若干 service_health 数据点。`raises=True` 时模拟查询炸掉。"""

    def __init__(self, points=None, raises=False):
        self._points = points or []
        self._raises = raises
        self.calls = 0

    async def query(self, org_id, request, connection_ids=None, timeout_s=None, use_cache=True):
        self.calls += 1
        if self._raises:
            raise RuntimeError("连接器炸了")
        return FederatedResult(
            request=request,
            results=[QueryResult(connection_id="c1", system_name="sys", points=self._points)],
        )


def _pt(service, metric, value):
    return DataPoint(ts=0.0, value=value, text="", labels={"service": service, "metric": metric})


class TestMeasureInstanceCount:
    @pytest.mark.asyncio
    async def test_reads_the_instances_metric_of_the_named_service(self):
        engine = _FakeEngine([_pt("a", "instances", 6), _pt("b", "instances", 99),
                              _pt("a", "error_rate", 0.01)])
        assert await measure_instance_count(engine, "org", "a") == 6

    @pytest.mark.asyncio
    async def test_multiple_connectors_take_the_minimum(self):
        """同一个服务被两套监控看到时取**最小值**。

        取最大或取平均都会在某个连接器读数偏高时把天花板一起抬上去——
        那正是这次修复要消灭的东西。取最小是保守方向。
        """
        engine = _FakeEngine([_pt("a", "instances", 9), _pt("a", "instances", 3)])
        assert await measure_instance_count(engine, "org", "a") == 3

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", [0, -2, None, True])
    async def test_non_positive_or_non_numeric_values_are_ignored(self, bad):
        """`True` 也要排除——它是 `int` 的子类，不排除的话会被当成"1 个实例"，
        算出一个荒谬但看起来合法的上界。"""
        engine = _FakeEngine([_pt("a", "instances", bad)])
        assert await measure_instance_count(engine, "org", "a") is None

    @pytest.mark.asyncio
    async def test_missing_service_returns_none_not_zero(self):
        """测不到要跟"测到 0 个"分开——上层对这两者的处理不同。"""
        engine = _FakeEngine([_pt("other", "instances", 5)])
        assert await measure_instance_count(engine, "org", "a") is None

    @pytest.mark.asyncio
    async def test_query_failure_returns_none_and_does_not_raise(self):
        """连接器炸了要返回 None（上层据此拒绝），**不能抛异常**——
        抛出去会让"扩缩容判定不了"变成"服务器 500"，两者对调用方完全不同。"""
        assert await measure_instance_count(_FakeEngine(raises=True), "org", "a") is None

    @pytest.mark.asyncio
    async def test_no_engine_returns_none(self):
        assert await measure_instance_count(None, "org", "a") is None


class TestWithMeasuredBaseline:
    @pytest.mark.asyncio
    async def test_non_scale_actions_do_not_trigger_a_query(self):
        """重启/清盘/回滚不需要基线，不该为它们多发一次连接器查询。"""
        engine = _FakeEngine([_pt("a", "instances", 3)])
        out = await with_measured_baseline(engine, "org",
                                           {"action_type": "restart_service", "target": "a"})
        assert engine.calls == 0
        assert MEASURED_KEY not in out

    @pytest.mark.asyncio
    async def test_injects_the_measured_value_for_scale_actions(self):
        engine = _FakeEngine([_pt("a", "instances", 4)])
        out = await with_measured_baseline(engine, "org",
                                           {"action_type": "scale_instances", "target": "a"})
        assert out[MEASURED_KEY] == 4

    @pytest.mark.asyncio
    async def test_does_not_mutate_the_original_plan(self):
        """`plan` 会被原样落库。把平台算出来的字段混进去，之后没人分得清
        哪些是 AI 提议的、哪些是平台补的——而"模型以为几个"和"实际几个"
        不一致本身是有用的信号。"""
        engine = _FakeEngine([_pt("a", "instances", 4)])
        plan = {"action_type": "scale_instances", "target": "a"}
        await with_measured_baseline(engine, "org", plan)
        assert MEASURED_KEY not in plan

    @pytest.mark.asyncio
    async def test_unmeasurable_leaves_the_key_absent_so_the_check_denies(self):
        """测不到时**不要塞一个占位值**。键缺失正是越界判定用来拒绝的信号；
        塞 0 或塞自报值都会让拒绝逻辑失效。"""
        engine = _FakeEngine([])
        out = await with_measured_baseline(engine, "org",
                                           {"action_type": "scale_instances", "target": "a"})
        assert MEASURED_KEY not in out

    @pytest.mark.asyncio
    async def test_self_reported_baseline_is_carried_through_but_not_renamed(self):
        """AI 自报的 `baseline_instances` 原样保留（它是个信号），
        但**不会**被当成实测值使用。"""
        engine = _FakeEngine([_pt("a", "instances", 3)])
        out = await with_measured_baseline(
            engine, "org",
            {"action_type": "scale_instances", "target": "a", "baseline_instances": 5000})
        assert out["baseline_instances"] == 5000
        assert out[MEASURED_KEY] == 3
