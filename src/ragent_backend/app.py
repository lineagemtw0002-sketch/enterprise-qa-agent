"""
RAG Backend API - 会话级知识库版本

主要变更：
1. 每个对话有独立的 collection（conv_{conversation_id}）
2. 支持文件上传和实时 ingest
3. RAG 检索限定在当前对话的文件范围内
4. 保持滑动窗口记忆管理
"""

from __future__ import annotations

import asyncio
import asyncpg
import httpx
import json
import os
import sys
import time
import uuid
from typing import Any, AsyncGenerator, Dict, FrozenSet, List, Optional
from pathlib import Path

# Windows: psycopg async 需要 SelectorEventLoop，而不是 ProactorEventLoop
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# 加载 .env 文件
try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', '..', '.env'))
    load_dotenv()  # 也尝试当前目录
except ImportError:
    pass  # python-dotenv 未安装

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, UploadFile, File, Form, Request, WebSocket, WebSocketDisconnect, Query
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager

from langchain_core.messages import HumanMessage

# LangGraph checkpointer
from langgraph.checkpoint.postgres import PostgresSaver

from src.ragent_backend.schemas import (
    ChatRequest, ChatResponse, ActiveWorkflowSummary, RollbackRequest,
    LoginRequest, LoginResponse, MeResponse, ChangePasswordRequest, RoleSummary,
    OrganizationSummary, AdminUserResponse, AdminCreateUserRequest,
    AdminOrganizationResponse, AdminCreateOrganizationRequest,
    TenantConnectorResponse, UpsertTenantConnectorRequest, GatewayConnectorResponse,
    RoleResponse, CreateRoleRequest, UpdateRoleRequest, SetUserRolesRequest,
    SetRoleCollectionsRequest,
    CollectionResponse, CreateCollectionRequest,
    CollectionCatalogEntry, UploadStartedResponse, UploadProgressResponse,
    TenantKbUploadResponse,
    DashboardOverviewResponse, DashboardTrendResponse, DashboardTrendPointResponse,
    AuditLogResponse, AuditLogListResponse, CostOverviewResponse,
    WorkflowTemplateResponse, CreateWorkflowTemplateRequest, UpdateWorkflowTemplateRequest,
    WorkflowApproverAssignmentResponse, SetWorkflowApproverRequest,
    WorkflowInstanceResponse, WorkflowActionRequest, WorkflowReturnRequest, WorkflowRejectRequest,
    NotificationResponse,
    AdminKbChunkPreview,
    AdminCreatedUserCredential, SetSeatLimitRequest, SetUserDisabledRequest,
    ActivateAccountRequest, BulkImportRowResult, BulkImportResponse,
    OpsConnectorResponse, RegisterOpsConnectorRequest, SetAiopsModuleEnabledRequest,
    OpsConnectorRegisterTokenResponse,
    RemediationScopeResponse, UpsertRemediationScopeRequest,
    RemediationActionResponse, ProposeRemediationActionRequest,
    RoleOpsPermissionResponse, SetRoleOpsPermissionRequest,
)
from src.ragent_backend import account_import as _acct_import
from src.ragent_backend import activation as _activation
from src.ragent_backend.store import build_archive_store, ConversationArchiveStore
from src.ragent_backend.db_pool import close_shared_pools
from src.ragent_backend.workflow import RAGWorkflow
from src.ragent_backend.ltm_store import LTMStore
from src.ragent_backend.file_store import build_file_store, ConversationFileStore
from src.ragent_backend.conversation_store import build_conversation_store, ConversationStore, Conversation
from src.ragent_backend.user_store import UserStore, User
from src.ragent_backend.role_store import Role, RoleStore, ROLE_SUPER_ADMIN, ROLE_ORG_ADMIN
from src.ragent_backend.workflow_store import WorkflowStore, WorkflowTemplate, WorkflowInstance
from src.ragent_backend.attendance_store import AttendanceStore
from src.ragent_backend.org_store import OrgStore
from src.ragent_backend.tenant_connector_store import TenantConnectorStore, CAPABILITY_KNOWLEDGE_BASE, CONNECTOR_TYPE_HTTP_API
from src.ragent_backend.collection_store import OrgCollectionStore
from src.ragent_backend.dashboard_stats import DashboardStatsService
from src.ragent_backend.tenant_identity_store import TenantIdentityStore
from src.ragent_backend.audit_store import AuditStore
from src.ragent_backend.ops_store import (
    OpsStore, IllegalStatusTransition,
    STATUS_PENDING_APPROVAL, STATUS_REJECTED, STATUS_REJECTED_PRE,
)
from src.ragent_backend import aiops_scope
from src.ops import connector_session
from src.ops.connector_transport import WebSocketConnectorTransport, WebSocketRemediationDispatcher
from src.ops.federation.engine import FederatedQueryEngine
from src.ops.store_adapters import OpsStoreDirectory
from src.ops.tools import OpsToolset
from src.ragent_backend.auth import (
    AuthenticatedUser, create_access_token, get_current_user, require_role,
    require_same_org_or_platform, require_platform_admin, get_jwt_secret,
    _decode_token,
)
from src.ingestion.pipeline import IngestionPipeline
from src.ingestion.delegated_compute import compute_chunks_for_delegation
from src.core.settings import load_settings, resolve_path
from src.tool_agent.tool_registry import ToolRegistry
from src.tool_agent.builtin_tools import register_builtin_tools
from src.tool_agent.mcp_client import MCPClient
from src.mcp_server.tools.query_knowledge_hub import QueryKnowledgeHubTool
from src.observability.logger import get_logger
from src.observability.context import bind_request_context, clear_request_context, get_request_context
from src.observability.middleware import RequestContextMiddleware

logger = get_logger(__name__)


def create_checkpointer():
    """
    创建 checkpointer
    Agent 层统一使用 PostgreSQL (AsyncPostgresSaver)
    """
    import concurrent.futures
    import selectors
    from psycopg import AsyncConnection
    from psycopg.rows import dict_row
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    postgres_url = os.getenv("RAGENT_POSTGRES_URL")
    if not postgres_url:
        raise ValueError(
            "RAGENT_POSTGRES_URL is required. "
            "Example: postgresql://user:password@localhost:5432/ragent"
        )

    def _create():
        async def _make():
            conn = await AsyncConnection.connect(
                postgres_url,
                autocommit=True,
                prepare_threshold=0,
                row_factory=dict_row,
            )
            saver = AsyncPostgresSaver(conn)
            await saver.setup()
            return saver
        return asyncio.run(
            _make(),
            loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()),
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_create)
        checkpointer = future.result()

    logger.info("using PostgreSQL checkpointer (async)")
    return checkpointer


async def _trim_checkpoints(checkpointer, thread_id: str, keep_checkpoint_id: Optional[str]) -> None:
    """
    物理裁剪 checkpoint：保留 keep_checkpoint_id 对应的状态，删除该 thread 下所有其他 checkpoint 记录。

    本项目实际使用的是 langgraph-checkpoint-postgres 的 AsyncPostgresSaver（app.py
    create_checkpointer() 里用 psycopg.AsyncConnection 构造），它的连接对象存在
    `checkpointer.conn`，不是这里原来假设的 `_async_connection`；且 psycopg3 用 `%s`
    占位符，不是 asyncpg 的 `$1/$2`。原实现的类型判断和属性名都对不上，会直接跳过整个
    分支、静默不删除任何东西，但外层仍然报告 trimmed=True。

    checkpoint_blobs 表按 (thread_id, checkpoint_ns, channel, version) 寻址，没有
    checkpoint_id 列——它是内容寻址、可能被多个 checkpoint 共享引用的，这里不裁剪，
    避免误删被保留的 checkpoint 仍然引用的 blob。
    """
    conn = getattr(checkpointer, "conn", None)
    if conn is None:
        logger.warning("checkpointer has no available connection, skipping trim", extra={"thread_id": thread_id})
        return

    try:
        if keep_checkpoint_id:
            await conn.execute(
                "DELETE FROM checkpoints WHERE thread_id = %s AND checkpoint_id != %s",
                (thread_id, keep_checkpoint_id),
            )
            await conn.execute(
                "DELETE FROM checkpoint_writes WHERE thread_id = %s AND checkpoint_id != %s",
                (thread_id, keep_checkpoint_id),
            )
        else:
            await conn.execute("DELETE FROM checkpoints WHERE thread_id = %s", (thread_id,))
            await conn.execute("DELETE FROM checkpoint_writes WHERE thread_id = %s", (thread_id,))
        logger.info(
            "postgres checkpoint trimmed",
            extra={"thread_id": thread_id, "kept_checkpoint_id": keep_checkpoint_id},
        )
    except Exception:
        logger.exception("postgres checkpoint trim failed", extra={"thread_id": thread_id})


# 全局并发控制：限制同时执行的 ingest 后台任务数量，防止 LLM API 配额和内存被打爆
INGEST_SEMAPHORE = asyncio.Semaphore(2)

# WebSocket 连接管理：conversation_id -> list[WebSocket]
active_trace_ws: dict[str, list[WebSocket]] = {}

# 智能运维模块（docs/aiops_module_design.md §10.1）：connection_id -> 当前存活
# 的 WebSocket 连接。进程内内存字典，不落库——"这个连接器现在是不是真的连着"
# 这件事的最权威来源就是这个进程持不持有它的 socket，落库的
# ops_system_connections.connector_status/last_heartbeat_at 是心跳驱动的
# 派生状态，用于跨进程/重启后的展示，两者不是同一个概念，不要混用。
active_ops_connector_ws: dict[str, WebSocket] = {}

# `message id -> Future`：`WebSocketConnectorTransport`/`WebSocketRemediationDispatcher`
# （src/ops/connector_transport.py）发出 query_request/exec_request 时在这里登记，
# WS 接收循环收到关联的 query_result/exec_result 帧时按 id 找到对应的 Future 并
# resolve 它——这是"请求-响应"语义叠加在"消息帧"这种发布/订阅式协议上的标准做法。
active_ops_pending_requests: dict[str, "asyncio.Future"] = {}

async def broadcast_trace(conversation_id: str, data: dict) -> None:
    """向该对话的所有 WebSocket 客户端广播 trace 事件"""
    sockets = active_trace_ws.get(conversation_id, [])
    if not sockets:
        return
    dead = []
    for ws in sockets:
        try:
            await ws.send_json(data)
        except Exception:
            dead.append(ws)
    for ws in dead:
        try:
            sockets.remove(ws)
        except ValueError:
            pass


# 允许上传的文件扩展名白名单
ALLOWED_EXTENSIONS = {
    '.pdf', '.docx', '.txt', '.md', '.csv',
    '.xlsx', '.xls', '.pptx', '.html', '.htm',
    '.json', '.yaml', '.yml',
}


async def ingest_file_task(
    file_store: ConversationFileStore,
    conversation_id: str,
    file_id: str,
    file_path: str,
    collection: str,
    settings: Settings,
    org_id: Optional[str] = None,
    owner_user_id: Optional[str] = None,
) -> None:
    """
    后台任务：将文件 ingest 到对话的 collection
    受全局 INGEST_SEMAPHORE 控制，避免无限制并发导致资源耗尽。
    """
    async with INGEST_SEMAPHORE:
        try:
            # 更新状态为 ingesting
            await file_store.update_file_status(conversation_id, file_id, "ingesting")
            
            # 创建 ingestion pipeline，指定 target collection
            # org_id / owner_user_id 供 OpenSearch 侧的对话私有库做企业内隔离过滤。
            # 两者都来自端点里已校验的身份（_require_conversation_owner 已确认
            # 当前用户就是该对话的所有者），不是请求体里的声明。
            pipeline = IngestionPipeline(
                settings,
                collection=collection,
                org_id=org_id,
                owner_user_id=owner_user_id,
            )
            
            # 执行 ingest（在线程池中运行，避免阻塞事件循环）
            result = await asyncio.to_thread(
                pipeline.run,
                file_path=file_path,
            )
            
            # 获取 doc_id（从 result 中提取）
            doc_id = result.doc_id if result.success else None
            
            # 从 loader metadata 中提取额外信息
            meta = result.metadata or {}
            extract_method = meta.get("extract_method")
            page_count = meta.get("page_count")
            word_count = meta.get("word_count")
            
            # 更新状态为 ready
            if result.success and doc_id:
                await file_store.update_file_status(
                    conversation_id, file_id, "ready", doc_id=doc_id,
                    extract_method=extract_method,
                    page_count=page_count,
                    word_count=word_count,
                )
                logger.info(
                    "file ingested successfully",
                    extra={"file_id": file_id, "collection": collection, "doc_id": doc_id, "extract_method": extract_method},
                )
            else:
                error_msg = result.error or "Unknown error"
                await file_store.update_file_status(
                    conversation_id, file_id, "error", error_message=error_msg,
                    extract_method=extract_method,
                    page_count=page_count,
                    word_count=word_count,
                )
                logger.warning("failed to ingest file", extra={"file_id": file_id, "error": error_msg})

        except Exception as e:
            logger.exception("failed to ingest file", extra={"file_id": file_id})
            await file_store.update_file_status(
                conversation_id, file_id, "error", error_message=str(e)
            )


def _build_active_workflow_summary(active_workflow: Optional[dict]) -> Optional[ActiveWorkflowSummary]:
    """把 RAGState.active_workflow（工作流节点自己维护的内部字典）投影成
    ChatResponse/SSE 用的公开字段（work-flow.md 7 节）。None 表示这一轮结束后
    没有进行中的工作流。"""
    if not active_workflow:
        return None
    return ActiveWorkflowSummary(
        workflow_type=active_workflow["workflow_type"],
        display_name=active_workflow["display_name"],
        missing_count=len(active_workflow.get("missing_field_keys", [])),
        total_count=active_workflow.get("total_required_count", 0),
    )


def resolve_cors_origins(
    raw: Optional[str] = None,
    debug_mode: Optional[bool] = None,
) -> List[str]:
    """决定 CORS 允许的来源列表，不再使用通配符。

    2026-08-26 P0：原本是 `allow_origins=["*"]` + `allow_credentials=True`——
    这个组合允许任意网站携带用户凭证跨域调用本服务的 API，是明确的错误配置。
    改为显式来源清单：`RAGENT_ALLOWED_ORIGINS`（逗号分隔）。未配置时，
    只有 `RAGENT_DEBUG=true` 才回退到本地前端开发常见来源；生产环境未配置
    则返回空列表——请求会被浏览器挡在 CORS 这一层，是"看得见的失败"，
    不是"悄悄放行"，不需要像 JWT 密钥那样拒绝启动。

    参数可注入是为了能直接单测，不依赖进程环境变量。

    `*` 在这里永远不被当成合法来源——哪怕有人把它显式配进
    `RAGENT_ALLOWED_ORIGINS`，也会被过滤掉，不会重新变回原缺陷那样的通配符。
    """
    if raw is None:
        raw = os.getenv("RAGENT_ALLOWED_ORIGINS", "")
    if debug_mode is None:
        debug_mode = os.getenv("RAGENT_DEBUG", "false").strip().lower() == "true"

    origins = [o.strip() for o in raw.split(",") if o.strip() and o.strip() != "*"]
    if origins:
        return origins
    if debug_mode:
        return ["http://localhost:5173", "http://127.0.0.1:5173"]
    return []


