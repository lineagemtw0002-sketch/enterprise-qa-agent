"""事件指纹与生命周期判定——纯函数，不碰数据库、不发查询。

设计见 `docs/alert_push_design.md` §4。这里回答三个问题：

1. **这条告警属于哪一件事？**（指纹）
2. **这是新的一件事，还是已经打开的那件事又来了？**（复用判定）
3. **它是不是在反复横跳？**（flapping）

跟 `aiops_scope.py` / `service_health.py` 同一个模式：判定写成纯函数才测得动。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence

STATUS_OPEN = "open"
STATUS_RESOLVED = "resolved"

REOPEN_COOLDOWN_SECONDS = 300.0
"""事件 resolved 之后多久之内，同指纹的新告警算"又抖了一下"而不是新故障。

⚠️ **这个冷却期是 flapping 抑制的全部** —— 没有它，进程反复重启时每次"变坏"
都会新建事件、重新跑一次 RCA（实测 7–10 秒），一分钟抖 5 次就是 5 次 LLM
调用和 5 条几乎一样的结论。

5 分钟这个值对齐 Prometheus 里常见的 `for: 5m`；真实部署应当可配。
"""

FLAP_THRESHOLD = 3
"""同一个事件被拉回 open 多少次之后，算"反复重启"这一类独立故障。

⚠️ 越过阈值时**要重新分析一次**——反复横跳的根因跟"挂了一次"根本不同
（多半是 OOM 反复重启、健康检查配置不当、依赖不稳定），值得单独给一次结论。
但**只分析这一次**，之后只累加计数，否则又回到"每次抖都写一遍作文"。
"""

# 指纹里**刻意排除**的标签：它们每次告警都不一样，算进去等于每条告警都是新事件。
_VOLATILE_LABELS = frozenset({
    "ts", "timestamp", "time", "value", "alert_id", "id", "fingerprint",
    "instance_id", "pod", "pod_name", "container_id", "request_id", "trace_id",
})


def alert_fingerprint(*, targets: Sequence[str], labels: Dict[str, Any]) -> str:
    """一件事的稳定标识。

    ⚠️ **必须对"同一次故障的第 2…N 条告警"给出相同的值**，否则指纹去重形同虚设。
    所以：targets 排序去重、labels 按 key 排序、**剔除每次都变的字段**
    （pod 名、trace id、时间戳……算进去的话每条告警都会是一件新事）。

    ⚠️ 用 `sha256` 而不是内置 `hash()`：后者带进程随机种子，**同样的输入在两次
    进程启动之间会得到不同的值**——事件在重启后全部对不上。这个坑
    `src/ops/types.py::QueryRequest.fingerprint()` 已经踩过一次并写了注释。
    """
    stable = {k: str(v) for k, v in sorted((labels or {}).items())
              if k not in _VOLATILE_LABELS}
    payload = json.dumps(
        {"targets": sorted({t for t in targets if t}), "labels": stable},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def fingerprint_of_incident(incident: Any) -> str:
    """从 `correlate_alerts` 产出的 Incident 算指纹。"""
    return alert_fingerprint(targets=getattr(incident, "targets", []) or [],
                             labels=getattr(incident, "shared_labels", {}) or {})


@dataclass(frozen=True)
class IncidentDecision:
    """收到一批同指纹告警之后，该拿已有事件怎么办。"""

    action: str                 # "create" | "update" | "reopen"
    should_analyze: bool
    reason: str
    flap_count: int = 0

    @property
    def is_flapping(self) -> bool:
        return self.flap_count >= FLAP_THRESHOLD


def decide(existing: Optional[Dict[str, Any]], *, now: float,
           cooldown_s: float = REOPEN_COOLDOWN_SECONDS,
           flap_threshold: int = FLAP_THRESHOLD) -> IncidentDecision:
    """核心判定：这批告警要新建事件、更新已有事件、还是把关闭的拉回来？

    `existing` 是同指纹下最近的一条事件记录（`None` 表示从没见过），
    需要 `status` / `resolved_at` / `flap_count` 三个字段。

    四种结果：

    | 情形 | action | 要不要分析 | 为什么 |
    |---|---|---|---|
    | 从没见过 | create | **是** | 新故障，第一时间给结论 |
    | 已打开 | update | **否** | 同一件事又来一条告警，只累加计数 |
    | 已关闭、还在冷却期内 | reopen | 看是否越过 flapping 阈值 | 抖动 |
    | 已关闭、冷却期已过 | create | **是** | 隔了很久又坏，是新的一次故障 |
    """
    if existing is None:
        return IncidentDecision("create", True, "首次出现，立即分析")

    status = existing.get("status")
    if status == STATUS_OPEN:
        return IncidentDecision(
            "update", False, "事件已打开，同指纹告警只累加计数不重新分析",
            flap_count=int(existing.get("flap_count") or 0))

    resolved_at = existing.get("resolved_at")
    if resolved_at is None:
        # 状态是 resolved 却没有 resolved_at——数据不一致。**当成新故障处理**
        # 而不是猜一个时间：猜错会把真实的新故障并进一个陈旧事件里。
        return IncidentDecision("create", True, "已关闭但缺少关闭时间，按新故障处理")

    elapsed = now - float(resolved_at)
    flap = int(existing.get("flap_count") or 0) + 1
    if elapsed <= cooldown_s:
        if flap >= flap_threshold:
            return IncidentDecision(
                "reopen", True,
                f"{cooldown_s / 60:.0f} 分钟内第 {flap} 次复发，按「反复重启」重新分析一次",
                flap_count=flap)
        return IncidentDecision(
            "reopen", False,
            f"关闭后 {elapsed:.0f} 秒内又出现（第 {flap} 次），复用同一事件、不重新分析",
            flap_count=flap)

    return IncidentDecision("create", True,
                            f"距上次关闭已 {elapsed / 60:.0f} 分钟，按新故障处理")


def should_reanalyze_on_change(previous_targets: Iterable[str],
                               current_targets: Iterable[str]) -> bool:
    """事件"性质变了"要重新分析——判据是**波及了新的服务**。

    只是告警条数变多不算：那是同一件事在持续，结论不会变。波及面扩大才是
    新信息（"只有 order-service 挂"和"order-service 带着 payment-gateway 一起挂"
    是完全不同的两个结论）。
    """
    return bool(set(current_targets or []) - set(previous_targets or []))
