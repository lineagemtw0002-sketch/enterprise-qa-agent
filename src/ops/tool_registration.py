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
EXECUTE_REMEDIATION_SCHEMA = {
    "type": "object",
    "properties": {
        "action_id": {"type": "string", "description": "要执行的修复动作 id"},
        "action_type": {"type": "string", "description": "动作类型，用于执行前复查允许范围"},
    },
    "required": ["action_id"],
}


def register_ops_tools(registry: "ToolRegistry", toolset: "OpsToolset") -> None:
    """把三个工具注册进 registry。

    ⚠️ **`org_id` 由调用方从服务端会话注入，不能来自 LLM 的工具入参**——
    跟 `query_knowledge_hub` 的 `user_id` 是同一条铁律（见那个工具 `execute()`
    的 docstring）。所以三个 schema 里都**没有** org_id 字段：不给模型这个参数，
    它就没法伪造。运行时由 `tool_subgraph` 注入，跟现有 user_id 走同一条路。
    """
    from src.ops.tools import ToolOutcome

    def _format(outcome: "ToolOutcome") -> str:
        return outcome.message

    async def _query(target: str, metric: str = "error_rate", window_minutes: int = 60,
                     org_id: str = None, **_: Any) -> Any:
        if not org_id:
            return ToolOutcome(ok=False, message="缺少调用方身份，无法查询运维系统。")
        return await toolset.query_ops_system(
            org_id=org_id, target=target, metric=metric, window_minutes=window_minutes)

    async def _propose(connection_id: str, action_type: str, intent: str, plan: dict,
                       impact_radius: str = None, org_id: str = None, user_id: str = None,
                       **_: Any) -> Any:
        if not org_id or not user_id:
            return ToolOutcome(ok=False, message="缺少调用方身份，无法提交修复建议。")
        return await toolset.propose_remediation(
            org_id=org_id, connection_id=connection_id, proposed_by=user_id,
            action_type=action_type, intent=intent, plan=plan, impact_radius=impact_radius)

    async def _execute(action_id: str, action_type: str = None, org_id: str = None, **_: Any) -> Any:
        if not org_id:
            return ToolOutcome(ok=False, message="缺少调用方身份，无法执行修复动作。")
        return await toolset.execute_approved_remediation(
            org_id=org_id, action_id=action_id, action_type=action_type)

    for name, desc, schema, handler, timeout in (
        (QUERY_OPS_SYSTEM_NAME, QUERY_OPS_SYSTEM_DESCRIPTION, QUERY_OPS_SYSTEM_SCHEMA, _query, 30.0),
        (PROPOSE_REMEDIATION_NAME, PROPOSE_REMEDIATION_DESCRIPTION, PROPOSE_REMEDIATION_SCHEMA, _propose, 30.0),
        # 执行超时给得比查询宽：重启/扩容/回滚都不是秒级动作。
        (EXECUTE_REMEDIATION_NAME, EXECUTE_REMEDIATION_DESCRIPTION, EXECUTE_REMEDIATION_SCHEMA, _execute, 90.0),
    ):
        registry.register(wrap_function_tool(
            name=name, description=desc, handler=handler, input_schema=schema,
            timeout_seconds=timeout, result_formatter=_format,
        ))
