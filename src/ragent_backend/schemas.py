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
    """用户当前拥有的角色摘要，用于 /auth/me 展示——角色直接携带知识库权限
    （见 role_store.py 顶部说明），这里不重复带 collection_names，知识库信息
    统一走 allowed_collections（后端已经算好并集）。"""
    role_id: str
    name: str
    display_name: str


class OrganizationSummary(BaseModel):
    org_id: str
    name: str
    is_platform: bool = False
    # 2026-08-26 补：智能运维模块是否已对本企业开通（`organizations.
    # aiops_module_enabled`）。原来只有 PUT 端点、没有任何 GET 会回这个字段，
    # 前端没有任何合规的方式知道"导航入口该不该显示"，只能靠调业务端点吃
    # 403 来试探——那正是"入口按开关显示，不能只在点击时才发现被拒"这条
    # 要求想避免的。见 CLAUDE.md §5。
    aiops_module_enabled: bool = False


class MeResponse(BaseModel):
    user_id: str
    username: str
    roles: List[RoleSummary]          # 一人一角色（当前业务规则），列表长度 0 或 1
    allowed_collections: List[str]    # 保留：后端按角色关联算好的知识库并集，前端不用二次拼接
    organization: Optional[OrganizationSummary] = None
    created_at: float
    # 2026-08-26 补：role_ops_systems 落地后，"能不能看到运维审批入口"不再
    # 等价于"是不是 org_admin"——一个被授予 can_approve 的普通员工也该看到。
    # 跟 allowed_collections 同一个思路：后端把"该用户在任意连接器上是否有
    # 权限"这个并集算好，前端不用重复实现 org_admin 通配符这层判断逻辑。
    ops_can_view: bool = False
    ops_can_approve: bool = False


class ChangePasswordRequest(BaseModel):
    old_password: str = Field(..., min_length=1)
    new_password: str = Field(..., min_length=6)


# ============== 管理后台 API（仅 super_admin） ==============

class AdminUserResponse(BaseModel):
    user_id: str
    username: str
    roles: List[RoleSummary]
    allowed_collections: List[str]
    organization: Optional[OrganizationSummary] = None
    created_at: float
    # 2026-08-26 账号生命周期（docs/account_lifecycle_design.md §4.2 §4.1b）。
    # 都给默认值，既有的构造点不用全改。
    disabled_at: Optional[float] = None
    activated_at: Optional[float] = None
    # 还没设过密码（判据是 password_hash IS NULL，不是 activated_at IS NULL——
    # 后者会把全部存量账号误判成待激活，见 user_store.User 的说明）。
    pending_activation: bool = False


class AdminCreateUserRequest(BaseModel):
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=6)
    role_ids: List[str] = Field(default_factory=list, description="初始分配的角色 id 列表，最多 1 个（一人一角色）")
    org_id: Optional[str] = Field(
        default=None,
        description="所属企业；非平台管理员建号时后端会忽略这个字段，强制用调用者自己的 org_id",
    )


class AdminCreatedUserCredential(BaseModel):
    """建号 / 导入之后，一次性回给管理员的激活凭证。

    ⚠️ **`activation_code` 是明文，全系统只在这一个响应里出现一次。**
    它不落库（库里只有 SHA-256）、不写日志、刷新页面就没了。
    管理员必须当场保存并分发——用户已定不做邮件短信（O-1），
    分发只能是人工的。
    """

    username: str
    activation_code: str
    expires_at: float


class SetSeatLimitRequest(BaseModel):
    # None = 不限。0 是合法的：用来暂停一家企业的新建号。
    seat_limit: Optional[int] = Field(default=None, ge=0)


class SetUserDisabledRequest(BaseModel):
    disabled: bool


class ActivateAccountRequest(BaseModel):
    """无鉴权端点 `/api/v1/activate` 的入参。

    ⚠️ 这是全系统唯一不带 Authorization 的写端点（设计 §6 风险 R-4）。
    密码长度下限跟 `AdminCreateUserRequest.password` 保持一致（6），
    不能因为这条路径没有管理员盯着就放松。
    """

    username: str = Field(..., min_length=1)
    activation_code: str = Field(..., min_length=1)
    new_password: str = Field(..., min_length=6)


class BulkImportRowResult(BaseModel):
    line_no: int
    username: str
    action: str  # create / update / error
    reason: Optional[str] = None