def create_app() -> FastAPI:
    # 最先校验 JWT 密钥：配置不安全时在这里就崩掉，不要等到有人登录才发现。
    # 密钥用了源码内置默认值意味着任何人都能伪造任意用户身份，下面这一整套
    # 权限设计（require_role / 多租户 collection ACL / tenant_ 前缀拦截）
    # 全部形同虚设，所以这属于"必须挡在启动阶段"的配置错误。
    # 见 auth.py `resolve_jwt_secret` 与 2026-08-24 代码审计 P0-2。
    get_jwt_secret()

    # 加载配置
    settings = load_settings()

    # user_store 先建好，因为 ToolRegistry 里的工具要用它做 ACL 校验
    user_store: UserStore = UserStore()
    # role_store：角色 CRUD + 用户<->角色关联 + 角色<->知识库关联（角色直接携带
    # 知识库权限，见 role_store.py 顶部说明）；user_store.get_allowed_collections
    # 内部会委托它算权限并集
    role_store: RoleStore = RoleStore()
    # workflow_store：流程模板 + 工作流实例 + 站内信（work-flow.md / work-flow-web.md）
    workflow_store: WorkflowStore = WorkflowStore()
    # attendance_store：每日打卡记录，供 query_attendance 工具查询
    attendance_store: AttendanceStore = AttendanceStore()
    # org_store：组织（企业）归属 + 平台管理员判断（attendance-tenant-federation.md 第 8 节）
    org_store: OrgStore = OrgStore()
    # tenant_connector_store：某企业的某项能力（如 knowledge_base/attendance）委托给谁查
    # （knowledge-base-tenant-federation.md 第 3 节 / attendance-tenant-federation.md 第 3 节）
    tenant_connector_store: TenantConnectorStore = TenantConnectorStore()
    # org_collection_store：企业自建知识库的归属登记（只针对本地检索的企业，
    # 见 collection_store.py 顶部说明）
    org_collection_store: OrgCollectionStore = OrgCollectionStore()
    # dashboard_stats_service：运营仪表盘只读聚合查询（见 dashboard_stats.py）
    dashboard_stats_service: DashboardStatsService = DashboardStatsService()
    # tenant_identity_store：我方 user_id <-> 企业考勤系统工号的映射，只有委托考勤
    # 查询用得到（attendance-tenant-federation.md 第 3 节）
    tenant_identity_store: TenantIdentityStore = TenantIdentityStore()
    # audit_store：治理与合规——管理后台变更操作 + 工具调用的审计记录（见
    # audit_store.py）
    audit_store: AuditStore = AuditStore()
    # ops_store：智能运维模块（docs/aiops_module_design.md）——连接器/修复范围
    # 白名单/审批状态机，阶段一存储层。BYOC 连接器的实际运行时（心跳/联邦查询/
    # AI 分析）尚未实现，这里接的只是管理面的 CRUD 端点。
    ops_store: OpsStore = OpsStore()
    # 知识库管理工具：统计/查看/清空一家企业名下知识库的数据。
    # 企业管理员在「知识库权限」页面自助删除知识库/分页查看数据（见下面
    # admin_delete_collection / admin_list_collection_chunks，只传调用方自己的
    # org_id，不会越权碰到别的企业）。
    #
    # 2026-08-26 已删除：曾经还有一组【测试专用，正式上线前删除】的
    # admin_test_query_knowledge_base 等 debug 端点，额外调用这个实例上
    # 绕过 ACL 的 execute_admin_bypass（允许调用方指定任意 org_id，不收窄到
    # 自己的企业）。那组端点本身、其专属的 execute_admin_bypass /
    # _build_empty_response_for_org、对应的 schemas、对应的前端页面已一并删除，
    # 见 `CLAUDE.md` §5「已修复」。list_org_collection_stats/chunks/
    # clear_org_collection 这几个方法继续保留，只服务上面这条正式路径。
    _kb_management_tool: QueryKnowledgeHubTool = QueryKnowledgeHubTool(
        org_store=org_store, tenant_connector_store=tenant_connector_store,
    )

    async def _audit_log(
        user_id: Optional[str],
        action: str,
        resource_type: str,
        resource_id: Optional[str],
        detail: dict,
        success: bool = True,
    ) -> None:
        """审计日志回调：补上 org_id/username 再落库。传给 RAGWorkflow（工具
        调用审计）和各管理端点（管理操作审计）共用同一个函数，保证两类事件
        落进同一张表、同一套字段。org_store/user_store 这里都能查——跟工具
        调用发生在同一个进程内，不需要额外的服务间调用。"""
        try:
            org = await org_store.get_org_for_user(user_id) if user_id else None
            user = await user_store.get_user_by_id(user_id) if user_id else None
            await audit_store.record(
                org_id=org.org_id if org else None,
                user_id=user_id,
                username=user.username if user else None,
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                detail=detail,
                success=success,
            )
        except Exception:
            logger.exception("failed to record audit log", extra={"action": action, "resource_type": resource_type})

    # 初始化 ToolRegistry（内置工具 + MCP 外部工具）
    tool_registry = ToolRegistry()
    # 返回值是真正会被 ReAct 工具子图调用的 QueryKnowledgeHubTool 实例——
    # 跟下面的 _kb_management_tool、RAGWorkflow 内部的 _retrieval_tool 是三个
    # 各自独立的实例，启动阶段预热（见 lifespan 里的 _preload_retrieval_models）
    # 需要拿到这个引用，见 register_builtin_tools 的 Returns 说明。
    # 智能运维模块工具集（docs/aiops_module_design.md §3.5/§3.6）——
    # WebSocketConnectorTransport/WebSocketRemediationDispatcher 复用
    # ops_connector_register_ws 那个 WS 端点维护的两个模块级注册表
    # （active_ops_connector_ws/active_ops_pending_requests），保证"谁在维护
    # 活连接"只有一份权威来源，不会出现协议实现自己另建一套连接表。
    _ops_transport = WebSocketConnectorTransport(active_ops_connector_ws, active_ops_pending_requests, ops_store)
    _ops_dispatcher = WebSocketRemediationDispatcher(active_ops_connector_ws, active_ops_pending_requests, ops_store)
    _ops_engine = FederatedQueryEngine(transport=_ops_transport, directory=OpsStoreDirectory(ops_store))
    ops_toolset = OpsToolset(_ops_engine, ops_store, dispatcher=_ops_dispatcher)

    # ⚠️ 已知缺口：工具注册目前是全局的，没有按 aiops_module_enabled 过滤——
    # 模块未开通的企业用户也会在 LLM 可用工具列表里看到 query_ops_system 等
    # 三个工具。不是数据泄露（该企业不可能注册连接器，query 会拿到空结果，
    # propose/execute 会在 org 归属校验那一步被拒），但体验不完美，见
    # CLAUDE.md §5 该条"未做的"。
    chat_kb_tool = register_builtin_tools(
        tool_registry,
        user_store=user_store,
        workflow_store=workflow_store,
        attendance_store=attendance_store,
        org_store=org_store,
        tenant_connector_store=tenant_connector_store,
        tenant_identity_store=tenant_identity_store,
        ops_toolset=ops_toolset,
    )
    logger.info("registered built-in tools", extra={"tool_count": tool_registry.tool_count})

    # 初始化组件
    checkpointer = create_checkpointer()
    archive_store: ConversationArchiveStore = build_archive_store()
    file_store: ConversationFileStore = build_file_store()
    conversation_store: ConversationStore = build_conversation_store()

    async def _require_conversation_owner(
        conversation_id: str, current_user: AuthenticatedUser
    ) -> Conversation:
        """校验对话存在且属于当前登录用户；不存在 404，存在但不是自己的 403。"""
        conv = await conversation_store.get_conversation(conversation_id)
        if not conv:
            raise HTTPException(status_code=404, detail="Conversation not found")
        if conv.user_id != current_user.user_id:
            raise HTTPException(status_code=403, detail="无权访问该对话")
        return conv

    async def _is_workflow_approver_for_conversation(conversation_id: str, user_id: str) -> bool:
        """这个对话上有没有一条工作流实例，且当前用户持有"申请人所在企业"给
        这类流程配的审批角色——审批角色按企业配置（见 workflow_store.py 顶部
        说明），要按申请人（不是当前查看者）所属企业去查，两者本来就该是
        同一家企业，不然 mode="approver" 的权限检查通不过。"""
        instance = await workflow_store.get_latest_instance_by_conversation(conversation_id)
        if instance is None:
            return False
        requester_org = await org_store.get_org_for_user(instance.requester_user_id)
        if requester_org is None:
            return False
        approver_role_id = await workflow_store.get_org_approver_role_id(requester_org.org_id, instance.workflow_type)
        if not approver_role_id:
            return False
        user_role_ids = {r.role_id for r in await role_store.get_user_roles(user_id)}
        return approver_role_id in user_role_ids

    # 初始化 LLM（配置完全来自 settings.yaml + 环境变量覆盖）
    def _build_llm(model: str):
        from langchain_openai import ChatOpenAI
        llm_kwargs = {
            "model": model,
            "temperature": settings.llm.temperature,
            "max_tokens": settings.llm.max_tokens,
        }
        base_url = getattr(settings.llm, "base_url", None)
        api_key = getattr(settings.llm, "api_key", None)
        if settings.llm.provider == "ollama":
            # Ollama exposes an OpenAI-compatible endpoint under /v1; ChatOpenAI
            # requires a non-empty api_key even though Ollama ignores it.
            llm_kwargs["base_url"] = f"{(base_url or 'http://localhost:11434').rstrip('/')}/v1"
            llm_kwargs["api_key"] = api_key or "ollama"
        else:
            if base_url:
                llm_kwargs["base_url"] = base_url
            if api_key:
                llm_kwargs["api_key"] = api_key
        return ChatOpenAI(**llm_kwargs)

    try:
        llm = _build_llm(settings.llm.model)
    except Exception:
        logger.exception("failed to init main LLM")
        llm = None

    # 意图分类专用模型（docs/optimization_tracking.md 耗时优化任务）：LoRA 微调
    # qwen2.5:1.5b（训练数据覆盖"指代消解 + 子查询拆分 + 四分类"合并任务
    # analyze_and_route()，见 workflow.py RAGWorkflow._intent_llm 旁的说明）+
    # 反量化转 GGUF 导入 Ollama 得到，准确率不输 7b 跑同一个合并任务（部分
    # 边界案例反而更准），单次调用耗时约 3.2s，比 7b 的约 8.7s 快 2.7 倍左右。
    # RAGENT_INTENT_MODEL 留空或者这个模型在当前环境没有被 `ollama create`
    # 出来，直接退回主模型，不让意图节点因为模型不存在报错——这是"用哪个
    # 模型做分类"的开关，不是"要不要做意图分类"的开关。
    intent_model_name = os.getenv("RAGENT_INTENT_MODEL", "qwen2.5-1.5b-router")
    if intent_model_name:
        try:
            intent_llm = _build_llm(intent_model_name)
        except Exception:
            logger.exception(
                "failed to init intent-classification LLM, falling back to main LLM",
                extra={"intent_model_name": intent_model_name},
            )
            intent_llm = llm
    else:
        intent_llm = llm

    # 创建工作流（传入 tool_registry）
    # LTMStore was never constructed here, so RAGWorkflow always received
    # ltm_store=None -- long-term memory recall/extraction and the rollback
    # endpoint's LTM trim step were silently no-ops despite being fully
    # implemented in ltm_store.py.
    ltm_store = LTMStore()

    workflow = RAGWorkflow(
        store=archive_store,
        llm=llm,
        intent_llm=intent_llm,
        checkpointer=checkpointer,
        max_messages=int(os.getenv("RAGENT_MAX_MESSAGES", "20")),
        keep_recent=int(os.getenv("RAGENT_KEEP_RECENT", "4")),
        tool_registry=tool_registry,
        ltm_store=ltm_store,
        workflow_store=workflow_store,
        audit_log=_audit_log,
    )

    _llms_to_keep_warm = [m for m in {id(llm): llm, id(intent_llm): intent_llm}.values() if m is not None]

    # 跟 _llms_to_keep_warm 是同一个问题的另一半：三个 QueryKnowledgeHubTool
    # 实例各自持有自己的 embedding_client（见 _preload_retrieval_models 的
    # 说明，互不共享），启动时预热过一次，但之前没有被纳入下面的周期保活——
    # embedding 模型走的也是 Ollama，同样受 5 分钟 keep_alive 支配，空闲超过
    # 这个窗口后一样会被换出。实测（logs/traces.jsonl 的 narrow_detail:
    # embed_query 分段计时）：间隔 81s 时 46ms，间隔 6分18秒 时 834ms——
    # 跟 llm/intent_llm 同款问题，只是漏了这一层保活。
    _retrieval_tools_to_keep_warm = {
        "chat_kb_tool": chat_kb_tool,
        "kb_management_tool": _kb_management_tool,
        "workflow_retrieval_tool": workflow._retrieval_tool,
    }

    async def _ping_llm_once(ping_llm) -> None:
        try:
            await ping_llm.bind(max_tokens=1).ainvoke([HumanMessage(content="ping")])
        except Exception:
            logger.warning(
                "keep-alive ping failed", extra={"model_name": getattr(ping_llm, "model_name", "?")}, exc_info=True,
            )

    async def _warm_llms_at_startup() -> None:
        """启动阶段预热 llm/intent_llm 本身（本轮追加，`_keep_models_warm` 之前
        遗漏的一层）——`_preload_retrieval_models` 只预热了 retrieval 侧的
        reranker/embedding client，从没让 Ollama 真正把 `llm`（7b）/`intent_llm`
        （1.5b-router）这两个模型的权重加载进内存。而 `_keep_models_warm` 的
        保活循环第一次 ping 是在 `asyncio.sleep(240)` 之后才发生——也就是说
        重启后头 4 分钟内，谁的请求第一个真正走到需要这两个模型的节点
        （intent / 工具决策 think_node / generate），谁就要现付 Ollama
        把权重从磁盘搬进内存的钱（用户反馈"工具调用这一步第一次很慢"就是
        撞在这里）。在这里跟 retrieval 预热一起、在 `yield` 之前跑完，
        对用户完全不可见；单个失败只打日志，不阻断启动。"""
        for ping_llm in _llms_to_keep_warm:
            await _ping_llm_once(ping_llm)

    async def _keep_models_warm():
        """后台模型保活探测（docs/optimization_tracking.md 耗时优化任务）——
        Ollama 默认 keep_alive 是 5 分钟，空闲超时模型会被换出显存，下一次
        真实用户请求撞上就要额外付出几秒到十几秒的冷启动加载耗时
        （query_knowledge_hub.py 里已经有专门的 cold_start 检测逻辑，说明
        这个问题真实发生过）。现在同时有生成用的 llm 和意图分类专用的
        intent_llm 两个模型要保活，任何一个被换出都会让下一次请求平白多等。

        每隔 4 分钟（小于 5 分钟的默认超时窗口）分别给两个模型发一个只要求
        输出 1 个 token 的极短请求——Ollama 那边只要收到请求就会重置"最近
        使用时间"、顺延保活窗口，不需要改 Ollama 服务本身的 keep_alive 配置
        （那需要重启 Ollama daemon）。ping 失败只打日志，不影响正常请求
        （下一次真实请求该走的重试/降级逻辑不受这里影响）。首次 ping 由
        `_warm_llms_at_startup` 在启动阶段跑掉，这里的循环只负责之后的
        周期性保活，避免重复预热一次。

        同一个循环里顺带重跑一遍每个 QueryKnowledgeHubTool 的
        `preload_models()`——理由见 `_retrieval_tools_to_keep_warm` 旁的
        说明：embedding client 走的也是 Ollama，同样会被 5 分钟 keep_alive
        换出，只在启动时预热一次不够。复用 `preload_models()` 而不是新写一次
        embed 调用，跟"预热逻辑只有一份"的原则一致（`_ensure_shared_clients`
        的说明），reranker 那部分重复调用是幂等的、warm 之后只有几十毫秒。"""
        if not _llms_to_keep_warm and not _retrieval_tools_to_keep_warm:
            return
        while True:
            try:
                await asyncio.sleep(240)
                for ping_llm in _llms_to_keep_warm:
                    await _ping_llm_once(ping_llm)
                for name, tool in _retrieval_tools_to_keep_warm.items():
                    try:
                        await tool.preload_models()
                    except Exception:
                        logger.warning("retrieval warm-up failed", extra={"tool_name": name}, exc_info=True)
            except asyncio.CancelledError:
                break

    async def _scan_expired_ops_approvals():
        """智能运维审批超时扫描（`docs/aiops_module_design.md` §10.4）——
        `pending_approval` 状态的修复动作如果超过连接器配置的
        `approval_timeout_minutes`（5～1440 分钟，见 `aiops_scope.
        validate_approval_timeout_minutes`）还没人处理，转成 `expired`，
        不能永远挂在待审批队列里。之前 `OpsStore` 只有查询方法
        `list_pending_approval_older_than`（且接口设计本身有问题——接收单个
        全局 cutoff，没法表达"不同连接器超时长度不同"），从未接过任何调用方，
        这里是第一次真正接上定时任务，见 `CLAUDE.md` §5。

        5 分钟扫一次：这是一个"最长可能多等 5 分钟才被标记过期"的宽限，不是
        审批本身的时限——跟 `_keep_models_warm` 的保活扫描同一个数量级，
        没有理由扫得比这更勤（`approval_timeout_minutes` 下限是 5 分钟，
        扫描间隔跟下限相近很正常，不是巧合也不是问题：即使一次超时窗口只
        5 分钟的连接器，最坏情况也只是多等一个扫描周期才被标记，不影响
        "过期动作不能再被执行"这条硬约束本身——`STATUS_EXPIRED` 是终态，
        审批/执行两个专用方法都会在状态机层面拒绝对一个已经不在
        `pending_approval` 的动作起作用）。单次扫描异常只记日志，不影响下一轮。
        """
        while True:
            try:
                await asyncio.sleep(300)
                expired_ids = await ops_store.expire_stale_pending_approvals()
                if expired_ids:
                    logger.info(
                        "expired stale pending ops approvals",
                        extra={"expired_count": len(expired_ids)},
                    )
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("ops approval timeout scan failed")

    async def _preload_retrieval_models() -> None:
        """启动阶段预热知识库检索用到的 reranker/embedding client（
        docs/optimization_tracking.md 耗时优化任务，"知识库检索为什么要
        7 秒"那次排查的结论）——`QueryKnowledgeHubTool._build_hybrid_search_for`
        原来是"谁第一个真的查知识库，谁就现场付一次模型加载的钱"（本地
        cross-encoder `BAAI/bge-reranker-base` 首次加载实测 6.4 秒），现在
        挪到这里，在 `lifespan` 里 `yield`（开始真正接受请求）之前跑完，
        对用户完全不可见。

        `QueryKnowledgeHubTool` 在这个进程里被实例化了三处（这里、
        `_kb_management_tool`、`RAGWorkflow._retrieval_tool`），各自的
        `_embedding_client`/`_reranker` 是实例级缓存，互不共享——预热一个
        不能省下另外两个的加载，所以三个都要单独调一次 `preload_models()`。
        单个失败只打日志、不阻断启动：预热本身是优化手段，不是正确性前提，
        真出问题时该走的懒加载兜底路径仍然生效，只是退化成没预热的效果。"""
        for name, tool in _retrieval_tools_to_keep_warm.items():
            try:
                await tool.preload_models()
                logger.info("reranker/embedding client warmed up", extra={"tool_name": name})
            except Exception:
                logger.warning(
                    "preload failed (non-fatal, falls back to lazy load)", extra={"tool_name": name}, exc_info=True,
                )

    # lifespan：异步连接 MCP Servers（必须在 FastAPI 构造函数之前定义）
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await _preload_retrieval_models()
        await _warm_llms_at_startup()
        keep_warm_task = asyncio.create_task(_keep_models_warm())
        ops_timeout_scan_task = asyncio.create_task(_scan_expired_ops_approvals())

        # 启动时连接配置的 MCP Servers
        if settings.mcp_servers:
            for name, cfg in settings.mcp_servers.items():
                try:
                    client = MCPClient(server_name=name)
                    if cfg.transport == "stdio":
                        await client.connect_stdio(
                            command=cfg.command or "",
                            args=cfg.args,
                            env=cfg.env,
                            cwd=cfg.cwd,
                        )
                    elif cfg.transport == "sse":
                        await client.connect_sse(url=cfg.url or "")
                    else:
                        logger.warning("unknown MCP transport", extra={"transport": cfg.transport, "server_name": name})
                        continue

                    await tool_registry.register_from_mcp_client(
                        client, name, timeout_seconds=cfg.timeout_seconds
                    )
                    logger.info("connected and registered MCP server", extra={"server_name": name})
                except Exception:
                    logger.exception("failed to connect MCP server", extra={"server_name": name})

        yield

        # 关闭时断开所有 MCP 连接
        await tool_registry.disconnect_all_mcp()
        logger.info("all MCP connections closed")

        keep_warm_task.cancel()
        try:
            await keep_warm_task
        except asyncio.CancelledError:
            pass

        ops_timeout_scan_task.cancel()
        try:
            await ops_timeout_scan_task
        except asyncio.CancelledError:
            pass

    # 创建 FastAPI app
    app = FastAPI(
        title="RAG Agent Backend", 
        version="0.4.0",
        description="支持会话级知识库的 RAG Agent（三分支意图路由 + 统一工具层）",
        lifespan=lifespan,
    )

    # 添加 CORS 中间件
    app.add_middleware(
        CORSMiddleware,
        allow_origins=resolve_cors_origins(),
        allow_credentials=True,
        allow_methods=["*"],  # 允许所有方法
        allow_headers=["*"],  # 允许所有头
    )

    # RequestContextMiddleware 必须在 CORSMiddleware 之后注册——Starlette 中间件是
    # 洋葱模型，后注册的在更外层，这样连 CORS 预检失败的请求也能带上 request_id。
    # 见 docs/observability_design.md §5「中间件顺序」。
    app.add_middleware(RequestContextMiddleware)

    @app.get("/health")
    async def health() -> dict:
        return {
            "status": "ok",
            "version": "0.4.0",
            "features": [
                "rolling_memory",
                "conversation_kb",
                "file_upload",
                "3_way_intent_routing",
                "unified_tool_layer",
                "tool_subgraph",
                "mcp_client",
            ]
        }

    # ==================== 鉴权 API ====================

    @app.post("/api/v1/activate")
    async def activate_account(request: ActivateAccountRequest) -> dict:
        """凭一次性激活码设置初始密码。**全系统唯一不带鉴权的写端点。**

        设计 `docs/account_lifecycle_design.md` §4.1b、风险 R-4。
        用户已定不做邮件短信（O-1），所以凭证分发是人工的；既然如此就让被分发
        的东西尽可能不值钱——码是 7 天一次性的，而任何形式的"初始密码"都长期有效。

        ## 四条防护，这里落地了三条

        1. **只存哈希** —— 库里是 SHA-256，`activation.hash_activation_code`。
        2. **≥128 bit 熵** —— `secrets.token_urlsafe(16)`，有测试钉住熵值本身
           （它是"用 SHA-256 而不是 bcrypt"这个选择成立的前提）。
        3. **恒定时间比较 + 失败原因不可区分** —— 见下面的注释。
        4. **限流** —— ⚠️ **没做。** 全仓没有任何限流基础设施可复用，
           要么引入（如 slowapi）要么手写，属独立一项工作。
           **在补上之前，这个端点可以被无限次尝试。** 128 bit 的码扛得住爆破，
           但扛不住有人拿它当免费的 CPU 消耗入口。已在设计 §9 记为未覆盖。

        ## 为什么失败一律同一句话

        它无鉴权，任何人都能调。如果"用户不存在"和"码不对"返回不同的错误，
        攻击者随便试一个 username，就能从差异里反推出这家企业的员工花名册。
        `ActivationCheck.public_detail` 把四种失败塌缩成同一句，
        内部原因只进审计日志。
        """
        state = await user_store.get_activation_state(request.username)
        check = _activation.check_activation(
            submitted_code=request.activation_code,
            stored_hash=(state or {}).get("activation_code_hash"),
            expires_at=(state or {}).get("activation_expires_at"),
            activated_at=(state or {}).get("activated_at"),
            now=time.time(),
            user_exists=state is not None,
        )

        # 被停用的账号即使手里有有效的码也不能激活——否则"停用"就能被一张
        # 旧的激活码清单绕过。这一条走跟其他失败完全相同的对外文案。
        if check.ok and state and state.get("disabled_at") is not None:
            check = _activation.ActivationCheck(
                False, _activation.ActivationFailure.NO_SUCH_USER
            )

        if not check.ok:
            # ⚠️ **审计日志里记内部原因，但绝不记提交上来的码。**
            # 那串东西要么是有效凭证（记下来等于明文存凭证），要么是攻击载荷。
            await _audit_log(
                None, "activate_account_failed", "user", None,
                {"username": request.username,
                 "reason": check.failure.value if check.failure else "unknown"},
                success=False,
            )
            raise HTTPException(status_code=400, detail=check.public_detail)

        # 单次使用的最后一道闸在 SQL 里（`WHERE activated_at IS NULL`），
        # 不是靠上面那次读——两个请求拿同一个码同时打进来，只有一条改得到行。
        # 上面的检查是为了给出正确的错误信息，不是并发正确性的依据。
        if not await user_store.complete_activation(state["id"], request.new_password):
            raise HTTPException(status_code=400, detail=_activation.PUBLIC_FAILURE_DETAIL)

        await _audit_log(state["id"], "activate_account", "user", state["id"],
                         {"username": request.username})
        return {"success": True}

    @app.post("/api/v1/auth/login", response_model=LoginResponse)
    async def login(request: LoginRequest) -> LoginResponse:
        user = await user_store.authenticate(request.username, request.password)
        if user is None:
            raise HTTPException(status_code=401, detail="用户名或密码错误")

        token = create_access_token(user_id=user.user_id, username=user.username)
        return LoginResponse(access_token=token, user_id=user.user_id, username=user.username)

    async def _org_summary_for_user(user_id: str) -> Optional[OrganizationSummary]:
        org = await org_store.get_org_for_user(user_id)
        if org is None:
            return None
        return OrganizationSummary(
            org_id=org.org_id, name=org.name, is_platform=org.is_platform,
            aiops_module_enabled=await ops_store.is_module_enabled(org.org_id),
        )

    @app.get("/api/v1/auth/me", response_model=MeResponse)
    async def get_me(current_user: AuthenticatedUser = Depends(get_current_user)) -> MeResponse:
        user = await user_store.get_user_by_id(current_user.user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="用户不存在")
        roles = await role_store.get_user_roles(user.user_id)
        return MeResponse(
            user_id=user.user_id,
            username=user.username,
            roles=[RoleSummary(role_id=r.role_id, name=r.name, display_name=r.display_name) for r in roles],
            allowed_collections=await role_store.get_allowed_collections_for_user(user.user_id),
            organization=await _org_summary_for_user(user.user_id),
            created_at=user.created_at,
        )

    @app.post("/api/v1/auth/change-password")
    async def change_password(
        request: ChangePasswordRequest,
        current_user: AuthenticatedUser = Depends(get_current_user),
    ) -> dict:
        ok = await user_store.change_password(
            current_user.user_id, request.old_password, request.new_password
        )
        if not ok:
            raise HTTPException(status_code=400, detail="旧密码错误")
        return {"success": True}

    # ==================== 管理后台 API ====================
    # 两层角色模型（2026-08-24 起废弃平台侧的 admin/user 两个系统角色，运营方
    # 只保留 super_admin 一个身份档位，见 role_store.py 顶部说明）：
    #   super_admin —— 平台运营方，管平台本身（建企业、配连接器、定义角色……），
    #     但不了解客户企业内部的部门架构，所以对某个客户企业能做的唯一一件事
    #     是"任命谁是这家企业的 org_admin"，不直接管理该企业内部的员工角色/
    #     知识库权限。
    #   org_admin（企业管理员）—— 客户企业侧，被平台层任命后，管理自己企业
    #     内部的员工（增/删/查/分配角色，含知识库权限角色），管不到别的企业，
    #     也不能任命新的 org_admin（那还是平台层的事）。
    # 人员管理（增/查/删/分配角色）两层角色都能碰，具体边界由
    # `_validate_role_assignment` 按"谁在给哪家企业的人分配什么角色"判断，不是
    # 简单的"有没有权限调这个接口"能表达清楚的，所以拆成单独的校验函数。
    # 组织管理、连接器配置、角色定义、工作流模板这类跨企业/平台级操作仍然只对
    # super_admin 开放（各自的 Depends 没有改）。
    # 角色判断是每次请求都现查数据库（见 auth.require_role），不是信 token。

    _require_super_admin = require_role(ROLE_SUPER_ADMIN)
    _require_user_admin_tier = require_role(ROLE_SUPER_ADMIN, ROLE_ORG_ADMIN)
    # 企业自建知识库（新增/列出）只对 org_admin 开放，平台管理员（super_admin）
    # 不在允许名单里——不是"看不全"，是这两个端点对他们直接 403，企业内部的
    # 知识库归属信息平台运营方压根碰不到（跟"角色管理"页面不展示知识库、
    # 「用户与角色分配」平台视角不展示"可访问知识库"列是同一个边界，见对应组件
    # 顶部注释）。
    _require_org_admin = require_role(ROLE_ORG_ADMIN)
    # 运营仪表盘专用——平台侧只剩 super_admin 一个身份档位后，跟
    # _require_super_admin 已无实质区别，单独保留是为了让这批"运营仪表盘"
    # 端点的 Depends 读起来语义独立（不是随手复用了用户管理那档权限）。
    _require_platform_tier = require_role(ROLE_SUPER_ADMIN)

    async def _validate_role_assignment(
        actor: AuthenticatedUser, target_org_id: Optional[str], role_ids: List[str],
        existing_role_names: FrozenSet[str] = frozenset(),
    ) -> None:
        """校验 `actor` 能不能把 `role_ids` 这组角色发给 `target_org_id` 这家企业的
        某个用户，三条边界见本节顶部的角色模型说明：

        1. super_admin 这个平台角色只有 super_admin 自己能授予——防止越权
           互相提权。
        2. org_admin（企业管理员）角色只有平台层（super_admin）能授予——这是
           "任命企业管理员"这个动作本身，企业管理员不能任命同事、不能给自己
           续任。
        3. 平台层账号（super_admin）如果是在给客户企业（非平台组织）的用户
           分配角色，只能分配 org_admin 这一种——不能替客户企业分配具体的
           部门/知识库角色，因为他们不了解客户企业内部架构，那是该企业
           org_admin 被任命后自己的事。
        4. 企业角色（`role.org_id` 非空，某家企业管理员自己建的、直接携带知识库
           权限的角色）只能分配给同一家企业的员工——不能把 Acme 建的角色发给
           测试新公司的员工，跨了会直接导致这个员工能看到 Acme 的知识库内容。

        以上四条只审查"新授予"的角色（`role_ids` 里目标用户原本没有的部分，
        由调用方通过 `existing_role_names` 传入目标用户当前已有的角色名）。
        企业管理员在企业内本就是最高权限，编辑自己或同事时，Select 提交的是
        完整角色集合，必然带着自己已有的 org_admin——如果连"保留自己已有的
        角色"都按"新任命"的标准审查，企业管理员会连自己的其他权限（比如换
        一个部门角色）都改不了，等于企业内最高权限者反而管不了自己。只有目标
        用户原本没有、这次新加进来的角色，才真的构成"任命"，才需要走上面
        四条边界。新建用户时 `existing_role_names` 传空集，等价于所有角色都
        是"新授予"，跟原来的校验强度一致。
        """
        actor_role_names = {r.name for r in await role_store.get_user_roles(actor.user_id)}
        is_platform_tier = ROLE_SUPER_ADMIN in actor_role_names

        requested_roles = [r for r in [await role_store.get_role_by_id(rid) for rid in role_ids] if r is not None]
        newly_granted = [r for r in requested_roles if r.name not in existing_role_names]
        newly_granted_names = {r.name for r in newly_granted}

        if ROLE_SUPER_ADMIN in newly_granted_names and ROLE_SUPER_ADMIN not in actor_role_names:
            raise HTTPException(status_code=403, detail="只有超级管理员能授予超级管理员角色")

        if ROLE_ORG_ADMIN in newly_granted_names and not is_platform_tier:
            raise HTTPException(status_code=403, detail="只有平台管理员（超级管理员）能任命企业管理员")

        if is_platform_tier and target_org_id is not None:
            target_org = await org_store.get_organization(target_org_id)
            if target_org is not None and not target_org.is_platform:
                disallowed = newly_granted_names - {ROLE_ORG_ADMIN}
                if disallowed:
                    raise HTTPException(
                        status_code=403,
                        detail="平台管理员不了解客户企业内部架构，只能任命该企业的企业管理员，"
                               "具体的员工角色/知识库权限请由该企业的企业管理员分配",
                    )

        cross_org = [r for r in newly_granted if r.org_id is not None and r.org_id != target_org_id]
        if cross_org:
            raise HTTPException(status_code=403, detail="不能分配其他企业创建的角色")

    async def _build_admin_user_response(user: User) -> AdminUserResponse:
        roles = await role_store.get_user_roles(user.user_id)
        return AdminUserResponse(
            user_id=user.user_id,
            username=user.username,
            roles=[RoleSummary(role_id=r.role_id, name=r.name, display_name=r.display_name) for r in roles],
            allowed_collections=await role_store.get_allowed_collections_for_user(user.user_id),
            organization=await _org_summary_for_user(user.user_id),
            created_at=user.created_at,
            # ⚠️ AdminUserResponse 有**两个**构造点：这里（单个用户，建号/改角色/
            # 停用的响应）和 `admin_list_users` 里的批量版。**加字段必须两处一起改。**
            # 2026-08-26 就漏过一次：只改了批量版，于是列表页显示正常，
            # 而停用接口返回的 disabled_at 恒为 null，前端开关点完不刷新状态。
            # 单测和 create_app 都抓不到，是 HTTP 端到端跑出来的。
            disabled_at=user.disabled_at,
            activated_at=user.activated_at,
            pending_activation=user.pending_activation,
        )

    @app.get("/api/v1/admin/users", response_model=List[AdminUserResponse])
    async def admin_list_users(
        current_user: AuthenticatedUser = Depends(_require_user_admin_tier),
    ) -> List[AdminUserResponse]:
        """2026-08-26 P1-14 修复：原来对每个用户单独查角色/知识库权限/所属企业
        （`_build_admin_user_response` 逐个 await），50 用户约 300 次串行查询。
        改为批量查询一次拿齐所有用户的数据，再在内存里拼装——查询数不再随
        用户数线性增长（固定常数次，不含 `is_platform_admin`/`get_org_for_user`
        这两次判断当前登录者身份的查询）。"""
        users = await user_store.list_users()
        user_ids = [u.user_id for u in users]
        orgs_by_user = await org_store.get_orgs_for_users_batch(user_ids)

        # 平台管理员看全部；普通企业管理员只看自己企业的——过滤发生在这里，
        # 不是前端拿到全量再自己藏几行（attendance-tenant-federation.md 图4）。
        if not await org_store.is_platform_admin(current_user.user_id):
            actor_org = await org_store.get_org_for_user(current_user.user_id)
            actor_org_id = actor_org.org_id if actor_org else None
            users = [
                u for u in users
                if (org := orgs_by_user.get(u.user_id)) is not None and org.org_id == actor_org_id
            ]
            user_ids = [u.user_id for u in users]

        roles_by_user = await role_store.get_user_roles_batch(user_ids)
        collections_by_user = await role_store.get_allowed_collections_for_users_batch(user_ids)
        # OrganizationSummary 加了 aiops_module_enabled 字段后这里也得跟着填
        # （见下面 organization= 那行旁边"加字段时两处必须一起改"的既有教训）——
        # 按去重后的 org_id 批量查一次，不随 user 数线性增长。
        aiops_enabled_by_org = await ops_store.is_module_enabled_batch(
            list({org.org_id for org in orgs_by_user.values()})
        )

        return [
            AdminUserResponse(
                user_id=u.user_id,
                username=u.username,
                roles=[
                    RoleSummary(role_id=r.role_id, name=r.name, display_name=r.display_name)
                    for r in roles_by_user.get(u.user_id, [])
                ],
                allowed_collections=collections_by_user.get(u.user_id, []),
                organization=(
                    OrganizationSummary(
                        org_id=org.org_id, name=org.name, is_platform=org.is_platform,
                        aiops_module_enabled=aiops_enabled_by_org.get(org.org_id, False),
                    )
                    if (org := orgs_by_user.get(u.user_id)) is not None else None
                ),
                created_at=u.created_at,
                # 账号生命周期三个字段（2026-08-26）。批量版和
                # `_build_admin_user_response` 是同一个响应模型的两个构造点，
                # **加字段时两处必须一起改** —— 漏掉这里的话，用户列表页
                # （唯一真正用到它们的地方）会静默拿到默认值：所有人都显示
                # "正常"，停用和待激活状态完全看不见，而且不报任何错。
                disabled_at=u.disabled_at,
                activated_at=u.activated_at,
                pending_activation=u.pending_activation,
            )
            for u in users
        ]

    async def _enforce_seat_capacity(org_id: str, delta: int) -> None:
        """席位校验的**唯一**入口。三个调用点共用：建号、批量导入、重新启用。

        判定本身在 `account_import.check_seat_capacity`（纯函数、有单测），
        这里只负责取数和翻译成 HTTP —— 跟 `activation` 那边同样的分工。

        403 而不是 400：这不是请求写错了，是超出了这家企业的合同额度。
        """
        check = _acct_import.check_seat_capacity(
            seats_used=await user_store.count_active_users(org_id),
            seat_limit=await org_store.get_seat_limit(org_id),
            delta=delta,
        )
        if not check.ok:
            raise HTTPException(status_code=403, detail=check.detail)

    async def _import_context(actor: AuthenticatedUser, org_id: str, usernames: list) -> "_acct_import.ImportContext":
        """把批量导入要的库内事实一次性查好。

        ⚠️ **`assignable_roles` 必须先按企业过滤再传进去。** 跨企业角色校验在
        纯函数层退化成一次字典查找，"别家企业的角色"能不能被分配，
        完全取决于这里放没放进去。这是 `_validate_role_assignment` 那四条边界
        在导入路径上的等价物——不能因为走了新端点就漏掉（设计 §6 风险 R-2）。
        """
        # `list_roles_for_org` = 全局角色（部门身份）+ 这家企业自建的角色，
        # 正好是企业管理员在「用户管理」下拉框里能选的那一组。
        #
        # ⚠️ **只再排除两个平台档位角色。** super_admin / org_admin 是"任命"
        # 而不是"分配部门"，必须走 `admin_set_user_roles` 那条有
        # `_validate_role_assignment` 四条边界把关的路径。让导入能发这两个，
        # 等于给了一条"上传一个 CSV 就把自己提成超管"的近路。
        #
        # ⚠️ 用 `list_roles_for_org` 而不是自己写 `r.org_id == org_id` 过滤：
        # 前者是"这个企业管理员能分配什么"的**权威定义**，「用户管理」下拉框
        # 用的就是它，两处口径必须一致，否则会出现"界面上能选、导入却说角色
        # 不存在"。它包含全局角色 + 本企业角色。
        # （2026-08-26 实测本库里全局角色只有 super_admin / org_admin 两个，
        # 部门角色全是企业级的——所以这两种写法当前结果相同。但依赖这个巧合
        # 是错的：只要将来加一个全局部门角色，自己写的过滤就会漏掉它。）
        assignable = {
            r.name: r.role_id
            for r in await role_store.list_roles_for_org(org_id)
            if r.name not in (ROLE_SUPER_ADMIN, ROLE_ORG_ADMIN)
        }
        return _acct_import.ImportContext(
            actor_org_id=org_id,
            assignable_roles=assignable,
            existing_users=await user_store.get_org_ids_for_usernames(usernames),
            seat_limit=await org_store.get_seat_limit(org_id),
            seats_used=await user_store.count_active_users(org_id),
        )

    @app.put("/api/v1/admin/users/{user_id}/disabled", response_model=AdminUserResponse)
    async def admin_set_user_disabled(
        user_id: str,
        request: SetUserDisabledRequest,
        current_user: AuthenticatedUser = Depends(_require_user_admin_tier),
        _same_org: AuthenticatedUser = Depends(require_same_org_or_platform),
    ) -> AdminUserResponse:
        """停用 / 重新启用。企业管理员能做的"离职处理"就是这个（设计 §4.2）。

        ⚠️ 生效时机不对称，见 `CLAUDE.md` §3.2：管理端 19 个端点立刻生效，
        问答等 35 个端点最长 24 小时。新登录会被立刻拒（`authenticate`），
        所以窗口有界。
        """
        if user_id == current_user.user_id and request.disabled:
            # 停用自己会立刻把自己锁在管理后台外面，且没有第二个人能救
            # （企业里可能只有一个管理员）。
            raise HTTPException(status_code=400, detail="不能停用自己")

        user = await user_store.get_user_by_id(user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="用户不存在")

        # ⚠️ **重新启用也要过席位校验** —— 三个校验点里最容易漏的一个：
        # 它不创建账号，却会让占用数 +1（设计 §4.4）。
        if not request.disabled:
            target_org = await org_store.get_org_for_user(user_id)
            if target_org:
                await _enforce_seat_capacity(target_org.org_id, delta=1)

        await user_store.set_disabled(user_id, request.disabled)
        await _audit_log(
            current_user.user_id,
            "disable_user" if request.disabled else "enable_user",
            "user", user_id, {"username": user.username},
        )
        refreshed = await user_store.get_user_by_id(user_id)
        return await _build_admin_user_response(refreshed or user)

    @app.put("/api/v1/admin/organizations/{org_id}/seat-limit", response_model=AdminOrganizationResponse)
    async def admin_set_seat_limit(
        org_id: str,
        request: SetSeatLimitRequest,
        current_user: AuthenticatedUser = Depends(require_platform_admin),
    ) -> AdminOrganizationResponse:
        """⚠️ **仅平台管理员。** 席位是合同条款不是配置项——企业管理员能改
        自己企业的上限，这个功能就等于不存在（设计 §4.4）。"""
        try:
            ok = await org_store.set_seat_limit(org_id, request.seat_limit)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        if not ok:
            raise HTTPException(status_code=404, detail="企业不存在")
        await _audit_log(
            current_user.user_id, "set_seat_limit", "organization", org_id,
            {"seat_limit": request.seat_limit},
        )
        org = await org_store.get_organization(org_id)
        return AdminOrganizationResponse(
            org_id=org.org_id, name=org.name, is_platform=org.is_platform,
            created_at=org.created_at, seat_limit=org.seat_limit,
            seats_used=await user_store.count_active_users(org_id),
            aiops_module_enabled=await ops_store.is_module_enabled(org_id),
        )

    @app.post("/api/v1/admin/users/bulk-import", response_model=BulkImportResponse)
    async def admin_bulk_import_users(
        file: UploadFile = File(...),
        validate_only: bool = Form(default=True),
        current_user: AuthenticatedUser = Depends(_require_user_admin_tier),
    ) -> BulkImportResponse:
        """CSV 批量导入（设计 §4.1）。

        ⚠️ **`validate_only` 默认 True。** 这是一个能一次影响上万账号的操作，
        默认值必须是"什么都不做"——前端漏传、curl 手敲、脚本写错，任何一种
        意外都只会得到一份预演报告，而不是一万个账号。真跑必须显式说要真跑。

        **预演和真跑走同一个 `plan_import`**，只是前者算完就返回。
        如果两者走不同代码路径，预演就保证不了真跑会发生同样的事。

        企业归属**强制用调用者自己的**，请求里没有任何地方能指定它 ——
        跟 `admin_create_user` 同一条防护，批量导入是同一个越权面。
        """
        actor_org = await org_store.get_org_for_user(current_user.user_id)
        if actor_org is None:
            raise HTTPException(status_code=400, detail="当前账号没有所属企业，无法导入")

        raw = await file.read()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            try:
                # Excel「另存为 CSV」在中文 Windows 上默认就是 GBK，
                # 直接报"文件编码不对"等于把问题丢回给不懂编码的人事同事。
                text = raw.decode("gbk")
            except UnicodeDecodeError:
                raise HTTPException(
                    status_code=400,
                    detail="文件编码无法识别，请另存为 UTF-8 或 GBK 编码的 CSV",
                )

        # 先扫一遍拿 username 列去查归属；解析失败时 rows 为空，
        # plan_import 会把同一个 fatal 再报一次，这里不用重复处理。
        pre_rows, _ = _acct_import.parse_csv(text)
        usernames = [r.get("username", "").strip() for r in pre_rows]
        ctx = await _import_context(current_user, actor_org.org_id, [u for u in usernames if u])
        plan = _acct_import.plan_import(text, ctx)

        def _resp(applied: bool, creds: list) -> BulkImportResponse:
            return BulkImportResponse(
                applied=applied,
                summary=_acct_import.format_dry_run_summary(plan),
                to_create=len(plan.to_create), to_update=len(plan.to_update),
                errors=[
                    BulkImportRowResult(
                        line_no=e.line_no, username=e.username,
                        action=e.action.value, reason=e.reason,
                    ) for e in plan.errors
                ],
                seat_ok=plan.seat_check.ok, seats_used=plan.seat_check.seats_used,
                seat_limit=plan.seat_check.seat_limit, fatal_error=plan.fatal_error,
                credentials=creds,
            )

        if validate_only or not plan.applicable:
            await _audit_log(
                current_user.user_id, "bulk_import_users_dry_run", "organization",
                actor_org.org_id,
                {"to_create": len(plan.to_create), "to_update": len(plan.to_update),
                 "errors": len(plan.errors), "seat_ok": plan.seat_check.ok},
            )
            return _resp(applied=False, creds=[])

        now = time.time()
        credentials: list = []
        for row in plan.to_create:
            code, code_hash, expires = _activation.issue_activation(now)
            try:
                user = await user_store.create_pending_user(
                    username=row.username, activation_code_hash=code_hash,
                    activation_expires_at=expires, org_id=actor_org.org_id,
                )
            except ValueError:
                # 两次并发导入，或者 plan 算完到这里之间有人手工建了同名账号。
                # 逐行隔离：跳过这一行，其余照做，不整体回滚。
                continue
            if row.role_id:
                await role_store.assign_user_roles(user.user_id, [row.role_id])
            credentials.append(AdminCreatedUserCredential(
                username=row.username, activation_code=code, expires_at=expires,
            ))

        for row in plan.to_update:
            # ⚠️ **更新只动角色，绝不碰密码/激活状态**（设计 T-5）。
            # 幂等的意义就是"修正后重传整份文件是安全的"，如果重传会重置密码，
            # 一次误传就能把全公司锁在外面。
            existing = await user_store.get_user_by_username(row.username)
            if existing and row.role_id:
                await role_store.assign_user_roles(existing.user_id, [row.role_id])

        await _audit_log(
            current_user.user_id, "bulk_import_users", "organization", actor_org.org_id,
            {"created": len(credentials), "updated": len(plan.to_update),
             "errors": len(plan.errors)},
        )
        return _resp(applied=True, creds=credentials)

    @app.post("/api/v1/admin/users", response_model=AdminUserResponse)
    async def admin_create_user(
        request: AdminCreateUserRequest,
        current_user: AuthenticatedUser = Depends(_require_user_admin_tier),
    ) -> AdminUserResponse:
        # 平台管理员可以把新用户指派给任意企业（不传就落到自己所在的企业）；
        # 企业管理员不管请求体里传了什么 org_id，一律强制用自己的——不能靠
        # 改请求体把新账号建到别的企业名下。目标企业要先算出来，因为角色校验
        # （能不能给这家企业的人分配这些角色）依赖这个结果。
        is_platform = await org_store.is_platform_admin(current_user.user_id)
        actor_org = await org_store.get_org_for_user(current_user.user_id)
        target_org_id = request.org_id if (is_platform and request.org_id) else (actor_org.org_id if actor_org else None)

        # 一人一角色：业务规则改成单角色制之后，user_roles 表结构本身不变
        # （多对多，历史遗留的多角色用户不强制迁移，见 role_store.py 顶部
        # 说明），但新的写路径一律只接受 0 或 1 个 role_id——UI 也同步从
        # 多选框改成单选框，这里是后端兜底，不信任前端不会被绕过。
        if len(request.role_ids) > 1:
            raise HTTPException(status_code=400, detail="每个用户只能有一个角色，请只选一个")

        if request.role_ids:
            await _validate_role_assignment(current_user, target_org_id, request.role_ids)
            unknown = [rid for rid in request.role_ids if await role_store.get_role_by_id(rid) is None]
            if unknown:
                raise HTTPException(status_code=400, detail=f"角色不存在: {sorted(unknown)}")

        # 席位（docs/account_lifecycle_design.md §4.4）。三个校验点之一，
        # 另两个是批量导入与「重新启用已停用用户」。口径由 count_active_users
        # 保证：只数 disabled_at IS NULL 的，停用的人不占席位。
        if target_org_id:
            await _enforce_seat_capacity(target_org_id, delta=1)

        try:
            user = await user_store.create_user(request.username, request.password)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        if request.role_ids:
            await role_store.assign_user_roles(user.user_id, request.role_ids)
        if target_org_id:
            await org_store.set_user_organization(user.user_id, target_org_id)

        await _audit_log(
            current_user.user_id, "create_user", "user", user.user_id,
            {"username": user.username, "role_ids": request.role_ids, "org_id": target_org_id},
        )
        return await _build_admin_user_response(user)

    @app.delete("/api/v1/admin/users/{user_id}")
    async def admin_delete_user(
        user_id: str,
        current_user: AuthenticatedUser = Depends(require_platform_admin),
    ) -> dict:
        """⚠️ **2026-08-26 起仅平台管理员可用**（设计 §4.2，O-3 已拍板）。

        原来挂的是 `_require_user_admin_tier`，企业管理员能删自己企业的员工。
        改掉的理由是这两件事性质不同：停用是**人事操作**，企业自己天天要做；
        删除是**不可逆的数据销毁**——`conversations.user_id` 会一起失去归属，
        "离职员工做过什么"就再也追溯不到了。企业管理员现在只能停用
        （`admin_set_user_disabled`），删除只保留给真正的数据清除请求
        （如 GDPR 删除权），需要一次跨组织的确认。

        `require_same_org_or_platform` 一并去掉：平台管理员本来就跨企业，
        那个依赖对他恒真，留着只是多两次查询。
        """
        if user_id == current_user.user_id:
            raise HTTPException(status_code=400, detail="不能删除自己")
        found = await user_store.delete_user(user_id)
        if not found:
            raise HTTPException(status_code=404, detail="用户不存在")
        await _audit_log(current_user.user_id, "delete_user", "user", user_id, {})
        return {"success": True}

    @app.put("/api/v1/admin/users/{user_id}/roles", response_model=AdminUserResponse)
    async def admin_set_user_roles(
        user_id: str,
        request: SetUserRolesRequest,
        current_user: AuthenticatedUser = Depends(_require_user_admin_tier),
        _same_org: AuthenticatedUser = Depends(require_same_org_or_platform),
    ) -> AdminUserResponse:
        # 一人一角色，见上面 admin_create_user 旁的说明——历史遗留的多角色
        # 用户（如种子数据里的 bob）不强制清理，但下一次有人经这个端点改动
        # 他的角色，就只能落到 0 或 1 个，不能带着老的多角色继续加新角色。
        if len(request.role_ids) > 1:
            raise HTTPException(status_code=400, detail="每个用户只能有一个角色，请只选一个")

        user = await user_store.get_user_by_id(user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="用户不存在")

        unknown = [rid for rid in request.role_ids if await role_store.get_role_by_id(rid) is None]
        if unknown:
            raise HTTPException(status_code=400, detail=f"角色不存在: {sorted(unknown)}")

        target_org = await org_store.get_org_for_user(user_id)
        current_roles = await role_store.get_user_roles(user_id)
        current_role_ids = {r.role_id for r in current_roles}
        existing_role_names = frozenset(r.name for r in current_roles)
        await _validate_role_assignment(
            current_user, target_org.org_id if target_org else None, request.role_ids,
            existing_role_names=existing_role_names,
        )

        if user_id == current_user.user_id:
            # 防止超级管理员误操作把自己的 super_admin 角色摘掉，导致管理后台再也进不去
            super_admin_role = await role_store.get_role_by_name(ROLE_SUPER_ADMIN)
            if (
                super_admin_role
                and super_admin_role.role_id in current_role_ids
                and super_admin_role.role_id not in request.role_ids
            ):
                raise HTTPException(status_code=400, detail="不能取消自己的超级管理员角色")

        await role_store.assign_user_roles(user_id, request.role_ids)
        await _audit_log(
            current_user.user_id, "set_user_roles", "user", user_id,
            {"role_ids": request.role_ids},
        )
        return await _build_admin_user_response(user)

    # ==================== 审计日志 API（治理与合规） ====================
    # 记录谁在何时对哪个资源做了什么——管理后台变更操作（见上面各端点里的
    # `_audit_log(...)` 调用）+ 工具调用（知识库检索/考勤查询/工作流操作，
    # 见 RAGWorkflow 构造时传入的 audit_log 回调 -> subgraph.py tool_node）。
    # 权限跟"用户管理"同一档（_require_user_admin_tier）：平台管理员能看
    # 全平台记录（可选 org_id 过滤某一家企业），企业管理员只能看自己企业的
    # ——不管请求里传了什么 org_id，一律强制用自己的，跟 admin_create_user
    # 里"企业管理员不管请求体传了什么 org_id，一律用自己的"同一个模式。

    @app.get("/api/v1/admin/audit-logs", response_model=AuditLogListResponse)
    async def admin_list_audit_logs(
        org_id: Optional[str] = None,
        user_id: Optional[str] = None,
        action: Optional[str] = None,
        start: Optional[float] = None,
        end: Optional[float] = None,
        limit: int = 50,
        offset: int = 0,
        current_user: AuthenticatedUser = Depends(_require_user_admin_tier),
    ) -> AuditLogListResponse:
        if limit < 1 or limit > 200:
            raise HTTPException(status_code=400, detail="limit 必须在 1-200 之间")

        is_platform = await org_store.is_platform_admin(current_user.user_id)
        effective_org_id = org_id
        if not is_platform:
            actor_org = await org_store.get_org_for_user(current_user.user_id)
            effective_org_id = actor_org.org_id if actor_org else None

        entries, total = await audit_store.list_logs(
            org_id=effective_org_id, user_id=user_id, action=action,
            start=start, end=end, limit=limit, offset=offset,
        )
        org_names = {o.org_id: o.name for o in await org_store.list_organizations()} if is_platform else {}
        items = [
            AuditLogResponse(
                audit_id=e.audit_id, org_id=e.org_id, org_name=org_names.get(e.org_id),
                user_id=e.user_id, username=e.username, action=e.action,
                resource_type=e.resource_type, resource_id=e.resource_id,
                detail=e.detail, success=e.success, created_at=e.created_at,
            )
            for e in entries
        ]
        return AuditLogListResponse(items=items, total=total)

    # 注意：没有"改派用户所属企业"的端点——员工的企业归属只在创建时确定一次
    # （见 admin_create_user），创建之后任何管理员（包括平台管理员）都不能再
    # 改派，用户如果确实换了公司，只能删除旧账号、在新企业下重新创建。这是
    # 有意的产品决策：避免"企业 A 的管理员看着看着一个用户突然从列表里消失，
    # 因为被平台管理员偷偷过继给了企业 B"这类容易误解的操作。

    # ==================== 组织管理 API（仅平台管理员） ====================

    def _org_response(org, aiops_module_enabled: bool = False) -> AdminOrganizationResponse:
        """⚠️ **刻意不填 `seats_used`。**

        它要 `await user_store.count_active_users(org_id)`，而这个函数被企业
        列表逐个调用——填进去就是一个 N+1，正是 2026-08-26 刚在
        `admin_list_users` 上修掉的那类问题（P1-14，约 300 次串行查询）。
        列表页只给上限，用量在单个企业的详情/改上限响应里给。
        真要在列表上显示用量，得先写一个 `count_active_users_batch`。

        `aiops_module_enabled` 反过来——**特意作为参数传入，不在这里查**，
        因为调用方有的是"批量列表"场景（`admin_list_organizations`，需要
        批量查询避免 N+1）、有的是"我刚写完这个值，不需要再读一次"场景
        （`admin_set_aiops_module_enabled`），两种取值方式不一样，硬塞进
        这个纯同步的映射函数里反而两头都不讨好。
        """
        return AdminOrganizationResponse(
            org_id=org.org_id, name=org.name, is_platform=org.is_platform,
            created_at=org.created_at, seat_limit=org.seat_limit,
            aiops_module_enabled=aiops_module_enabled,
        )

    @app.get("/api/v1/admin/organizations", response_model=List[AdminOrganizationResponse])
    async def admin_list_organizations(
        current_user: AuthenticatedUser = Depends(_require_user_admin_tier),
    ) -> List[AdminOrganizationResponse]:
        # 平台管理员看全部组织；企业内 admin（含 org_admin）只能看到自己那一条
        # （给"新建用户"弹窗、个人信息展示等场景确认自己企业的名字用，不是把
        # 组织列表当成可浏览的目录露出去）。UserRoleAssignment.jsx 的
        # loadAll() 会无条件拉这个端点，之前网关只放 super_admin 进来，导致
        # org_admin 一进"用户与角色分配"页就在 Promise.all 里被这一路 403
        # 拖垮整体加载——网关本该跟下面的分支逻辑一样宽。
        if await org_store.is_platform_admin(current_user.user_id):
            orgs = await org_store.list_organizations()
        else:
            own_org = await org_store.get_org_for_user(current_user.user_id)
            orgs = [own_org] if own_org else []
        aiops_enabled_by_org = await ops_store.is_module_enabled_batch([o.org_id for o in orgs])
        return [_org_response(o, aiops_enabled_by_org.get(o.org_id, False)) for o in orgs]

    @app.post("/api/v1/admin/organizations", response_model=AdminOrganizationResponse)
    async def admin_create_organization(
        request: AdminCreateOrganizationRequest,
        current_user: AuthenticatedUser = Depends(_require_super_admin),
        __: AuthenticatedUser = Depends(require_platform_admin),
    ) -> AdminOrganizationResponse:
        org = await org_store.create_organization(request.name)
        await _audit_log(current_user.user_id, "create_organization", "organization", org.org_id, {"name": org.name})
        return _org_response(org)

    # ==================== 租户连接器 API（仅平台管理员） ====================
    # 见 knowledge-base-tenant-federation.md 第 3 节 / attendance-tenant-federation.md
    # 第 3 节——连接器配置里带 API token，只有平台管理员能看/改，企业内 admin 看不到，
    # 跟 admin_set_user_organization 用同一套双重网关（超级管理员 + 平台管理员）。

    async def _check_connector_health(c) -> str:
        """打开连接器面板时现查一次存活状态——不落库，纯展示用（第 5 节网关页
        的调用/失败计数才是持久化的运行时指标，这里只回答"这个 endpoint 现在
        通不通"）。internal_* 连接器没有独立的远端服务，不需要探活。"""
        if c.connector_type.startswith("internal"):
            return "internal"
        if not c.is_active:
            return "disabled"
        if not c.endpoint:
            return "unreachable"
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                resp = await client.get(f"{c.endpoint.rstrip('/')}/healthz")
            return "connected" if resp.status_code < 400 else "unreachable"
        except (httpx.TimeoutException, httpx.ConnectError, httpx.HTTPError):
            return "unreachable"

    async def _connector_response(c) -> TenantConnectorResponse:
        return TenantConnectorResponse(
            connector_id=c.connector_id, org_id=c.org_id, capability=c.capability,
            connector_type=c.connector_type, endpoint=c.endpoint,
            has_token=bool(c.auth_config.get("token")),
            remote_tool_name=c.remote_tool_name, field_mapping=c.field_mapping,
            is_active=c.is_active, created_at=c.created_at,
            health_status=await _check_connector_health(c),
        )

    @app.get("/api/v1/admin/organizations/{org_id}/connectors", response_model=List[TenantConnectorResponse])
    async def admin_list_tenant_connectors(
        org_id: str,
        _: AuthenticatedUser = Depends(_require_super_admin),
        __: AuthenticatedUser = Depends(require_platform_admin),
    ) -> List[TenantConnectorResponse]:
        if await org_store.get_organization(org_id) is None:
            raise HTTPException(status_code=404, detail="组织不存在")
        connectors = await tenant_connector_store.list_for_org(org_id)
        # P1-14 同类问题（N 次串行 await，这里是 HTTP 探活不是 SQL 查询）：
        # 逐个 await _connector_response 会让每个连接器的 2s 超时探活排队
        # 累加，改成并发发起。
        return list(await asyncio.gather(*[_connector_response(c) for c in connectors]))

    @app.put(
        "/api/v1/admin/organizations/{org_id}/connectors/{capability}",
        response_model=TenantConnectorResponse,
    )
    async def admin_upsert_tenant_connector(
        org_id: str,
        capability: str,
        request: UpsertTenantConnectorRequest,
        current_user: AuthenticatedUser = Depends(_require_super_admin),
        __: AuthenticatedUser = Depends(require_platform_admin),
    ) -> TenantConnectorResponse:
        org = await org_store.get_organization(org_id)
        if org is None:
            raise HTTPException(status_code=404, detail="组织不存在")

        # 平台自己这个组织，任何能力都不允许配置成委托外部服务（http_api/
        # http_webhook/...）——只能用 internal_* 这类本地实现。这不是"防止误配
        # 到某个客户企业"这么窄的事：平台运营方压根不应该有能力把自己的知识库/
        # 考勤查询委托给任何外部端点，包括客户自己的微服务——那等于让平台账号
        # 绕过 query_knowledge_hub.py 里"本地路径拒绝 tenant_*_kb"那道显式拦截，
        # 从"查询时拦"退化成只靠"没人这么配"兜底。这里在连接器配置这一层就把
        # 口子焊死，而不是指望调用方每次都拦对。
        if org.is_platform and not request.connector_type.startswith("internal"):
            raise HTTPException(
                status_code=403,
                detail="平台自身组织不能把任何能力委托给外部服务，只能使用 internal_* 类型的本地实现",
            )

        # token 留空表示"不修改现有凭证"——沿用已有连接器里存的那份，不是清空。
        auth_config: dict = {}
        if request.token:
            auth_config = {"token": request.token}
        else:
            # 用 list_for_org 而不是 get()——get() 会过滤 is_active=FALSE 的行，
            # 而这里恰恰可能是在重新启用一个之前被停用的连接器，此时也要找回
            # 它原来存的 token，不能因为 is_active 过滤查不到就把凭证清空。
            existing = next(
                (c for c in await tenant_connector_store.list_for_org(org_id) if c.capability == capability),
                None,
            )
            if existing is not None:
                auth_config = existing.auth_config

        connector = await tenant_connector_store.upsert(
            org_id=org_id,
            capability=capability,
            connector_type=request.connector_type,
            endpoint=request.endpoint,
            auth_config=auth_config,
            remote_tool_name=request.remote_tool_name,
            field_mapping=request.field_mapping,
            is_active=request.is_active,
        )
        await _audit_log(
            current_user.user_id, "upsert_connector", "connector", connector.connector_id,
            {"org_id": org_id, "capability": capability, "connector_type": request.connector_type, "is_active": request.is_active},
        )
        return await _connector_response(connector)

    # ==================== 网关监控 API（仅平台管理员） ====================
    # "网关"这个说法呼应 plan.md 里"API 网关"的设计初衷——我们自己并不跑一个
    # 物理网关进程，这里是数据面：把所有企业配置的外部微服务连接器（知识库/
    # 考勤）横向列出来，配合各自的存活状态和调用/失败计数，给平台运维一个
    # "现在有哪些外部微服务接进来了、健不健康、调用量多大"的全局视图。

    @app.get("/api/v1/admin/gateway/connectors", response_model=List[GatewayConnectorResponse])
    async def admin_gateway_connectors(
        _: AuthenticatedUser = Depends(_require_super_admin),
        __: AuthenticatedUser = Depends(require_platform_admin),
    ) -> List[GatewayConnectorResponse]:
        connectors = await tenant_connector_store.list_all()
        # 内置连接器（internal_chroma/internal_postgres）不是独立微服务，网关页
        # 只关心真正对外发请求的那些。
        connectors = [c for c in connectors if not c.connector_type.startswith("internal")]
        org_names = {o.org_id: o.name for o in await org_store.list_organizations()}

        # P1-14 同类问题：这是全平台所有外部连接器一起探活，逐个串行 await
        # 会让总耗时随连接器数量线性叠加（每个最多 2s 超时）。并发发起。
        health_statuses = await asyncio.gather(*[_check_connector_health(c) for c in connectors])
        results = [
            GatewayConnectorResponse(
                connector_id=c.connector_id, org_id=c.org_id,
                org_name=org_names.get(c.org_id, c.org_id),
                capability=c.capability, connector_type=c.connector_type, endpoint=c.endpoint,
                is_active=c.is_active, health_status=health,
                call_count=c.call_count, failure_count=c.failure_count,
                last_called_at=c.last_called_at, last_latency_ms=c.last_latency_ms, last_error=c.last_error,
            )
            for c, health in zip(connectors, health_statuses)
        ]
        return results

    # ==================== 智能运维模块 API（docs/aiops_module_design.md） ====================
    # 阶段二：管理面 CRUD 端点，接的是 ops_store.py 的存储层（阶段一）。
    # BYOC 连接器的实际运行时（WebSocket 心跳/联邦查询/AI 分析）、审批工作流、
    # LangGraph 接入均未实现——这里注册的连接器目前只是"元数据存在"，还不会
    # 真的连上任何客户系统。见 CLAUDE.md §5 该条"什么没做"。

    async def _require_aiops_enabled_org(current_user: AuthenticatedUser):
        """模块开关叠加在角色/ACL 校验之前的新一层（§4.1），不是替代——
        `_require_org_admin` 这层 Depends 已经先过了，这里再加一道"这家企业
        开没开通"的业务闸。跟 `_require_local_retrieval_org` 是同一个模式：
        返回调用方所属的 Organization，避免调用方再查一次。"""
        org = await org_store.get_org_for_user(current_user.user_id)
        if org is None:
            raise HTTPException(status_code=403, detail="账号未关联任何企业")
        if not await ops_store.is_module_enabled(org.org_id):
            raise HTTPException(status_code=403, detail="智能运维模块未对本企业开通，请联系平台管理员")
        return org

    async def _get_owned_connector(org_id: str, connection_id: str):
        """404 不是 403——不能让"这个连接器存在但不是你的"这个信息泄露给
        跨企业的调用方，跟 admin_delete_collection 的既有约定一致。"""
        connector = await ops_store.get_connector(connection_id)
        if connector is None or connector.org_id != org_id:
            raise HTTPException(status_code=404, detail="连接器不存在")
        return connector

    def _ops_connector_response(c) -> OpsConnectorResponse:
        return OpsConnectorResponse(
            connection_id=c.connection_id, org_id=c.org_id, name=c.name,
            system_type=c.system_type, connector_status=c.connector_status,
            last_heartbeat_at=c.last_heartbeat_at, created_by=c.created_by,
            approval_timeout_minutes=c.approval_timeout_minutes, created_at=c.created_at,
        )

    @app.post("/api/v1/admin/ops/connectors", response_model=OpsConnectorResponse)
    async def admin_register_ops_connector(
        request: RegisterOpsConnectorRequest,
        current_user: AuthenticatedUser = Depends(_require_org_admin),
    ) -> OpsConnectorResponse:
        org = await _require_aiops_enabled_org(current_user)
        try:
            timeout_minutes = aiops_scope.validate_approval_timeout_minutes(
                request.approval_timeout_minutes
            )
        except aiops_scope.InvalidApprovalTimeout as e:
            raise HTTPException(status_code=400, detail=str(e))
        connector = await ops_store.register_connector(
            org.org_id, request.name, request.system_type, current_user.user_id,
            approval_timeout_minutes=timeout_minutes,
        )
        await _audit_log(
            current_user.user_id, "register_ops_connector", "ops_connector", connector.connection_id,
            {"org_id": org.org_id, "name": request.name, "system_type": request.system_type},
        )
        return _ops_connector_response(connector)

    @app.get("/api/v1/admin/ops/connectors", response_model=List[OpsConnectorResponse])
    async def admin_list_ops_connectors(
        current_user: AuthenticatedUser = Depends(_require_org_admin),
    ) -> List[OpsConnectorResponse]:
        org = await _require_aiops_enabled_org(current_user)
        connectors = await ops_store.list_connectors_for_org(org.org_id)
        return [_ops_connector_response(c) for c in connectors]

    @app.post(
        "/api/v1/admin/ops/connectors/{connection_id}/register-token",
        response_model=OpsConnectorRegisterTokenResponse,
    )
    async def admin_generate_ops_connector_register_token(
        connection_id: str,
        current_user: AuthenticatedUser = Depends(_require_org_admin),
    ) -> OpsConnectorRegisterTokenResponse:
        """§10.1 步骤 1：org 管理员在连接器管理页生成 register_token，给客户
        环境的连接器进程用来发起第一次 WebSocket 握手。**明文只在这次响应里
        出现一次**，平台数据库只存哈希（`connector_session.hash_token`）。
        生成新的会顶掉这个连接器上一个还没握手成功的 token（`set_register_token`
        是 UPSERT），符合"管理员重新点一次生成，上一个就该失效"的直觉。"""
        org = await _require_aiops_enabled_org(current_user)
        await _get_owned_connector(org.org_id, connection_id)

        raw_token = connector_session.generate_register_token()
        expires_at = time.time() + connector_session.REGISTER_TOKEN_TTL_SECONDS
        await ops_store.set_register_token(connection_id, connector_session.hash_token(raw_token), expires_at)
        await _audit_log(
            current_user.user_id, "generate_ops_register_token", "ops_connector", connection_id, {},
        )
        return OpsConnectorRegisterTokenResponse(
            connection_id=connection_id, register_token=raw_token, expires_at=expires_at,
        )

    @app.websocket("/ws/ops/connector/register")
    async def ops_connector_register_ws(
        websocket: WebSocket,
        connection_id: str = Query(...),
        token: str = Query(...),
    ):
        """§10.1 的 WebSocket 注册握手 + 心跳 + refresh 循环。

        浏览器/客户端原生 WebSocket API 握手阶段不能带自定义 header，所以
        `connection_id` 与一次性 `register_token` 都走查询参数——跟
        `trace_websocket` 的 `?token=` 是同一个约定。**握手校验在
        `accept()` 之前完成**，不满足直接 `close()`，不建立连接（同一条
        既有安全教训：trace WebSocket 那次 P0 就是"先 accept 再鉴权"）。

        成功后连接进入长连接状态，处理两类帧：`heartbeat`（更新
        `last_heartbeat_at`）、`refresh`（refresh_token 轮换，检测到重放会
        撤销该连接器全部会话并断开，逼它重新走一遍这个握手）。

        ⚠️ **这里还没有 `query_request`/`exec_request` 帧的处理**——那需要
        `ConnectorTransport.query()` 把请求路由到这个活连接、等待关联的
        `query_result` 帧返回，是下一步要做的事，本次只做到"连接器能连上、
        能被判断在线"，见 `CLAUDE.md` §5 该条"未做的"。
        """
        conn = await ops_store.get_connector(connection_id)
        if conn is None:
            await websocket.close(code=4404)
            return
        if not await ops_store.is_module_enabled(conn.org_id):
            await websocket.close(code=4403)
            return

        token_state = await ops_store.get_register_token_state(connection_id)
        check = connector_session.check_register_token(
            stored_hash=token_state["token_hash"] if token_state else None,
            provided_token=token, used=token_state["used"] if token_state else False,
            expires_at=token_state["expires_at"] if token_state else None,
        )
        if not check.ok:
            await websocket.close(code=4401)
            return

        await websocket.accept()
        await ops_store.mark_register_token_used(connection_id)

        secret = connector_session.derive_connector_jwt_secret(get_jwt_secret())
        session_token = connector_session.create_connector_session_jwt(connection_id, conn.org_id, secret)
        refresh_token = connector_session.generate_refresh_token()
        await ops_store.issue_refresh_token(
            connection_id, connector_session.hash_token(refresh_token),
            ttl_seconds=connector_session.REFRESH_TOKEN_TTL_SECONDS,
        )
        await ops_store.record_heartbeat(connection_id)
        active_ops_connector_ws[connection_id] = websocket

        await websocket.send_json({
            "type": "registered", "id": str(uuid.uuid4()), "connector_id": connection_id,
            "ts": time.time(),
            "payload": {"session_token": session_token, "refresh_token": refresh_token},
        })

        try:
            while True:
                frame = await websocket.receive_json()
                frame_type = frame.get("type")

                if frame_type == "heartbeat":
                    await ops_store.record_heartbeat(connection_id)
                    await websocket.send_json({
                        "type": "heartbeat", "id": frame.get("id"), "connector_id": connection_id,
                        "ts": time.time(), "payload": {},
                    })

                elif frame_type == "refresh":
                    provided = (frame.get("payload") or {}).get("refresh_token", "")
                    provided_hash = connector_session.hash_token(provided)
                    rt_state = await ops_store.get_refresh_token_state(connection_id, provided_hash)
                    rcheck = connector_session.check_refresh_token(
                        stored_hash=rt_state["token_hash"] if rt_state else None,
                        provided_token=provided,
                        consumed_at=rt_state["consumed_at"] if rt_state else None,
                        expires_at=rt_state["expires_at"] if rt_state else None,
                    )
                    if rcheck.is_replay:
                        # §10.1：视为泄露信号，强制该连接器重新走注册流程——
                        # 撤销全部会话令牌并断开，不只是拒绝这一次刷新。
                        await ops_store.revoke_all_refresh_tokens(connection_id)
                        await websocket.send_json({
                            "type": "error", "id": frame.get("id"), "connector_id": connection_id,
                            "ts": time.time(),
                            "payload": {"reason": "refresh_token_replayed", "detail": "检测到已消费的 refresh_token 被重复使用，视为泄露信号，请重新注册"},
                        })
                        await websocket.close(code=4409)
                        break
                    if not rcheck.ok:
                        await websocket.send_json({
                            "type": "error", "id": frame.get("id"), "connector_id": connection_id,
                            "ts": time.time(), "payload": {"reason": "refresh_token_invalid"},
                        })
                        continue

                    await ops_store.consume_refresh_token(connection_id, provided_hash)
                    new_session_token = connector_session.create_connector_session_jwt(
                        connection_id, conn.org_id, secret,
                    )
                    new_refresh_token = connector_session.generate_refresh_token()
                    await ops_store.issue_refresh_token(
                        connection_id, connector_session.hash_token(new_refresh_token),
                        ttl_seconds=connector_session.REFRESH_TOKEN_TTL_SECONDS,
                    )
                    await websocket.send_json({
                        "type": "refresh", "id": frame.get("id"), "connector_id": connection_id,
                        "ts": time.time(),
                        "payload": {"session_token": new_session_token, "refresh_token": new_refresh_token},
                    })

                elif frame_type in ("query_result", "exec_result", "error"):
                    # 这些帧是连接器对之前 query_request/exec_request 的响应，
                    # 找到对应的等待中 Future 并 resolve——真正解析 payload 的
                    # 逻辑在 WebSocketConnectorTransport/WebSocketRemediationDispatcher
                    # 里（src/ops/connector_transport.py），这里不关心内容，
                    # 只按 id 转发。**没有匹配的 pending 项就静默丢弃**，不是
                    # bug：调用方可能已经超时放弃了这次等待（`_send_and_await_response`
                    # 的 finally 会清理 pending），连接器晚到的响应不该让整个
                    # WS 连接报错。
                    msg_id = frame.get("id")
                    fut = active_ops_pending_requests.get(msg_id) if msg_id else None
                    if fut is not None and not fut.done():
                        fut.set_result(frame)

                else:
                    await websocket.send_json({
                        "type": "error", "id": frame.get("id"), "connector_id": connection_id,
                        "ts": time.time(),
                        "payload": {"reason": "unsupported_frame_type", "detail": f"暂不支持的帧类型：{frame_type}"},
                    })
        except WebSocketDisconnect:
            pass
        finally:
            active_ops_connector_ws.pop(connection_id, None)
            await ops_store.mark_offline(connection_id)

    @app.put(
        "/api/v1/admin/organizations/{org_id}/aiops-module-enabled",
        response_model=AdminOrganizationResponse,
    )
    async def admin_set_aiops_module_enabled(
        org_id: str,
        request: SetAiopsModuleEnabledRequest,
        current_user: AuthenticatedUser = Depends(_require_super_admin),
        __: AuthenticatedUser = Depends(require_platform_admin),
    ) -> AdminOrganizationResponse:
        """§4.1：只有 super_admin 能切换，企业管理员看不到、改不了——跟连接器
        配置（§4 那张对照表）同一套边界，双重网关（超级管理员 + 平台管理员）
        跟 `admin_upsert_tenant_connector` 一致。"""
        org = await org_store.get_organization(org_id)
        if org is None:
            raise HTTPException(status_code=404, detail="组织不存在")
        await ops_store.set_module_enabled(org_id, request.enabled)
        await _audit_log(
            current_user.user_id, "set_aiops_module_enabled", "organization", org_id,
            {"enabled": request.enabled},
        )
        # 用刚写入的值直接回，不用再读一次数据库——我们本来就知道自己刚设的是
        # 什么，读回反而多一次可以省掉的查询（也顺带避免了理论上的"写后读到
        # 旧值"疑虑，虽然同一个连接内不会真的发生）。这是「刘德华」摸底运维
        # 塔台时发现的真实阻塞：这个端点原来的响应里压根没有这个字段，
        # PUT 之后前端没有任何办法确认这次点击是否真的生效。
        return _org_response(org, request.enabled)

    def _remediation_scope_response(s) -> RemediationScopeResponse:
        return RemediationScopeResponse(
            scope_id=s.scope_id, org_id=s.org_id, connection_id=s.connection_id,
            action_type=s.action_type, scope_config=s.scope_config,
            configured_by=s.configured_by, updated_at=s.updated_at,
        )

    @app.put(
        "/api/v1/admin/ops/connectors/{connection_id}/remediation-scopes/{action_type}",
        response_model=RemediationScopeResponse,
    )
    async def admin_upsert_remediation_scope(
        connection_id: str,
        action_type: str,
        request: UpsertRemediationScopeRequest,
        current_user: AuthenticatedUser = Depends(_require_org_admin),
    ) -> RemediationScopeResponse:
        """§3.3.1："谁能配置这份白名单——收紧为 org 管理员专属权限"。
        这是高风险操作（一份错误的白名单会持续影响此后所有次执行），
        `_require_org_admin` 已经是这条边界，不额外放宽给 role_ops_systems
        的 can_approve 持有者。"""
        org = await _require_aiops_enabled_org(current_user)
        try:
            aiops_scope.validate_action_type(action_type)
        except aiops_scope.InvalidActionType as e:
            raise HTTPException(status_code=400, detail=str(e))
        await _get_owned_connector(org.org_id, connection_id)  # 404 若不属于本企业
        scope = await ops_store.upsert_remediation_scope(
            org.org_id, connection_id, action_type, request.scope_config, current_user.user_id,
        )
        await _audit_log(
            current_user.user_id, "upsert_remediation_scope", "ops_remediation_scope", scope.scope_id,
            {"connection_id": connection_id, "action_type": action_type},
        )
        return _remediation_scope_response(scope)

    @app.get(
        "/api/v1/admin/ops/connectors/{connection_id}/remediation-scopes",
        response_model=List[RemediationScopeResponse],
    )
    async def admin_list_remediation_scopes(
        connection_id: str,
        current_user: AuthenticatedUser = Depends(_require_org_admin),
    ) -> List[RemediationScopeResponse]:
        org = await _require_aiops_enabled_org(current_user)
        await _get_owned_connector(org.org_id, connection_id)
        scopes = await ops_store.list_remediation_scopes(connection_id)
        return [_remediation_scope_response(s) for s in scopes]

    def _role_ops_permission_response(p) -> RoleOpsPermissionResponse:
        return RoleOpsPermissionResponse(
            role_id=p.role_id, connection_id=p.connection_id,
            can_view=p.can_view, can_approve=p.can_approve,
        )

    async def _require_grantable_role(actor_org_id: str, role_id: str):
        """§10.6：`role_ops_systems` 只能配给企业自建角色，两个内置系统角色
        （`super_admin`/`org_admin`）不允许配置——`org_admin` 已经是通配符，
        配了也不生效，一并挡掉避免误导；`super_admin` 是"从不自动获得任何
        连接器权限"这条铁律本身，允许给它配置就是打开一个后门。
        跟 `admin_set_role_collections` 校验角色归属的方式完全一致。"""
        role = await role_store.get_role_by_id(role_id)
        if role is None:
            raise HTTPException(status_code=404, detail="角色不存在")
        if role.is_system:
            raise HTTPException(status_code=403, detail="系统内置角色（super_admin/org_admin）不支持配置运维权限")
        if role.org_id is not None and role.org_id != actor_org_id:
            raise HTTPException(status_code=403, detail="只能给本企业的角色配置运维权限")
        return role

    @app.put(
        "/api/v1/admin/roles/{role_id}/ops-permissions/{connection_id}",
        response_model=RoleOpsPermissionResponse,
    )
    async def admin_set_role_ops_permission(
        role_id: str,
        connection_id: str,
        request: SetRoleOpsPermissionRequest,
        current_user: AuthenticatedUser = Depends(_require_org_admin),
    ) -> RoleOpsPermissionResponse:
        """把"能查看/能批准哪个连接器"授权给一个自定义角色——§10.6 细粒度
        审批权限的写入口，只有 org_admin 能配（跟白名单配置同一档权限）。"""
        org = await _require_aiops_enabled_org(current_user)
        await _require_grantable_role(org.org_id, role_id)
        await _get_owned_connector(org.org_id, connection_id)
        perm = await ops_store.set_role_ops_permission(
            role_id, connection_id, can_view=request.can_view, can_approve=request.can_approve,
        )
        await _audit_log(
            current_user.user_id, "set_role_ops_permission", "role_ops_systems", role_id,
            {"connection_id": connection_id, "can_view": perm.can_view, "can_approve": perm.can_approve},
        )
        return _role_ops_permission_response(perm)

    @app.delete("/api/v1/admin/roles/{role_id}/ops-permissions/{connection_id}")
    async def admin_revoke_role_ops_permission(
        role_id: str,
        connection_id: str,
        current_user: AuthenticatedUser = Depends(_require_org_admin),
    ) -> dict:
        org = await _require_aiops_enabled_org(current_user)
        await _require_grantable_role(org.org_id, role_id)
        await _get_owned_connector(org.org_id, connection_id)
        await ops_store.revoke_role_ops_permission(role_id, connection_id)
        await _audit_log(
            current_user.user_id, "revoke_role_ops_permission", "role_ops_systems", role_id,
            {"connection_id": connection_id},
        )
        return {"success": True}

    @app.get(
        "/api/v1/admin/ops/connectors/{connection_id}/permissions",
        response_model=List[RoleOpsPermissionResponse],
    )
    async def admin_list_role_ops_permissions(
        connection_id: str,
        current_user: AuthenticatedUser = Depends(_require_org_admin),
    ) -> List[RoleOpsPermissionResponse]:
        org = await _require_aiops_enabled_org(current_user)
        await _get_owned_connector(org.org_id, connection_id)
        perms = await ops_store.list_role_ops_permissions(connection_id)
        return [_role_ops_permission_response(p) for p in perms]

    def _remediation_action_response(a, scope_check_reason: Optional[str] = None) -> RemediationActionResponse:
        return RemediationActionResponse(
            action_id=a.action_id, org_id=a.org_id, connection_id=a.connection_id,
            proposed_by=a.proposed_by, intent=a.intent, plan=a.plan,
            impact_radius=a.impact_radius, status=a.status,
            approver_user_id=a.approver_user_id, approved_at=a.approved_at,
            executed_at=a.executed_at, result=a.result, rollback_plan=a.rollback_plan,
            outcome_effective=a.outcome_effective, created_at=a.created_at,
            scope_check_reason=scope_check_reason,
        )

    async def _get_owned_action(org_id: str, action_id: str):
        """跟 `_get_owned_connector` 同一个约定：404 不是 403。"""
        action = await ops_store.get_action(action_id)
        if action is None or action.org_id != org_id:
            raise HTTPException(status_code=404, detail="修复动作不存在")
        return action

    @app.post(
        "/api/v1/admin/ops/connectors/{connection_id}/remediation-actions",
        response_model=RemediationActionResponse,
    )
    async def admin_propose_remediation_action(
        connection_id: str,
        request: ProposeRemediationActionRequest,
        current_user: AuthenticatedUser = Depends(_require_org_admin),
    ) -> RemediationActionResponse:
        """§3.3.1 的核心拦截点：越界判定必须在进入 `pending_approval` 之前发生，
        不能流到审批人那一步才依赖人肉发现。这里的流程是
        `create_proposed_action`（总是先落一条 `proposed`）→
        `aiops_scope.check_target_in_scope` → 通过则 `advance_status` 到
        `pending_approval`，越界或没配白名单则转 `rejected_pre`——两条路径
        都会在 `remediation_actions` 表里留下记录，不是"判定失败就当没发生过"，
        审计需要看到"AI/管理员提议过这个、但被拦下了"这件事本身。

        ⚠️ **V1 还没有 AI 分析**（§3 待实现），`proposed_by` 目前只能是发起
        这次调用的 org_admin 本人——这是"手动提议"当占位符，不是设计终态，
        等 AI 分析阶段落地后 `proposed_by` 应该能是系统身份。"""
        org = await _require_aiops_enabled_org(current_user)
        try:
            aiops_scope.validate_action_type(request.action_type)
        except aiops_scope.InvalidActionType as e:
            raise HTTPException(status_code=400, detail=str(e))
        await _get_owned_connector(org.org_id, connection_id)

        action = await ops_store.create_proposed_action(
            org.org_id, connection_id, current_user.user_id, request.intent,
            request.plan, impact_radius=request.impact_radius, rollback_plan=request.rollback_plan,
        )

        scope = await ops_store.get_remediation_scope(connection_id, request.action_type)
        if scope is None:
            # 没有配置白名单 = 没有边界可言，默认拒绝，不是默认放行
            # （§8"明确不做的"：跳过审批的快速通道一律不留口子，这条是它的
            # 姊妹原则——没有约束的目标同样不给通过）。
            action = await ops_store.advance_status(action.action_id, STATUS_REJECTED_PRE)
            await _audit_log(
                current_user.user_id, "propose_remediation_action_rejected_no_scope",
                "remediation_action", action.action_id, {"connection_id": connection_id, "action_type": request.action_type},
            )
            return _remediation_action_response(
                action, scope_check_reason=f"连接器 '{connection_id}' 尚未为 '{request.action_type}' 配置修复范围白名单"
            )

        try:
            check = aiops_scope.check_target_in_scope(request.action_type, scope.scope_config, request.plan)
        except aiops_scope.InvalidScopeConfig as e:
            action = await ops_store.advance_status(action.action_id, STATUS_REJECTED_PRE)
            return _remediation_action_response(action, scope_check_reason=f"白名单配置本身有问题：{e}")

        if not check.allowed:
            action = await ops_store.advance_status(action.action_id, STATUS_REJECTED_PRE)
            await _audit_log(
                current_user.user_id, "propose_remediation_action_rejected_out_of_scope",
                "remediation_action", action.action_id,
                {"connection_id": connection_id, "action_type": request.action_type, "reason": check.reason},
            )
            return _remediation_action_response(action, scope_check_reason=check.reason)

        action = await ops_store.advance_status(action.action_id, STATUS_PENDING_APPROVAL)
        await _audit_log(
            current_user.user_id, "propose_remediation_action", "remediation_action", action.action_id,
            {"connection_id": connection_id, "action_type": request.action_type},
        )
        return _remediation_action_response(action)

    @app.get("/api/v1/admin/ops/remediation-actions", response_model=List[RemediationActionResponse])
    async def admin_list_remediation_actions(
        status: Optional[str] = None,
        current_user: AuthenticatedUser = Depends(get_current_user),
    ) -> List[RemediationActionResponse]:
        """网关从 `_require_org_admin` 放宽到"任意登录用户"——§10.6 细粒度
        权限落地后，持有 `role_ops_systems.can_view` 的非 org_admin 角色也该
        看得到（比如被指定为审批人但本身不是企业管理员）。真正的收窄发生在
        下面：org_admin 走 `viewable_connection_ids_for_user` 返回的 `None`
        （不过滤），其余角色按显式授权的 connection_id 集合过滤——没有任何
        授权时那个集合是空列表，`list_actions_for_org` 传空的
        `connection_ids` 必须返回空结果，不能被误当成"没传参数=不过滤"。"""
        org = await _require_aiops_enabled_org(current_user)
        viewable = await ops_store.viewable_connection_ids_for_user(current_user.user_id, org.org_id)
        if viewable is not None and not viewable:
            return []
        actions = await ops_store.list_actions_for_org(org.org_id, status=status)
        if viewable is not None:
            viewable_set = set(viewable)
            actions = [a for a in actions if a.connection_id in viewable_set]
        return [_remediation_action_response(a) for a in actions]

    async def _require_can_approve(user_id: str, connection_id: str) -> None:
        perm = await ops_store.get_ops_permission(user_id, connection_id)
        if not perm["can_approve"]:
            raise HTTPException(status_code=403, detail="没有这个连接器的审批权限")

    @app.post(
        "/api/v1/admin/ops/remediation-actions/{action_id}/approve",
        response_model=RemediationActionResponse,
    )
    async def admin_approve_remediation_action(
        action_id: str,
        current_user: AuthenticatedUser = Depends(get_current_user),
    ) -> RemediationActionResponse:
        """§10.6 细粒度审批权限已接线：`org_admin` 仍是通配符（企业内全部
        连接器自动 can_approve），非 org_admin 角色必须先被显式授予
        `role_ops_systems.can_approve` 才能批准这个连接器上的动作——不再是
        "任何 org_admin 都能批"这一档粗粒度门禁。"""
        org = await _require_aiops_enabled_org(current_user)
        action = await _get_owned_action(org.org_id, action_id)
        await _require_can_approve(current_user.user_id, action.connection_id)
        try:
            action = await ops_store.approve_action(action.action_id, approver_user_id=current_user.user_id)
        except IllegalStatusTransition as e:
            raise HTTPException(status_code=409, detail=str(e))
        await _audit_log(
            current_user.user_id, "approve_remediation_action", "remediation_action", action.action_id, {},
        )
        return _remediation_action_response(action)

    @app.post(
        "/api/v1/admin/ops/remediation-actions/{action_id}/reject",
        response_model=RemediationActionResponse,
    )
    async def admin_reject_remediation_action(
        action_id: str,
        current_user: AuthenticatedUser = Depends(get_current_user),
    ) -> RemediationActionResponse:
        """拒绝跟批准走同一档权限（`can_approve`），不是 `can_view`——能看不
        代表能拍板，"只能看不能定"的用户不该有否决权，这条跟批准对称。"""
        org = await _require_aiops_enabled_org(current_user)
        action = await _get_owned_action(org.org_id, action_id)
        await _require_can_approve(current_user.user_id, action.connection_id)
        try:
            action = await ops_store.advance_status(action.action_id, STATUS_REJECTED)
        except IllegalStatusTransition as e:
            raise HTTPException(status_code=409, detail=str(e))
        await _audit_log(
            current_user.user_id, "reject_remediation_action", "remediation_action", action.action_id, {},
        )
        return _remediation_action_response(action)

    # ⚠️ 未实现：mark_executing / mark_result 没有对应端点——真正执行需要
    # BYOC 连接器的运行时（§10.1 WebSocket 协议）把"已批准的执行计划"发给
    # 客户环境本地执行，那部分完全没做，approved 状态目前是终点，走不到
    # executing。见 CLAUDE.md §5 该条"未做的"。

    # ==================== 运营仪表盘 API（仅平台管理员，见 dashboard_stats.py） ====================
    # _require_platform_tier 目前只剩 super_admin 一档（2026-08-24 起平台侧
    # 废弃 admin 角色，见文件顶部说明），单独保留这个 Depends 只是为了语义
    # 独立；require_platform_admin 是第二层校验（组织归属，双重校验同一套
    # 模式），确保只有 org.is_platform=True 的账号能进来。这是平台整体的运营
    # 指标（会话数/消息数/活跃用户/响应延迟），不是任何企业的知识库内容，跟
    # "平台运营方不该看到企业内部知识库数据"这条边界不冲突，但仍然只暴露给
    # 平台管理员，企业管理员（org_admin）看不到跨企业的平台整体数据。前端
    # 对应的可见性判断见 App.jsx 的 PLATFORM_ADMIN_ROLE_NAMES。

    def _pct_change(current: Optional[float], previous: Optional[float]) -> Optional[float]:
        if current is None or not previous:
            return None
        return round((current - previous) / previous * 100, 1)

    @app.get("/api/v1/admin/dashboard/overview", response_model=DashboardOverviewResponse)
    async def admin_dashboard_overview(
        window: str = "7d",
        _: AuthenticatedUser = Depends(_require_platform_tier),
        __: AuthenticatedUser = Depends(require_platform_admin),
    ) -> DashboardOverviewResponse:
        if window not in ("24h", "7d", "30d"):
            raise HTTPException(status_code=400, detail="window 必须是 24h/7d/30d 之一")
        try:
            overview = await dashboard_stats_service.get_overview(window)
        except asyncpg.exceptions.UndefinedTableError:
            # 全新部署、一次对话都没发生过——conversations/conversation_archive
            # 表还没被 ConversationStore/ConversationArchiveStore 建出来，不是
            # 错误，是"暂无数据"，返回全零。
            return DashboardOverviewResponse(
                window=window, session_count=0, message_count=0, active_users=0,
            )
        return DashboardOverviewResponse(
            window=window,
            session_count=overview.session_count,
            session_count_change=_pct_change(overview.session_count, overview.session_count_prev),
            message_count=overview.message_count,
            message_count_change=_pct_change(overview.message_count, overview.message_count_prev),
            active_users=overview.active_users,
            active_users_change=_pct_change(overview.active_users, overview.active_users_prev),
            avg_latency_ms=overview.avg_latency_ms,
            avg_latency_ms_change=_pct_change(overview.avg_latency_ms, overview.avg_latency_ms_prev),
        )

    @app.get("/api/v1/admin/dashboard/trend", response_model=DashboardTrendResponse)
    async def admin_dashboard_trend(
        metric: str,
        window: str = "7d",
        _: AuthenticatedUser = Depends(_require_platform_tier),
        __: AuthenticatedUser = Depends(require_platform_admin),
    ) -> DashboardTrendResponse:
        if window not in ("24h", "7d", "30d"):
            raise HTTPException(status_code=400, detail="window 必须是 24h/7d/30d 之一")
        if metric not in ("sessions", "messages", "active_users", "latency"):
            raise HTTPException(status_code=400, detail="metric 必须是 sessions/messages/active_users/latency 之一")
        try:
            points = await dashboard_stats_service.get_trend(metric, window)
        except asyncpg.exceptions.UndefinedTableError:
            points = []
        return DashboardTrendResponse(
            metric=metric, window=window,
            points=[DashboardTrendPointResponse(bucket=p.bucket, value=p.value) for p in points],
        )

    # ==================== 成本与质量可观测性 API（仅平台管理员） ====================
    # token 用量来自 conversation_archive（_generate_node 每轮写入，见
    # workflow.py _extract_token_usage 旁的说明），工具调用成功率来自
    # audit_logs（治理与合规那组端点/回调顺手落的审计记录，见上面
    # `_audit_log`）——两类数据本来就是为各自的需求（成本追踪/审计）落库的，
    # 这里只是复用，没有为了这块仪表盘新增埋点。权限跟运营仪表盘概览同一档
    # （_require_platform_tier + require_platform_admin），企业管理员看不到
    # 跨企业的平台整体数据。

    # 单价参考自各家官网公开报价（USD / 1M tokens，(输入单价, 输出单价)），
    # 不是实时价格、也不代表任何合同折扣——只用于给运营方一个数量级参考。
    # key 用小写子串匹配 settings.llm.model，匹配不到就不显示成本（不编一个
    # 不知道对不对的数字），比如本地跑的 qwen2.5:7b 不在表里，只展示 token
    # 用量，不展示"预估成本"。
    _MODEL_PRICE_PER_1M_USD: Dict[str, tuple] = {
        "gpt-4o-mini": (0.15, 0.6),
        "gpt-4o": (2.5, 10.0),
        "gpt-4-turbo": (10.0, 30.0),
        "gpt-3.5-turbo": (0.5, 1.5),
        "deepseek-chat": (0.27, 1.1),
        "deepseek-reasoner": (0.55, 2.19),
    }

    def _estimate_cost_usd(prompt_tokens: int, completion_tokens: int) -> Optional[float]:
        # 本地 Ollama 模型没有按 token 计费的推理成本，直接是 0，不是"未知"。
        if settings.llm.provider == "ollama":
            return 0.0
        model_name = (settings.llm.model or "").lower()
        for key, (price_in, price_out) in _MODEL_PRICE_PER_1M_USD.items():
            if key in model_name:
                return round(prompt_tokens / 1_000_000 * price_in + completion_tokens / 1_000_000 * price_out, 4)
        return None

    @app.get("/api/v1/admin/dashboard/cost-overview", response_model=CostOverviewResponse)
    async def admin_dashboard_cost_overview(
        window: str = "7d",
        _: AuthenticatedUser = Depends(_require_platform_tier),
        __: AuthenticatedUser = Depends(require_platform_admin),
    ) -> CostOverviewResponse:
        if window not in ("24h", "7d", "30d"):
            raise HTTPException(status_code=400, detail="window 必须是 24h/7d/30d 之一")
        try:
            overview = await dashboard_stats_service.get_cost_overview(window)
        except asyncpg.exceptions.UndefinedTableError:
            # 全新部署，conversation_archive/audit_logs 都还没建出来——"暂无数据"，
            # 跟 admin_dashboard_overview 同一个兜底方式。
            return CostOverviewResponse(
                window=window, total_tokens=0, estimated_cost_usd=None,
                tool_call_count=0, tool_success_rate=None, tool_failure_count=0,
            )
        success_rate = (
            round(overview.tool_success_count / overview.tool_call_count * 100, 1)
            if overview.tool_call_count else None
        )
        success_rate_prev = (
            round(overview.tool_success_count_prev / overview.tool_call_count_prev * 100, 1)
            if overview.tool_call_count_prev else None
        )
        return CostOverviewResponse(
            window=window,
            total_tokens=overview.total_tokens,
            total_tokens_change=_pct_change(overview.total_tokens, overview.total_tokens_prev),
            estimated_cost_usd=_estimate_cost_usd(overview.prompt_tokens, overview.completion_tokens),
            tool_call_count=overview.tool_call_count,
            tool_success_rate=success_rate,
            tool_success_rate_change=_pct_change(success_rate, success_rate_prev),
            tool_failure_count=overview.tool_call_count - overview.tool_success_count,
        )

    @app.get("/api/v1/admin/dashboard/cost-trend", response_model=DashboardTrendResponse)
    async def admin_dashboard_cost_trend(
        metric: str,
        window: str = "7d",
        _: AuthenticatedUser = Depends(_require_platform_tier),
        __: AuthenticatedUser = Depends(require_platform_admin),
    ) -> DashboardTrendResponse:
        if window not in ("24h", "7d", "30d"):
            raise HTTPException(status_code=400, detail="window 必须是 24h/7d/30d 之一")
        if metric not in ("tokens", "tool_success_rate"):
            raise HTTPException(status_code=400, detail="metric 必须是 tokens/tool_success_rate 之一")
        try:
            points = await dashboard_stats_service.get_cost_trend(metric, window)
        except asyncpg.exceptions.UndefinedTableError:
            points = []
        return DashboardTrendResponse(
            metric=metric, window=window,
            points=[DashboardTrendPointResponse(bucket=p.bucket, value=p.value) for p in points],
        )

    # ==================== 角色管理 API ====================
    # 角色直接携带知识库权限（role_store.py 顶部说明），分两类，用 org_id 是否
    # 为空区分：
    #   全局角色（org_id=None）：系统权限档位，固定只有 super_admin/org_admin
    #     两个内置角色，super_admin 只能改展示名，不能新建/删除——运营商的
    #     角色（super_admin）本身就没有配置知识库的入口，"无知识库权限"是
    #     天然结果，不是额外拦出来的。全局角色曾经还支持"新建一个跨企业共用
    #     的部门身份"（设想给工作流审批用），但 2026-08-23 工作流审批人分配
    #     改成按企业独立配置（见 workflow_store.py 顶部说明），全局角色不再
    #     服务这个用途，也没有别的消费方，新建入口已经去掉（`admin_create_role`
    #     现在只接受企业角色）。
    #   企业角色（org_id 非空）：某家企业管理员自己建的、可以配置知识库关联的
    #     角色，只能操作自己企业的，只有该企业的 org_admin 能建/改名/删/配置
    #     知识库——是原来「知识库分组 API」的直接延续。
    # 列表读取对两层都开放（谁能不能真的把某个角色分配出去，由
    # admin_set_user_roles / admin_create_user 里的 _validate_role_assignment
    # 负责拦，这里只是读，读了不代表能用）。

    async def _actor_role_names(current_user: AuthenticatedUser) -> set:
        return {r.name for r in await role_store.get_user_roles(current_user.user_id)}

    def _role_response(role_obj: Role) -> RoleResponse:
        return RoleResponse(
            role_id=role_obj.role_id,
            name=role_obj.name,
            display_name=role_obj.display_name,
            is_system=role_obj.is_system,
            org_id=role_obj.org_id,
            collection_names=list(getattr(role_obj, "collection_names", [])),
            created_at=role_obj.created_at,
        )

    @app.get("/api/v1/admin/roles", response_model=List[RoleResponse])
    async def admin_list_roles(
        current_user: AuthenticatedUser = Depends(_require_user_admin_tier),
    ) -> List[RoleResponse]:
        if await org_store.is_platform_admin(current_user.user_id):
            roles = await role_store.list_roles()
            return [_role_response(r) for r in roles]
        org = await org_store.get_org_for_user(current_user.user_id)
        if org is None:
            return []
        roles = await role_store.list_roles_for_org(org.org_id)
        return [_role_response(r) for r in roles]

    async def _authorize_role_mutation(current_user: AuthenticatedUser, role: Role) -> None:
        """全局角色只有 super_admin 能改/删；企业角色只有该企业的 org_admin 能改/删。"""
        if role.org_id is None:
            if ROLE_SUPER_ADMIN not in await _actor_role_names(current_user):
                raise HTTPException(status_code=403, detail="只有超级管理员能修改平台角色")
            return
        actor_org = await org_store.get_org_for_user(current_user.user_id)
        if actor_org is None or actor_org.org_id != role.org_id:
            raise HTTPException(status_code=403, detail="只能操作本企业的角色")
        if ROLE_ORG_ADMIN not in await _actor_role_names(current_user):
            raise HTTPException(status_code=403, detail="只有企业管理员能修改本企业角色")

    @app.post("/api/v1/admin/roles", response_model=RoleResponse)
    async def admin_create_role(
        request: CreateRoleRequest,
        current_user: AuthenticatedUser = Depends(_require_org_admin),
    ) -> RoleResponse:
        """只建企业角色——建全局角色的入口已经去掉（见本节顶部说明），
        `_require_org_admin` 已经挡掉了平台管理员，这里不用再判断"是不是
        平台管理员"这个分支。"""
        actor_org = await org_store.get_org_for_user(current_user.user_id)
        if actor_org is None:
            raise HTTPException(status_code=403, detail="只有企业管理员能新建本企业角色")
        org_id = actor_org.org_id
        try:
            role = await role_store.create_role(org_id, request.name, request.display_name)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        await _audit_log(current_user.user_id, "create_role", "role", role.role_id, {"name": role.name, "org_id": org_id})
        return _role_response(role)

    @app.patch("/api/v1/admin/roles/{role_id}", response_model=RoleResponse)
    async def admin_update_role(
        role_id: str,
        request: UpdateRoleRequest,
        current_user: AuthenticatedUser = Depends(_require_user_admin_tier),
    ) -> RoleResponse:
        existing = await role_store.get_role_by_id(role_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="角色不存在")
        await _authorize_role_mutation(current_user, existing)
        role = await role_store.update_role(role_id, request.display_name)
        await _audit_log(current_user.user_id, "update_role", "role", role_id, {"display_name": request.display_name})
        # role_store.update_role 返回的是不带 collection_names 的裸 Role（改名
        # 不影响知识库关联，没必要每次都重新算），这里按角色自己的 org_id
        # 补一次查询，响应体里不要让"重命名"看起来把知识库关联清空了。
        if role.org_id is not None:
            roles = await role_store.list_roles_for_org(role.org_id)
            role = next((r for r in roles if r.role_id == role_id), role)
        return _role_response(role)

    @app.delete("/api/v1/admin/roles/{role_id}")
    async def admin_delete_role(
        role_id: str,
        current_user: AuthenticatedUser = Depends(_require_user_admin_tier),
    ) -> dict:
        existing = await role_store.get_role_by_id(role_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="角色不存在")
        await _authorize_role_mutation(current_user, existing)
        try:
            found = await role_store.delete_role(role_id)
        except ValueError as e:
            raise HTTPException(status_code=403, detail=str(e))
        if not found:
            raise HTTPException(status_code=404, detail="角色不存在")
        await _audit_log(current_user.user_id, "delete_role", "role", role_id, {})
        return {"success": True}

    @app.put("/api/v1/admin/roles/{role_id}/collections", response_model=RoleResponse)
    async def admin_set_role_collections(
        role_id: str,
        request: SetRoleCollectionsRequest,
        current_user: AuthenticatedUser = Depends(_require_org_admin),
    ) -> RoleResponse:
        """给角色配置知识库关联——只有 org_admin 能碰（运营商的角色没有这个
        入口，见文件顶部说明），且只按调用方自己的企业写 role_collections：
        企业角色只能配自己的；全局共用的部门角色（如 IT部）也能配，但只影响
        "调用方企业名下持有这个角色的人"，不会波及其他企业（role_collections
        按 (role_id, org_id) 隔离，见 role_store.py）。两个内置系统角色
        （super_admin/org_admin）不允许配置——"运营商的角色没有知识库权限"
        是业务规则本身，不是遗漏；org_admin 虽然不是运营商，但已经隐式拥有
        企业内全部知识库，配置了也不会生效，一并挡掉避免误导。"""
        role = await role_store.get_role_by_id(role_id)
        if role is None:
            raise HTTPException(status_code=404, detail="角色不存在")
        if role.is_system:
            raise HTTPException(status_code=403, detail="系统内置角色不支持配置知识库")
        actor_org = await org_store.get_org_for_user(current_user.user_id)
        if actor_org is None:
            raise HTTPException(status_code=403, detail="账号未关联任何企业")
        if role.org_id is not None and role.org_id != actor_org.org_id:
            raise HTTPException(status_code=403, detail="只能给本企业的角色配置知识库")
        await role_store.set_role_collections(role_id, actor_org.org_id, request.collection_names)
        await _audit_log(
            current_user.user_id, "set_role_collections", "role", role_id,
            {"collection_names": request.collection_names},
        )
        roles = await role_store.list_roles_for_org(actor_org.org_id)
        return _role_response(next(r for r in roles if r.role_id == role_id))

    async def _require_local_retrieval_org(current_user: AuthenticatedUser):
        """企业自建知识库只对"走本地 Chroma 检索"的企业开放（跟平台自己的 6 个
        部门库同一套检索机制）——像 Acme/Globex 这类把 knowledge_base 能力委托
        给自己微服务的企业（`tenant_connectors` 里配了 http_api 连接器），本地
        新建/关联的 collection 对它们的实际问答毫无意义（`query_knowledge_hub.py`
        的 `is_remote` 分支完全绕开本地检索，见该模块说明），所以在这里就地
        拒绝，报出清楚的原因，而不是让管理员建了一堆库、配了半天角色，结果
        员工提问永远用不上，自己也不知道为什么。返回调用方所属的 Organization，
        避免调用方再查一次。"""
        org = await org_store.get_org_for_user(current_user.user_id)
        if org is None:
            raise HTTPException(status_code=403, detail="账号未关联任何企业")
        connector = await tenant_connector_store.get(org.org_id, CAPABILITY_KNOWLEDGE_BASE)
        if connector is not None and connector.connector_type == CONNECTOR_TYPE_HTTP_API:
            raise HTTPException(
                status_code=400,
                detail="该企业的知识库检索已委托给企业自己的系统管理，不支持在平台内新增/配置知识库",
            )
        return org

    @app.get("/api/v1/admin/collections", response_model=List[CollectionResponse])
    async def admin_list_collections(
        current_user: AuthenticatedUser = Depends(_require_org_admin),
    ) -> List[CollectionResponse]:
        """列出本企业自建的知识库，供「知识库权限」页面"配置知识库"多选框、以及
        「新增知识库」页面的已有列表用。只对 org_admin 开放，且只返回调用方
        自己企业名下登记过的 collection（org_collections 表，见 collection_store.py）
        ——平台的 6 个固定部门库不在这张表里，也不会出现在这个列表中；别的
        企业自建的知识库同理看不到。委托模式企业（Acme/Globex）调这个端点会
        被 `_require_local_retrieval_org` 拒绝，报出清楚原因。"""
        org = await _require_local_retrieval_org(current_user)
        owned = await org_collection_store.list_for_org(org.org_id)
        # chunk_count 现查 Chroma——量级（企业自建库通常几个到几十个）不需要
        # 缓存，配合下面的分页查数据/删除功能，管理员一进页面就知道每个库有没有
        # 摄入内容、大概多大，不用点进去才发现是空的。
        stats = await _kb_management_tool.list_org_collection_stats(org.org_id)
        counts = {s["collection_name"]: s["chunk_count"] for s in stats}
        return [
            CollectionResponse(
                collection_name=c.collection_name, display_name=c.display_name,
                chunk_count=counts.get(c.collection_name, 0), created_at=c.created_at,
            )
            for c in owned
        ]

    @app.post("/api/v1/admin/collections", response_model=CollectionResponse)
    async def admin_create_collection(
        request: CreateCollectionRequest,
        current_user: AuthenticatedUser = Depends(_require_org_admin),
    ) -> CollectionResponse:
        """企业管理员新增一个知识库——只登记名字和归属企业，不做物理摄入（见
        collection_store.py 顶部说明，文档摄入仍走现有的摄入脚本/流程）。"""
        org = await _require_local_retrieval_org(current_user)

        # 内部标识不能撞平台保留名——`tenant_*_kb`（委托模式企业专属命名约定）、
        # `default`（历史遗留、不该有人再摄入内容的库，见 query_knowledge_hub.py
        # 顶部说明）、`conv_*`（每个对话私有），以及那 6 个知识库分组名字
        # （hr_admin_kb 等，现在专门用于委托模式企业的类目过滤，见
        # query_knowledge_hub.py DEPARTMENT_KB_GROUP_TO_REMOTE_CATEGORIES）——虽然
        # 平台自己已经没有同名的本地 collection 了，但企业自建库用同一个名字
        # 容易让人以为这个库参与了委托模式的类目过滤，实际上两者毫不相关，
        # 干脆继续保留这几个名字不让用。这里不查 org_collections 表里存不存在
        # ——存在的话下面 create() 的唯一约束自然会报错，不用在这里重复判断。
        _RESERVED_KB_GROUP_NAMES = {
            "hr_admin_kb", "finance_kb", "it_support_kb",
            "sales_marketing_kb", "rd_product_kb", "customer_success_kb",
        }
        name = request.collection_name.strip()
        reserved = (
            name in _RESERVED_KB_GROUP_NAMES
            or name == "default"
            or name.startswith("conv_")
            or (name.startswith("tenant_") and name.endswith("_kb"))
        )
        if reserved:
            raise HTTPException(status_code=400, detail=f"'{name}' 是平台保留的知识库标识，换一个试试")

        try:
            created = await org_collection_store.create(
                org_id=org.org_id,
                collection_name=request.collection_name.strip(),
                display_name=request.display_name.strip(),
                created_by=current_user.user_id,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        await _audit_log(
            current_user.user_id, "create_collection", "collection", created.collection_name,
            {"display_name": created.display_name, "org_id": org.org_id},
        )
        return CollectionResponse(
            collection_name=created.collection_name, display_name=created.display_name, created_at=created.created_at,
        )

    @app.get("/api/v1/admin/collections/{collection_name}/chunks", response_model=List[AdminKbChunkPreview])
    async def admin_list_collection_chunks(
        collection_name: str,
        offset: int = 0,
        limit: int = 20,
        current_user: AuthenticatedUser = Depends(_require_org_admin),
    ) -> List[AdminKbChunkPreview]:
        """分页查看一个知识库的原始 chunk 内容——「知识库权限」页面"查看数据"
        用，只对本企业自建库开放（`list_org_collection_chunks` 内部会校验
        collection 归属这家企业，跟 admin_list_collections 同一条边界）。"""
        org = await _require_local_retrieval_org(current_user)
        try:
            chunks = await _kb_management_tool.list_org_collection_chunks(
                org.org_id, collection_name, limit=limit, offset=offset,
            )
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))
        return [AdminKbChunkPreview(**c) for c in chunks]

    @app.delete("/api/v1/admin/collections/{collection_name}")
    async def admin_delete_collection(
        collection_name: str,
        current_user: AuthenticatedUser = Depends(_require_org_admin),
    ) -> dict:
        """企业管理员删除一个自建知识库——三步都要做，缺一步都会留下不一致的
        僵尸状态：1）物理数据（Chroma collection + BM25 索引 + 摄入去重历史，
        复用 clear_org_collection，它内部已经校验 collection 归属这家企业）；
        2）org_collections 里的归属登记；3）从所有角色的知识库关联里摘掉这个
        名字（role_store.remove_collection_everywhere，见该方法旁的说明——
        角色关联存的是裸字符串，不摘的话万一这个名字以后被重新注册，老角色会
        意外拿到新知识库的访问权限）。"""
        org = await _require_local_retrieval_org(current_user)
        try:
            cleared = await _kb_management_tool.clear_org_collection(org.org_id, collection_name)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))
        await org_collection_store.delete(collection_name)
        await role_store.remove_collection_everywhere(collection_name)
        await _audit_log(
            current_user.user_id, "delete_collection", "collection", collection_name,
            {"org_id": org.org_id, "cleared_chunks": cleared},
        )
        return {"success": True}

    # 知识库文档上传的进度状态——按 upload_id 存，供前端轮询进度条用。放内存
    # 里就够了（不需要跨进程/重启存活）：单进程 dev/demo 部署，且这本来就是
    # "这次上传现在到哪一步了"这种转瞬即逝的状态，不是需要审计追溯的持久数据
    # （持久的那份是 audit_log 里落库的最终结果）。
    _upload_progress: Dict[str, Dict[str, Any]] = {}

    # ==================== 知识库目录 + 文档上传 API（任意登录员工） ====================
    # 跟上面「企业自建知识库 API」的区别：那组是 org_admin 专属的管理入口；这组是
    # 给普通员工上传资料用的自助入口——列出自己企业的全部知识库（不管有没有
    # 权限都列出来，用 accessible 字段配合前端把没权限的选项置灰，而不是直接
    # 从列表里拿掉；这样员工至少知道"这个库存在，只是我看不了，得找管理员要
    # 权限"，而不是一头雾水地以为公司压根没建过这个库）。委托模式企业
    # （Acme/Globex）复用 `_require_local_retrieval_org` 直接拒绝——道理跟企业
    # 自建知识库那组端点一样：本地上传对它们的实际问答没有意义。

    async def _local_org_owned_collections(org) -> Dict[str, str]:
        """跟 query_knowledge_hub.py `_org_owned_collections` 保持同一个基准——
        平台自己（org_platform）没有任何本地业务知识库，其余企业是
        `org_collections` 表里登记的自建库，不处理委托模式（调用方在此之前
        已经用 `_require_local_retrieval_org` 拒绝了委托模式企业），返回
        {collection_name: display_name}。"""
        if org.is_platform:
            return {}
        owned = await org_collection_store.list_for_org(org.org_id)
        return {c.collection_name: c.display_name for c in owned}

    @app.get("/api/v1/collections/catalog", response_model=List[CollectionCatalogEntry])
    async def collections_catalog(
        current_user: AuthenticatedUser = Depends(get_current_user),
    ) -> List[CollectionCatalogEntry]:
        org = await _require_local_retrieval_org(current_user)
        owned = await _local_org_owned_collections(org)
        allowed = await user_store.get_allowed_collections(current_user.user_id)
        accessible_all = "*" in allowed
        return [
            CollectionCatalogEntry(
                collection_name=name, display_name=display_name,
                accessible=accessible_all or name in allowed,
            )
            for name, display_name in sorted(owned.items())
        ]

    async def _run_collection_ingest_task(
        upload_id: str, collection_name: str, dest_path: Path,
        user_id: str, org_id: str, original_name: str,
    ) -> None:
        """后台任务：真正跑摄入流水线，边跑边把进度写进 `_upload_progress`。

        `on_progress` 是 pipeline.run() 内部同步调的回调（跑在 asyncio.to_thread
        开的工作线程里），这里只是往一个 dict 里写几个字段，CPython 下这种粒度
        的写入不需要额外加锁——真出现读到"半更新"状态的极小概率窗口，最多是
        进度条数字抖一下，不影响正确性（不会读到跨请求串号的数据，dict 本身
        按 upload_id 隔离）。
        """
        def on_progress(stage: str, current: int, total: int) -> None:
            _upload_progress[upload_id].update(stage=stage, current=current, total=total)

        try:
            async with INGEST_SEMAPHORE:
                pipeline = IngestionPipeline(settings, collection=collection_name)
                result = await asyncio.to_thread(pipeline.run, file_path=str(dest_path), on_progress=on_progress)

            _upload_progress[upload_id].update(
                done=True, success=result.success, chunk_count=result.chunk_count,
                duplicate_chunk_count=result.duplicate_chunk_count,
                error=result.error if not result.success else None,
                stage="upsert", current=_upload_progress[upload_id]["total"],
            )
            await _audit_log(
                user_id, "upload_document", "collection", collection_name,
                {
                    "filename": original_name, "org_id": org_id,
                    "success": result.success, "chunk_count": result.chunk_count,
                    "duplicate_chunk_count": result.duplicate_chunk_count,
                },
                success=result.success,
            )
        except Exception as e:
            logger.exception("KB upload ingest task failed", extra={"upload_id": upload_id})
            _upload_progress[upload_id].update(done=True, success=False, error=str(e))
            await _audit_log(
                user_id, "upload_document", "collection", collection_name,
                {"filename": original_name, "org_id": org_id, "error": str(e)}, success=False,
            )

    @app.post("/api/v1/collections/{collection_name}/documents", response_model=UploadStartedResponse)
    async def upload_collection_document(
        collection_name: str,
        file: UploadFile = File(...),
        current_user: AuthenticatedUser = Depends(get_current_user),
    ) -> UploadStartedResponse:
        """把一份文档摄入到企业知识库共享 collection——跟 `/conversations/{id}/files`
        （摄入到私有的 conv_{id} collection）是两条独立的路径，不复用同一个端点：
        那边靠 `_require_conversation_owner` 判断"这是不是我自己的对话"，这里靠
        "这个 collection 是不是我自己企业名下的、我是不是真的有权限"两层判断，
        校验逻辑完全不是一回事。

        前端会把没权限的知识库选项置灰，但那只是 UX 提示——不能只靠前端不让
        选就默认后端安全，这里必须重新校验一遍，否则拼一个请求直接改
        collection_name 就能绕过置灰，往任意知识库塞内容。

        权限校验/存盘都是同步做完才返回 upload_id——真正耗时的摄入流水线
        （embedding/LLM 精炼这些）扔进后台任务，前端拿 upload_id 轮询
        `GET .../uploads/{upload_id}` 显示进度条，不用为了看进度把 HTTP
        连接一直挂着等。
        """
        org = await _require_local_retrieval_org(current_user)
        owned = await _local_org_owned_collections(org)
        if collection_name not in owned:
            raise HTTPException(status_code=404, detail="知识库不存在，或不属于你所在的企业")

        allowed = await user_store.get_allowed_collections(current_user.user_id)
        if "*" not in allowed and collection_name not in allowed:
            await _audit_log(
                current_user.user_id, "upload_document", "collection", collection_name,
                {"filename": file.filename, "org_id": org.org_id}, success=False,
            )
            raise HTTPException(status_code=403, detail="你没有权限上传到这个知识库")

        content = await file.read()
        if not content:
            raise HTTPException(status_code=400, detail="Empty file")

        original_name = file.filename or ""
        file_ext = Path(original_name).suffix.lower()
        if file_ext == '.doc':
            raise HTTPException(status_code=400, detail="旧版 .doc 格式暂不支持，请先转换为 .docx 后上传")
        if file_ext not in ALLOWED_EXTENSIONS:
            raise HTTPException(
                status_code=400,
                detail=f"不支持的文件格式: {file_ext}。支持: {', '.join(sorted(ALLOWED_EXTENSIONS))}",
            )

        # 落盘位置只按 collection 分目录，不像对话文件那样需要 ConversationFileStore
        # 追踪归属——这条路径没有"文件列表/删除单个文件"的管理需求（跟
        # collection_store.py 顶部说明一致：这层只关心"库存不存在、归哪家企业"，
        # 库里具体有哪些原始文件不是这组 API 的职责）。
        upload_dir = resolve_path(f"data/kb_uploads/{collection_name}")
        upload_dir.mkdir(parents=True, exist_ok=True)
        safe_name = f"{uuid.uuid4().hex}_{original_name}"
        dest_path = upload_dir / safe_name
        dest_path.write_bytes(content)

        upload_id = uuid.uuid4().hex
        # total 先给个 1，真实值等 pipeline 第一次回调 on_progress 才知道
        # （阶段总数由 pipeline.py 决定，这里不该硬编码一份可能会漂移的副本）——
        # 前端看到 total=1 且 done=false 就是"刚提交，还没收到第一个进度事件"。
        _upload_progress[upload_id] = {
            "collection_name": collection_name, "filename": original_name,
            "stage": "queued", "current": 0, "total": 1,
            "done": False, "success": None, "chunk_count": 0,
            "duplicate_chunk_count": 0, "error": None,
        }
        asyncio.create_task(_run_collection_ingest_task(
            upload_id, collection_name, dest_path, current_user.user_id, org.org_id, original_name,
        ))

        return UploadStartedResponse(upload_id=upload_id)

    @app.get("/api/v1/collections/uploads/{upload_id}", response_model=UploadProgressResponse)
    async def get_upload_progress(
        upload_id: str,
        current_user: AuthenticatedUser = Depends(get_current_user),
    ) -> UploadProgressResponse:
        state = _upload_progress.get(upload_id)
        if state is None:
            raise HTTPException(status_code=404, detail="上传任务不存在或已过期")
        return UploadProgressResponse(upload_id=upload_id, **state)

    # ==================== 委托模式企业知识库上传（方案 2："平台代算，企业只存储"，
    # 见 knowledge-base-tenant-federation.md 第 4.4 节） ====================
    # 跟上面 upload_collection_document（本地模式，按 org_collections 登记的
    # collection_name 路由）是两条独立入口，不共用同一个端点：委托模式的
    # "知识库"是整个企业一份，没有 collection 可选；本地模式的员工也不会走到
    # 这里——两条路径靠企业的连接器类型（internal_chroma vs http_api）互斥。
    #
    # 权限：委托模式下没有知识库分组那套细粒度 ACL（5.2 节"权限职责
    # 转移"——本来就没有比"是不是这家企业的人"更细的粒度可以在平台侧判断），
    # 所以只要求"属于这家企业"，不额外校验角色。

    REMOTE_INGEST_TIMEOUT_SECONDS = 60.0  # 写入比查询耗时更长，比 8s 的查询超时更宽松

    async def _require_delegated_retrieval_org(current_user: AuthenticatedUser):
        """委托模式上传的前置校验，跟 `_require_local_retrieval_org` 互斥对称：
        必须是配置了 http_api 连接器的企业才能走这条入口。返回 (org, connector)。"""
        org = await org_store.get_org_for_user(current_user.user_id)
        if org is None:
            raise HTTPException(status_code=403, detail="账号未关联任何企业")
        connector = await tenant_connector_store.get(org.org_id, CAPABILITY_KNOWLEDGE_BASE)
        if connector is None or connector.connector_type != CONNECTOR_TYPE_HTTP_API:
            raise HTTPException(
                status_code=400,
                detail="该企业的知识库检索没有委托给外部系统，请使用「新增知识库」里的本地上传入口",
            )
        return org, connector

    @app.post("/api/v1/tenant-kb/documents", response_model=TenantKbUploadResponse)
    async def upload_tenant_kb_document(
        file: UploadFile = File(...),
        category: Optional[str] = Form(default=None),
        current_user: AuthenticatedUser = Depends(get_current_user),
    ) -> TenantKbUploadResponse:
        """委托模式企业的员工上传文档：平台切块 + embedding（复用
        `IngestionPipeline` 里无状态的那几个组件，见
        src/ingestion/delegated_compute.py），推给企业自己的知识库微服务
        存储——平台这边不落任何本地向量/索引。同步等待整个链路完成才返回，
        大文件会比本地模式的异步轮询上传慢，这是当前版本的已知取舍，不是
        本地模式那套 upload_id 轮询进度条。

        `category`：这份文档归属企业内部哪个子库/分类，可选，原样透传给
        委托契约 `/v1/vectors`（4.4 节）——平台不校验取值范围，是不是合法
        类目、企业服务要不要认这个字段完全由对方决定，跟查询侧
        `DEPARTMENT_KB_GROUP_TO_REMOTE_CATEGORIES` 用的是同一份约定，但那是
        权限过滤用的映射，不是这里的校验依据。"""
        org, connector = await _require_delegated_retrieval_org(current_user)

        content = await file.read()
        if not content:
            raise HTTPException(status_code=400, detail="Empty file")

        original_name = file.filename or ""
        file_ext = Path(original_name).suffix.lower()
        if file_ext == '.doc':
            raise HTTPException(status_code=400, detail="旧版 .doc 格式暂不支持，请先转换为 .docx 后上传")
        if file_ext not in ALLOWED_EXTENSIONS:
            raise HTTPException(
                status_code=400,
                detail=f"不支持的文件格式: {file_ext}。支持: {', '.join(sorted(ALLOWED_EXTENSIONS))}",
            )

        upload_dir = resolve_path(f"data/kb_uploads/tenant_{org.org_id}")
        upload_dir.mkdir(parents=True, exist_ok=True)
        dest_path = upload_dir / f"{uuid.uuid4().hex}_{original_name}"
        dest_path.write_bytes(content)

        try:
            # 切块 + embedding 是阻塞/CPU 密集操作，扔进线程池，不卡住事件循环
            # （跟 query_knowledge_hub.py `_ensure_initialized` 的做法一致）。
            computed = await asyncio.to_thread(compute_chunks_for_delegation, load_settings(), str(dest_path))
        except Exception as e:
            await _audit_log(
                current_user.user_id, "upload_tenant_kb_document", "tenant_kb", org.org_id,
                {"filename": original_name, "org_id": org.org_id, "error": str(e)}, success=False,
            )
            raise HTTPException(status_code=500, detail=f"文档解析/编码失败: {e}")

        if not computed["chunks"]:
            return TenantKbUploadResponse(
                chunk_count=0, message="文档没有可摄入的内容（可能是空文件，或格式虽支持但提取不出正文）",
            )

        token = connector.auth_config.get("token", "")
        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=REMOTE_INGEST_TIMEOUT_SECONDS) as client:
                resp = await client.post(
                    f"{connector.endpoint.rstrip('/')}/v1/vectors",
                    json={"doc_id": computed["doc_id"], "chunks": computed["chunks"], "category": category},
                    headers={
                        "Authorization": f"Bearer {token}",
                        "X-Organization-Id": org.org_id,
                        "Content-Type": "application/json",
                    },
                )
            elapsed_ms = (time.monotonic() - t0) * 1000.0
            resp.raise_for_status()
            result = resp.json()
        except httpx.HTTPStatusError as e:
            elapsed_ms = (time.monotonic() - t0) * 1000.0
            await tenant_connector_store.record_call(connector.connector_id, False, elapsed_ms, str(e))
            await _audit_log(
                current_user.user_id, "upload_tenant_kb_document", "tenant_kb", org.org_id,
                {"filename": original_name, "org_id": org.org_id, "error": str(e)}, success=False,
            )
            raise HTTPException(status_code=502, detail="企业知识库暂时无法写入，请稍后再试")
        except (httpx.TimeoutException, httpx.ConnectError):
            elapsed_ms = (time.monotonic() - t0) * 1000.0
            await tenant_connector_store.record_call(connector.connector_id, False, elapsed_ms, "timeout_or_unreachable")
            await _audit_log(
                current_user.user_id, "upload_tenant_kb_document", "tenant_kb", org.org_id,
                {"filename": original_name, "org_id": org.org_id, "error": "timeout_or_unreachable"}, success=False,
            )
            raise HTTPException(status_code=502, detail="企业知识库暂时无法访问，请稍后再试")

        await tenant_connector_store.record_call(connector.connector_id, True, elapsed_ms, None)
        await _audit_log(
            current_user.user_id, "upload_tenant_kb_document", "tenant_kb", org.org_id,
            {
                "filename": original_name, "org_id": org.org_id,
                "chunk_count": result.get("chunk_count", 0), "category": category,
            },
        )
        return TenantKbUploadResponse(chunk_count=result.get("chunk_count", 0))

    # ==================== 工作流模板管理 API（仅超级管理员） ====================
    # work-flow.md 第 7 节：模板定义"某类流程需要哪些结构化字段"，附件材料只有
    # 一句提醒文案（attachments_note），不逐条建模校验——这是跨企业共用的表单
    # 结构，不含审批人信息。"这类流程谁来批"2026-08-23 起改成企业内部的事，
    # 由各企业管理员在自己的「审批设置」页面配置（见下面「工作流审批人分配
    # API」），平台这里不再管。

    def _workflow_template_response(template: WorkflowTemplate) -> WorkflowTemplateResponse:
        return WorkflowTemplateResponse(
            template_id=template.template_id,
            workflow_type=template.workflow_type,
            display_name=template.display_name,
            description=template.description,
            required_fields=template.required_fields,
            attachments_note=template.attachments_note,
            is_system=template.is_system,
            created_at=template.created_at,
        )

    @app.get("/api/v1/admin/workflow-templates", response_model=List[WorkflowTemplateResponse])
    async def admin_list_workflow_templates(
        _: AuthenticatedUser = Depends(_require_super_admin),
    ) -> List[WorkflowTemplateResponse]:
        templates = await workflow_store.list_templates()
        return [_workflow_template_response(t) for t in templates]

    @app.post("/api/v1/admin/workflow-templates", response_model=WorkflowTemplateResponse)
    async def admin_create_workflow_template(
        request: CreateWorkflowTemplateRequest,
        _: AuthenticatedUser = Depends(_require_super_admin),
    ) -> WorkflowTemplateResponse:
        try:
            template = await workflow_store.create_template(
                workflow_type=request.workflow_type,
                display_name=request.display_name,
                description=request.description,
                required_fields=[f.model_dump() for f in request.required_fields],
                attachments_note=request.attachments_note,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return _workflow_template_response(template)

    @app.patch("/api/v1/admin/workflow-templates/{template_id}", response_model=WorkflowTemplateResponse)
    async def admin_update_workflow_template(
        template_id: str,
        request: UpdateWorkflowTemplateRequest,
        _: AuthenticatedUser = Depends(_require_super_admin),
    ) -> WorkflowTemplateResponse:
        updates = request.model_dump(exclude_unset=True)
        kwargs: dict = {}
        if "display_name" in updates:
            kwargs["display_name"] = updates["display_name"]
        if "description" in updates:
            kwargs["description"] = updates["description"]
        if "required_fields" in updates:
            kwargs["required_fields"] = updates["required_fields"]
        if "attachments_note" in updates:
            kwargs["attachments_note"] = updates["attachments_note"]

        template = await workflow_store.update_template(template_id, **kwargs)
        if template is None:
            raise HTTPException(status_code=404, detail="流程模板不存在")
        return _workflow_template_response(template)

    @app.delete("/api/v1/admin/workflow-templates/{template_id}")
    async def admin_delete_workflow_template(
        template_id: str,
        _: AuthenticatedUser = Depends(_require_super_admin),
    ) -> dict:
        try:
            found = await workflow_store.delete_template(template_id)
        except ValueError as e:
            raise HTTPException(status_code=403, detail=str(e))
        if not found:
            raise HTTPException(status_code=404, detail="流程模板不存在")
        return {"success": True}

    # ==================== 工作流审批人分配 API（仅 org_admin，按企业隔离） ====================
    # "这类流程谁来批"是企业内部的事——同一个 workflow_type（比如"请假申请"）
    # 在不同企业应该能配不同的审批角色，只能由该企业自己的管理员配置，见
    # workflow_store.py 顶部说明。平台管理员不管这个（工作流模板本身的表单
    # 结构才归平台管，见上面「工作流模板管理 API」）。

    @app.get("/api/v1/admin/workflow-approvers", response_model=List[WorkflowApproverAssignmentResponse])
    async def admin_list_workflow_approvers(
        current_user: AuthenticatedUser = Depends(_require_org_admin),
    ) -> List[WorkflowApproverAssignmentResponse]:
        """列出全部流程类型 + 本企业当前给每个类型配的审批角色（没配就是
        null）——前端拿这份列表渲染"每类流程选一个审批角色"的表单。"""
        org = await org_store.get_org_for_user(current_user.user_id)
        if org is None:
            raise HTTPException(status_code=403, detail="账号未关联任何企业")
        templates = await workflow_store.list_templates()
        assignments = await workflow_store.list_org_approver_roles(org.org_id)
        role_ids = list({rid for rid in assignments.values() if rid})
        # N+1 审计发现（P1-14 同类问题）：原来对每个不重复 role_id 单独查一次。
        roles_by_id = await role_store.get_roles_by_ids_batch(role_ids)
        result = []
        for t in templates:
            approver_role_id = assignments.get(t.workflow_type)
            approver_role = roles_by_id.get(approver_role_id) if approver_role_id else None
            result.append(WorkflowApproverAssignmentResponse(
                workflow_type=t.workflow_type,
                display_name=t.display_name,
                approver_role_id=approver_role_id,
                approver_role_display_name=approver_role.display_name if approver_role else None,
            ))
        return result

    @app.put("/api/v1/admin/workflow-approvers/{workflow_type}", response_model=WorkflowApproverAssignmentResponse)
    async def admin_set_workflow_approver(
        workflow_type: str,
        request: SetWorkflowApproverRequest,
        current_user: AuthenticatedUser = Depends(_require_org_admin),
    ) -> WorkflowApproverAssignmentResponse:
        org = await org_store.get_org_for_user(current_user.user_id)
        if org is None:
            raise HTTPException(status_code=403, detail="账号未关联任何企业")
        template = await workflow_store.get_template_by_type(workflow_type)
        if template is None:
            raise HTTPException(status_code=404, detail="流程模板不存在")

        approver_role = None
        if request.approver_role_id is not None:
            approver_role = await role_store.get_role_by_id(request.approver_role_id)
            if approver_role is None:
                raise HTTPException(status_code=400, detail="审批角色不存在")
            # 只能指定本企业自己的角色——全局角色（部门身份、系统角色）不再
            # 作为审批人来源，工作流跟角色/知识库一样是"企业内部的事"，见
            # workflow_store.py 顶部说明。
            if approver_role.org_id != org.org_id:
                raise HTTPException(status_code=403, detail="只能指定本企业自己的角色作为审批人")

        await workflow_store.set_org_approver_role(org.org_id, workflow_type, request.approver_role_id)
        await _audit_log(
            current_user.user_id, "set_workflow_approver", "workflow_template", workflow_type,
            {"approver_role_id": request.approver_role_id},
        )
        return WorkflowApproverAssignmentResponse(
            workflow_type=workflow_type,
            display_name=template.display_name,
            approver_role_id=request.approver_role_id,
            approver_role_display_name=approver_role.display_name if approver_role else None,
        )

    # ==================== 工作流 API ====================
    # work-flow.md 第 7 节 + work-flow-web.md 第 3/6.2 节

    @app.get("/api/v1/workflow-templates", response_model=List[WorkflowTemplateResponse])
    async def list_workflow_templates(
        _: AuthenticatedUser = Depends(get_current_user),
    ) -> List[WorkflowTemplateResponse]:
        """轻量列表，登录用户可调（不要求 super_admin），供前端"发起工作流"
        入口的下拉框用。"""
        templates = await workflow_store.list_templates()
        return [_workflow_template_response(t) for t in templates]

    @app.get("/api/v1/workflow-templates/approvable-types", response_model=List[WorkflowTemplateResponse])
    async def list_approvable_workflow_types(
        current_user: AuthenticatedUser = Depends(get_current_user),
    ) -> List[WorkflowTemplateResponse]:
        """登录用户可调，只返回当前用户角色能审批的流程类型，用于"待我审批"
        Tab 的可见性判断，不要求 super_admin。"""
        org = await org_store.get_org_for_user(current_user.user_id)
        if org is None:
            return []
        role_ids = [r.role_id for r in await role_store.get_user_roles(current_user.user_id)]
        templates = await workflow_store.approvable_templates_for_org_and_role_ids(org.org_id, role_ids)
        return [_workflow_template_response(t) for t in templates]

    async def _build_workflow_instance_response(instance: WorkflowInstance) -> WorkflowInstanceResponse:
        template = await workflow_store.get_template_by_type(instance.workflow_type)
        requester = await user_store.get_user_by_id(instance.requester_user_id)
        # 材料就是发起该工作流那条对话里传的文件（work-flow.md 6.2 节），列表页
        # 展示"审批材料"这一列时不用再单独调一次接口，这里顺带把数量算出来。
        attachment_count = 0
        if instance.conversation_id:
            attachment_count = len(await file_store.list_files(instance.conversation_id))
        return WorkflowInstanceResponse(
            instance_id=instance.instance_id,
            workflow_type=instance.workflow_type,
            display_name=template.display_name if template else instance.workflow_type,
            requester_user_id=instance.requester_user_id,
            requester_username=requester.username if requester else None,
            conversation_id=instance.conversation_id,
            fields=instance.fields,
            status=instance.status,
            approver_user_id=instance.approver_user_id,
            approval_comment=instance.approval_comment,
            history=instance.history,
            attachment_count=attachment_count,
            created_at=instance.created_at,
            updated_at=instance.updated_at,
        )

    async def _require_workflow_access(
        instance_id: str, current_user: AuthenticatedUser, mode: str,
    ) -> WorkflowInstance:
        """`mode`: "owner" 必须是发起人；"approver" 必须持有"申请人所在企业"给
        该实例对应流程类型配的审批角色；"owner_or_approver" 满足其一即可。
        审批角色现在按企业配置（见 workflow_store.py 顶部说明），不能用静态的
        `require_role(*names)` 工厂，运行期按申请人所属企业现查后再判断。"""
        instance = await workflow_store.get_instance(instance_id)
        if instance is None:
            raise HTTPException(status_code=404, detail="工作流不存在")

        is_owner = instance.requester_user_id == current_user.user_id
        if mode == "owner":
            if not is_owner:
                raise HTTPException(status_code=403, detail="无权访问该工作流")
            return instance

        requester_org = await org_store.get_org_for_user(instance.requester_user_id)
        is_approver = False
        if requester_org is not None:
            approver_role_id = await workflow_store.get_org_approver_role_id(
                requester_org.org_id, instance.workflow_type,
            )
            if approver_role_id:
                role_ids = {r.role_id for r in await role_store.get_user_roles(current_user.user_id)}
                is_approver = approver_role_id in role_ids

        if mode == "approver":
            if not is_approver:
                raise HTTPException(status_code=403, detail="无权审批该工作流")
            return instance

        if not (is_owner or is_approver):
            raise HTTPException(status_code=403, detail="无权访问该工作流")
        return instance

    async def _transition_workflow(
        instance_id: str, new_status: str, actor_user_id: str, comment: Optional[str],
    ) -> WorkflowInstance:
        try:
            updated = await workflow_store.transition(
                instance_id, new_status, actor_user_id=actor_user_id, comment=comment, role_store=role_store,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        if updated is None:
            raise HTTPException(status_code=404, detail="工作流不存在")
        return updated

    @app.get("/api/v1/workflows", response_model=List[WorkflowInstanceResponse])
    async def list_my_workflows(
        status: Optional[str] = None,
        current_user: AuthenticatedUser = Depends(get_current_user),
    ) -> List[WorkflowInstanceResponse]:
        """我发起的工作流列表。"""
        instances = await workflow_store.list_instances_for_user(current_user.user_id, status=status)
        return [await _build_workflow_instance_response(i) for i in instances]

    @app.get("/api/v1/workflows/pending-approval", response_model=List[WorkflowInstanceResponse])
    async def list_pending_approval_workflows(
        current_user: AuthenticatedUser = Depends(get_current_user),
    ) -> List[WorkflowInstanceResponse]:
        """我（按角色）能审批的待处理列表——按我自己所在的企业过滤，见
        workflow_store.list_pending_for_org_and_role_ids 旁的说明。"""
        org = await org_store.get_org_for_user(current_user.user_id)
        if org is None:
            return []
        role_ids = [r.role_id for r in await role_store.get_user_roles(current_user.user_id)]
        instances = await workflow_store.list_pending_for_org_and_role_ids(org.org_id, role_ids)
        return [await _build_workflow_instance_response(i) for i in instances]

    @app.get("/api/v1/workflows/{instance_id}", response_model=WorkflowInstanceResponse)
    async def get_workflow(
        instance_id: str,
        current_user: AuthenticatedUser = Depends(get_current_user),
    ) -> WorkflowInstanceResponse:
        instance = await _require_workflow_access(instance_id, current_user, mode="owner_or_approver")
        return await _build_workflow_instance_response(instance)

    @app.post("/api/v1/workflows/{instance_id}/approve", response_model=WorkflowInstanceResponse)
    async def approve_workflow(
        instance_id: str,
        request: WorkflowActionRequest,
        current_user: AuthenticatedUser = Depends(get_current_user),
    ) -> WorkflowInstanceResponse:
        """材料齐全时用。"""
        await _require_workflow_access(instance_id, current_user, mode="approver")
        updated = await _transition_workflow(instance_id, "approved", current_user.user_id, request.comment)
        return await _build_workflow_instance_response(updated)

    @app.post("/api/v1/workflows/{instance_id}/return", response_model=WorkflowInstanceResponse)
    async def return_workflow(
        instance_id: str,
        request: WorkflowReturnRequest,
        current_user: AuthenticatedUser = Depends(get_current_user),
    ) -> WorkflowInstanceResponse:
        """材料不齐时用：打回补充材料，`pending_approval -> returned_for_revision`，
        申请人补完可重新提交，不是终态。"""
        await _require_workflow_access(instance_id, current_user, mode="approver")
        updated = await _transition_workflow(instance_id, "returned_for_revision", current_user.user_id, request.comment)
        return await _build_workflow_instance_response(updated)

    @app.post("/api/v1/workflows/{instance_id}/reject", response_model=WorkflowInstanceResponse)
    async def reject_workflow(
        instance_id: str,
        request: WorkflowRejectRequest,
        current_user: AuthenticatedUser = Depends(get_current_user),
    ) -> WorkflowInstanceResponse:
        """申请本身不该批时用（不是材料问题），终态，不可恢复。"""
        await _require_workflow_access(instance_id, current_user, mode="approver")
        updated = await _transition_workflow(instance_id, "rejected", current_user.user_id, request.comment)
        return await _build_workflow_instance_response(updated)

    @app.post("/api/v1/workflows/{instance_id}/resubmit", response_model=WorkflowInstanceResponse)
    async def resubmit_workflow_endpoint(
        instance_id: str,
        current_user: AuthenticatedUser = Depends(get_current_user),
    ) -> WorkflowInstanceResponse:
        """仅 `returned_for_revision` 状态可用，`-> pending_approval`。对话里由
        `resubmit_workflow` 工具触发的是同一条转换（builtin_tools.py），这个
        端点是给前端"我发起的"列表页用的等价入口。"""
        await _require_workflow_access(instance_id, current_user, mode="owner")
        updated = await _transition_workflow(instance_id, "pending_approval", current_user.user_id, None)
        return await _build_workflow_instance_response(updated)

    @app.post("/api/v1/workflows/{instance_id}/complete", response_model=WorkflowInstanceResponse)
    async def complete_workflow(
        instance_id: str,
        current_user: AuthenticatedUser = Depends(get_current_user),
    ) -> WorkflowInstanceResponse:
        await _require_workflow_access(instance_id, current_user, mode="owner_or_approver")
        updated = await _transition_workflow(instance_id, "completed", current_user.user_id, None)
        return await _build_workflow_instance_response(updated)

    @app.post("/api/v1/workflows/{instance_id}/cancel", response_model=WorkflowInstanceResponse)
    async def cancel_workflow(
        instance_id: str,
        current_user: AuthenticatedUser = Depends(get_current_user),
    ) -> WorkflowInstanceResponse:
        """仅发起人可取消，任何未到终态的状态都可用（`pending_approval`/
        `returned_for_revision`/`approved`），到 `rejected`/`completed`/`cancelled`
        之后不可再取消。"""
        await _require_workflow_access(instance_id, current_user, mode="owner")
        updated = await _transition_workflow(instance_id, "cancelled", current_user.user_id, None)
        return await _build_workflow_instance_response(updated)

    # ==================== 站内信 API ====================
    # work-flow-web.md 第 6.2 节：通用的"事件 -> 提醒"投递，目前只有工作流状态
    # 变化会触发（见 workflow_store.py 的 notify_requester/notify_approvers）。

    @app.get("/api/v1/notifications", response_model=List[NotificationResponse])
    async def list_notifications(
        unread_only: bool = False,
        limit: int = 20,
        offset: int = 0,
        current_user: AuthenticatedUser = Depends(get_current_user),
    ) -> List[NotificationResponse]:
        notifications = await workflow_store.list_notifications(
            current_user.user_id, unread_only=unread_only, limit=limit, offset=offset,
        )
        return [
            NotificationResponse(
                notification_id=n.notification_id, type=n.type, title=n.title, body=n.body,
                link=n.link, is_read=n.is_read, created_at=n.created_at,
            )
            for n in notifications
        ]

    @app.get("/api/v1/notifications/unread-count")
    async def get_unread_notification_count(
        current_user: AuthenticatedUser = Depends(get_current_user),
    ) -> dict:
        count = await workflow_store.unread_count(current_user.user_id)
        return {"count": count}

    @app.post("/api/v1/notifications/{notification_id}/read")
    async def mark_notification_read(
        notification_id: str,
        current_user: AuthenticatedUser = Depends(get_current_user),
    ) -> dict:
        found = await workflow_store.mark_read(notification_id, current_user.user_id)
        if not found:
            raise HTTPException(status_code=404, detail="通知不存在")
        return {"success": True}

    @app.post("/api/v1/notifications/mark-all-read")
    async def mark_all_notifications_read(
        current_user: AuthenticatedUser = Depends(get_current_user),
    ) -> dict:
        count = await workflow_store.mark_all_read(current_user.user_id)
        return {"success": True, "count": count}

    # ==================== 文件管理 API ====================

    @app.post("/api/v1/conversations/{conversation_id}/files")
    async def upload_file(
        conversation_id: str,
        file: UploadFile = File(...),
        current_user: AuthenticatedUser = Depends(get_current_user),
    ) -> dict:
        """
        上传文件到对话

        文件会被：
        1. 保存到磁盘
        2. 记录到数据库
        3. 后台异步 ingest 到对话的 collection
        """
        await _require_conversation_owner(conversation_id, current_user)
        try:
            # 读取文件内容
            content = await file.read()
            
            if not content:
                raise HTTPException(status_code=400, detail="Empty file")
            
            # 扩展名校验
            original_name = file.filename or ""
            file_ext = Path(original_name).suffix.lower()
            if file_ext == '.doc':
                raise HTTPException(
                    status_code=400,
                    detail="旧版 .doc 格式暂不支持，请先转换为 .docx 后上传"
                )
            if file_ext not in ALLOWED_EXTENSIONS:
                raise HTTPException(
                    status_code=400,
                    detail=f"不支持的文件格式: {file_ext}。支持: {', '.join(sorted(ALLOWED_EXTENSIONS))}"
                )
            
            # 保存文件
            file_info = await file_store.save_file(
                conversation_id=conversation_id,
                file_content=content,
                original_filename=file.filename,
                mime_type=file.content_type or "application/octet-stream",
            )
            
            # 构建 collection 名称
            collection = f"conv_{conversation_id}"
            
            # 启动后台 ingest 任务
            asyncio.create_task(
                ingest_file_task(
                    file_store=file_store,
                    conversation_id=conversation_id,
                    file_id=file_info.file_id,
                    file_path=file_info.file_path,
                    collection=collection,
                    settings=settings,
                    org_id=(actor_org.org_id if (actor_org := await org_store.get_org_for_user(current_user.user_id)) else None),
                    owner_user_id=current_user.user_id,
                )
            )
            
            # 更新对话文件计数
            conv = await conversation_store.get_conversation(conversation_id)
            if conv:
                await conversation_store.update_conversation(
                    conversation_id,
                    file_count=conv.file_count + 1,
                )
            
            return {
                "file_id": file_info.file_id,
                "filename": file_info.original_name,
                "size": file_info.file_size,
                "status": file_info.status,
                "message": "File uploaded successfully, processing in background"
            }
            
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to upload file: {e}")
    
    @app.get("/api/v1/conversations/{conversation_id}/files")
    async def list_files(
        conversation_id: str,
        current_user: AuthenticatedUser = Depends(get_current_user),
    ) -> dict:
        """列出对话的所有文件"""
        await _require_conversation_owner(conversation_id, current_user)
        try:
            files = await file_store.list_files(conversation_id)
            return {
                "conversation_id": conversation_id,
                "file_count": len(files),
                "files": [
                    {
                        "file_id": f.file_id,
                        "filename": f.original_name,
                        "size": f.file_size,
                        "status": f.status,
                        "doc_id": f.doc_id,
                        "created_at": f.created_at.isoformat() if f.created_at else None,
                        "file_type": f.file_type,
                        "page_count": f.page_count,
                        "extract_method": f.extract_method,
                        "word_count": f.word_count,
                    }
                    for f in files
                ]
            }
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to list files: {e}")
    
    @app.get("/api/v1/conversations/{conversation_id}/files/{file_id}/download")
    async def download_file(
        conversation_id: str,
        file_id: str,
        current_user: AuthenticatedUser = Depends(get_current_user),
    ) -> FileResponse:
        """下载对话里的原始文件。鉴权比普通的"仅对话所有者"宽一档：该对话关联的
        工作流实例的审批人也能下载——审批人要能实际打开申请人传的材料才能判断
        齐不齐全，不是可选项（work-flow.md 第 8 节风险）。"""
        conv = await conversation_store.get_conversation(conversation_id)
        if not conv:
            raise HTTPException(status_code=404, detail="Conversation not found")
        if conv.user_id != current_user.user_id:
            allowed = await _is_workflow_approver_for_conversation(conversation_id, current_user.user_id)
            if not allowed:
                raise HTTPException(status_code=403, detail="无权访问该对话的文件")

        file_info = await file_store.get_file(conversation_id, file_id)
        if file_info is None:
            raise HTTPException(status_code=404, detail="File not found")
        return FileResponse(
            path=file_info.file_path,
            filename=file_info.original_name,
            media_type=file_info.mime_type or "application/octet-stream",
        )

    @app.delete("/api/v1/conversations/{conversation_id}/files/{file_id}")
    async def delete_file(
        conversation_id: str,
        file_id: str,
        current_user: AuthenticatedUser = Depends(get_current_user),
    ) -> dict:
        """删除对话中的文件"""
        await _require_conversation_owner(conversation_id, current_user)
        try:
            success = await file_store.delete_file(conversation_id, file_id)
            if not success:
                raise HTTPException(status_code=404, detail="File not found")
            return {"message": "File deleted successfully"}
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to delete file: {e}")
    
    # ==================== 对话管理 API ====================
    
    @app.post("/api/v1/conversations")
    async def create_conversation_endpoint(
        request: dict = None,
        current_user: AuthenticatedUser = Depends(get_current_user),
    ) -> dict:
        """创建新对话"""
        try:
            title = request.get("title") if request else None
            conv = await conversation_store.create_conversation(user_id=current_user.user_id, title=title)
            return {
                "conversation_id": conv.conversation_id,
                "title": conv.title,
                "created_at": conv.created_at.isoformat(),
                "updated_at": conv.updated_at.isoformat(),
            }
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to create conversation: {e}")
    
    @app.get("/api/v1/conversations")
    async def list_conversations_endpoint(
        status: str = "active",
        limit: int = 100,
        offset: int = 0,
        current_user: AuthenticatedUser = Depends(get_current_user),
    ) -> dict:
        """获取当前登录用户自己的对话列表（按更新时间倒序）"""
        try:
            conversations = await conversation_store.list_conversations(
                user_id=current_user.user_id, status=status, limit=limit, offset=offset
            )
            return {
                "total": len(conversations),
                "conversations": [
                    {
                        "conversation_id": c.conversation_id,
                        "title": c.title,
                        "created_at": c.created_at.isoformat(),
                        "updated_at": c.updated_at.isoformat(),
                        "message_count": c.message_count,
                        "file_count": c.file_count,
                        "status": c.status,
                    }
                    for c in conversations
                ]
            }
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to list conversations: {e}")
    
    @app.get("/api/v1/conversations/{conversation_id}")
    async def get_conversation_endpoint(
        conversation_id: str,
        current_user: AuthenticatedUser = Depends(get_current_user),
    ) -> dict:
        """获取单个对话详情"""
        try:
            conv = await _require_conversation_owner(conversation_id, current_user)
            return {
                "conversation_id": conv.conversation_id,
                "title": conv.title,
                "created_at": conv.created_at.isoformat(),
                "updated_at": conv.updated_at.isoformat(),
                "message_count": conv.message_count,
                "file_count": conv.file_count,
                "status": conv.status,
            }
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to get conversation: {e}")
    
    @app.patch("/api/v1/conversations/{conversation_id}")
    async def update_conversation_endpoint(
        conversation_id: str,
        request: dict,
        current_user: AuthenticatedUser = Depends(get_current_user),
    ) -> dict:
        """更新对话信息（标题、状态等）"""
        await _require_conversation_owner(conversation_id, current_user)
        try:
            success = await conversation_store.update_conversation(
                conversation_id,
                title=request.get("title"),
                status=request.get("status"),
            )
            if not success:
                raise HTTPException(status_code=404, detail="Conversation not found")
            return {"message": "Conversation updated successfully"}
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to update conversation: {e}")
    
    @app.delete("/api/v1/conversations/{conversation_id}")
    async def delete_conversation_endpoint(
        conversation_id: str,
        current_user: AuthenticatedUser = Depends(get_current_user),
    ) -> dict:
        """删除对话（软删除）"""
        await _require_conversation_owner(conversation_id, current_user)
        try:
            success = await conversation_store.delete_conversation(conversation_id)
            if not success:
                raise HTTPException(status_code=404, detail="Conversation not found")
            # 同时删除关联的文件
            await file_store.delete_conversation_files(conversation_id)
            return {"message": "Conversation deleted successfully"}
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to delete conversation: {e}")
    
    # ==================== 对话 API ====================
    
    @app.post("/api/v1/chat", response_model=ChatResponse)
    async def chat(
        request: ChatRequest,
        current_user: AuthenticatedUser = Depends(get_current_user),
    ) -> ChatResponse:
        """
        对话接口

        检索范围自动限定为当前对话的 collection（conv_{conversation_id}）
        """
        # 使用 conversation_id 作为 thread_id，或创建新对话
        if request.conversation_id:
            thread_id = request.conversation_id
            conv = await _require_conversation_owner(thread_id, current_user)
        else:
            # 创建新对话
            conv = await conversation_store.create_conversation(user_id=current_user.user_id)
            thread_id = conv.conversation_id
        
        # 准备初始状态（user_id 来自校验过的 token，不信任客户端请求体）
        initial_state = {
            "query": request.query,
            "user_id": current_user.user_id,
            "conversation_id": thread_id,
            "task_id": request.task_id or os.urandom(8).hex(),
            "top_k": request.top_k,
            "workflow_type_hint": request.workflow_type,
            # collection 由 workflow 内部自动构建为 f"conv_{thread_id}"
        }

        # 运行工作流
        final_state = await workflow.run(initial_state, thread_id=thread_id)

        # 更新对话消息计数
        await conversation_store.update_conversation(
            thread_id,
            message_count=conv.message_count + 2 if conv else 2,  # user + assistant
        )

        return ChatResponse(
            conversation_id=final_state["conversation_id"],
            task_id=final_state["task_id"],
            answer=final_state.get("final_answer", ""),
            model_id=final_state.get("used_model", "unknown"),
            active_workflow=_build_active_workflow_summary(final_state.get("active_workflow")),
            kb_sources=final_state.get("kb_sources", []),
        )

    @app.post("/api/v1/chat/stream")
    async def chat_stream(
        request: ChatRequest,
        req: Request,
        current_user: AuthenticatedUser = Depends(get_current_user),
    ) -> StreamingResponse:
        """真流式对话接口：token-by-token 输出，客户端断开时自动回滚脏 checkpoint"""
        # `RequestContextMiddleware` 设的 contextvar 能否透传进下面这个生成器体，
        # 未实测（docs/observability_design.md R1）——这里不依赖那条假设，
        # 在中间件已确定的上下文里先取一次 request_id，显式传进生成器，
        # 保证跟响应头里的 X-Request-Id 一致，不会因为透传与否而生成两个不同的 id。
        _mw_request_id = (get_request_context() or bind_request_context()).request_id

        async def event_stream() -> AsyncGenerator[str, None]:
            bind_request_context(request_id=_mw_request_id, user_id=current_user.user_id)
            # 1. 确定 thread / conversation
            if request.conversation_id:
                thread_id = request.conversation_id
                conv = await conversation_store.get_conversation(thread_id)
                if not conv:
                    yield f"data: {json.dumps({'type': 'error', 'error': 'Conversation not found'}, ensure_ascii=False)}\n\n"
                    return
                if conv.user_id != current_user.user_id:
                    yield f"data: {json.dumps({'type': 'error', 'error': 'Forbidden'}, ensure_ascii=False)}\n\n"
                    return
            else:
                conv = await conversation_store.create_conversation(user_id=current_user.user_id)
                thread_id = conv.conversation_id
            
            _task_id = request.task_id or os.urandom(8).hex()
            bind_request_context(conversation_id=thread_id, task_id=_task_id)

            initial_state = {
                "query": request.query,
                "user_id": current_user.user_id,
                "conversation_id": thread_id,
                "task_id": _task_id,
                "top_k": request.top_k,
                "workflow_type_hint": request.workflow_type,
            }

            # 2. 记录干净 checkpoint id（流式开始前）
            clean_checkpoint_id = None
            try:
                config = {"configurable": {"thread_id": thread_id}}
                if hasattr(checkpointer, 'aget'):
                    cp = await checkpointer.aget(config)
                elif hasattr(checkpointer, 'get'):
                    cp = checkpointer.get(config)
                else:
                    cp = None
                
                if cp:
                    if hasattr(cp, 'checkpoint_id'):
                        clean_checkpoint_id = cp.checkpoint_id
                    elif hasattr(cp, 'config') and hasattr(cp.config, 'configurable'):
                        clean_checkpoint_id = cp.config.configurable.get('checkpoint_id')
                    elif isinstance(cp, dict):
                        clean_checkpoint_id = cp.get('checkpoint_id') or cp.get('id')
            except Exception:
                logger.exception("failed to get clean checkpoint", extra={"thread_id": thread_id})

            interrupted = False
            final_state = {}
            
            try:
                # 新对话立即通知前端
                if not request.conversation_id:
                    yield f"data: {json.dumps({'type': 'conversation_created', 'conversation_id': thread_id}, ensure_ascii=False)}\n\n"
                
                # 3. 真流式执行（带心跳保活）
                stream = workflow.run_stream(initial_state, thread_id=thread_id)
                event_queue = asyncio.Queue()
                
                async def _pump_stream():
                    try:
                        async for evt in stream:
                            await event_queue.put(evt)
                    finally:
                        await event_queue.put(None)
                
                async def _heartbeat():
                    while True:
                        await asyncio.sleep(2)
                        await event_queue.put({"type": "heartbeat"})
                
                pump_task = asyncio.create_task(_pump_stream())
                hb_task = asyncio.create_task(_heartbeat())
                
                while True:
                    try:
                        event = await asyncio.wait_for(event_queue.get(), timeout=5)
                    except asyncio.TimeoutError:
                        if await req.is_disconnected():
                            interrupted = True
                            break
                        continue
                    
                    if event is None:
                        break
                    
                    if event.get("type") == "heartbeat":
                        yield f"data: {json.dumps({'type': 'heartbeat'}, ensure_ascii=False)}\n\n"
                        if await req.is_disconnected():
                            interrupted = True
                            break
                        continue
                    
                    if await req.is_disconnected():
                        interrupted = True
                        break
                    
                    if event.get("type") == "trace":
                        await broadcast_trace(thread_id, event)
                    elif event.get("type") == "token":
                        yield f"data: {json.dumps({'type': 'token', 'content': event['content']}, ensure_ascii=False)}\n\n"
                    elif event.get("type") == "done":
                        final_state = event.get("state", {})
                
                # 4. 正常结束：发送 done 并更新统计
                if not interrupted and final_state:
                    memory_stats = workflow.get_memory_stats(final_state)
                    active_workflow_summary = _build_active_workflow_summary(final_state.get("active_workflow"))
                    done_event = {
                        "type": "done",
                        "conversation_id": final_state.get("conversation_id"),
                        "task_id": final_state.get("task_id"),
                        "model_id": final_state.get("used_model"),
                        "trace": final_state.get("trace_events", []),
                        "memory_stats": memory_stats,
                        "active_workflow": active_workflow_summary.model_dump() if active_workflow_summary else None,
                        "kb_sources": final_state.get("kb_sources", []),
                    }
                    yield f"data: {json.dumps(done_event, ensure_ascii=False)}\n\n"
                    
                    await conversation_store.update_conversation(
                        thread_id,
                        message_count=conv.message_count + 2 if conv else 2,
                    )
                    
            except asyncio.CancelledError:
                interrupted = True
                logger.info("stream cancelled", extra={"thread_id": thread_id})
            except Exception as e:
                logger.exception("chat stream failed", extra={"thread_id": thread_id})
                yield f"data: {json.dumps({'type': 'error', 'error': str(e)}, ensure_ascii=False)}\n\n"
            finally:
                # 5. 中断时回滚脏 checkpoint
                if interrupted and clean_checkpoint_id:
                    await _trim_checkpoints(checkpointer, thread_id, clean_checkpoint_id)
                # 生成器体是显式再绑的一份上下文（见函数开头注释），用完显式清理，
                # 不依赖中间件那份 finally 一定覆盖到这里。
                clear_request_context()

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    @app.websocket("/ws/trace/{conversation_id}")
    async def trace_websocket(
        websocket: WebSocket, conversation_id: str, token: str = Query(default=""),
    ):
        """LangGraph 实时追踪 WebSocket：推送节点级执行进度。

        2026-08-26 P0：原来握手即 accept，零鉴权——trace 里含检索片段与 prompt，
        等同于旁路读取该会话的完整问答内容。浏览器原生 WebSocket API 握手阶段
        不能带自定义 header，所以约定 token 走查询参数 `?token=`（跟 Authorization
        header 里那份是同一张 JWT，解码逻辑复用 `_decode_token`）。鉴权要求跟同一份
        对话的 REST 接口一致：token 需要解出真实用户，且该用户必须是这条
        conversation 的所有者（复用 `_require_conversation_owner` 的同一判断），
        不满足则在 `accept()` 之前拒绝，不建立连接。
        """
        try:
            current_user = _decode_token(token)
            await _require_conversation_owner(conversation_id, current_user)
        except HTTPException:
            await websocket.close(code=4401)
            return

        await websocket.accept()
        active_trace_ws.setdefault(conversation_id, []).append(websocket)
        try:
            while True:
                # 保持连接，接收前端心跳/指令（如中断请求可扩展）
                data = await websocket.receive_text()
                if data == "ping":
                    await websocket.send_text("pong")
        except WebSocketDisconnect:
            pass
        finally:
            sockets = active_trace_ws.get(conversation_id, [])
            if websocket in sockets:
                sockets.remove(websocket)
            if not sockets:
                active_trace_ws.pop(conversation_id, None)

    @app.post("/api/v1/conversations/{conversation_id}/rollback")
    async def rollback_conversation(
        conversation_id: str,
        request: RollbackRequest,
        current_user: AuthenticatedUser = Depends(get_current_user),
    ) -> dict:
        """
        三位一体时间裁剪：回滚对话到指定消息边界。
        同时清理：
        1. LangGraph Checkpoint（状态层）
        2. PostgreSQL conversation_archive（存储层）
        3. PostgreSQL long_term_memories（记忆层）
        """
        await _require_conversation_owner(conversation_id, current_user)
        # 1. 获取目标 turn
        turn_info = await archive_store.get_turn_by_message_id(conversation_id, request.target_message_id)
        if not turn_info:
            raise HTTPException(status_code=404, detail="Target message not found")
        
        target_turn_id = turn_info["turn_id"]
        if not target_turn_id:
            raise HTTPException(status_code=400, detail="Target message has no associated turn_id")
        
        # 2. 确定要保留的 checkpoint（状态层）
        # 先从历史中提取目标 turn 之前的那个 turn_id
        keep_checkpoint_id = None
        config = {"configurable": {"thread_id": conversation_id}}
        previous_turn_id = None
        try:
            history_msgs = await archive_store.load_full_history(conversation_id)
            # 按 created_at 排序后提取 turn_id 序列
            turn_order = []
            seen_turns = set()
            for m in history_msgs:
                tid = m.get("turn_id")
                if tid and tid not in seen_turns:
                    seen_turns.add(tid)
                    turn_order.append(tid)
            if target_turn_id in turn_order:
                target_idx = turn_order.index(target_turn_id)
                if target_idx > 0:
                    previous_turn_id = turn_order[target_idx - 1]
        except Exception:
            logger.exception("rollback: failed to determine previous turn", extra={"conversation_id": conversation_id})
        
        try:
            if hasattr(checkpointer, 'alist') and previous_turn_id:
                candidates = []
                async for cp in checkpointer.alist(config):
                    # cp 是 CheckpointTuple，包含 config / checkpoint / metadata 等字段
                    cfg = cp.config if isinstance(cp.config, dict) else {}
                    checkpoint_id = cfg.get("configurable", {}).get("checkpoint_id")
                    
                    # 从 CheckpointTuple.checkpoint 提取状态
                    checkpoint_state = cp.checkpoint if hasattr(cp, 'checkpoint') else cp
                    if isinstance(checkpoint_state, dict):
                        channel_values = checkpoint_state.get('channel_values', {})
                    else:
                        channel_values = getattr(checkpoint_state, 'channel_values', {}) or {}
                    turn_id_in_cp = channel_values.get('current_turn_id') if isinstance(channel_values, dict) else None
                    
                    if turn_id_in_cp == previous_turn_id and checkpoint_id:
                        ts = checkpoint_state.get('ts', 0) if isinstance(checkpoint_state, dict) else getattr(checkpoint_state, 'ts', 0)
                        candidates.append((ts, checkpoint_id))
                if candidates:
                    # 取时间戳最大的（即最新的）一个前一 turn 的 checkpoint
                    candidates.sort(key=lambda x: x[0])
                    keep_checkpoint_id = candidates[-1][1]
        except Exception:
            logger.exception("rollback: failed to list checkpoints", extra={"conversation_id": conversation_id})
        
        # 3. 执行三层回滚（互不阻断）
        trimmed = {"checkpoint": False, "messages": 0, "ltm": 0}
        
        try:
            await _trim_checkpoints(checkpointer, conversation_id, keep_checkpoint_id)
            trimmed["checkpoint"] = True
        except Exception:
            logger.exception("rollback: checkpoint trim failed", extra={"conversation_id": conversation_id})
        
        try:
            trimmed["messages"] = await archive_store.delete_messages_from_turn(conversation_id, target_turn_id)
        except Exception:
            logger.exception("rollback: message delete failed", extra={"conversation_id": conversation_id})
        
        try:
            if workflow._ltm_store:
                trimmed["ltm"] = await workflow._ltm_store.delete_facts_from_turn(conversation_id, target_turn_id)
        except Exception:
            logger.exception("rollback: LTM delete failed", extra={"conversation_id": conversation_id})
        
        # 4. 更新 conversation 的 message_count
        try:
            history = await archive_store.load_full_history(conversation_id)
            await conversation_store.update_conversation(
                conversation_id,
                message_count=len(history),
                metadata={"last_rollback_turn_id": target_turn_id}
            )
        except Exception:
            logger.exception("rollback: failed to update conversation stats", extra={"conversation_id": conversation_id})
        
        return {
            "success": True,
            "conversation_id": conversation_id,
            "trimmed_turn_id": target_turn_id,
            "kept_checkpoint_id": keep_checkpoint_id,
            "trimmed": trimmed,
        }

    @app.get("/api/v1/history/{conversation_id}")
    async def get_history(
        conversation_id: str,
        current_user: AuthenticatedUser = Depends(get_current_user),
    ) -> dict:
        """
        获取完整对话历史（从 PostgreSQL 加载）
        这是用户可见的完整历史，不是 checkpoint 中的精简状态
        """
        await _require_conversation_owner(conversation_id, current_user)
        try:
            history = await archive_store.load_full_history(conversation_id)
            return {
                "conversation_id": conversation_id,
                "message_count": len(history),
                "messages": history
            }
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to load history: {e}")

    @app.get("/api/v1/memory/{conversation_id}/stats")
    async def get_memory_stats(
        conversation_id: str,
        current_user: AuthenticatedUser = Depends(get_current_user),
    ) -> dict:
        """
        获取当前记忆的统计信息（调试用）
        这会从 checkpoint 加载并返回统计
        """
        await _require_conversation_owner(conversation_id, current_user)
        config = {"configurable": {"thread_id": conversation_id}}
        try:
            if hasattr(checkpointer, 'aget'):
                checkpoint = await checkpointer.aget(config)
            elif hasattr(checkpointer, 'get'):
                checkpoint = checkpointer.get(config)
            else:
                checkpoint = None
        except Exception:
            logger.warning("failed to load checkpoint for memory stats", extra={"conversation_id": conversation_id}, exc_info=True)
            checkpoint = None
        
        if not checkpoint:
            return {"error": "Conversation not found"}
        
        state = {}
        if isinstance(checkpoint, dict):
            state = checkpoint.get("channel_values", {})
        elif hasattr(checkpoint, "checkpoint") and isinstance(checkpoint.checkpoint, dict):
            state = checkpoint.checkpoint.get("channel_values", {})
        elif hasattr(checkpoint, "channel_values"):
            state = checkpoint.channel_values
        
        messages = state.get("messages", [])
        summary = state.get("summary", "")
        
        return {
            "conversation_id": conversation_id,
            "message_count": len(messages),
            "summary_length": len(summary),
            "summary_preview": summary[:200] + "..." if len(summary) > 200 else summary,
            "recent_messages": [
                {"role": "user" if m.type == "human" else "assistant", "content": m.content[:100]}
                for m in messages[-4:]
            ]
        }

    @app.on_event("shutdown")
    async def shutdown():
        """关闭时清理资源。

        14 个 Store 现在共享同一批连接池（db_pool.py，P1-2），逐个调用各
        Store 的 close() 不再有意义（那只清引用，不做真实关闭，见各 Store
        close() 方法的注释）——真正的关闭只需要调一次共享池的关闭入口。
        """
        await close_shared_pools()

    return app


def _chunk_text(text: str, size: int):
    """将文本分块，用于模拟流式输出"""
    for i in range(0, len(text), size):
        yield text[i : i + size]


def run() -> None:
    """运行服务器"""
    # Windows: loop="none" 让 Uvicorn 使用当前策略创建的事件循环
    # 我们在顶部已设置 WindowsSelectorEventLoopPolicy，避免 psycopg add_reader 报错
    uvicorn.run(
        "src.ragent_backend.app:create_app", 
        factory=True, 
        host="0.0.0.0", 
        port=int(os.getenv("RAGENT_PORT", "8000")),
        reload=False,
        loop="none"
    )


if __name__ == "__main__":
    run()
