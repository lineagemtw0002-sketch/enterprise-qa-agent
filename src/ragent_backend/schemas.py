from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional, TypedDict, Annotated
from pydantic import BaseModel, Field
from langchain_core.messages import AnyMessage, HumanMessage, AIMessage, RemoveMessage
from langgraph.graph import add_messages
import uuid


# ============== API Models ==============

class ChatRequest(BaseModel):
    query: str = Field(..., min_length=1, description="User query")
    conversation_id: Optional[str] = Field(default=None)
    task_id: Optional[str] = Field(default=None)
    collection: Optional[str] = Field(default=None)
    top_k: int = Field(default=5, ge=1, le=20)
    workflow_type: Optional[str] = Field(
        default=None,
        description="前端'发起工作流'入口显式带出的类型（work-flow.md 5.1 节）；"
                    "跳过后端意图分类，直接确定这是一次工作流发起",
    )


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)


class LoginResponse(BaseModel):
    access_token: str
    user_id: str
    username: str


class RoleSummary(BaseModel):
    """用户当前拥有的角色摘要，用于 /auth/me 展示——不含关联的知识库列表，
    那是角色管理界面才需要的东西。"""
    role_id: str
    name: str
    display_name: str


class MeResponse(BaseModel):
    user_id: str
    username: str
    roles: List[RoleSummary]          # 原来是 role: str；一个用户可以有多个角色
    allowed_collections: List[str]    # 保留：后端算好的角色并集，前端不用二次拼接
    created_at: float


class ChangePasswordRequest(BaseModel):
    old_password: str = Field(..., min_length=1)
    new_password: str = Field(..., min_length=6)


# ============== 管理后台 API（仅 super_admin） ==============

class AdminUserResponse(BaseModel):
    user_id: str
    username: str
    roles: List[RoleSummary]
    allowed_collections: List[str]
    created_at: float


class AdminCreateUserRequest(BaseModel):
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=6)
    role_ids: List[str] = Field(default_factory=list, description="初始分配的角色 id 列表")


# ============== 角色管理 API（仅 super_admin） ==============

class RoleResponse(BaseModel):
    role_id: str
    name: str
    display_name: str
    is_system: bool
    collection_names: List[str]
    created_at: float


class CreateRoleRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    display_name: str = Field(..., min_length=1, max_length=128)


class UpdateRoleRequest(BaseModel):
    display_name: str = Field(..., min_length=1, max_length=128)


class SetRoleCollectionsRequest(BaseModel):
    collection_names: List[str] = Field(default_factory=list)


class SetUserRolesRequest(BaseModel):
    role_ids: List[str] = Field(default_factory=list)


class ActiveWorkflowSummary(BaseModel):
    """当前对话是否还处于工作流多轮收集中，供前端渲染"填写中"状态胶囊用
    （work-flow-web.md 5.2 节）；由 RAGState.active_workflow 直接投影而来，
    前端不用自己在多轮之间累加进度。"""
    workflow_type: str
    display_name: str
    missing_count: int
    total_count: int


class ChatResponse(BaseModel):
    conversation_id: str
    task_id: str
    answer: str
    model_id: str
    active_workflow: Optional[ActiveWorkflowSummary] = None


class RollbackRequest(BaseModel):
    target_message_id: str = Field(..., description="回溯边界消息 ID，删除该消息所在 turn 及之后的所有记录")


class IntentResult(BaseModel):
    """意图识别结果 — 四分支：clarify / rag / tool / workflow"""
    intent_type: Literal["clarify", "rag", "tool", "workflow"] = "rag"
    confidence: float
    rewritten_query: str
    target_tool: Optional[str] = None      # tool 意图时指定目标工具
    tool_args: Optional[Dict[str, Any]] = None  # tool 意图时预解析参数
    need_clarify: bool = False
    clarify_prompt: Optional[str] = None
    reasoning: Optional[str] = None        # LLM 分类理由（可观测）
    workflow_type: Optional[str] = None    # workflow 意图时，匹配到的模板 workflow_type


# ============== 工作流 API（work-flow.md） ==============

class WorkflowFieldSpec(BaseModel):
    key: str
    label: str
    type: Literal["text", "date", "enum", "number"]
    required: bool = True
    options: Optional[List[str]] = None
    description: Optional[str] = None


class WorkflowTemplateResponse(BaseModel):
    """公开的轻量视图（`GET /api/v1/workflow-templates`）和管理视图
    （`GET /api/v1/admin/workflow-templates`）复用同一个响应模型——管理视图只是
    多暴露 `approver_role_id`，不需要为此拆两个模型。"""
    template_id: str
    workflow_type: str
    display_name: str
    description: str
    required_fields: List[WorkflowFieldSpec]
    attachments_note: str
    approver_role_id: Optional[str] = None
    is_system: bool
    created_at: float