class BulkImportResponse(BaseModel):
    """导入结果。**dry-run 与真跑用同一个响应模型**，靠 `applied` 区分。

    同一个模型是刻意的：如果预演和真跑返回不同的形状，前端就要写两套渲染，
    而两套渲染必然会漂移——预演展示的东西就不再等于真跑会发生的事，
    预演也就失去了意义。
    """

    applied: bool  # False = 仅预演，什么都没落库
    summary: str
    to_create: int
    to_update: int
    errors: List[BulkImportRowResult] = Field(default_factory=list)
    seat_ok: bool = True
    seats_used: int = 0
    seat_limit: Optional[int] = None
    fatal_error: Optional[str] = None
    # 只有 applied=True 且真的建了号时才有值，见 AdminCreatedUserCredential
    credentials: List[AdminCreatedUserCredential] = Field(default_factory=list)


# ============== 组织管理 API（仅平台管理员） ==============

class AdminOrganizationResponse(BaseModel):
    org_id: str
    name: str
    is_platform: bool
    created_at: float
    # 2026-08-26 席位（docs/account_lifecycle_design.md §4.4）。
    # seat_limit=None 表示不限；seats_used 是当前在用（只数未停用的）。
    # 两个一起给，是因为平台管理员改上限时必须看得到当前用量——
    # 只给上限的话，把 5 改成 3 会不会当场锁死一家企业，他不知道。
    seat_limit: Optional[int] = None
    seats_used: Optional[int] = None
    # 同 OrganizationSummary 那条注释——super_admin 的企业列表/开关控件需要
    # 这个字段才能渲染出"当前是开是关"，而不是两个盲按钮。
    aiops_module_enabled: bool = False


class AdminCreateOrganizationRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)


# ============== 租户连接器 API（仅平台管理员，见 knowledge-base-tenant-federation.md /
# attendance-tenant-federation.md）==============

class TenantConnectorResponse(BaseModel):
    connector_id: str
    org_id: str
    capability: str
    connector_type: str
    endpoint: Optional[str]
    # 出于安全考虑不回传原始 token，只回传"是否已配置"——前端据此显示
    # "已配置凭证"还是"未配置"，编辑时留空表示不修改现有 token。
    has_token: bool
    remote_tool_name: Optional[str]
    field_mapping: Dict[str, Any]
    is_active: bool
    created_at: float
    # 打开连接器面板时现查的存活状态，不是存量字段——见 app.py 里
    # `_check_connector_health`。取值：connected / unreachable / disabled / internal。
    health_status: str


class UpsertTenantConnectorRequest(BaseModel):
    connector_type: str = Field(..., min_length=1)
    endpoint: Optional[str] = None
    token: Optional[str] = Field(default=None, description="留空表示不修改现有 token")
    remote_tool_name: Optional[str] = None
    field_mapping: Dict[str, Any] = Field(default_factory=dict)
    is_active: bool = True


class GatewayConnectorResponse(BaseModel):
    """网关监控页一行——跨所有企业的连接器 + 运行时调用指标，仅平台管理员可见。"""
    connector_id: str
    org_id: str
    org_name: str
    capability: str
    connector_type: str
    endpoint: Optional[str]
    is_active: bool
    health_status: str
    call_count: int
    failure_count: int
    last_called_at: Optional[float]
    last_latency_ms: Optional[float]
    last_error: Optional[str]


# ============== 智能运维模块 API（docs/aiops_module_design.md，阶段一存储层
# 已实施；这里是阶段二——端点接线用的 schema）==============
# 只有 org_admin 能注册连接器/配置修复范围白名单（§3.3.1）；只有 super_admin
# 能切换 aiops_module_enabled 开关（§4.1）；两条边界跟角色管理/连接器管理是
# 同一套既有分工，不是这次新发明的。

class OpsConnectorResponse(BaseModel):
    connection_id: str
    org_id: str
    name: str
    system_type: str
    connector_status: str
    last_heartbeat_at: Optional[float]
    created_by: str
    approval_timeout_minutes: int
    created_at: float


class RegisterOpsConnectorRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    system_type: str = Field(..., min_length=1, max_length=64)
    approval_timeout_minutes: int = Field(
        default=30, description="[5, 1440] 分钟，越界会被拒绝，不静默夹紧（见 aiops_scope.py）"
    )


class SetAiopsModuleEnabledRequest(BaseModel):
    enabled: bool


class OpsConnectorRegisterTokenResponse(BaseModel):
    connection_id: str
    register_token: str = Field(..., description="只在这次响应里明文出现一次，平台只存哈希")
    expires_at: float


class RemediationScopeResponse(BaseModel):
    scope_id: str
    org_id: str
    connection_id: str
    action_type: str
    scope_config: Dict[str, Any]
    configured_by: str
    updated_at: float


