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
import httpx
import json
import os
import sys
from typing import AsyncGenerator, List, Optional
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
from fastapi import Depends, FastAPI, HTTPException, UploadFile, File, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager

# LangGraph checkpointer
from langgraph.checkpoint.postgres import PostgresSaver

from src.ragent_backend.schemas import (
    ChatRequest, ChatResponse, ActiveWorkflowSummary, RollbackRequest,
    LoginRequest, LoginResponse, MeResponse, ChangePasswordRequest, RoleSummary,
    OrganizationSummary, AdminUserResponse, AdminCreateUserRequest,
    AdminOrganizationResponse, AdminCreateOrganizationRequest,
    TenantConnectorResponse, UpsertTenantConnectorRequest, GatewayConnectorResponse,
    RoleResponse, CreateRoleRequest, UpdateRoleRequest,
    SetRoleCollectionsRequest, SetUserRolesRequest,
    WorkflowTemplateResponse, CreateWorkflowTemplateRequest, UpdateWorkflowTemplateRequest,
    WorkflowInstanceResponse, WorkflowActionRequest, WorkflowReturnRequest, WorkflowRejectRequest,
    NotificationResponse,
)
from src.ragent_backend.store import build_archive_store, ConversationArchiveStore
from src.ragent_backend.workflow import RAGWorkflow
from src.ragent_backend.ltm_store import LTMStore
from src.ragent_backend.file_store import build_file_store, ConversationFileStore
from src.ragent_backend.conversation_store import build_conversation_store, ConversationStore, Conversation
from src.ragent_backend.user_store import UserStore, User
from src.ragent_backend.role_store import RoleStore, ROLE_SUPER_ADMIN, ROLE_ADMIN
from src.ragent_backend.workflow_store import WorkflowStore, WorkflowTemplate, WorkflowInstance
from src.ragent_backend.attendance_store import AttendanceStore
from src.ragent_backend.org_store import OrgStore
from src.ragent_backend.tenant_connector_store import TenantConnectorStore
from src.ragent_backend.tenant_identity_store import TenantIdentityStore
from src.ragent_backend.auth import (
    AuthenticatedUser, create_access_token, get_current_user, require_role,
    require_same_org_or_platform, require_platform_admin,
)
from src.ingestion.pipeline import IngestionPipeline
from src.core.settings import load_settings
from src.tool_agent.tool_registry import ToolRegistry
from src.tool_agent.builtin_tools import register_builtin_tools
from src.tool_agent.mcp_client import MCPClient


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

    print(f"[Checkpointer] Using PostgreSQL (Async)")
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
        print(f"[TrimCheckpoint] checkpointer 没有可用连接，跳过 thread={thread_id}")
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
        print(f"[TrimCheckpoint] Postgres trimmed for thread={thread_id}, kept={keep_checkpoint_id}")
    except Exception as e:
        print(f"[TrimCheckpoint] Postgres trim failed: {e}")


# 全局并发控制：限制同时执行的 ingest 后台任务数量，防止 LLM API 配额和内存被打爆
INGEST_SEMAPHORE = asyncio.Semaphore(2)

# WebSocket 连接管理：conversation_id -> list[WebSocket]
active_trace_ws: dict[str, list[WebSocket]] = {}

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
            pipeline = IngestionPipeline(settings, collection=collection)
            
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
                print(f"[Ingest] File {file_id} ingested successfully to {collection}, doc_id={doc_id}, method={extract_method}")
            else:
                error_msg = result.error or "Unknown error"
                await file_store.update_file_status(
                    conversation_id, file_id, "error", error_message=error_msg,
                    extract_method=extract_method,
                    page_count=page_count,
                    word_count=word_count,
                )
                print(f"[Ingest] Failed to ingest file {file_id}: {error_msg}")
            
        except Exception as e:
            print(f"[Ingest] Failed to ingest file {file_id}: {e}")
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


