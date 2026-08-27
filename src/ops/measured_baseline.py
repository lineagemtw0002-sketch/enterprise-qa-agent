"""向连接器实测服务当前实例数——扩缩容越界判定**唯一可信的基线来源**。

## 为什么需要这个模块（2026-08-27 用户拍板）

`scale_instances` 的上界是 `baseline × max_multiplier_of_baseline`，而原来的
`baseline` 跟 `target` **来自同一份 AI 提议**：模型谎报一个虚高基线就能把自己的
天花板抬到任意高（baseline=5000 / target=10000 在 multiplier=2.0 下被判合法）。
**一个由被约束方自己填写的约束，不是约束。**

现在基线只认这里测出来的值。跟 `rca.py` 那条「依据引用只从输入推导、绝不采信
模型输出」是同一条原则的延伸——区别只在于那边防的是模型编造依据，这里防的是
模型编造自己的约束条件。

## 测不到时一律拒绝，不回退

连接器离线、不支持上报实例数、查询超时——**任何一种情况都返回 None，
上层据此拒绝这次扩缩容**。回退到 AI 自报的值等于让这个防护在最需要它的时候
（连接器不可达）自动失效。判定不了就不放行，跟白名单"没配置 = 一律不允许"
是同一个默认。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Sequence

from src.ops.types import QueryRequest, TimeRange

logger = logging.getLogger(__name__)

MEASURED_KEY = "measured_baseline_instances"
"""注入 `proposed` 的键名。**只有平台侧的调用方会填这个键**——
`aiops_scope._check_scale_instances` 认它、不认 AI 提议里的 `baseline_instances`。"""

_INSTANCES_METRIC = "instances"


async def measure_instance_count(engine: Any, org_id: str, target: str, *,
                                 connection_ids: Optional[Sequence[str]] = None,
                                 timeout_s: float = 8.0) -> Optional[float]:
    """问连接器：`target` 这个服务现在实际有几个实例？测不到返回 `None`。

    走既有的 `service_health` 查询类别（每个服务回若干个
    `labels.metric` 数据点），**没有为它新增查询类别或帧型**——真实连接器
    只要在已经实现的那个查询里多回一个 `instances` 点即可，不用升级协议。

    ⚠️ **多个连接器同时报同一个服务时取最小值。** 这种情况在现实里就存在
    （同一个服务被两套监控系统看到）。取最小是刻意的保守选择：基线越小、
    算出来的上界越低、越不容易放过一次过量扩容。取最大或取平均都会在
    某个连接器读数偏高时把天花板一起抬上去，那正是这次要修的问题。
    """
    if engine is None:
        return None
    import time as _time

    now = _time.time()
    try:
        request = QueryRequest(
            kind="service_health", target="",
            time_range=TimeRange(start_ts=now - 300.0, end_ts=now),
        )
        result = await engine.query(org_id, request, connection_ids=connection_ids,
                                    timeout_s=timeout_s, use_cache=False)
    except Exception as e:  # noqa: BLE001
        # 测不到就是测不到，上层会拒绝——**不要在这里吞掉异常然后返回一个猜测值**。
        logger.warning("measure_instance_count failed for target=%s: %s", target, e)
        return None

    values = [
        float(p.value)
        for r in result.results
        for p in r.points
        if (p.labels or {}).get("service") == target
        and (p.labels or {}).get("metric") == _INSTANCES_METRIC
        and isinstance(p.value, (int, float)) and not isinstance(p.value, bool)
        and p.value > 0
    ]
    return min(values) if values else None


async def with_measured_baseline(engine: Any, org_id: str, plan: Dict[str, Any], *,
                                 connection_ids: Optional[Sequence[str]] = None,
                                 ) -> Dict[str, Any]:
    """返回一份注入了实测基线的 `plan` 副本，供越界判定使用。

    ⚠️ **返回副本，不原地改**：`plan` 会被原样落库（`remediation_actions.plan`），
    把一个平台侧算出来的字段混进去，之后没人分得清哪些是 AI 提议的、哪些是
    平台补的——而"模型说有几个实例"和"实际有几个实例"不一致本身是有用的信号。

    非 `scale_instances` 的动作原样返回，不做多余的查询。
    """
    if (plan or {}).get("action_type") != "scale_instances":
        return dict(plan or {})
    target = (plan or {}).get("target")
    measured = await measure_instance_count(engine, org_id, target,
                                            connection_ids=connection_ids) if target else None
    merged = dict(plan or {})
    if measured is not None:
        merged[MEASURED_KEY] = measured
    return merged