class CreateWorkflowTemplateRequest(BaseModel):
    workflow_type: str = Field(..., min_length=1, max_length=64)
    display_name: str = Field(..., min_length=1, max_length=128)
    description: str = ""
    required_fields: List[WorkflowFieldSpec] = Field(default_factory=list)
    attachments_note: str = ""


class UpdateWorkflowTemplateRequest(BaseModel):
    display_name: Optional[str] = None
    description: Optional[str] = None
    required_fields: Optional[List[WorkflowFieldSpec]] = None
    attachments_note: Optional[str] = None
    approver_role_id: Optional[str] = None


class WorkflowInstanceResponse(BaseModel):
    instance_id: str
    workflow_type: str
    display_name: str
    requester_user_id: str
    requester_username: Optional[str] = None
    conversation_id: Optional[str] = None
    fields: Dict[str, Any]
    status: str
    approver_user_id: Optional[str] = None
    approval_comment: Optional[str] = None
    history: List[Dict[str, Any]]
    created_at: float
    updated_at: float


class WorkflowActionRequest(BaseModel):
    comment: Optional[str] = None


class WorkflowReturnRequest(BaseModel):
    comment: str = Field(..., min_length=1, description="打回原因，必须写清楚缺什么材料")


class WorkflowRejectRequest(BaseModel):
    comment: str = Field(..., min_length=1, description="驳回原因")


# ============== 站内信 API（work-flow-web.md） ==============

class NotificationResponse(BaseModel):
    notification_id: str
    type: str
    title: str
    body: str
    link: Optional[str] = None
    is_read: bool
    created_at: float


# ============== State Management ==============

def ensure_message_ids(messages: List[AnyMessage]) -> List[AnyMessage]:
    """
    确保所有消息都有 ID。RemoveMessage 依赖 m.id 来删除消息。
    如果消息没有 ID，会重新创建带 ID 的消息。
    """
    result = []
    for m in messages:
        if not hasattr(m, 'id') or m.id is None:
            # 根据消息类型重新创建带 ID 的消息
            if isinstance(m, HumanMessage):
                new_msg = HumanMessage(content=m.content, id=str(uuid.uuid4()))
            elif isinstance(m, AIMessage):
                new_msg = AIMessage(content=m.content, id=str(uuid.uuid4()))
            else:
                # 通用处理
                new_msg = type(m)(content=m.content, id=str(uuid.uuid4()))
            result.append(new_msg)
        else:
            result.append(m)
    return result


class RAGState(TypedDict, total=False):
    """
    RAG 工作流状态定义。
    
    关键设计：
    1. messages: 使用 Annotated + add_messages 管理消息列表
       - 支持追加新消息
       - 支持 RemoveMessage 删除旧消息（滑动窗口压缩）
    2. summary: 滚动摘要，当消息超出限制时合并更新
    3. _to_archive: 内部标记，本轮要归档到 PostgreSQL 的消息（不存入 checkpoint）
    """
    
    # === 核心：给模型用的记忆（会被 checkpointer 自动管理）===
    messages: Annotated[List[AnyMessage], add_messages]
    summary: str
    
    # === 对话元数据 ===
    user_id: str
    task_id: str
    conversation_id: str
    
    # === 用户输入 ===
    query: str
    rewritten_query: str
    sub_queries: List[str]
    collection: Optional[str]
    top_k: int
    
    # === 意图识别 ===
    intent_type: str
    intent_confidence: float
    need_clarify: bool
    clarify_prompt: str
    target_tool: Optional[str]
    tool_args: Optional[Dict[str, Any]]

    # === 工作流：前端显式提示（瞬态，只在当轮消费，不需要跨轮持久化） ===
    workflow_type_hint: Optional[str]
    # === 工作流：intent 节点本轮分类/短路确定的类型，供 workflow 节点首次发起时读取 ===
    target_workflow_type: Optional[str]
    # === 工作流：跨轮持久化的填表进度（靠 checkpointer），None = 当前没有进行中的工作流 ===
    active_workflow: Optional[Dict[str, Any]]

    # === 检索结果 ===
    retrieval_context: str
    retrieval_contexts: List[str]
    
    # === 生成结果 ===
    final_answer: str
    used_model: str
    
    # === 长期记忆（跨会话认知连续）===
    memories: List[str]
    
    # === 追踪 ===
    trace_events: List[Dict[str, Any]]
    
    # === 本轮标识（用于三层时间裁剪回滚）===
    current_turn_id: str
    
    # === 工具执行结果 ===
    tool_summary: str
    tool_execution_trace: List[Dict[str, Any]]
    
    # === 可用工具（动态注入）===
    available_tools: List[Dict[str, Any]]
    
    # === 内部临时状态（不会存入 checkpoint）===
    _to_archive: List[Dict[str, Any]]  # 本轮要归档的消息


class ArchivedMessage(BaseModel):
    """归档消息的数据结构（存入 PostgreSQL）"""
    role: str
    content: str
    message_id: Optional[str] = None
    ts: float
