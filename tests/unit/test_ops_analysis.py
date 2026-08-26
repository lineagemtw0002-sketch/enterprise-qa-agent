"""智能运维 AI 分析层（`src/ops/analysis/`）——设计 `docs/aiops_module_design.md` §2。

全假件，不连模型/库/网络，毫秒级。

**判别力说明**（CLAUDE.md §7.2）：这是全新代码，没有旧实现可对照，所以每条用例
守的是一条**改坏了不会报错、只会静默变成错误行为**的规则。其中四条最关键：

- `test_mad_catches_the_spike_that_stddev_would_miss` —— 把中位数+MAD 换成
  均值+标准差（一个看起来完全等价的"优化"）就会红
- `test_model_cannot_inject_its_own_evidence` —— 让模型输出里的 evidence 生效就会红
- `test_shared_labels_use_intersection_not_union` —— 把交集改成并集就会红
- `test_empty_input_is_not_full_noise_reduction` —— 空输入返回 1.0 就会红
"""

from __future__ import annotations

import statistics

import pytest

from src.ops.analysis import (
    Alert,
    analyze_root_cause,
    build_evidence,
    correlate_alerts,
    detect_anomalies,
)
from src.ops.analysis.detection import DIRECTION_DIP, DIRECTION_SPIKE, MIN_SAMPLES
from src.ops.types import DataPoint


def _pts(values, start_ts=0.0, step=60.0):
    return [DataPoint(ts=start_ts + i * step, value=v) for i, v in enumerate(values)]


class TestAnomalyDetection:
    def test_mad_catches_the_spike_that_stddev_would_miss(self):
        """⚠️ **这条是"为什么用 MAD 不用标准差"的判别式。**

        遮蔽效应：几根足够高的尖峰会把标准差本身抬上去，于是它们自己落在
        「均值 ± 3σ」里面。下面这组数据就是这种情况（实测 z=2.19，标准差法抓不到；
        稳健 z=600，MAD 法一眼看穿）——先断言前提成立，再断言我们的实现抓得到。
        把 detection.py 换成均值+标准差，这条立刻红。

        ⚠️ 构造这组数据时踩过一个坑：**一根尖峰是遮蔽不了标准差的**
        （20 个 10 加一个 900，z 仍有 4.36，标准差法照样抓得到）。
        遮蔽需要多根离群点一起把方差抬起来。写这类"证明 A 方法优于 B 方法"的
        测试时，前提那一步必须真的断言，不能凭直觉假设它成立。
        """
        values = [float(v) for v in [10, 11, 9, 10, 12, 8, 10, 11, 9, 10] * 2] + [900.0] * 4
        mean = statistics.fmean(values)
        stdev = statistics.stdev(values)
        assert abs(900.0 - mean) / stdev < 3.0, "前提不成立：这组数据的尖峰没有遮蔽标准差"

        report = detect_anomalies(_pts(values), target="order-service", metric="error_rate")
        assert report.has_anomaly, "MAD 应该抓到这些被标准差遮蔽的尖峰"
        assert {a.value for a in report.anomalies} == {900.0}
        assert len(report.anomalies) == 4
        assert all(a.direction == DIRECTION_SPIKE for a in report.anomalies)

    def test_too_few_samples_is_not_evaluated_rather_than_normal(self):
        """样本不足要返回「没评估」，不能伪装成「没异常」——
        前者该去把时间窗拉长，后者可以放心，两者对调用方是相反的信息。"""
        report = detect_anomalies(_pts([1.0] * (MIN_SAMPLES - 1)), target="x")
        assert report.evaluated is False
        assert report.has_anomaly is False
        assert "少于" in report.reason
        assert "未做异常判断" in report.describe()

    def test_constant_series_has_no_anomaly_and_does_not_divide_by_zero(self):
        """常数序列的 MAD 是 0。除零会崩，而"每个点都算异常"更糟——
        恒定值指标是真实存在的（比如一个一直是 0 的错误率）。"""
        report = detect_anomalies(_pts([5.0] * 30), target="x")
        assert report.evaluated is True and report.anomalies == []

    def test_near_constant_series_still_finds_the_outlier(self):
        """一半以上的点相同导致 MAD=0，但序列并非常数——这时要用平均绝对偏差兜底，
        不能直接判定"无异常"，否则最容易检测的那种异常反而漏掉。"""
        report = detect_anomalies(_pts([5.0] * 19 + [80.0]), target="x")
        assert report.has_anomaly and report.anomalies[0].value == 80.0

    def test_dip_is_detected_and_labelled(self):
        """跌到 0 跟涨到天上一样是故障信号（比如 QPS 突然归零）。"""
        report = detect_anomalies(_pts([100.0] * 20 + [0.0]), target="x")
        assert report.has_anomaly and report.anomalies[0].direction == DIRECTION_DIP

    def test_none_values_are_skipped_not_treated_as_zero(self):
        """采集缺失（value=None）不能当成 0——那会把每个空洞都报成暴跌。"""
        points = _pts([100.0] * 20) + [DataPoint(ts=999.0, value=None)]
        report = detect_anomalies(points, target="x")
        assert report.sample_count == 20 and not report.has_anomaly


