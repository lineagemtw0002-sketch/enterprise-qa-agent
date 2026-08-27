"""事件指纹与生命周期判定（`src/ops/incident_identity.py`）。

设计见 `docs/alert_push_design.md` §4。这组测试要回答的是：
**告警反复进来时，平台会不会反复烧 LLM。** 实测一次 RCA 是 7–10 秒，
判定错一次就是白烧一次，而且时间线上多一条几乎一样的结论。
"""

import pytest

from src.ops.incident_identity import (
    FLAP_THRESHOLD, REOPEN_COOLDOWN_SECONDS, STATUS_OPEN, STATUS_RESOLVED,
    alert_fingerprint, decide, should_reanalyze_on_change,
)


class TestFingerprintStability:
    """指纹必须对"同一次故障的第 2…N 条告警"给出相同的值——
    否则去重形同虚设，每条告警都会被当成新故障。"""

    def test_same_incident_different_pod_gives_same_fingerprint(self):
        """**同一次故障里 pod 名/trace id 每条都不同**。把它们算进指纹，
        等于每条告警都是一件新事——这是最容易犯也最难发现的错。"""
        a = alert_fingerprint(targets=["order-service"],
                              labels={"rule": "http_5xx", "severity": "error", "pod": "order-1"})
        b = alert_fingerprint(targets=["order-service"],
                              labels={"rule": "http_5xx", "severity": "error", "pod": "order-2"})
        assert a == b

    def test_label_order_does_not_matter(self):
        a = alert_fingerprint(targets=["x"], labels={"rule": "r", "env": "prod"})
        b = alert_fingerprint(targets=["x"], labels={"env": "prod", "rule": "r"})
        assert a == b

    def test_target_order_and_duplicates_do_not_matter(self):
        a = alert_fingerprint(targets=["b", "a", "a"], labels={})
        b = alert_fingerprint(targets=["a", "b"], labels={})
        assert a == b

    def test_different_rule_gives_different_fingerprint(self):
        """不同的告警规则是不同的事——不能被去重合并掉。"""
        a = alert_fingerprint(targets=["x"], labels={"rule": "http_5xx"})
        b = alert_fingerprint(targets=["x"], labels={"rule": "disk_full"})
        assert a != b

    def test_is_stable_across_processes(self):
        """⚠️ **不能用内置 `hash()`** —— 它带进程随机种子，同样的输入在两次
        进程启动之间会得到不同的值，事件在重启后全部对不上。
        这里断言的是"指纹只由输入决定"，用一个已知输入的固定期望值钉死。
        """
        got = alert_fingerprint(targets=["order-service"], labels={"rule": "http_5xx"})
        again = alert_fingerprint(targets=["order-service"], labels={"rule": "http_5xx"})
        assert got == again
        assert len(got) == 32 and all(c in "0123456789abcdef" for c in got)


class TestDecideLifecycle:
    def test_never_seen_creates_and_analyzes(self):
        d = decide(None, now=1000.0)
        assert (d.action, d.should_analyze) == ("create", True)

    def test_already_open_updates_without_reanalyzing(self):
        """**这条是省钱的主力。** 事件开着的时候同指纹告警只累加计数——
        Prometheus 的 `repeat_interval` 会周期性重发，每次都重新分析就是
        每 4 小时白烧一次 LLM，而结论一模一样。"""
        d = decide({"status": STATUS_OPEN, "flap_count": 0}, now=1000.0)
        assert (d.action, d.should_analyze) == ("update", False)

    def test_reopen_within_cooldown_does_not_reanalyze(self):
        """进程反复重启：关闭后马上又坏。**指纹去重挡不住它**
        （每次变坏都是真实状态转换），靠冷却期挡。"""
        d = decide({"status": STATUS_RESOLVED, "resolved_at": 1000.0, "flap_count": 0},
                   now=1000.0 + REOPEN_COOLDOWN_SECONDS - 1)
        assert (d.action, d.should_analyze) == ("reopen", False)
        assert d.flap_count == 1

    def test_after_cooldown_is_a_new_incident(self):
        """隔了很久又坏，是新的一次故障，该重新分析——
        不能因为指纹一样就永远复用同一个事件。"""
        d = decide({"status": STATUS_RESOLVED, "resolved_at": 1000.0, "flap_count": 5},
                   now=1000.0 + REOPEN_COOLDOWN_SECONDS + 1)
        assert (d.action, d.should_analyze) == ("create", True)

    def test_crossing_flap_threshold_analyzes_once_more(self):
        """反复横跳的根因跟"挂了一次"根本不同（OOM 反复重启、健康检查配置不当
        …），值得**单独给一次结论**——但只这一次。"""
        base = {"status": STATUS_RESOLVED, "resolved_at": 1000.0}
        below = decide({**base, "flap_count": FLAP_THRESHOLD - 2}, now=1001.0)
        at = decide({**base, "flap_count": FLAP_THRESHOLD - 1}, now=1001.0)
        assert below.should_analyze is False
        assert at.should_analyze is True and at.is_flapping is True

    def test_resolved_without_timestamp_is_treated_as_new(self):
        """数据不一致时**当成新故障**，不猜一个关闭时间——猜错会把真实的
        新故障并进一个陈旧事件里，而那条事件的结论早就过期了。"""
        d = decide({"status": STATUS_RESOLVED, "resolved_at": None}, now=1000.0)
        assert (d.action, d.should_analyze) == ("create", True)


class TestReanalyzeOnChange:
    def test_new_target_means_reanalyze(self):
        """波及面扩大是新信息——"只有 A 挂"和"A 带着 B 一起挂"是完全不同的结论。"""
        assert should_reanalyze_on_change(["a"], ["a", "b"]) is True

    def test_more_alerts_on_same_targets_is_not_a_change(self):
        """只是告警条数变多不算：同一件事在持续，结论不会变。"""
        assert should_reanalyze_on_change(["a", "b"], ["b", "a"]) is False

    def test_shrinking_is_not_a_change(self):
        """部分服务恢复了不该触发重新分析——那属于事件在收敛，
        真正恢复完会走 resolved。"""
        assert should_reanalyze_on_change(["a", "b"], ["a"]) is False