def create_app() -> FastAPI:
    # 加载配置
    settings = load_settings()

    # user_store 先建好，因为 ToolRegistry 里的工具要用它做 ACL 校验
    user_store: UserStore = UserStore()
    # role_store：角色 CRUD + 角色<->知识库/用户<->角色 关联；
    # user_store.get_allowed_collections 内部会委托它算权限并集
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
    # tenant_identity_store：我方 user_id <-> 企业考勤系统工号的映射，只有委托考勤
    # 查询用得到（attendance-tenant-federation.md 第 3 节）
    tenant_identity_store: TenantIdentityStore = TenantIdentityStore()

    # 初始化 ToolRegistry（内置工具 + MCP 外部工具）
    tool_registry = ToolRegistry()
    register_builtin_tools(
        tool_registry,
        user_store=user_store,
        workflow_store=workflow_store,
        attendance_store=attendance_store,
        org_store=org_store,
        tenant_connector_store=tenant_connector_store,
        tenant_identity_store=tenant_identity_store,
    )
    print(f"[Init] Registered {tool_registry.tool_count} built-in tools")

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
        """这个对话上有没有一条工作流实例，且当前用户持有它对应模板的审批角色。"""
        instance = await workflow_store.get_latest_instance_by_conversation(conversation_id)
        if instance is None:
            return False
        template = await workflow_store.get_template_by_type(instance.workflow_type)
        if template is None or not template.approver_role_id:
            return False
        user_role_ids = {r.role_id for r in await role_store.get_user_roles(user_id)}
        return template.approver_role_id in user_role_ids

    # 初始化 LLM（配置完全来自 settings.yaml + 环境变量覆盖）
    try:
        from langchain_openai import ChatOpenAI
        llm_kwargs = {
            "model": settings.llm.model,
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
        llm = ChatOpenAI(**llm_kwargs)
    except Exception as e:
        print(f"[Init] Failed to init LLM: {e}")
        llm = None

    # 创建工作流（传入 tool_registry）
    # LTMStore was never constructed here, so RAGWorkflow always received
    # ltm_store=None -- long-term memory recall/extraction and the rollback
    # endpoint's LTM trim step were silently no-ops despite being fully
    # implemented in ltm_store.py.
    ltm_store = LTMStore()

    workflow = RAGWorkflow(
        store=archive_store,
        llm=llm,
        checkpointer=checkpointer,
        max_messages=int(os.getenv("RAGENT_MAX_MESSAGES", "20")),
        keep_recent=int(os.getenv("RAGENT_KEEP_RECENT", "4")),
        tool_registry=tool_registry,
        ltm_store=ltm_store,
        workflow_store=workflow_store,
    )

    # lifespan：异步连接 MCP Servers（必须在 FastAPI 构造函数之前定义）
    @asynccontextmanager
    async def lifespan(app: FastAPI):
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
                        print(f"[MCP] Unknown transport '{cfg.transport}' for server '{name}'")
                        continue
                    
                    await tool_registry.register_from_mcp_client(
                        client, name, timeout_seconds=cfg.timeout_seconds
                    )
                    print(f"[MCP] Connected and registered server: {name}")
                except Exception as e:
                    print(f"[MCP] Failed to connect server '{name}': {e}")
        
        yield
        
        # 关闭时断开所有 MCP 连接
        await tool_registry.disconnect_all_mcp()
        print("[MCP] All MCP connections closed")

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
        allow_origins=["*"],  # 允许所有来源
        allow_credentials=True,
        allow_methods=["*"],  # 允许所有方法
        allow_headers=["*"],  # 允许所有头
    )

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
        return OrganizationSummary(org_id=org.org_id, name=org.name, is_platform=org.is_platform)

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
    # 人员管理（增/查/删/分配角色）对 super_admin 和 admin（企业管理员）都开放——
    # 企业管理员只能管自己企业内的人（下面每个端点内部按 org 过滤/校验），
    # super_admin 不受此限。组织管理、连接器配置、角色定义、工作流模板这类跨
    # 企业/平台级操作仍然只对 super_admin 开放（各自的 Depends 没有改）。
    # 角色判断是每次请求都现查数据库（见 auth.require_role），不是信 token。

    _require_super_admin = require_role(ROLE_SUPER_ADMIN)
    _require_admin_or_super = require_role(ROLE_ADMIN, ROLE_SUPER_ADMIN)

    async def _reject_privilege_escalation(actor: AuthenticatedUser, role_ids: List[str]) -> None:
        """企业管理员（非 super_admin）不能把 admin/super_admin 角色发给任何人
        （包括新建用户时）——不然"只有平台管理员能新增企业管理员"这条边界就
        形同虚设，企业管理员可以自己再造一个企业管理员甚至超级管理员出来。"""
        actor_role_names = {r.name for r in await role_store.get_user_roles(actor.user_id)}
        if ROLE_SUPER_ADMIN in actor_role_names:
            return
        all_roles = {r.role_id: r for r in await role_store.list_roles()}
        escalating = {
            rid for rid in role_ids
            if rid in all_roles and all_roles[rid].name in (ROLE_ADMIN, ROLE_SUPER_ADMIN)
        }
        if escalating:
            raise HTTPException(status_code=403, detail="企业管理员不能授予管理员/超级管理员角色，只有平台管理员能做")

    async def _build_admin_user_response(user: User) -> AdminUserResponse:
        roles = await role_store.get_user_roles(user.user_id)
        return AdminUserResponse(
            user_id=user.user_id,
            username=user.username,
            roles=[RoleSummary(role_id=r.role_id, name=r.name, display_name=r.display_name) for r in roles],
            allowed_collections=await role_store.get_allowed_collections_for_user(user.user_id),
            organization=await _org_summary_for_user(user.user_id),
            created_at=user.created_at,
        )

    @app.get("/api/v1/admin/users", response_model=List[AdminUserResponse])
    async def admin_list_users(
        current_user: AuthenticatedUser = Depends(_require_admin_or_super),
    ) -> List[AdminUserResponse]:
        users = await user_store.list_users()
        # 平台管理员看全部；普通企业管理员只看自己企业的——过滤发生在这里，
        # 不是前端拿到全量再自己藏几行（attendance-tenant-federation.md 图4）。
        if not await org_store.is_platform_admin(current_user.user_id):
            actor_org = await org_store.get_org_for_user(current_user.user_id)
            actor_org_id = actor_org.org_id if actor_org else None
            filtered = []
            for u in users:
                user_org = await org_store.get_org_for_user(u.user_id)
                if user_org is not None and user_org.org_id == actor_org_id:
                    filtered.append(u)
            users = filtered
        return [await _build_admin_user_response(u) for u in users]

    @app.post("/api/v1/admin/users", response_model=AdminUserResponse)
    async def admin_create_user(
        request: AdminCreateUserRequest,
        current_user: AuthenticatedUser = Depends(_require_admin_or_super),
    ) -> AdminUserResponse:
        if request.role_ids:
            await _reject_privilege_escalation(current_user, request.role_ids)
        try:
            user = await user_store.create_user(request.username, request.password)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        if request.role_ids:
            all_role_ids = {r.role_id for r in await role_store.list_roles()}
            unknown = set(request.role_ids) - all_role_ids
            if unknown:
                raise HTTPException(status_code=400, detail=f"角色不存在: {sorted(unknown)}")
            await role_store.assign_user_roles(user.user_id, request.role_ids)

        # 平台管理员可以把新用户指派给任意企业（不传就落到自己所在的企业）；
        # 非平台管理员不管请求体里传了什么 org_id，一律强制用自己的——不能靠
        # 改请求体把新账号建到别的企业名下。
        is_platform = await org_store.is_platform_admin(current_user.user_id)
        actor_org = await org_store.get_org_for_user(current_user.user_id)
        target_org_id = request.org_id if (is_platform and request.org_id) else (actor_org.org_id if actor_org else None)
        if target_org_id:
            await org_store.set_user_organization(user.user_id, target_org_id)

        return await _build_admin_user_response(user)

    @app.delete("/api/v1/admin/users/{user_id}")
    async def admin_delete_user(
        user_id: str,
        current_user: AuthenticatedUser = Depends(_require_admin_or_super),
        _same_org: AuthenticatedUser = Depends(require_same_org_or_platform),
    ) -> dict:
        if user_id == current_user.user_id:
            raise HTTPException(status_code=400, detail="不能删除自己")
        found = await user_store.delete_user(user_id)
        if not found:
            raise HTTPException(status_code=404, detail="用户不存在")
        return {"success": True}

    @app.put("/api/v1/admin/users/{user_id}/roles", response_model=AdminUserResponse)
    async def admin_set_user_roles(
        user_id: str,
        request: SetUserRolesRequest,
        current_user: AuthenticatedUser = Depends(_require_admin_or_super),
        _same_org: AuthenticatedUser = Depends(require_same_org_or_platform),
    ) -> AdminUserResponse:
        user = await user_store.get_user_by_id(user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="用户不存在")

        all_roles = {r.role_id: r for r in await role_store.list_roles()}
        unknown = set(request.role_ids) - set(all_roles)
        if unknown:
            raise HTTPException(status_code=400, detail=f"角色不存在: {sorted(unknown)}")

        await _reject_privilege_escalation(current_user, request.role_ids)

        if user_id == current_user.user_id:
            # 防止超级管理员误操作把自己的 super_admin 角色摘掉，导致管理后台再也进不去
            super_admin_role_id = next(
                (rid for rid, r in all_roles.items() if r.name == ROLE_SUPER_ADMIN), None
            )
            current_role_ids = {r.role_id for r in await role_store.get_user_roles(user_id)}
            if (
                super_admin_role_id
                and super_admin_role_id in current_role_ids
                and super_admin_role_id not in request.role_ids
            ):
                raise HTTPException(status_code=400, detail="不能取消自己的超级管理员角色")

        await role_store.assign_user_roles(user_id, request.role_ids)
        return await _build_admin_user_response(user)

    # 注意：没有"改派用户所属企业"的端点——员工的企业归属只在创建时确定一次
    # （见 admin_create_user），创建之后任何管理员（包括平台管理员）都不能再
    # 改派，用户如果确实换了公司，只能删除旧账号、在新企业下重新创建。这是
    # 有意的产品决策：避免"企业 A 的管理员看着看着一个用户突然从列表里消失，
    # 因为被平台管理员偷偷过继给了企业 B"这类容易误解的操作。

    # ==================== 组织管理 API（仅平台管理员） ====================

    def _org_response(org) -> AdminOrganizationResponse:
        return AdminOrganizationResponse(
            org_id=org.org_id, name=org.name, is_platform=org.is_platform, created_at=org.created_at,
        )

    @app.get("/api/v1/admin/organizations", response_model=List[AdminOrganizationResponse])
    async def admin_list_organizations(
        current_user: AuthenticatedUser = Depends(_require_super_admin),
    ) -> List[AdminOrganizationResponse]:
        # 平台管理员看全部组织；企业内 admin 只能看到自己那一条（给"新建用户"
        # 弹窗、个人信息展示等场景确认自己企业的名字用，不是把组织列表当成
        # 可浏览的目录露出去）。
        if await org_store.is_platform_admin(current_user.user_id):
            orgs = await org_store.list_organizations()
        else:
            own_org = await org_store.get_org_for_user(current_user.user_id)
            orgs = [own_org] if own_org else []
        return [_org_response(o) for o in orgs]

    @app.post("/api/v1/admin/organizations", response_model=AdminOrganizationResponse)
    async def admin_create_organization(
        request: AdminCreateOrganizationRequest,
        _: AuthenticatedUser = Depends(_require_super_admin),
        __: AuthenticatedUser = Depends(require_platform_admin),
    ) -> AdminOrganizationResponse:
        org = await org_store.create_organization(request.name)
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
        return [await _connector_response(c) for c in connectors]

    @app.put(
        "/api/v1/admin/organizations/{org_id}/connectors/{capability}",
        response_model=TenantConnectorResponse,
    )
    async def admin_upsert_tenant_connector(
        org_id: str,
        capability: str,
        request: UpsertTenantConnectorRequest,
        _: AuthenticatedUser = Depends(_require_super_admin),
        __: AuthenticatedUser = Depends(require_platform_admin),
    ) -> TenantConnectorResponse:
        if await org_store.get_organization(org_id) is None:
            raise HTTPException(status_code=404, detail="组织不存在")

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

        results = []
        for c in connectors:
            results.append(GatewayConnectorResponse(
                connector_id=c.connector_id, org_id=c.org_id,
                org_name=org_names.get(c.org_id, c.org_id),
                capability=c.capability, connector_type=c.connector_type, endpoint=c.endpoint,
                is_active=c.is_active, health_status=await _check_connector_health(c),
                call_count=c.call_count, failure_count=c.failure_count,
                last_called_at=c.last_called_at, last_latency_ms=c.last_latency_ms, last_error=c.last_error,
            ))
        return results

    # ==================== 角色管理 API（仅超级管理员） ====================

    def _role_response(role_obj) -> RoleResponse:
        return RoleResponse(
            role_id=role_obj.role_id,
            name=role_obj.name,
            display_name=role_obj.display_name,
            is_system=role_obj.is_system,
            collection_names=getattr(role_obj, "collection_names", []),
            created_at=role_obj.created_at,
        )

    @app.get("/api/v1/admin/roles", response_model=List[RoleResponse])
    async def admin_list_roles(
        _: AuthenticatedUser = Depends(_require_super_admin),
    ) -> List[RoleResponse]:
        roles = await role_store.list_roles()
        return [_role_response(r) for r in roles]

    @app.post("/api/v1/admin/roles", response_model=RoleResponse)
    async def admin_create_role(
        request: CreateRoleRequest,
        _: AuthenticatedUser = Depends(_require_super_admin),
    ) -> RoleResponse:
        try:
            role = await role_store.create_role(request.name, request.display_name)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return _role_response(role)

    @app.patch("/api/v1/admin/roles/{role_id}", response_model=RoleResponse)
    async def admin_update_role(
        role_id: str,
        request: UpdateRoleRequest,
        _: AuthenticatedUser = Depends(_require_super_admin),
    ) -> RoleResponse:
        role = await role_store.update_role(role_id, request.display_name)
        if role is None:
            raise HTTPException(status_code=404, detail="角色不存在")
        # update_role 返回的是不带 collection_names 的 Role；重新从 list_roles()
        # 取一次带关联知识库的完整视图，避免响应把已有的知识库关联显示成空。
        roles = await role_store.list_roles()
        return _role_response(next(r for r in roles if r.role_id == role_id))

    @app.delete("/api/v1/admin/roles/{role_id}")
    async def admin_delete_role(
        role_id: str,
        _: AuthenticatedUser = Depends(_require_super_admin),
    ) -> dict:
        try:
            found = await role_store.delete_role(role_id)
        except ValueError as e:
            raise HTTPException(status_code=403, detail=str(e))
        if not found:
            raise HTTPException(status_code=404, detail="角色不存在")
        return {"success": True}

    @app.put("/api/v1/admin/roles/{role_id}/collections", response_model=RoleResponse)
    async def admin_set_role_collections(
        role_id: str,
        request: SetRoleCollectionsRequest,
        _: AuthenticatedUser = Depends(_require_super_admin),
    ) -> RoleResponse:
        role = await role_store.get_role_by_id(role_id)
        if role is None:
            raise HTTPException(status_code=404, detail="角色不存在")
        await role_store.set_role_collections(role_id, request.collection_names)
        roles = await role_store.list_roles()
        return _role_response(next(r for r in roles if r.role_id == role_id))

    @app.get("/api/v1/admin/collections", response_model=List[str])
    async def admin_list_collections(
        _: AuthenticatedUser = Depends(_require_super_admin),
    ) -> List[str]:
        """列出 ChromaDB 现有 collection 名，供"配置知识库"多选框做数据源。
        对话私有的 conv_{id} collection 不是可分配的共享知识库，排除掉。"""
        from src.mcp_server.tools.list_collections import ListCollectionsTool

        tool = ListCollectionsTool(settings=settings)
        collections = await asyncio.to_thread(tool.list_collections, False)
        return sorted(c.name for c in collections if not c.name.startswith("conv_"))

    # ==================== 工作流模板管理 API（仅超级管理员） ====================
    # work-flow.md 第 7 节：模板定义"某类流程需要哪些结构化字段"，附件材料只有
    # 一句提醒文案（attachments_note），不逐条建模校验。

    def _workflow_template_response(template: WorkflowTemplate) -> WorkflowTemplateResponse:
        return WorkflowTemplateResponse(
            template_id=template.template_id,
            workflow_type=template.workflow_type,
            display_name=template.display_name,
            description=template.description,
            required_fields=template.required_fields,
            attachments_note=template.attachments_note,
            approver_role_id=template.approver_role_id,
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
        # exclude_unset：区分"这次 PATCH 没传这个字段"和"显式传了 null"，
        # approver_role_id 本身允许为 null（表示暂无审批人），两种情况不能混淆。
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
        if "approver_role_id" in updates:
            if updates["approver_role_id"] is not None:
                role = await role_store.get_role_by_id(updates["approver_role_id"])
                if role is None:
                    raise HTTPException(status_code=400, detail="审批角色不存在")
            kwargs["approver_role_id"] = updates["approver_role_id"]

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
        role_ids = [r.role_id for r in await role_store.get_user_roles(current_user.user_id)]
        templates = await workflow_store.approvable_templates_for_role_ids(role_ids)
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
        """`mode`: "owner" 必须是发起人；"approver" 必须持有该实例所属模板的审批
        角色；"owner_or_approver" 满足其一即可。审批角色是 per-模板动态的，不能
        用静态的 `require_role(*names)` 工厂，运行期查模板后再判断（work-flow.md
        第 7 节）。"""
        instance = await workflow_store.get_instance(instance_id)
        if instance is None:
            raise HTTPException(status_code=404, detail="工作流不存在")

        is_owner = instance.requester_user_id == current_user.user_id
        if mode == "owner":
            if not is_owner:
                raise HTTPException(status_code=403, detail="无权访问该工作流")
            return instance

        template = await workflow_store.get_template_by_type(instance.workflow_type)
        is_approver = False
        if template is not None and template.approver_role_id:
            role_ids = {r.role_id for r in await role_store.get_user_roles(current_user.user_id)}
            is_approver = template.approver_role_id in role_ids

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
        """我（按角色）能审批的待处理列表。"""
        role_ids = [r.role_id for r in await role_store.get_user_roles(current_user.user_id)]
        instances = await workflow_store.list_pending_for_role_ids(role_ids)
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
        )

    @app.post("/api/v1/chat/stream")
    async def chat_stream(
        request: ChatRequest,
        req: Request,
        current_user: AuthenticatedUser = Depends(get_current_user),
    ) -> StreamingResponse:
        """真流式对话接口：token-by-token 输出，客户端断开时自动回滚脏 checkpoint"""
        
        async def event_stream() -> AsyncGenerator[str, None]:
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
            
            initial_state = {
                "query": request.query,
                "user_id": current_user.user_id,
                "conversation_id": thread_id,
                "task_id": request.task_id or os.urandom(8).hex(),
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
            except Exception as e:
                print(f"[ChatStream] Failed to get clean checkpoint: {e}")
            
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
                    }
                    yield f"data: {json.dumps(done_event, ensure_ascii=False)}\n\n"
                    
                    await conversation_store.update_conversation(
                        thread_id,
                        message_count=conv.message_count + 2 if conv else 2,
                    )
                    
            except asyncio.CancelledError:
                interrupted = True
                print(f"[ChatStream] Stream cancelled, thread={thread_id}")
            except Exception as e:
                yield f"data: {json.dumps({'type': 'error', 'error': str(e)}, ensure_ascii=False)}\n\n"
            finally:
                # 5. 中断时回滚脏 checkpoint
                if interrupted and clean_checkpoint_id:
                    await _trim_checkpoints(checkpointer, thread_id, clean_checkpoint_id)
        
        return StreamingResponse(event_stream(), media_type="text/event-stream")

    @app.websocket("/ws/trace/{conversation_id}")
    async def trace_websocket(websocket: WebSocket, conversation_id: str):
        """LangGraph 实时追踪 WebSocket：推送节点级执行进度"""
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
        except Exception as e:
            print(f"[Rollback] Failed to determine previous turn: {e}")
        
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
        except Exception as e:
            print(f"[Rollback] Failed to list checkpoints: {e}")
        
        # 3. 执行三层回滚（互不阻断）
        trimmed = {"checkpoint": False, "messages": 0, "ltm": 0}
        
        try:
            await _trim_checkpoints(checkpointer, conversation_id, keep_checkpoint_id)
            trimmed["checkpoint"] = True
        except Exception as e:
            print(f"[Rollback] Checkpoint trim failed: {e}")
        
        try:
            trimmed["messages"] = await archive_store.delete_messages_from_turn(conversation_id, target_turn_id)
        except Exception as e:
            print(f"[Rollback] Message delete failed: {e}")
        
        try:
            if workflow._ltm_store:
                trimmed["ltm"] = await workflow._ltm_store.delete_facts_from_turn(conversation_id, target_turn_id)
        except Exception as e:
            print(f"[Rollback] LTM delete failed: {e}")
        
        # 4. 更新 conversation 的 message_count
        try:
            history = await archive_store.load_full_history(conversation_id)
            await conversation_store.update_conversation(
                conversation_id,
                message_count=len(history),
                metadata={"last_rollback_turn_id": target_turn_id}
            )
        except Exception as e:
            print(f"[Rollback] Failed to update conversation stats: {e}")
        
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
        except Exception as e:
            print(f"[MemoryStats] Failed to load checkpoint: {e}")
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
        """关闭时清理资源"""
        await archive_store.close()
        await file_store.close()
        await conversation_store.close()
        await user_store.close()
        await role_store.close()
        await workflow_store.close()
        await attendance_store.close()
        await org_store.close()
        await tenant_connector_store.close()
        await tenant_identity_store.close()

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