class UpsertRemediationScopeRequest(BaseModel):
    scope_config: Dict[str, Any]


class RoleOpsPermissionResponse(BaseModel):
    role_id: str
    connection_id: str
    can_view: bool
    can_approve: bool


class SetRoleOpsPermissionRequest(BaseModel):
    can_view: bool = False
    can_approve: bool = False


class AnalysisSummaryResponse(BaseModel):
    summary_id: str
    org_id: str
    connection_id: str
    summary: str
    evidence_refs: List[Dict[str, Any]]
    created_at: float


class SetOutcomeEffectiveRequest(BaseModel):
    effective: bool


class OpsMetricsResponse(BaseModel):
    # 三个字段用 Optional 而不是缺省 0.0——分母为 0（还没有样本）跟"比例
    # 恰好是 0"是两件不同的事，见 `ops_store.py::compute_ops_metrics` 的说明。
    approval_timeliness_rate: Optional[float] = None
    execution_success_rate: Optional[float] = None
    alert_noise_reduction: Optional[float] = None
    # MTTR = 最早关联告警时间 → 修复动作执行完成时间（2026-08-27 用户拍板的
    # 口径）。只统计真正 completed 且链接了分析摘要的动作，没有样本时是 None。
    mttr_seconds: Optional[float] = None
    outcome_effective_counts: Dict[str, int]
    sample_sizes: Dict[str, int]


class ServiceHealthEntry(BaseModel):
    service: str
    status: str                       # critical / warning / stale / ok
    error_rate: Optional[float] = None
    p95_latency_ms: Optional[float] = None
    queue_latency_ms: Optional[float] = None
    connection_id: Optional[str] = None
    connector_name: Optional[str] = None


class AnalyzeOpsIncidentRequest(BaseModel):
    target: str = Field(..., min_length=1, description="要分析哪个服务")
    metric: str = "error_rate"
    window_minutes: int = Field(default=60, ge=5, le=1440)


class AnalyzeOpsIncidentResponse(BaseModel):
    """⚠️ 字段名**照抄** `OpsToolset.analyze_ops_incident` 返回的 `data`，不是凭
    语义猜的——本产品线已经三次栽在猜字段名上（§10.5 指标的四个键、
    `/auth/me` 的 roles 形状、`Segmented` 白屏）。"""

    ok: bool
    message: str
    summary_id: Optional[str] = None
    # 分析可能"跑通了但没发现问题"——那**不是失败**。调用方要能区分
    # "没查到数据/被拒绝"和"查了，一切正常"。
    has_findings: bool = False
    # 降级 = RCA 那一步没有模型参与，结论只是数据复述。必须显式告诉用户，
    # 否则他会把一段复述当成分析结论。
    degraded: bool = False
    anomaly_targets: List[str] = []
    alert_count: int = 0
    incident_count: int = 0
    # 部分连接器查不到时要显式说出来（§3.5 第 4 条）——
    # "分析完了没发现问题"和"有一半数据没拿到"在结论可信度上完全不同。
    unavailable: List[str] = []


class ServiceThresholdsRequest(BaseModel):
    # 可以只写其中几个字段，缺的逐字段回退到连接器默认、再回退到平台默认。
    # **不要求写全**——要求写全等于把没打算改的那几个也冻结在填写那天的值上。
    thresholds: Dict[str, float]


class ServiceThresholdsEntry(BaseModel):
    service: str                      # "*" = 该连接器的默认配置
    thresholds: Dict[str, float]      # 只含被覆盖的字段，不是完整的六个
    effective: Dict[str, float]       # 叠加平台默认之后**真正生效**的六个值
    updated_at: Optional[float] = None


class OpsLiveOverviewResponse(BaseModel):
    """总览大屏里**需要现场问连接器**的那部分（服务健康网格 + 今日告警合并）。

    跟 `/admin/ops/metrics`（纯数据库统计）分成两个端点，是因为这个要走
    联邦查询、耗时取决于客户环境，不该把纯数据库的那几个指标一起拖慢。

    `unavailable` 非空 = 部分连接器没查到（离线/超时/越权），**必须在界面上
    显示出来**（§3.5 第 4 条）——服务网格少了几个服务和"这些服务都健康"
    在视觉上没有区别，不标注就是在骗人。
    """

    services: List[ServiceHealthEntry] = []
    today_alert_count: int = 0
    today_incident_count: int = 0
    today_noise_reduction: Optional[float] = None
    unavailable: List[str] = []


