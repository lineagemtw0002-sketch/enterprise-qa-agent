"""
Builtin Tools Registration — 将现有的 MCP Server 工具注册到 ToolRegistry。

实现双出口：
- MCP Server 出口：通过 protocol_handler 注册（原有逻辑不变）
- Function Tool 出口：通过 wrap_function_tool 注册到 ToolRegistry（新增）

Usage:
    >>> from src.tool_agent.builtin_tools import register_builtin_tools
    >>> from src.tool_agent.tool_registry import ToolRegistry
    >>> registry = ToolRegistry()
    >>> register_builtin_tools(registry)
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, TYPE_CHECKING

from src.tool_agent.adapters import wrap_function_tool
from src.tool_agent.tool_registry import ToolRegistry

if TYPE_CHECKING:
    from src.ragent_backend.user_store import UserStore
    from src.ragent_backend.workflow_store import WorkflowStore

# 导入现有工具类
from src.mcp_server.tools.query_knowledge_hub import (
    QueryKnowledgeHubTool,
    TOOL_NAME as QUERY_KNOWLEDGE_HUB_NAME,
    TOOL_DESCRIPTION as QUERY_KNOWLEDGE_HUB_DESCRIPTION,
    TOOL_INPUT_SCHEMA as QUERY_KNOWLEDGE_HUB_SCHEMA,
)
from src.mcp_server.tools.list_collections import (
    ListCollectionsTool,
    TOOL_NAME as LIST_COLLECTIONS_NAME,
    TOOL_DESCRIPTION as LIST_COLLECTIONS_DESCRIPTION,
    TOOL_INPUT_SCHEMA as LIST_COLLECTIONS_SCHEMA,
)
from src.mcp_server.tools.get_document_summary import (
    GetDocumentSummaryTool,
    TOOL_NAME as GET_DOCUMENT_SUMMARY_NAME,
    TOOL_DESCRIPTION as GET_DOCUMENT_SUMMARY_DESCRIPTION,
    TOOL_INPUT_SCHEMA as GET_DOCUMENT_SUMMARY_SCHEMA,
)


def _mcp_result_to_text(result: Any) -> str:
    """将 MCP CallToolResult 提取为文本。"""
    if hasattr(result, "content"):
        texts: List[str] = []
        for item in result.content:
            text = getattr(item, "text", None)
            if text:
                texts.append(text)
        return "\n".join(texts).strip()
    return str(result)


def register_builtin_tools(
    registry: ToolRegistry,
    user_store: Optional["UserStore"] = None,
    workflow_store: Optional["WorkflowStore"] = None,
) -> None:
    """注册所有内置工具到 ToolRegistry。

    Args:
        registry: ToolRegistry 实例
        user_store: 共享的 UserStore，供三个工具做 ACL 校验用。传入 app.py 里
            已经创建好的那个实例，避免每个工具各自建一个连接池；不传时每个
            工具会各自懒加载一个（见各工具类的 user_store 属性）。
        workflow_store: 共享的 WorkflowStore，供工作流状态查询/重新提交两个
            工具用（work-flow.md 6.3 节）；不传则这两个工具不注册——工作流功能
            未初始化时，不应该让 LLM 看到一个调用了会报错的工具。
    """
    _register_query_knowledge_hub(registry, user_store)
    _register_list_collections(registry, user_store)
    _register_get_document_summary(registry, user_store)
    if workflow_store is not None:
        _register_check_workflow_status(registry, workflow_store)
        _register_resubmit_workflow(registry, workflow_store)


def _register_query_knowledge_hub(registry: ToolRegistry, user_store: Optional["UserStore"] = None) -> None:
    """注册 query_knowledge_hub 工具。"""
    tool = QueryKnowledgeHubTool(user_store=user_store)

    async def handler(query: str, top_k: int = 5, collection: str = None, user_id: str = None) -> Any:
        return await tool.execute(query=query, top_k=top_k, collection=collection, user_id=user_id)

    unified_tool = wrap_function_tool(
        name=QUERY_KNOWLEDGE_HUB_NAME,
        description=QUERY_KNOWLEDGE_HUB_DESCRIPTION,
        handler=handler,
        input_schema=QUERY_KNOWLEDGE_HUB_SCHEMA,
        result_formatter=lambda r: r.content if hasattr(r, "content") else str(r),
    )
    registry.register(unified_tool)


def _register_list_collections(registry: ToolRegistry, user_store: Optional["UserStore"] = None) -> None:
    """注册 list_collections 工具。"""
    tool = ListCollectionsTool(user_store=user_store)

    async def handler(include_stats: bool = True, user_id: str = None) -> Any:
        return await tool.execute(include_stats=include_stats, user_id=user_id)

    unified_tool = wrap_function_tool(
        name=LIST_COLLECTIONS_NAME,
        description=LIST_COLLECTIONS_DESCRIPTION,
        handler=handler,
        input_schema=LIST_COLLECTIONS_SCHEMA,
        result_formatter=_mcp_result_to_text,
    )
    registry.register(unified_tool)


def _register_get_document_summary(registry: ToolRegistry, user_store: Optional["UserStore"] = None) -> None:
    """注册 get_document_summary 工具。"""
    tool = GetDocumentSummaryTool(user_store=user_store)

    async def handler(doc_id: str, collection: str = None, user_id: str = None) -> Any:
        return await tool.execute(doc_id=doc_id, collection=collection, user_id=user_id)

    unified_tool = wrap_function_tool(
        name=GET_DOCUMENT_SUMMARY_NAME,
        description=GET_DOCUMENT_SUMMARY_DESCRIPTION,
        handler=handler,
        input_schema=GET_DOCUMENT_SUMMARY_SCHEMA,
        result_formatter=_mcp_result_to_text,
    )
    registry.register(unified_tool)


CHECK_WORKFLOW_STATUS_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "workflow_id": {
            "type": "string",
            "description": "要查询的工作流实例 id（编号）；不传则查该用户最近一条",
        },
    },
    "required": [],
}

RESUBMIT_WORKFLOW_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "workflow_id": {
            "type": "string",
            "description": "要重新提交的工作流实例 id；不传则取该用户最近一条「已打回」的申请",
        },
    },
    "required": [],
}


def _register_check_workflow_status(registry: ToolRegistry, workflow_store: "WorkflowStore") -> None:
    """注册 check_workflow_status 工具（work-flow.md 6.3 节）：单轮、参数清晰的
    状态查询，走通用工具调用路径，跟"创建"（多轮收集，专属 workflow 节点）分开。"""

    async def handler(workflow_id: str = None, user_id: str = None) -> Any:
        if not user_id:
            return "缺少身份信息，无法查询。"

        if workflow_id:
            instance = await workflow_store.get_instance(workflow_id)
            if instance is None or instance.requester_user_id != user_id:
                return "没有找到这条工作流记录，或它不属于你。"
        else:
            instances = await workflow_store.list_instances_for_user(user_id)
            if not instances:
                return "你还没有发起过任何工作流申请。"
            instance = instances[0]  # list_instances_for_user 已按 created_at 倒序

        template = await workflow_store.get_template_by_type(instance.workflow_type)
        display_name = template.display_name if template else instance.workflow_type
        lines = [f"「{display_name}」申请（#{instance.instance_id[:8]}）：当前状态 {instance.status}"]
        if instance.status == "returned_for_revision" and instance.approval_comment:
            lines.append(f"打回原因：{instance.approval_comment}")
        elif instance.status == "rejected" and instance.approval_comment:
            lines.append(f"驳回原因：{instance.approval_comment}")
        return "\n".join(lines)

    unified_tool = wrap_function_tool(
        name="check_workflow_status",
        description="查询我发起的工作流申请（报修/请假/出差/报销等）当前处理到哪一步了。"
                    "不传 workflow_id 则查最近一条。",
        handler=handler,
        input_schema=CHECK_WORKFLOW_STATUS_SCHEMA,
    )
    registry.register(unified_tool)


def _register_resubmit_workflow(registry: ToolRegistry, workflow_store: "WorkflowStore") -> None:
    """注册 resubmit_workflow 工具（work-flow.md 6.2 节）：申请被打回、员工在原
    对话里补充材料后，说"材料补好了/重新提交"命中这个工具，把状态从
    returned_for_revision 转回 pending_approval，不重新走多轮字段收集
    （字段本来就没变，只是材料补了）。"""

    async def handler(workflow_id: str = None, user_id: str = None) -> Any:
        if not user_id:
            return "缺少身份信息，无法操作。"

        if workflow_id:
            instance = await workflow_store.get_instance(workflow_id)
        else:
            candidates = await workflow_store.list_instances_for_user(user_id, status="returned_for_revision")
            instance = candidates[0] if candidates else None

        if instance is None or instance.requester_user_id != user_id:
            return "没有找到可以重新提交的申请，或它不属于你。"
        if instance.status != "returned_for_revision":
            return "这条申请当前不是「已打回」状态，不能重新提交。"

        from src.ragent_backend.role_store import RoleStore
        role_store = RoleStore()
        try:
            updated = await workflow_store.transition(
                instance.instance_id, "pending_approval", actor_user_id=user_id, role_store=role_store,
            )
        except ValueError as e:
            return f"重新提交失败：{e}"

        if updated is None:
            return "重新提交失败，请稍后再试。"
        return f"已重新提交（#{updated.instance_id[:8]}），等待审批。"

    unified_tool = wrap_function_tool(
        name="resubmit_workflow",
        description="员工在补充完材料后，重新提交一条被打回（returned_for_revision）的工作流申请。"
                    "不传 workflow_id 则取最近一条被打回的申请。",
        handler=handler,
        input_schema=RESUBMIT_WORKFLOW_SCHEMA,
    )
    registry.register(unified_tool)