class TestAlertCorrelation:
    def _burst(self, n=15, target="order-service", start=1000.0, step=10.0, labels=None):
        return [Alert(f"a{i}", ts=start + i * step, target=target,
                      labels=labels if labels is not None else {"env": "prod", "cluster": "c1"},
                      severity="error") for i in range(n)]

    def test_a_burst_of_similar_alerts_becomes_one_incident(self):
        result = correlate_alerts(self._burst())
        assert len(result.incidents) == 1
        assert result.incidents[0].alert_count == 15
        assert result.noise_reduction == pytest.approx(1 - 1 / 15)

    def test_unrelated_alerts_stay_separate(self):
        alerts = self._burst(3) + self._burst(3, target="billing", labels={"env": "prod", "cluster": "c9"})
        assert len(correlate_alerts(alerts).incidents) == 2

    def test_gap_larger_than_window_starts_a_new_incident(self):
        alerts = self._burst(3) + self._burst(3, start=1000.0 + 10_000.0)
        assert len(correlate_alerts(alerts, window_s=300.0).incidents) == 2

    def test_max_span_stops_infinite_chaining(self):
        """窗口是"跟最后一条比"，所以一条接一条能无限延长。max_span 兜住总长度——
        没有它，一个断断续续响了一整天的告警会变成一个"持续 24 小时"的事件。"""
        alerts = self._burst(n=40, step=100.0)   # 跨度 3900s
        result = correlate_alerts(alerts, window_s=300.0, max_span_s=1000.0)
        assert len(result.incidents) > 1
        assert all(i.ended_at - i.started_at <= 1000.0 for i in result.incidents)

    def test_shared_labels_use_intersection_not_union(self):
        """⚠️ 共同标签必须是交集。用并集的话，某一条告警独有的标签会被说成
        整个事件的特征，审批人照着它去查会扑空。"""
        alerts = [
            Alert("a1", ts=1000.0, target="svc", labels={"env": "prod", "az": "east"}),
            Alert("a2", ts=1010.0, target="svc", labels={"env": "prod", "az": "west"}),
        ]
        incident = correlate_alerts(alerts).incidents[0]
        assert incident.shared_labels == {"env": "prod"}
        assert "az" not in incident.shared_labels

    def test_empty_input_is_not_full_noise_reduction(self):
        """⚠️ 没有告警不等于降噪 100%——那会让空数据在报表上看起来像最好的成绩。"""
        assert correlate_alerts([]).noise_reduction == 0.0

    def test_incident_severity_is_the_worst_one(self):
        alerts = [
            Alert("a1", ts=1000.0, target="svc", labels={"env": "prod"}, severity="warning"),
            Alert("a2", ts=1010.0, target="svc", labels={"env": "prod"}, severity="critical"),
        ]
        assert correlate_alerts(alerts).incidents[0].severity == "critical"


class _FakeLLM:
    def __init__(self, reply=None, raises=None):
        self._reply, self._raises = reply, raises
        self.prompts = []

    async def ainvoke(self, prompt):
        self.prompts.append(prompt)
        if self._raises:
            raise self._raises
        return self._reply


def _sample_inputs():
    report = detect_anomalies(_pts([10.0] * 20 + [900.0]), target="order-service", metric="error_rate")
    incident = correlate_alerts([
        Alert(f"a{i}", ts=1000.0 + i * 10, target="order-service",
              labels={"env": "prod"}, text="5xx 比例升高", severity="error")
        for i in range(5)
    ]).incidents[0]
    return incident, [report]