class RemediationActionResponse(BaseModel):
    action_id: str
    org_id: str
    connection_id: str
    proposed_by: str
    intent: str
    plan: Dict[str, Any]
    impact_radius: Optional[str]
    status: str
    approver_user_id: Optional[str]
    approved_at: Optional[float]
    executed_at: Optional[float]
    result: Optional[Dict[str, Any]]
    rollback_plan: Optional[Dict[str, Any]]
    outcome_effective: Optional[bool]
    created_at: float
    # 越界判定的结果（§3.3.1，进入 pending_approval 之前的硬拦截）——只在刚
    # proposed 完就被拒绝（rejected_pre）时有值，给调用方看拒绝原因用，不
    # 落库（remediation_actions 表没有这个字段，越界原因是判定时才有的临时
    # 信息，落库的只有"结果是 rejected_pre"这个状态本身）。
    scope_check_reason: Optional[str] = None
    summary_id: Optional[str] = None


class PostmortemEntryResponse(BaseModel):
    action: RemediationActionResponse
    linked_summary: Optional[str] = None


class ProposeRemediationActionRequest(BaseModel):
    action_type: str = Field(..., description="必须是 V1 四类之一，见 aiops_scope.ACTION_TYPES")
    intent: str = Field(..., min_length=1, description="这个修复动作要解决什么问题")
    plan: Dict[str, Any] = Field(
        ..., description="动作类型专属的目标字段（如 restart_service 的 target），"
        "同时也是越界判定 check_target_in_scope 的 proposed 参数"
    )
    impact_radius: Optional[str] = Field(
        default=None,
        description="AI 定性推断，非实时拓扑图，不允许当成唯一决策依据（§3.3）",
    )
    rollback_plan: Optional[Dict[str, Any]] = None
    summary_id: Optional[str] = Field(
        default=None,
        description="可选，关联到哪次 analyze_ops_incident 分析（§9.2 事后复盘视图用）。"
        "跨企业引用会被静默丢弃，不会报错。",
    )


# 2026-08-26 已删除：这里原有 AdminTestKBQueryRequest/Response、
# AdminKbCollectionStat 三个类，专供已删除的【测试专用】知识库超权测试端点使用，
# 随端点一并删除，见 `CLAUDE.md` §5「已修复」。AdminKbChunkPreview 继续保留，
# 它是 `admin_list_collection_chunks`（企业管理员自助管理的正式功能）在用的。

class AdminKbChunkPreview(BaseModel):
    chunk_id: str
    text: str
    source_path: str = ""
    kb_name: Optional[str] = None


# ============== 角色管理 API ==============
# 角色直接携带知识库权限（role_store.py 顶部说明）。分两类，用 org_id 是否
# 为空区分：全局角色（org_id=None，系统权限档位 + 跨企业共用的部门身份，只有
# 平台管理员能建/改名/删）；企业角色（org_id 非空，某家企业管理员自己建的
# 角色，只在自己企业内可见/可分配，能配置知识库关联）。平台管理员管全局角色
# （/admin/roles，不涉及知识库——运营商的角色没有知识库权限，见 role_store.py），
# 企业管理员管自己企业的角色（同一组端点，权限档位不同则看到的范围不同）。

class RoleResponse(BaseModel):
    role_id: str
    name: str
    display_name: str
    is_system: bool
    org_id: Optional[str] = None
    collection_names: List[str] = Field(default_factory=list)
    created_at: float


class CreateRoleRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    display_name: str = Field(..., min_length=1, max_length=128)


class UpdateRoleRequest(BaseModel):
    display_name: str = Field(..., min_length=1, max_length=128)


class SetRoleCollectionsRequest(BaseModel):
    collection_names: List[str] = Field(default_factory=list)


# ============== 企业自建知识库 API（仅 org_admin，见 collection_store.py） ==============

class CollectionResponse(BaseModel):
    collection_name: str
    display_name: str
    chunk_count: int = 0
    created_at: float


class CreateCollectionRequest(BaseModel):
    collection_name: str = Field(..., min_length=1, max_length=64)
    display_name: str = Field(..., min_length=1, max_length=128)


# ============== 知识库目录 + 上传 API（任意登录用户，见 app.py collections_catalog） ==============
# 跟上面"企业自建知识库 API"的区别：上面那组是 org_admin 专属的管理入口（新建/
# 列出本企业注册过的库）；这组是给普通员工上传文档用的——列出自己企业名下
# 全部知识库（不管有没有权限都列出来，配合 `accessible` 字段在前端置灰，而不是
# 直接从列表里拿掉，员工至少知道"这个库存在，只是我看不了"）。

class CollectionCatalogEntry(BaseModel):
    collection_name: str
    display_name: str
    accessible: bool


