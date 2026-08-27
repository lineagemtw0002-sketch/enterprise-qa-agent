"""几套预置的模拟运维环境 + 故障注入模型。**演示件，不是产品交付件。**

## 跟第一版的关键区别：默认健康，故障要主动注入

第一版的数据是**静态**的——异常永远在那儿，于是只能演示"一个已经坏了的系统
长什么样"，演示不了"我现在把它弄坏 → 大屏亮红 → AI 分析 → 人工审批 →
执行修复 → 恢复绿色"这个**过程**，而后者才是这个模块真正要证明的东西。

现在：不注入故障时**一切正常**（检测器不该报任何异常）；注入之后对应服务的
指标才劣化、才产生告警。健康是默认状态，不是一个特殊分支。

## 仍然坚持：不 import 平台侧的检测阈值

真实客户系统不可能知道平台用 MAD 还是标准差、阈值是 3.5 还是 3.0。照着阈值
造数据等于把"检测器能不能发现问题"变成自证。这里只把正常态和故障态的量级
拉开一个数量级以上，能不能检出交给检测器自己回答——`scripts/` 下的场景脚本
会实际断言它确实检出了。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass(frozen=True)
class ServiceSpec:
    """一个服务的**正常态**基线。故障是叠加在它上面的。"""

    error_rate: float = 0.0005
    p95_latency_ms: Optional[float] = 120.0
    # 异步 worker 用队列延迟而不是 HTTP P95——两者的可接受范围差一个数量级，
    # 平台侧也是分开判的（见 src/ops/service_health.py）。
    queue_latency_ms: Optional[float] = None
    # 只被发现、不上报指标的服务（比如刚接入还在同步）。留一个这样的服务，
    # 是为了让"数据中断"这个状态在演示环境里真的被走到，而不是只存在于单测。
    reports_metrics: bool = True
    # 当前实例数。**扩缩容的越界判定只认这个实测值**，不认 AI 提议里自报的
    # baseline（见 `src/ragent_backend/aiops_scope.py::_check_scale_instances`）。
    # 各服务规模不同是刻意的：都设成一样的话，"上界按各自基线算"这件事在演示里
    # 看不出区别。
    instances: int = 3


@dataclass(frozen=True)
class Environment:
    key: str
    label: str
    cluster: str
    services: Dict[str, ServiceSpec] = field(default_factory=dict)


ENVIRONMENTS: Dict[str, Environment] = {
    "ecommerce": Environment(
        key="ecommerce", label="电商主站", cluster="prod-cluster-1",
        services={
            "order-service": ServiceSpec(error_rate=0.0008, p95_latency_ms=180.0, instances=6),
            "payment-gateway": ServiceSpec(error_rate=0.0004, p95_latency_ms=210.0, instances=4),
            "inventory-api": ServiceSpec(error_rate=0.0005, p95_latency_ms=121.0, instances=3),
            "auth-service": ServiceSpec(error_rate=0.0002, p95_latency_ms=88.0, instances=8),
            "notification-worker": ServiceSpec(error_rate=0.001, p95_latency_ms=None,
                                               queue_latency_ms=3000.0, instances=2),
            "search-index": ServiceSpec(reports_metrics=False),
        },
    ),
    "payments": Environment(
        key="payments", label="支付清结算", cluster="pay-cluster-a",
        services={
            "payment-api": ServiceSpec(error_rate=0.0002, p95_latency_ms=140.0, instances=5),
            "ledger-service": ServiceSpec(error_rate=0.0001, p95_latency_ms=95.0, instances=3),
            "fraud-detector": ServiceSpec(error_rate=0.0009, p95_latency_ms=320.0, instances=4),
            "settlement-worker": ServiceSpec(error_rate=0.0003, p95_latency_ms=None,
                                             queue_latency_ms=6000.0, instances=2),
            "kyc-service": ServiceSpec(error_rate=0.0006, p95_latency_ms=260.0, instances=2),
        },
    ),
    "internal": Environment(
        key="internal", label="内部平台", cluster="corp-cluster",
        services={
            "hr-portal": ServiceSpec(error_rate=0.0011, p95_latency_ms=340.0, instances=2),
            "wiki-service": ServiceSpec(error_rate=0.0007, p95_latency_ms=190.0, instances=2),
            "ci-runner": ServiceSpec(error_rate=0.0021, p95_latency_ms=None,
                                     queue_latency_ms=8000.0, instances=4),
            "artifact-registry": ServiceSpec(error_rate=0.0004, p95_latency_ms=150.0, instances=2),
            "ldap-sync": ServiceSpec(reports_metrics=False),
        },
    ),
}

DEFAULT_ENVIRONMENT = "ecommerce"


@dataclass(frozen=True)
class FaultKind:
    key: str
    label: str
    # 故障态下这三个指标各自变成什么。None 表示"这个故障不影响这个指标"。
    error_rate: Optional[float] = None
    p95_latency_ms: Optional[float] = None
    queue_latency_ms: Optional[float] = None
    # 该服务是否还上报指标（`down` 会让它彻底不上报 → 网格上是「数据中断」）。
    reports_metrics: bool = True
    alert_text: str = "{service} 指标异常"
    # 这种故障**通常**对应哪一类修复动作。
    # ⚠️ 只是演示用的配对提示，**不是自动决策**——真实修法由 AI 分析给建议、
    # 由人审批。写在这里是为了让每一类修复动作都有一个能把它演出来的故障，
    # 否则 `clean_disk`/`rollback_deployment` 这两类永远只能空跑。
    suggested_action: str = "restart_service"
    # 网格上该服务会变成什么状态，用于测试断言（见 tests/unit/test_probe_faults.py）。
    expected_status: str = "critical"


FAULT_KINDS: Dict[str, FaultKind] = {
    "error_spike": FaultKind(
        key="error_spike", label="错误率飙升",
        error_rate=0.081,
        alert_text="{service} 5xx 比例超过阈值（第 {n} 次触发）",
        suggested_action="restart_service",
    ),
    "latency_spike": FaultKind(
        key="latency_spike", label="延迟劣化",
        p95_latency_ms=2400.0, error_rate=0.012,
        alert_text="{service} P95 响应时间超过阈值（第 {n} 次触发）",
        suggested_action="restart_service",
    ),
    "queue_backlog": FaultKind(
        key="queue_backlog", label="队列积压",
        queue_latency_ms=45000.0, error_rate=0.004,
        alert_text="{service} 消费延迟持续增长，队列积压（第 {n} 次触发）",
        suggested_action="scale_instances",
    ),
    "down": FaultKind(
        key="down", label="失联（不再上报）",
        reports_metrics=False,
        alert_text="{service} 健康检查连续失败，实例可能已下线（第 {n} 次触发）",
        suggested_action="restart_service",
        # ⚠️ 不上报指标 → 网格上是「数据中断」而不是「异常」。
        # **这两者不能混**：前者是"我们对它一无所知"，后者是"我们看到它坏了"。
        expected_status="stale",
    ),
    "traffic_surge": FaultKind(
        key="traffic_surge", label="流量激增（容量不足）",
        p95_latency_ms=1800.0, error_rate=0.021,
        alert_text="{service} 请求量突增，实例饱和、响应劣化（第 {n} 次触发）",
        suggested_action="scale_instances",
        # ⚠️ 这一档**故意停在「观察中」而不是「异常」**：容量压力在真实运维里
        # 就是先劣化、后崩溃，指标（2.1% / 1800ms）确实都还没越过 critical 线。
        # 保留一种 warning 级故障，演示才有"还没坏但要处理了"这个中间态可看——
        # 全部故障都是红的，界面上就只剩两种颜色。
        # （这条原来写成 critical，被 test_probe_faults 当场抓出来：
        #   声明的预期状态跟数据算出来的对不上。）
        expected_status="warning",
    ),
    "disk_full": FaultKind(
        key="disk_full", label="磁盘写满",
        error_rate=0.055, p95_latency_ms=900.0,
        alert_text="{service} 所在节点磁盘使用率超过 95%，写入开始失败（第 {n} 次触发）",
        suggested_action="clean_disk",
    ),
    "bad_release": FaultKind(
        key="bad_release", label="坏版本上线",
        error_rate=0.134, p95_latency_ms=760.0,
        alert_text="{service} 新版本发布后错误率陡增（第 {n} 次触发）",
        suggested_action="rollback_deployment",
    ),
}

# 平台支持的四类修复动作，每一类都至少有一种故障能把它演出来。
# 用一条断言钉住这个对应关系——加了新动作类型却没有配套故障时，
# 演示里那一类就只能空跑，而这件事不会有任何其它地方报错。
ACTION_TYPES_WITH_FAULT = {k.suggested_action for k in FAULT_KINDS.values()}
assert ACTION_TYPES_WITH_FAULT == {
    "restart_service", "scale_instances", "clean_disk", "rollback_deployment",
}, f"有修复动作类型没有配套的可触发故障：{ACTION_TYPES_WITH_FAULT}"


def resolve_environment(key: Optional[str]) -> Environment:
    env = ENVIRONMENTS.get(key or DEFAULT_ENVIRONMENT)
    if env is None:
        raise KeyError(f"未知环境 {key!r}，可选：{sorted(ENVIRONMENTS)}")
    return env


def effective_spec(spec: ServiceSpec, fault: Optional[str]) -> ServiceSpec:
    """把故障叠加到正常态基线上。`fault` 为 None 时原样返回。

    ⚠️ **故障只覆盖它真正影响的指标**，其余保持基线。一个只影响错误率的故障
    把延迟也一并改掉，会让"到底是什么坏了"在界面上分辨不出来——而这正是
    这个模块要帮人回答的问题。
    """
    if not fault:
        return spec
    kind = FAULT_KINDS.get(fault)
    if kind is None:
        raise KeyError(f"未知故障类型 {fault!r}，可选：{sorted(FAULT_KINDS)}")
    if not kind.reports_metrics:
        return ServiceSpec(error_rate=spec.error_rate, p95_latency_ms=None,
                           queue_latency_ms=None, reports_metrics=False)
    return ServiceSpec(
        error_rate=kind.error_rate if kind.error_rate is not None else spec.error_rate,
        p95_latency_ms=(kind.p95_latency_ms if kind.p95_latency_ms is not None
                        else spec.p95_latency_ms),
        queue_latency_ms=(kind.queue_latency_ms if kind.queue_latency_ms is not None
                          else spec.queue_latency_ms),
        reports_metrics=spec.reports_metrics,
    )


def list_environments() -> List[Dict[str, object]]:
    return [
        {"key": e.key, "label": e.label, "cluster": e.cluster,
         "services": sorted(e.services)}
        for e in ENVIRONMENTS.values()
    ]
