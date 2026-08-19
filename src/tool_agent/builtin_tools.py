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
    from src.ragent_backend.attendance_store import AttendanceStore
    from src.ragent_backend.org_store import OrgStore
    from src.ragent_backend.tenant_connector_store import TenantConnectorStore

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
    attendance_store: Optional["AttendanceStore"] = None,
    org_store: Optional["OrgStore"] = None,
    tenant_connector_store: Optional["TenantConnectorStore"] = None,
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
        attendance_store: 共享的 AttendanceStore，供考勤查询工具用；不传则不
            注册（同上，避免暴露一个会报错的工具）。
        org_store: 共享的 OrgStore，供 query_knowledge_hub 判断调用者属于哪家
            企业用（knowledge-base-tenant-federation.md 第 5.1 节）；不传时
            工具会懒加载一个。
        tenant_connector_store: 共享的 TenantConnectorStore，供 query_knowledge_hub
            判断该企业知识库是否委托给自己的微服务；不传时工具会懒加载一个。
    """
    _register_query_knowledge_hub(registry, user_store, org_store, tenant_connector_store)
    _register_list_collections(registry, user_store)
    _register_get_document_summary(registry, user_store)
    if workflow_store is not None:
        _register_check_workflow_status(registry, workflow_store)
        _register_resubmit_workflow(registry, workflow_store)
    if attendance_store is not None:
        _register_query_attendance(registry, attendance_store)


def _register_query_knowledge_hub(
    registry: ToolRegistry,
    user_store: Optional["UserStore"] = None,
    org_store: Optional["OrgStore"] = None,
    tenant_connector_store: Optional["TenantConnectorStore"] = None,
) -> None:
    """注册 query_knowledge_hub 工具。"""
    tool = QueryKnowledgeHubTool(user_store=user_store, org_store=org_store, tenant_connector_store=tenant_connector_store)

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


QUERY_ATTENDANCE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "date": {
            "type": "string",
            "description": "只查某一天时用这个，YYYY-MM-DD 格式（如 2026-07-01）",
        },
        "start_date": {
            "type": "string",
            "description": "查一段时间时的起始日期，YYYY-MM-DD 格式；查单日请用 date 参数",
        },
        "end_date": {
            "type": "string",
            "description": "查一段时间时的结束日期，YYYY-MM-DD 格式；配合 start_date 使用",
        },
    },
    "required": [],
}

_ATTENDANCE_STATUS_LABELS: Dict[str, str] = {
    "normal": "正常",
    "late": "迟到",
    "early_leave": "早退",
    "late_and_early_leave": "迟到+早退",
    "absent": "缺勤",
    "leave": "请假",
}


def _register_query_attendance(registry: ToolRegistry, attendance_store: "AttendanceStore") -> None:
    """注册 query_attendance 工具：查询自己的每日打卡记录（上下班时间/迟到早退/
    请假缺勤），数据来自 attendance_records 表，跟工作流里的请假「申请」是两回事
    ——这里是打卡的实际结果，不是审批流程。"""

    from datetime import date as _date, datetime as _datetime, timedelta as _timedelta

    def _parse_date(value: Optional[str]) -> Optional[_date]:
        if not value:
            return None
        try:
            return _datetime.strptime(value.strip(), "%Y-%m-%d").date()
        except ValueError:
            return None

    async def handler(
        date: str = None, start_date: str = None, end_date: str = None, user_id: str = None, **_ignored: Any
    ) -> Any:
        if not user_id:
            return "缺少身份信息，无法查询。"

        # 小模型偶尔不按 schema 来，会传一个笼统的 "date" 而不是 start_date/
        # end_date（实测 qwen2.5:7b 在本地 Ollama 上有这个行为）；单日查询就
        # 把 date 当成 start_date/end_date 用，容错处理，不因为参数名不完全
        # 匹配就让整次查询失败。
        start_date = start_date or date
        end_date = end_date or date

        parsed_start = _parse_date(start_date)
        parsed_end = _parse_date(end_date)

        if start_date and parsed_start is None:
            return f"日期格式看不懂：{start_date!r}，请用 YYYY-MM-DD 格式（如 2026-07-01）。"
        if end_date and parsed_end is None:
            return f"日期格式看不懂：{end_date!r}，请用 YYYY-MM-DD 格式（如 2026-07-01）。"

        if parsed_start and not parsed_end:
            parsed_end = parsed_start
        elif parsed_end and not parsed_start:
            parsed_start = parsed_end
        elif not parsed_start and not parsed_end:
            parsed_end = _date.today()
            parsed_start = parsed_end - _timedelta(days=30)

        if parsed_start > parsed_end:
            parsed_start, parsed_end = parsed_end, parsed_start

        records = await attendance_store.list_records(user_id, start_date=parsed_start, end_date=parsed_end)
        if not records:
            return f"{parsed_start} 至 {parsed_end} 期间没有查到你的考勤记录（周末不打卡，也可能是入职前）。"

        lines = [f"{parsed_start} 至 {parsed_end} 的考勤记录（共 {len(records)} 天）："]
        for r in records:
            label = _ATTENDANCE_STATUS_LABELS.get(r.status, r.status)
            if r.check_in_at is None:
                lines.append(f"- {r.work_date}：{label}")
                continue
            check_in = _datetime.fromtimestamp(r.check_in_at).strftime("%H:%M")
            check_out = (
                _datetime.fromtimestamp(r.check_out_at).strftime("%H:%M") if r.check_out_at else "未打卡"
            )
            detail = f"- {r.work_date}：{label}，上班 {check_in}，下班 {check_out}"
            if r.late_minutes:
                detail += f"，迟到 {r.late_minutes} 分钟"
            if r.early_leave_minutes:
                detail += f"，早退 {r.early_leave_minutes} 分钟"
            lines.append(detail)
        return "\n".join(lines)

    unified_tool = wrap_function_tool(
        name="query_attendance",
        description="查询我自己某一天或某段时间的考勤打卡记录（上下班时间、是否迟到/早退/请假/缺勤）。"
                    "不传日期则默认查最近 30 天。",
        handler=handler,
        input_schema=QUERY_ATTENDANCE_SCHEMA,
    )
    registry.register(unified_tool)
