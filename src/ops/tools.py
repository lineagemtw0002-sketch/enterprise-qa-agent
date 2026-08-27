"""智能运维模块的工具层（`docs/aiops_module_design.md` §3.6）。

三个工具：`query_ops_system`（只读联邦查询）、`propose_remediation`（生成待审批
的修复提议）、`execute_approved_remediation`（执行已批准的动作）。

**这个文件存在的首要理由是设计文档 §3.6 的这一句**：

> 执行类工具**必须在工具层强制检查审批状态**，不能只依赖上游节点"应该已经检查过"
> 这种隐式假设——这类隐式假设正是 `workflow.py` 那条被证伪的并发注释导致 P0
> 长期未被发现的同一类错误。

所以 `execute_approved_remediation` 在真正下发之前，自己重新做四道检查，
**每一道在下游都另有一道对应的检查**，是刻意的重复而不是冗余代码：

| 工具层这道 | 下游对应的那道 | 为什么还要在这里再做一次 |
|---|---|---|
| 动作存在 | `ops_store.mark_executing` 会抛 | 抛异常给 LLM 看是很差的错误信息，这里给人话 |
| org 归属 | 端点侧的 ACL | 工具可能被 LLM 用任意参数调用，端点的 ACL 管不到工具入参 |
| 状态必须是 `approved` | `mark_executing` 的状态机 + approver 字段校验 | 状态机在"错了才报错"，这里在"错了不发起" |
| 目标仍在白名单内 | 提议时已查过一次 | **提议和执行之间白名单可能被管理员改小了**，这是唯一一道能发现它的检查 |

最后一行是这四道里唯一在下游**完全没有**对应检查的：`aiops_scope` 的越界判定
只在提议阶段跑过一次，而审批可能等 30 分钟（§10.4 默认超时）。这半小时里
管理员完全可能把某个服务从白名单里摘掉——不在执行前复查，那次修复就会打在
一个已经被明令禁止的目标上。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional, Protocol

from src.ops.federation.engine import FederatedQueryEngine, describe_unavailable
from src.ops.measured_baseline import with_measured_baseline
from src.ops.types import (
    QUERY_KIND_ALERT,
    QUERY_KIND_METRIC,
    ExecutionOutcome,
    QueryRequest,
    RemediationDispatcher,
    TimeRange,
)

logger = logging.getLogger(__name__)

STATUS_APPROVED = "approved"
STATUS_PENDING_APPROVAL = "pending_approval"
DEFAULT_EXEC_TIMEOUT_SECONDS = 60.0
"""执行比查询慢得多（重启服务、扩容、回滚都不是秒级），所以不复用查询那个 8s。"""


class RemediationStore(Protocol):
    """工具层需要的存储能力——**只声明用得到的那几个方法**。

    不直接依赖 `ops_store.OpsStore` 这个具体类，是为了让工具层能用假件单测
    （那个类的每个方法都要连 Postgres）。生产环境传真的 `OpsStore` 进来即可，
    它的签名是这个协议的超集。
    """

    async def is_module_enabled(self, org_id: str) -> bool: ...
    async def save_analysis_summary(
        self, org_id: str, connection_id: Optional[str], summary: str,
        evidence_refs: list,
    ) -> Any: ...
    async def get_analysis_summary(self, summary_id: str) -> Optional[Any]: ...
    async def get_action(self, action_id: str) -> Optional[Any]: ...
    async def get_remediation_scope(self, connection_id: str, action_type: str) -> Optional[Any]: ...
    async def create_proposed_action(
        self, org_id: str, connection_id: str, proposed_by: str, intent: str,
        plan: Dict[str, Any], impact_radius: Optional[str] = None,
        rollback_plan: Optional[Dict[str, Any]] = None,
        summary_id: Optional[str] = None,
    ) -> Any: ...
    async def advance_status(self, action_id: str, target_status: str) -> Any: ...
    async def mark_executing(self, action_id: str) -> Any: ...
    async def mark_result(self, action_id: str, target_status: str, result: Dict[str, Any]) -> Any: ...


@dataclass(frozen=True)
class ToolOutcome:
    """工具返回给上层的统一结果。

    `refused` 跟 `ok=False` 分开：**被规则挡下来**（越界、未审批、跨 org）
    和**执行失败**（连接器报错）在审计和产品语义上完全是两回事，
    压成一个布尔值会让"我们拦住了一次危险操作"和"我们搞砸了一次操作"
    在日志里长得一模一样。
    """

    ok: bool
    message: str
    refused: bool = False
    data: Dict[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.data is None:
            object.__setattr__(self, "data", {})


class OpsToolset:
    """三个工具的实现体。依赖全部构造注入，没有全局状态，可以纯假件单测。"""

    def __init__(
        self,
        engine: FederatedQueryEngine,
        store: RemediationStore,
        dispatcher: Optional[RemediationDispatcher] = None,
        exec_timeout_s: float = DEFAULT_EXEC_TIMEOUT_SECONDS,
        llm: Optional[Any] = None,
    ) -> None:
        self._engine = engine
        self._store = store
        self._dispatcher = dispatcher
        self._exec_timeout_s = exec_timeout_s
        # 复用主链路那个生成模型（app.py::_build_llm 建的实例），不新建、不引入
        # 另一套工厂——只有根因分析那一层用得到它，见 analysis/rca.py 顶部。
        self._llm = llm

    async def _module_disabled_outcome(self, org_id: str) -> Optional[ToolOutcome]:
        """企业没开通智能运维模块时，**明确说出来**。

        改这一处之前的行为是：工具照常执行、拿到空结果，LLM 看到空结果会自己编一个
        解释（"未查询到相关数据"），用户以为是真没数据。工具列表本身还没有按 org
        过滤（那要改成"工具列表按调用者动态生成"，是横切核心对话链路的改动，
        见 CLAUDE.md §5 该条已知缺口），但至少不能让它产生误导性回答。

        查询失败时**不拦**：宁可放行也不要因为一次数据库抖动就把可用的功能说成
        "未开通"——那会让管理员去找平台管理员开一个本来就是开着的开关。
        """
        try:
            if await self._store.is_module_enabled(org_id):
                return None
        except Exception as e:  # noqa: BLE001
            logger.warning("读取智能运维模块开关失败，按已开通放行: %s", e, exc_info=True)
            return None
        return ToolOutcome(
            ok=False, refused=True,
            message="本企业尚未开通智能运维模块，无法使用运维相关能力。请联系平台管理员开通。",
        )

    # ------------------------------------------------------------------ 只读
    async def query_ops_system(
        self, org_id: str, target: str, metric: str = "error_rate",
        window_minutes: int = 60, now_ts: Optional[float] = None,
        kind: str = QUERY_KIND_METRIC, connection_ids: Optional[list] = None,
    ) -> ToolOutcome:
        """查客户自己的运维系统。只读，不改变任何状态。"""
        import time as _time

        disabled = await self._module_disabled_outcome(org_id)
        if disabled is not None:
            return disabled

        end = now_ts if now_ts is not None else _time.time()
        request = QueryRequest(
            kind=kind, target=target, metric=metric if kind == QUERY_KIND_METRIC else None,
            time_range=TimeRange(end - window_minutes * 60, end),
        )
        result = await self._engine.query(org_id, request, connection_ids=connection_ids)

        lines = [
            f"「{r.system_name}」返回 {len(r.points)} 条数据"
            + ("（已截断，还有更多）" if r.truncated else "")
            for r in result.results
        ]
        # 部分失败必须显式呈现给用户（§3.5 第 4 条）——不能只放进 structured_data
        # 指望前端记得读，LLM 看到的文本里也要有，否则它会当作"全部数据"来推理。
        lines.extend(describe_unavailable(result))
        if result.all_failed:
            lines.insert(0, "⚠️ 所有已接入的运维系统这次都没能返回数据，下面只有失败原因：")
        elif not result.results:
            lines.append("这个时间范围内没有查到数据。")

        return ToolOutcome(
            ok=not result.all_failed,
            message="\n".join(lines) if lines else "没有已接入的运维系统。",
            data={
                "point_count": result.point_count(),
                "systems": [r.system_name for r in result.results],
                "unavailable": [
                    {"system": e.system_name, "reason": e.reason, "detail": e.detail}
                    for e in result.errors
                ],
                "partial": result.is_partial,
                "from_cache": result.from_cache,
            },
        )

    # ------------------------------------------------------------------ 提议
    async def propose_remediation(
        self, org_id: str, connection_id: str, proposed_by: str, action_type: str,
        intent: str, plan: Dict[str, Any], impact_radius: Optional[str] = None,
        rollback_plan: Optional[Dict[str, Any]] = None,
        summary_id: Optional[str] = None,
    ) -> ToolOutcome:
        """生成一条待审批的修复提议。

        §3.3.1：越界的目标**在进入 pending_approval 之前**就被挡掉，
        不允许流到审批人那一步再靠人肉发现。

        `summary_id` 可选——如果这次提议是紧接着一次 `analyze_ops_incident`
        分析提出的，LLM 可以把那次分析返回的 `summary_id` 传进来，给
        §9.2"事后复盘视图"留一条"这次修复是因为哪次分析而做"的链路。传了一个
        不属于这个 org 的 summary_id 时**静默不链接**（不报错、不拒绝这次
        提议）——链接只是复盘视图的辅助信息，不是这次提议本身的正确性前提，
        没必要因为一个无关紧要的字段填错就拒掉整条修复建议。"""
        if summary_id is not None:
            summary = await self._store.get_analysis_summary(summary_id)
            if summary is None or summary.org_id != org_id:
                logger.info(
                    "propose_remediation 的 summary_id 无效或跨 org，静默丢弃: "
                    "summary_id=%s caller_org=%s", summary_id, org_id,
                )
                summary_id = None
        from src.ragent_backend.aiops_scope import (
            InvalidActionType,
            InvalidScopeConfig,
            check_target_in_scope,
        )

        try:
            scope = await self._store.get_remediation_scope(connection_id, action_type)
        except Exception as e:  # noqa: BLE001
            logger.warning("读取修复范围白名单失败: %s", e, exc_info=True)
            return ToolOutcome(ok=False, message=f"无法读取修复范围配置: {e}")

        if scope is None:
            # 没登记白名单 = 一律不允许。**不是"没配置就放行"**——
            # 这个默认值反过来会让"忘了配"变成"全放开"。
            return ToolOutcome(
                ok=False, refused=True,
                message=(
                    f"企业管理员还没有为这个运维系统登记「{action_type}」类动作的允许范围，"
                    "在登记之前这类修复一律不能提议。"
                ),
            )

        # 同 app.py 那个提议端点：扩缩容基线只认实测值。**两条提议路径必须一致**
        # ——只在其中一条上防，模型换一条路就能绕过去。
        checked_plan = await with_measured_baseline(self._engine, org_id, plan)
        try:
            check = check_target_in_scope(action_type, scope.scope_config, checked_plan)
        except InvalidActionType as e:
            return ToolOutcome(ok=False, refused=True, message=str(e))
        except InvalidScopeConfig as e:
            # 管理员配错了 ≠ AI 提议越界，这两种失败要能被区分（见 aiops_scope 说明）
            return ToolOutcome(ok=False, message=f"修复范围配置本身有问题，请管理员检查: {e}")

        if not check.allowed:
            logger.info(
                "Refused out-of-scope remediation: org=%s conn=%s type=%s reason=%s",
                org_id, connection_id, action_type, check.reason,
            )
            return ToolOutcome(
                ok=False, refused=True,
                message=f"这个修复目标超出了管理员登记的允许范围，已拒绝：{check.reason}",
                data={"scope_reason": check.reason},
            )

        action = await self._store.create_proposed_action(
            org_id=org_id, connection_id=connection_id, proposed_by=proposed_by,
            intent=intent, plan=plan, impact_radius=impact_radius, rollback_plan=rollback_plan,
            summary_id=summary_id,
        )
        await self._store.advance_status(action.action_id, STATUS_PENDING_APPROVAL)
        return ToolOutcome(
            ok=True,
            message=(
                f"已生成待审批的修复建议（{action.action_id}）：{intent}。"
                "**尚未执行**，需要有审批权限的人在运维塔台确认后才会下发。"
            ),
            data={"action_id": action.action_id, "status": STATUS_PENDING_APPROVAL},
        )

    # ------------------------------------------------------------------ 执行
    async def execute_approved_remediation(
        self, org_id: str, action_id: str, action_type: Optional[str] = None,
    ) -> ToolOutcome:
        """下发一条**已批准**的修复动作。四道检查见模块顶部的表。"""
        if self._dispatcher is None:
            return ToolOutcome(ok=False, message="执行通道未配置，无法下发修复动作。")

        action = await self._store.get_action(action_id)
        if action is None:
            return ToolOutcome(ok=False, refused=True, message=f"找不到修复动作 {action_id}。")

        # ① 跨 org：工具入参是 LLM 给的，端点侧的 ACL 管不到这里。
        if action.org_id != org_id:
            logger.warning(
                "Blocked cross-org remediation execution: action=%s belongs to %s, caller org=%s",
                action_id, action.org_id, org_id,
            )
            return ToolOutcome(ok=False, refused=True, message=f"找不到修复动作 {action_id}。")

        # ② 状态必须是 approved。放在这里而不是只靠 mark_executing 的状态机——
        # 状态机是"错了才报错"，这里是"错了根本不发起"。
        if action.status != STATUS_APPROVED:
            return ToolOutcome(
                ok=False, refused=True,
                message=(
                    f"这条修复动作当前状态是「{action.status}」，只有「{STATUS_APPROVED}」"
                    "才允许执行。人工审批是硬性前置条件，不能绕过。"
                ),
                data={"status": action.status},
            )

        # ③ 白名单复查——**四道里唯一在下游完全没有对应检查的一道**。
        # 提议到批准之间可能隔了 30 分钟（§10.4 默认超时），这期间管理员完全
        # 可能把目标从白名单里摘掉。不在这里复查，就会打在一个已被禁止的目标上。
        # 调用方（LLM）没传类型时从落库的 plan 里推导——原来是直接跳过白名单
        # 复查，也就是说**模型只要漏传一个参数，最关键的那道检查就没了**。
        # 推导不出来（历史数据 plan 里没有）才回到"跳过"，那时确实无从检查。
        recheck = await self._recheck_scope(action, action_type or (action.plan or {}).get("action_type"))
        if recheck is not None:
            return recheck

        marked = await self._store.mark_executing(action_id)
        try:
            outcome: ExecutionOutcome = await self._dispatcher.execute(
                connection_id=action.connection_id, org_id=org_id, action_id=action_id,
                plan=action.plan, timeout_s=self._exec_timeout_s,
            )
        except Exception as e:  # noqa: BLE001
            # 下发炸了也必须把状态落到 failed——留在 executing 会让这条记录
            # 永远卡在中间态，既不会超时也不会被重试，只能人工去数据库里改。
            logger.warning("Remediation dispatch failed: action=%s err=%s", action_id, e, exc_info=True)
            await self._store.mark_result(action_id, "failed", {"error": str(e)})
            return ToolOutcome(ok=False, message=f"下发修复动作时失败：{e}", data={"action_id": action_id})

        target = "completed" if outcome.succeeded else "failed"
        await self._store.mark_result(action_id, target, {"detail": outcome.detail, **outcome.raw})
        return ToolOutcome(
            ok=outcome.succeeded,
            message=(f"修复动作已执行：{outcome.detail}" if outcome.succeeded
                     else f"修复动作执行失败：{outcome.detail}"),
            data={"action_id": action_id, "status": target, "approved_by": marked.approver_user_id},
        )

    async def _recheck_scope(self, action: Any, action_type: Optional[str]) -> Optional[ToolOutcome]:
        """执行前复查白名单。`action_type` 传 None 时跳过——调用方拿不到类型时
        不该假装检查过了，那比不检查更危险（会给人"已经查过"的错觉）。"""
        if not action_type:
            return None
        from src.ragent_backend.aiops_scope import check_target_in_scope

        scope = await self._store.get_remediation_scope(action.connection_id, action_type)
        if scope is None:
            return ToolOutcome(
                ok=False, refused=True,
                message="这类动作的允许范围已经被管理员移除，本次执行已拒绝。",
            )
        # 执行前复查同样要重新实测——**不能复用提议时那次的值**：提议到执行
        # 之间可能隔了 30 分钟（§10.4 默认超时），实例数完全可能已经变了，
        # 而这道复查的全部意义就是"批准之后到现在，情况有没有变"。
        checked_plan = await with_measured_baseline(self._engine, action.org_id, action.plan)
        check = check_target_in_scope(action_type, scope.scope_config, checked_plan)
        if not check.allowed:
            logger.warning(
                "Refused approved-but-now-out-of-scope execution: action=%s reason=%s",
                action.action_id, check.reason,
            )
            return ToolOutcome(
                ok=False, refused=True,
                message=(
                    "这条动作虽然已获批准，但目标现在已经超出管理员登记的允许范围"
                    f"（审批之后白名单被改过），已拒绝执行：{check.reason}"
                ),
                data={"scope_reason": check.reason},
            )
        return None

    # ------------------------------------------------------------------ 分析
    async def analyze_ops_incident(
        self, org_id: str, target: str, metric: str = "error_rate",
        window_minutes: int = 60, now_ts: Optional[float] = None,
        connection_ids: Optional[list] = None, persist: bool = True,
    ) -> ToolOutcome:
        """异常检测 + 告警关联 + 根因分析辅助，一次跑完（设计 §2 的三项 V1 能力）。

        编排顺序是刻意的：**先算出事实，再让模型解释事实**。
        指标和告警都来自联邦查询（数据始终留在客户自己的系统里），
        统计层把"哪里不对"算出来，模型只负责把这些事实串成可能的因果链。

        ⚠️ 落库的只有"结论摘要 + 依据引用"，**没有原始运维数据**——
        §3.1 的 BYOC 原则：平台不保存客户运维数据的副本。
        """
        import time as _time

        disabled = await self._module_disabled_outcome(org_id)
        if disabled is not None:
            return disabled

        from src.ops.analysis import Alert, analyze_root_cause, correlate_alerts, detect_anomalies

        end = now_ts if now_ts is not None else _time.time()
        window = TimeRange(end - window_minutes * 60, end)

        metric_result = await self._engine.query(
            org_id,
            QueryRequest(kind=QUERY_KIND_METRIC, target=target, metric=metric, time_range=window),
            connection_ids=connection_ids,
        )
        alert_result = await self._engine.query(
            org_id,
            QueryRequest(kind=QUERY_KIND_ALERT, target=target, time_range=window),
            connection_ids=connection_ids,
        )

        reports = [
            detect_anomalies(r.points, target=f"{target}@{r.system_name}", metric=metric)
            for r in metric_result.results
        ]

        alerts = [
            Alert(
                alert_id=f"{r.connection_id}:{idx}", ts=p.ts,
                target=p.labels.get("target") or target,
                labels=dict(p.labels), text=p.text or "",
                severity=p.labels.get("severity", "warning"),
            )
            for r in alert_result.results
            for idx, p in enumerate(r.points)
        ]
        correlation = correlate_alerts(alerts)
        incident = correlation.incidents[0] if correlation.incidents else None

        rca = await analyze_root_cause(incident=incident, anomaly_reports=reports, llm=self._llm)

        # 部分失败要显式带上——分析结论建立在残缺数据上时，用户必须知道
        # （§3.5 第 4 条），否则"没发现异常"会被当成"一切正常"。
        unavailable = [
            *describe_unavailable(metric_result),
            *describe_unavailable(alert_result),
        ]
        lines = [rca.to_text()]
        if unavailable:
            lines.append("⚠️ 以下数据源本次不可用，结论建立在残缺数据上：")
            lines += [f"  - {u}" for u in unavailable]

        summary_id = None
        if persist and not rca.degraded:
            # 降级结果（模型没参与）不落库：`ops_analysis_summaries` 是给审批人看
            # 数据血缘用的，存一条"其实只是数据复述"的记录会稀释它的意义。
            #
            # ⚠️ 告警关联的统计量（alert_count/incident_count/noise_reduction）
            # 原来只在这次调用的 ToolOutcome.data 里返回给调用方看一眼，从不落库
            # ——`docs/aiops_module_design.md` §10.5 定义的"告警合并率"验收指标
            # 因此从来没有持久化数据可用来算。这里跟 RCA 依据引用一起存进
            # `evidence_refs`（多一条 `source="alert_correlation_stats"` 的条目），
            # Store 层仍然不解析 evidence_refs 内部结构，只有度量计算这一层会按
            # `source` 字段过滤读取，属于建立在"不透明 JSON"契约之上的专用读法，
            # 不算破坏那条约定。
            correlation_stats_ref = {
                "source": "alert_correlation_stats",
                "description": f"{correlation.original_count} 条告警合并为 {len(correlation.incidents)} 个事件",
                "detail": {
                    "alert_count": correlation.original_count,
                    "incident_count": len(correlation.incidents),
                    "noise_reduction": round(correlation.noise_reduction, 4),
                },
            }
            try:
                saved = await self._store.save_analysis_summary(
                    org_id=org_id,
                    connection_id=(metric_result.results[0].connection_id if metric_result.results else None),
                    summary=rca.summary,
                    evidence_refs=[*(e.to_dict() for e in rca.evidence), correlation_stats_ref],
                )
                summary_id = getattr(saved, "summary_id", None)
            except Exception as e:  # noqa: BLE001
                logger.warning("保存分析摘要失败（不影响本次分析结果）: %s", e, exc_info=True)

        return ToolOutcome(
            ok=True,
            message="\n".join(lines),
            data={
                "summary_id": summary_id,
                "degraded": rca.degraded,
                "anomaly_targets": [r.target for r in reports if r.has_anomaly],
                "unevaluated_targets": [r.target for r in reports if not r.evaluated],
                "alert_count": correlation.original_count,
                "incident_count": len(correlation.incidents),
                "noise_reduction": round(correlation.noise_reduction, 4),
                "unavailable": [
                    {"system": e.system_name, "reason": e.reason}
                    for e in [*metric_result.errors, *alert_result.errors]
                ],
            },
        )
