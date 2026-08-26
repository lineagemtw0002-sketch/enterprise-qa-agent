"""把智能运维的三个工具接进既有的 `ToolRegistry`（`docs/aiops_module_design.md` §3.6）。

**为什么单独一个文件、而不是直接写进 `src/tool_agent/builtin_tools.py`**：
那个文件是多个会话都会碰的共享文件，把 schema、handler、说明文案全塞进去
必然产生合并冲突。这里放全部实现，那边只留一个"传了 toolset 才注册"的钩子——
跟 `workflow_store` / `attendance_store` 那两个工具用的是同一个既有约定：
**能力没初始化时，不要让 LLM 看到一个调用了会报错的工具。**

工具描述的措辞是刻意的，不是随手写的——LLM 是靠它决定调不调、怎么调：

- `propose_remediation` 的描述里明写"**只生成建议，不会执行**"，
  防止模型以为调完这个工具事情就办完了、然后对用户说"已经帮你重启了"。
- `execute_approved_remediation` 的描述里明写"**只能用于已经过人工批准的动作**，
  你不能自己批准"，堵住"模型自己先调 propose 拿到 action_id、紧接着调 execute"
  这条路。工具层还有硬检查兜底（见 tools.py 顶部那张表），但描述先把意图说清楚，
  能少掉很多本来就不该发生的调用。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from src.tool_agent.adapters import wrap_function_tool

if TYPE_CHECKING:
    from src.ops.tools import OpsToolset
    from src.ragent_backend.org_store import OrgStore
    from src.tool_agent.tool_registry import ToolRegistry

QUERY_OPS_SYSTEM_NAME = "query_ops_system"
QUERY_OPS_SYSTEM_DESCRIPTION = (
    "查询本企业已接入的运维系统（监控/日志/告警）的实时数据，用于排查线上问题。"
    "数据实时来自企业自己的系统，平台不保存副本。"
    "如果某个系统当前不可用，返回结果里会明确列出——"
    "**看到这类提示时必须在回答里如实告诉用户哪部分数据缺失，不要当作完整数据来下结论。**"
)
QUERY_OPS_SYSTEM_SCHEMA = {
    "type": "object",
    "properties": {
        "target": {"type": "string", "description": "要查的服务或实例名，例如 order-service"},
        "metric": {"type": "string", "description": "指标名，例如 error_rate / latency_p99 / cpu_usage",
                   "default": "error_rate"},
        "window_minutes": {"type": "integer", "description": "往前查多少分钟，默认 60", "default": 60},
    },
    "required": ["target"],
}

PROPOSE_REMEDIATION_NAME = "propose_remediation"
PROPOSE_REMEDIATION_DESCRIPTION = (
    "为线上问题生成一条修复建议，提交给人工审批。"
    "**这个工具只生成建议，绝对不会执行任何操作**——调用成功只代表建议已进入待审批队列，"
    "不代表问题已解决。回答用户时必须说明'需要有权限的同事批准后才会执行'。"
    "仅支持四类动作：restart_service（重启服务）、scale_instances（扩缩容）、"
    "clean_disk（清理磁盘）、rollback_deployment（回滚版本）。"
    "目标必须落在企业管理员预先登记的允许范围内，超出范围会被直接拒绝。"
)
PROPOSE_REMEDIATION_SCHEMA = {
    "type": "object",
    "properties": {
        "connection_id": {"type": "string", "description": "要在哪个已接入的运维系统上执行"},
        "action_type": {"type": "string",
                        "enum": ["restart_service", "scale_instances", "clean_disk", "rollback_deployment"]},
        "intent": {"type": "string", "description": "一句话说明为什么要做这个修复"},
        "plan": {"type": "object", "description": "动作参数，例如 {\"target\": \"order-service\"}"},
        "impact_radius": {"type": "string", "description": "预计影响范围，供审批人判断"},
        "summary_id": {
            "type": "string",
            "description": (
                "可选。如果这条提议是紧接着一次 analyze_ops_incident 分析做出的，"
                "把那次分析返回的 summary_id 填在这里，让审批人和事后复盘能看到"
                "『这次修复是因为哪次分析而做的』。不确定就留空，不影响这次提议本身。"
            ),
        },
    },
    "required": ["connection_id", "action_type", "intent", "plan"],
}

EXECUTE_REMEDIATION_NAME = "execute_approved_remediation"
EXECUTE_REMEDIATION_DESCRIPTION = (
    "下发一条**已经过人工批准**的修复动作。"
    "⚠️ 你不能自己批准动作，也不能在提议之后立刻调用这个工具——"
    "必须由有审批权限的人在运维塔台点了批准之后才可以。"
    "如果动作还没被批准，这个工具会拒绝执行并告诉你当前状态。"
)
ANALYZE_OPS_INCIDENT_NAME = "analyze_ops_incident"
ANALYZE_OPS_INCIDENT_DESCRIPTION = (
    "对某个服务做一次运维分析：查它的指标和告警，检测基线偏离，把重复告警合并成事件，"
    "并给出可能的根因方向和排查建议。"
    "⚠️ 产出是**排查线索，不是结论**——转述给用户时必须保留这个性质，不要说成"
    "「已确认原因是 X」。"
    "如果结果里带了「本次分析未经模型推理」或「以下数据源本次不可用」，"
    "**必须原样告诉用户**：结论可能建立在残缺数据上，隐瞒这一点比不分析更糟。"
)
ANALYZE_OPS_INCIDENT_SCHEMA = {
    "type": "object",
    "properties": {
        "target": {"type": "string", "description": "要分析的服务或实例名，例如 order-service"},
        "metric": {"type": "string", "description": "主要观察的指标，默认 error_rate", "default": "error_rate"},
        "window_minutes": {"type": "integer", "description": "往前看多少分钟，默认 60", "default": 60},
    },
    "required": ["target"],
}

EXECUTE_REMEDIATION_SCHEMA = {
    "type": "object",
    "properties": {
        "action_id": {"type": "string", "description": "要执行的修复动作 id"},
        "action_type": {"type": "string", "description": "动作类型，用于执行前复查允许范围"},
    },
    "required": ["action_id"],
}


def register_ops_tools(
    registry: "ToolRegistry", toolset: "OpsToolset", org_store: "OrgStore" = None,
) -> None:
    """把三个工具注册进 registry。

    ⚠️ **`org_id` 由调用方从服务端会话注入，不能来自 LLM 的工具入参**——
    跟 `query_knowledge_hub` 的 `user_id` 是同一条铁律（见那个工具 `execute()`
    的 docstring）。所以三个 schema 里都**没有** org_id 字段：不给模型这个参数，
    它就没法伪造。

    ⚠️ **2026-08-26 修复的一处真实 bug，如实记录**：本函数的 docstring 原来
    写着"运行时由 `tool_subgraph` 注入，跟现有 user_id 走同一条路"，但
    `src/tool_agent/subgraph.py::tool_node` 的注入逻辑**只覆盖 `user_id`，
    从未注入过 `org_id`**——三个 handler 原来直接接收一个不存在的 `org_id`
    形参，运行时恒为 `None`，任何真实对话调用这三个工具都会立刻撞上
    "缺少调用方身份"，是一个从未被跑通过的完全阻塞的 bug（只在直接单测
    handler、手工传 org_id 的场景下测不出来）。
    修法：改成跟 `query_attendance` 相同的模式——只信任 `tool_node` 真正
    注入的 `user_id`，`org_id` 由 handler 内部用
    `org_store.get_org_for_user(user_id)` 反查得到，不再假设一个不存在的
    注入通道。`org_store` 传 `None`（未初始化）时三个工具直接回"缺少调用方
    身份"，不会尝试用 `None` 去查。
    """
    from src.ops.tools import ToolOutcome

    def _format(outcome: "ToolOutcome") -> str:
        return outcome.message

    async def _resolve_org_id(user_id: Optional[str]) -> Optional[str]:
        if not user_id or org_store is None:
            return None
        org = await org_store.get_org_for_user(user_id)
        return org.org_id if org is not None else None

    async def _query(target: str, metric: str = "error_rate", window_minutes: int = 60,
                     user_id: str = None, **_: Any) -> Any:
        org_id = await _resolve_org_id(user_id)
        if not org_id:
            return ToolOutcome(ok=False, message="缺少调用方身份，无法查询运维系统。")
        return await toolset.query_ops_system(
            org_id=org_id, target=target, metric=metric, window_minutes=window_minutes)

    async def _propose(connection_id: str, action_type: str, intent: str, plan: dict,
                       impact_radius: str = None, summary_id: str = None, user_id: str = None,
                       **_: Any) -> Any:
        org_id = await _resolve_org_id(user_id)
        if not org_id or not user_id:
            return ToolOutcome(ok=False, message="缺少调用方身份，无法提交修复建议。")
        return await toolset.propose_remediation(
            org_id=org_id, connection_id=connection_id, proposed_by=user_id,
            action_type=action_type, intent=intent, plan=plan, impact_radius=impact_radius,
            summary_id=summary_id)

    async def _analyze(target: str, metric: str = "error_rate", window_minutes: int = 60,
                       user_id: str = None, **_: Any) -> Any:
        org_id = await _resolve_org_id(user_id)
        if not org_id:
            return ToolOutcome(ok=False, message="缺少调用方身份，无法执行运维分析。")
        return await toolset.analyze_ops_incident(
            org_id=org_id, target=target, metric=metric, window_minutes=window_minutes)

    async def _execute(action_id: str, action_type: str = None, user_id: str = None, **_: Any) -> Any:
        org_id = await _resolve_org_id(user_id)
        if not org_id:
            return ToolOutcome(ok=False, message="缺少调用方身份，无法执行修复动作。")
        return await toolset.execute_approved_remediation(
            org_id=org_id, action_id=action_id, action_type=action_type)

    for name, desc, schema, handler, timeout in (
        (QUERY_OPS_SYSTEM_NAME, QUERY_OPS_SYSTEM_DESCRIPTION, QUERY_OPS_SYSTEM_SCHEMA, _query, 30.0),
        (PROPOSE_REMEDIATION_NAME, PROPOSE_REMEDIATION_DESCRIPTION, PROPOSE_REMEDIATION_SCHEMA, _propose, 30.0),
        # 分析要跑两次联邦查询 + 一次模型推理，比单次查询慢得多，超时给宽一点。
        (ANALYZE_OPS_INCIDENT_NAME, ANALYZE_OPS_INCIDENT_DESCRIPTION, ANALYZE_OPS_INCIDENT_SCHEMA, _analyze, 90.0),
        # 执行超时给得比查询宽：重启/扩容/回滚都不是秒级动作。
        (EXECUTE_REMEDIATION_NAME, EXECUTE_REMEDIATION_DESCRIPTION, EXECUTE_REMEDIATION_SCHEMA, _execute, 90.0),
    ):
        registry.register(wrap_function_tool(
            name=name, description=desc, handler=handler, input_schema=schema,
            timeout_seconds=timeout, result_formatter=_format,
        ))