class UploadStartedResponse(BaseModel):
    upload_id: str


class UploadProgressResponse(BaseModel):
    upload_id: str
    collection_name: str
    filename: str
    stage: str  # integrity / load / split / transform / dedup / embed / upsert，见 pipeline.py on_progress
    current: int
    total: int
    done: bool
    success: Optional[bool] = None  # done=False 时未知，保持 None
    chunk_count: int = 0
    duplicate_chunk_count: int = 0
    error: Optional[str] = None


# ============== 委托模式企业知识库上传（方案 2，见 knowledge-base-tenant-
# federation.md 第 4.4 节）==============

class TenantKbUploadResponse(BaseModel):
    chunk_count: int
    message: Optional[str] = None


# ============== 运营仪表盘 API（仅平台管理员，见 dashboard_stats.py） ==============

class DashboardOverviewResponse(BaseModel):
    window: str
    session_count: int
    session_count_change: Optional[float] = None  # 环比百分比；上一窗口是 0 时给 None，不算除零的"无穷大涨幅"
    message_count: int
    message_count_change: Optional[float] = None
    active_users: int
    active_users_change: Optional[float] = None
    avg_latency_ms: Optional[float] = None
    avg_latency_ms_change: Optional[float] = None


class DashboardTrendPointResponse(BaseModel):
    bucket: str
    value: float


class DashboardTrendResponse(BaseModel):
    metric: str
    window: str
    points: List[DashboardTrendPointResponse]


class AuditLogResponse(BaseModel):
    """审计日志一行——管理后台变更操作或工具调用，见 audit_store.py。"""
    audit_id: str
    org_id: Optional[str] = None
    org_name: Optional[str] = None
    user_id: Optional[str] = None
    username: Optional[str] = None
    action: str
    resource_type: str
    resource_id: Optional[str] = None
    detail: Dict[str, Any] = Field(default_factory=dict)
    success: bool
    created_at: float


class AuditLogListResponse(BaseModel):
    items: List[AuditLogResponse]
    total: int


class CostOverviewResponse(BaseModel):
    """成本与质量仪表盘概览——token 用量/预估成本来自 conversation_archive，
    工具调用成功率来自 audit_logs（见 dashboard_stats.py）。"""
    window: str
    total_tokens: int
    total_tokens_change: Optional[float] = None
    estimated_cost_usd: Optional[float] = None  # None 表示当前模型没有可靠的单价估算（如本地 Ollama）
    estimated_cost_usd_change: Optional[float] = None
    tool_call_count: int
    tool_success_rate: Optional[float] = None  # 0-100，没有任何工具调用时为 None
    tool_success_rate_change: Optional[float] = None
    tool_failure_count: int


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
    kb_sources: List[str] = Field(default_factory=list)


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
    （`GET /api/v1/admin/workflow-templates`）复用同一个响应模型——纯表单结构，
    不含审批人信息，那是按企业配置的，见 WorkflowApproverAssignmentResponse。"""
    template_id: str
    workflow_type: str
    display_name: str
    description: str
    required_fields: List[WorkflowFieldSpec]
    attachments_note: str
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


# ============== 工作流审批人分配 API（仅 org_admin，按企业隔离） ==============
# "这类流程谁来批"是企业内部的事，见 workflow_store.py 顶部说明——同一个
# workflow_type 在不同企业可以配不同的审批角色。

class WorkflowApproverAssignmentResponse(BaseModel):
    workflow_type: str
    display_name: str
    approver_role_id: Optional[str] = None
    approver_role_display_name: Optional[str] = None


class SetWorkflowApproverRequest(BaseModel):
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
    attachment_count: int = 0
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
    kb_sources: List[str]  # 本轮回答实际用到的知识库 collection 名（去重），UI 来源角标用
    # 本轮 generate 节点的 token 用量（成本可观测性用，见 workflow.py
    # _generate_node/_archive_node、dashboard_stats.py）。字段：
    # prompt_tokens/completion_tokens/total_tokens/estimated（bool，True 表示
    # LLM 没有回传真实 usage_metadata，是按字符数估算的）。short-circuit
    # 分支（如 ACL 拒绝、clarify/workflow）没有调用 LLM，此字段为 None。
    last_turn_tokens: Optional[Dict[str, Any]]
    
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
    _turn_start_ts: float  # 本轮图执行起点（_session_node 设置），供 _archive_node 算端到端响应耗时


class ArchivedMessage(BaseModel):
    """归档消息的数据结构（存入 PostgreSQL）"""
    role: str
    content: str
    message_id: Optional[str] = None
    ts: float