class TestRootCauseAnalysis:
    @pytest.mark.asyncio
    async def test_no_llm_degrades_instead_of_failing(self):
        incident, reports = _sample_inputs()
        result = await analyze_root_cause(incident=incident, anomaly_reports=reports, llm=None)
        assert result.degraded is True and result.summary
        assert result.evidence, "降级也要给依据——依据是算出来的，跟模型无关"

    @pytest.mark.asyncio
    async def test_llm_failure_degrades_and_never_raises(self):
        """分析是辅助能力，模型挂了该少给信息，不该让整条链路失败
        （跟 doc_summary.py 的降级模式一致）。"""
        incident, reports = _sample_inputs()
        result = await analyze_root_cause(
            incident=incident, anomaly_reports=reports, llm=_FakeLLM(raises=RuntimeError("ollama down")))
        assert result.degraded is True and "ollama down" in result.degraded_reason

    @pytest.mark.asyncio
    async def test_degradation_warning_comes_first_in_the_text(self):
        """降级提示放末尾的话，模型转述时经常把它丢掉。"""
        incident, reports = _sample_inputs()
        result = await analyze_root_cause(incident=incident, anomaly_reports=reports, llm=None)
        assert result.to_text().splitlines()[0].startswith("⚠️")

    @pytest.mark.asyncio
    async def test_model_cannot_inject_its_own_evidence(self):
        """⚠️ **依据必须由代码从输入推导，绝不采信模型输出。**

        模型会编出看起来很像真的 PromQL 和时间窗，而审批人恰恰会因为"有引用"
        就更容易相信结论。这条构造一个在输出里塞假引用的模型，断言假引用不会
        出现在结果里、真引用一条不少。
        """
        incident, reports = _sample_inputs()
        evil = ('{"summary": "s", "likely_causes": ["c"], "next_steps": ["n"], '
                '"evidence": [{"source": "prometheus", "description": "伪造的依据", '
                '"detail": {"query": "rate(fake_metric[5m])"}}]}')
        result = await analyze_root_cause(
            incident=incident, anomaly_reports=reports, llm=_FakeLLM(reply=evil))

        blob = " ".join(e.description + str(e.detail) for e in result.evidence)
        assert "伪造的依据" not in blob and "fake_metric" not in blob
        assert {e.source for e in result.evidence} == {"alert_correlation", "anomaly_detection"}
        assert result.evidence == build_evidence(incident, reports), "依据应与纯推导结果逐项相同"

    @pytest.mark.asyncio
    async def test_free_text_reply_is_kept_but_not_marked_degraded(self):
        """模型给了自由文本而不是 JSON：内容仍有价值，但**不算降级**——
        模型确实推理了，只是格式没听话。混为一谈会让"模型挂了"和"格式没对"
        在 UI 上长得一样。"""
        incident, reports = _sample_inputs()
        result = await analyze_root_cause(
            incident=incident, anomaly_reports=reports, llm=_FakeLLM(reply="我觉得是数据库连接池满了"))
        assert result.degraded is False and "连接池" in result.summary

    @pytest.mark.asyncio
    async def test_prompt_forbids_fabrication_and_conclusions(self):
        """提示词是模型行为的第一道闸（工具层的硬约束是第二道）。"""
        incident, reports = _sample_inputs()
        llm = _FakeLLM(reply='{"summary":"s"}')
        await analyze_root_cause(incident=incident, anomaly_reports=reports, llm=llm)
        prompt = llm.prompts[0]
        assert "不要编造" in prompt and "不是结论" in prompt

    def test_unevaluated_reports_still_appear_in_evidence(self):
        """"这个指标我们其实没看"必须留证，否则审批人会默认它正常。"""
        skipped = detect_anomalies(_pts([1.0, 2.0]), target="billing", metric="latency")
        assert skipped.evaluated is False
        evidence = build_evidence(None, [skipped])
        assert evidence[0].detail["evaluated"] is False

    @pytest.mark.asyncio
    async def test_no_data_at_all_says_so(self):
        result = await analyze_root_cause(incident=None, anomaly_reports=[], llm=None)
        assert "没有可用于分析" in result.summary and result.evidence == []
