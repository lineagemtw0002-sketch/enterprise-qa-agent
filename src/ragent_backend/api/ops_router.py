"""智能运维（`/admin/ops`）的路由模块——`create_app()` 分层的批次 1。

## 为什么是"路由工厂"而不是 `APIRouter` + `Depends`

`docs/app_layering_design.md` 原方案写的是 `Depends` + `app.state`。真正动手时
改成了工厂函数，理由是**风险**：`Depends` 要重写每个端点的签名、函数体也会跟着
动；工厂方案能让 **23 个端点的函数体逐字不变**，于是"行为没变"这件事可以用
AST 机器验证，而不是靠人看。

而当下**恰恰没有端到端安全网**——真实 HTTP 验证脚本要起后端，后端启动会预热
reranker/embedding/LLM，跟另一个会话正在跑的 LoRA 训练抢内存。
**没有安全网的时候，选可机验的那条路。**

两者都满足设计的核心要求（不建整个 app 就能测——工厂传假件即可，见
`tests/unit/test_ops_router.py`）。等 HTTP 复验能跑了，再决定要不要进一步换成
`Depends`——那时候有安全网，代价就低了。

## 依赖注入的边界

`_audit_log` **不在这里**：它被 9 个域共用，批次 0 已提取到
`api_helpers.audit_log`，这里通过参数注入 `create_app()` 里那层薄包装。
其余 10 个辅助函数是 `admin/ops` 专属（实测：没有任何非 ops 端点调用它们），
跟着一起搬。
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException

from src.ragent_backend import api_helpers  # noqa: F401  (供搬迁过来的代码引用)
from src.ops import connector_session
from src.ops import service_health
from src.ops.analysis import Alert
from src.ops.analysis import correlate_alerts
from src.ops.measured_baseline import with_measured_baseline
from src.ops.types import QueryRequest
from src.ops.types import TimeRange
from src.ragent_backend import aiops_scope
from src.ragent_backend.auth import AuthenticatedUser
from src.ragent_backend.ops_store import IllegalStatusTransition
from src.ragent_backend.ops_store import STATUS_PENDING_APPROVAL
from src.ragent_backend.ops_store import STATUS_REJECTED
from src.ragent_backend.ops_store import STATUS_REJECTED_PRE
from src.ragent_backend.schemas import AnalysisSummaryResponse
from src.ragent_backend.schemas import AnalyzeOpsIncidentRequest
from src.ragent_backend.schemas import AnalyzeOpsIncidentResponse
from src.ragent_backend.schemas import OpsConnectorRegisterTokenResponse
from src.ragent_backend.schemas import OpsConnectorResponse
from src.ragent_backend.schemas import OpsLiveOverviewResponse
from src.ragent_backend.schemas import OpsMetricsResponse
from src.ragent_backend.schemas import PostmortemEntryResponse
from src.ragent_backend.schemas import ProposeRemediationActionRequest
from src.ragent_backend.schemas import RegisterOpsConnectorRequest
from src.ragent_backend.schemas import RemediationActionResponse
from src.ragent_backend.schemas import RemediationScopeResponse
from src.ragent_backend.schemas import RoleOpsPermissionResponse
from src.ragent_backend.schemas import ServiceHealthEntry
from src.ragent_backend.schemas import ServiceThresholdsEntry
from src.ragent_backend.schemas import ServiceThresholdsRequest
from src.ragent_backend.schemas import SetOutcomeEffectiveRequest
from src.ragent_backend.schemas import SetRoleOpsPermissionRequest
from src.ragent_backend.schemas import UpsertRemediationScopeRequest
import asyncio
import time
logger = logging.getLogger(__name__)


def build_ops_router(
    *,
    ops_store: Any,
    ops_toolset: Any,
    ops_engine: Any,
    role_store: Any,
    org_store: Any,
    get_current_user: Callable,
    require_org_admin: Callable,
    audit_log: Callable,
) -> APIRouter:
    # ⚠️ 这三行把注入进来的依赖绑成搬迁前的原名。**这是刻意的**：
    # 函数体因此一个字都不用改，"行为没变"就能用 AST 逐句比对来证明，
    # 而不是靠人读 727 行 diff。改名是后续批次的事，不该夹带进搬迁这一步。
    _ops_engine = ops_engine
    _require_org_admin = require_org_admin
    _audit_log = audit_log
    """把智能运维那 23 个端点装进一个 `APIRouter` 并返回。

    ⚠️ **函数体逐字来自 `create_app()`**，只把装饰器从 `@app.xxx` 换成
    `@router.xxx`。任何"顺手改好一点"都会让"行为没变"这个验收判据失效。
    """
    router = APIRouter()

    def _ops_connector_response(c) -> OpsConnectorResponse:
        return OpsConnectorResponse(
            connection_id=c.connection_id, org_id=c.org_id, name=c.name,
            system_type=c.system_type, connector_status=c.connector_status,
            last_heartbeat_at=c.last_heartbeat_at, created_by=c.created_by,
            approval_timeout_minutes=c.approval_timeout_minutes, created_at=c.created_at,
        )

    def _remediation_scope_response(s) -> RemediationScopeResponse:
        return RemediationScopeResponse(
            scope_id=s.scope_id, org_id=s.org_id, connection_id=s.connection_id,
            action_type=s.action_type, scope_config=s.scope_config,
            configured_by=s.configured_by, updated_at=s.updated_at,
        )

    def _remediation_action_response(a, scope_check_reason: Optional[str] = None) -> RemediationActionResponse:
        return RemediationActionResponse(
            action_id=a.action_id, org_id=a.org_id, connection_id=a.connection_id,
            proposed_by=a.proposed_by, intent=a.intent, plan=a.plan,
            impact_radius=a.impact_radius, status=a.status,
            approver_user_id=a.approver_user_id, approved_at=a.approved_at,
            executed_at=a.executed_at, result=a.result, rollback_plan=a.rollback_plan,
            outcome_effective=a.outcome_effective, created_at=a.created_at,
            scope_check_reason=scope_check_reason, summary_id=a.summary_id,
        )

    def _analysis_summary_response(s) -> AnalysisSummaryResponse:
        return AnalysisSummaryResponse(
            summary_id=s.summary_id, org_id=s.org_id, connection_id=s.connection_id,
            summary=s.summary, evidence_refs=s.evidence_refs, created_at=s.created_at,
        )

    def _role_ops_permission_response(p) -> RoleOpsPermissionResponse:
        """薄包装，实现见 `api_helpers.role_ops_permission_response`（纯函数）。"""
        return api_helpers.role_ops_permission_response(p)

    async def _require_aiops_enabled_org(current_user: AuthenticatedUser):
        """薄包装，实现见 `api_helpers.require_aiops_enabled_org`。"""
        return await api_helpers.require_aiops_enabled_org(
            org_store=org_store, ops_store=ops_store, current_user=current_user,
        )

    async def _get_owned_connector(org_id: str, connection_id: str):
        """薄包装，实现见 `api_helpers.get_owned_connector`。"""
        return await api_helpers.get_owned_connector(
            ops_store=ops_store, org_id=org_id, connection_id=connection_id,
        )

    async def _get_owned_action(org_id: str, action_id: str):
        """跟 `_get_owned_connector` 同一个约定：404 不是 403。"""
        action = await ops_store.get_action(action_id)
        if action is None or action.org_id != org_id:
            raise HTTPException(status_code=404, detail="修复动作不存在")
        return action

    async def _require_can_approve(user_id: str, connection_id: str) -> None:
        perm = await ops_store.get_ops_permission(user_id, connection_id)
        if not perm["can_approve"]:
            raise HTTPException(status_code=403, detail="没有这个连接器的审批权限")

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

    @router.post("/api/v1/admin/ops/connectors", response_model=OpsConnectorResponse)
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

    @router.get("/api/v1/admin/ops/connectors", response_model=List[OpsConnectorResponse])
    async def admin_list_ops_connectors(
        current_user: AuthenticatedUser = Depends(_require_org_admin),
    ) -> List[OpsConnectorResponse]:
        org = await _require_aiops_enabled_org(current_user)
        connectors = await ops_store.list_connectors_for_org(org.org_id)
        return [_ops_connector_response(c) for c in connectors]

    @router.delete("/api/v1/admin/ops/connectors/{connection_id}")
    async def admin_delete_ops_connector(
        connection_id: str,
        current_user: AuthenticatedUser = Depends(_require_org_admin),
    ) -> dict:
        """硬删除，级联清掉这个连接器下的权限授权/令牌/修复动作/白名单/分析
        摘要——原来没有这个端点，登记了就撤不掉（刘德华摸底"授权管理"时
        为了清理自己建的联调用连接器发现的真实缺口）。跟 `admin_delete_
        collection` 同一个信任模型：管理员的显式硬删除，不检查在飞状态，
        见 `ops_store.delete_connector` 的类内说明。"""
        org = await _require_aiops_enabled_org(current_user)
        await _get_owned_connector(org.org_id, connection_id)
        await ops_store.delete_connector(connection_id)
        await _audit_log(
            current_user.user_id, "delete_ops_connector", "ops_connector", connection_id,
            {"org_id": org.org_id},
        )
        return {"success": True}

    @router.post(
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

    @router.put(
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

    @router.get(
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

    @router.put(
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

    @router.delete("/api/v1/admin/roles/{role_id}/ops-permissions/{connection_id}")
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

    @router.get(
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

    @router.post(
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

        ⚠️ **这是管理员手动提议的入口**，`proposed_by` 是发起这次调用的
        org_admin 本人——AI 分析后的自动提议走的是另一条路（`analyze_ops_
        incident` → LLM 决定调用 `propose_remediation` 工具，`proposed_by`
        在那条路径上是发起对话的用户），两条路径共用同一个状态机和越界判定，
        不是"占位符等 AI 落地后再改"，AI 分析已经落地了，这条注释早前的说法
        已经过期，一并更正。

        `summary_id` 可选：如果这次提议关联到之前某次分析（§9.2 事后复盘
        视图用），先校验它属于同一个 org 才链接——跨 org 引用静默丢弃，
        不拒绝这次提议本身（链接只是复盘辅助信息，不是提议正确性的前提，
        跟 `src/ops/tools.py::propose_remediation` 走 LLM 路径时的处理
        逻辑一致，两条路径的行为需要保持对称）。"""
        org = await _require_aiops_enabled_org(current_user)
        try:
            aiops_scope.validate_action_type(request.action_type)
        except aiops_scope.InvalidActionType as e:
            raise HTTPException(status_code=400, detail=str(e))
        await _get_owned_connector(org.org_id, connection_id)

        summary_id = request.summary_id
        if summary_id is not None:
            summary = await ops_store.get_analysis_summary(summary_id)
            if summary is None or summary.org_id != org.org_id:
                summary_id = None

        action = await ops_store.create_proposed_action(
            org.org_id, connection_id, current_user.user_id, request.intent,
            # ⚠️ **把 `action_type` 盖进 plan 一起落库。**
            # `remediation_actions` 没有 action_type 列，它只活在请求参数里；
            # 而执行前的白名单复查（`OpsToolset._recheck_scope`）需要知道类型，
            # 拿不到就**静默跳过那道检查**——提议到执行之间管理员完全可能把目标
            # 从白名单里摘掉，跳过复查等于打在一个已被禁止的目标上。
            # 之前只有"调用方恰好把 action_type 写进了 plan"时才检查得了。
            {**request.plan, "action_type": request.action_type},
            impact_radius=request.impact_radius, rollback_plan=request.rollback_plan,
            summary_id=summary_id,
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

        # 扩缩容的基线必须现场向连接器实测——AI 提议里那个 `baseline_instances`
        # 不参与判定（它是被约束方自己填的，抬高它就能抬高自己的天花板）。
        # 测不到时下面的判定会直接拒绝，见 `measured_baseline.py`。
        checked_plan = await with_measured_baseline(_ops_engine, org.org_id, request.plan)
        try:
            check = aiops_scope.check_target_in_scope(request.action_type, scope.scope_config, checked_plan)
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

    @router.get("/api/v1/admin/ops/remediation-actions", response_model=List[RemediationActionResponse])
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

    @router.get("/api/v1/admin/ops/analysis-summaries", response_model=List[AnalysisSummaryResponse])
    async def admin_list_analysis_summaries(
        limit: int = 50,
        current_user: AuthenticatedUser = Depends(get_current_user),
    ) -> List[AnalysisSummaryResponse]:
        """给"运维塔台·总览"的告警关联时间线用——只列出真实调用过
        `analyze_ops_incident` 才会产生的记录（`save_analysis_summary`），
        没有任何一次分析发生过的企业这里就是空列表，不是编几条垫底。
        权限过滤跟 `admin_list_remediation_actions` 同一套——org_admin 不
        过滤，其余角色按 `can_view` 显式授权的连接器集合过滤，没有任何
        授权时直接空列表，不能误当成"没传参数=不过滤"。"""
        org = await _require_aiops_enabled_org(current_user)
        viewable = await ops_store.viewable_connection_ids_for_user(current_user.user_id, org.org_id)
        if viewable is not None and not viewable:
            return []
        summaries = await ops_store.list_analysis_summaries(org.org_id, limit=limit)
        if viewable is not None:
            viewable_set = set(viewable)
            summaries = [s for s in summaries if s.connection_id in viewable_set]
        return [_analysis_summary_response(s) for s in summaries]

    @router.get("/api/v1/admin/ops/metrics", response_model=OpsMetricsResponse)
    async def admin_get_ops_metrics(
        current_user: AuthenticatedUser = Depends(get_current_user),
    ) -> OpsMetricsResponse:
        """§10.5 定义的四个 V1 验收指标——`docs/aiops_module_design.md` §9.3
        原本标注"这个模块完全没有定义过效果怎么衡量"，§10.5 补了公式，但直到
        今天为止从来没有一段代码真的算过。这是第一次接线。

        权限跟其余总览类端点同一套：org_admin 看全企业，其余角色只统计自己
        `can_view` 的连接器范围内的样本，没有任何授权时四个指标全部是"没有
        样本"（`None`），不是意外算出全企业的数字给一个没权限的人看。"""
        org = await _require_aiops_enabled_org(current_user)
        viewable = await ops_store.viewable_connection_ids_for_user(current_user.user_id, org.org_id)
        metrics = await ops_store.compute_ops_metrics(org.org_id, connection_ids=viewable)
        return OpsMetricsResponse(**metrics)

    @router.get(
        "/api/v1/admin/ops/connectors/{connection_id}/service-thresholds",
        response_model=List[ServiceThresholdsEntry],
    )
    async def admin_list_service_thresholds(
        connection_id: str,
        current_user: AuthenticatedUser = Depends(get_current_user),
    ) -> List[ServiceThresholdsEntry]:
        """列出这个连接器上配过的健康阈值覆盖。

        每一条同时返回 `thresholds`（管理员实际填的那几个字段）和 `effective`
        （叠加平台默认之后真正生效的六个值）——**只回其中一个都不够**：
        只回填过的，界面上没法告诉用户"没填的那几个现在是多少"；
        只回生效值，用户分不清哪些是自己配的、哪些是平台默认，
        点了"恢复默认"之后会以为界面坏了。
        """
        org = await _require_aiops_enabled_org(current_user)
        await _get_owned_connector(org.org_id, connection_id)
        rows = await ops_store.list_service_thresholds(connection_id)
        by_service = {r["service"]: r["thresholds"] for r in rows}
        return [
            ServiceThresholdsEntry(
                service=r["service"], thresholds=r["thresholds"],
                effective=service_health.resolve_thresholds(
                    by_service.get(service_health.WILDCARD_SERVICE)
                    if r["service"] != service_health.WILDCARD_SERVICE else None,
                    r["thresholds"],
                ).to_dict(),
                updated_at=r["updated_at"],
            )
            for r in rows
        ]

    @router.put(
        "/api/v1/admin/ops/connectors/{connection_id}/service-thresholds/{service}",
        response_model=ServiceThresholdsEntry,
    )
    async def admin_set_service_thresholds(
        connection_id: str, service: str, request: ServiceThresholdsRequest,
        current_user: AuthenticatedUser = Depends(get_current_user),
    ) -> ServiceThresholdsEntry:
        """配置某个服务（`service="*"` 表示该连接器的默认）的健康判定阈值。

        跟白名单配置同一档权限（org_admin）——这是配置不是审批。

        非法配置一律 400 **当场报错，不夹紧成一个看起来正常的值**：写错字段名
        静默忽略的后果是管理员以为改了、界面也显示保存成功，实际一点没生效。
        """
        org = await _require_aiops_enabled_org(current_user)
        await _get_owned_connector(org.org_id, connection_id)
        try:
            cleaned = service_health.validate_thresholds(request.thresholds)
        except service_health.InvalidThresholds as e:
            raise HTTPException(status_code=400, detail=str(e))

        saved = await ops_store.upsert_service_thresholds(
            org.org_id, connection_id, service, cleaned, current_user.user_id)
        wildcard = None
        if service != service_health.WILDCARD_SERVICE:
            existing = await ops_store.list_service_thresholds(connection_id)
            wildcard = next((r["thresholds"] for r in existing
                             if r["service"] == service_health.WILDCARD_SERVICE), None)
        return ServiceThresholdsEntry(
            service=saved["service"], thresholds=saved["thresholds"],
            effective=service_health.resolve_thresholds(wildcard, saved["thresholds"]).to_dict(),
            updated_at=saved["updated_at"],
        )

    @router.delete("/api/v1/admin/ops/connectors/{connection_id}/service-thresholds/{service}")
    async def admin_delete_service_thresholds(
        connection_id: str, service: str,
        current_user: AuthenticatedUser = Depends(get_current_user),
    ) -> Dict[str, bool]:
        """删掉一条覆盖 = 这个服务回到上一层（连接器默认 → 平台默认）。"""
        org = await _require_aiops_enabled_org(current_user)
        await _get_owned_connector(org.org_id, connection_id)
        return {"deleted": await ops_store.delete_service_thresholds(connection_id, service)}

    @router.post("/api/v1/admin/ops/analyze", response_model=AnalyzeOpsIncidentResponse)
    async def admin_analyze_ops_incident(
        request: AnalyzeOpsIncidentRequest,
        current_user: AuthenticatedUser = Depends(get_current_user),
    ) -> AnalyzeOpsIncidentResponse:
        """对某个服务跑一次分析（异常检测 → 告警关联 → 根因辅助）。

        ## 为什么需要这个端点

        在它之前，`analyze_ops_incident` **只注册给了 LLM**——运维人员在塔台上
        看到一个服务变红，没有任何办法让系统去分析它，只能去对话里说一句、
        指望 7B 模型决定调那个工具。实测三次只成功一次：自然措辞时模型经常
        直接用知识库回答、或者不带 `target` 参数就调。
        **这跟"审批通过后没人能执行"是同一类缺陷**：人在界面上做不了 AI 能做
        的事，而这件事本来就该是人主动发起的。

        ## 跟 LLM 那条路走同一段代码

        直接调 `ops_toolset.analyze_ops_incident`，不另写一份编排——落库格式、
        依据引用规则、降级行为两条路径完全一致，不会随时间漂移。

        ## 权限

        `can_view` 就够（跟列表类端点同一档），**不是 `can_approve`**：
        分析是只读的，它不改变任何东西，也不给任何人执行资格。
        把只读操作卡在审批权限上，只会逼人去要更高的权限。
        """
        org = await _require_aiops_enabled_org(current_user)
        viewable = await ops_store.viewable_connection_ids_for_user(current_user.user_id, org.org_id)
        if viewable is not None and not viewable:
            raise HTTPException(status_code=403, detail="你没有被授权查看任何运维系统")

        outcome = await ops_toolset.analyze_ops_incident(
            org.org_id, request.target, metric=request.metric,
            window_minutes=request.window_minutes, connection_ids=viewable,
        )
        await _audit_log(
            current_user.user_id, "analyze_ops_incident", "ops_analysis",
            request.target, {"ok": outcome.ok},
        )
        data = outcome.data or {}
        return AnalyzeOpsIncidentResponse(
            ok=outcome.ok, message=outcome.message,
            summary_id=data.get("summary_id"),
            # "跑通了但一切正常"不是失败——调用方要能区分它和"查不到数据"。
            has_findings=bool(data.get("anomaly_targets") or data.get("incident_count")),
            degraded=bool(data.get("degraded")),
            anomaly_targets=list(data.get("anomaly_targets") or []),
            alert_count=int(data.get("alert_count") or 0),
            incident_count=int(data.get("incident_count") or 0),
            # 去重：指标查询和告警查询是两次 fan-out，同一个连接器失败会被报两次。
            # 界面上把同一个系统列两遍，看起来像"有两个系统出问题了"。
            unavailable=sorted({u.get("system", "") for u in (data.get("unavailable") or []) if u.get("system")}),
        )

    @router.get("/api/v1/admin/ops/live-overview", response_model=OpsLiveOverviewResponse)
    async def admin_ops_live_overview(
        current_user: AuthenticatedUser = Depends(get_current_user),
    ) -> OpsLiveOverviewResponse:
        """总览大屏里需要**现场问连接器**的两块：服务健康网格 + 今日告警合并。

        ## 服务清单为什么不存库

        服务清单由连接器自动发现（`kind="service_health"`），平台侧一条都不存
        ——跟 §3.1「不落库原始运维数据」是同一条原则。清单是运维现状的投影，
        存下来立刻开始跟现实脱节：服务下线了平台还画着它，新服务上线了没人
        去补。业界（Datadog Service Catalog / Grafana 的 label 发现）也是
        发现优先、配置只补 owner 这类人类才知道的元数据。

        ## 今日告警合并为什么现场算

        向连接器要当天全部告警，**当场跑一次 `correlate_alerts`**。
        另一种做法是只统计"人工触发过分析"的那部分告警，但那个口径答不了
        "今天"这个问题——没人去触发分析的时段就当作没有告警发生过。

        ## 部分失败必须显式暴露

        `unavailable` 带着"哪些连接器没查到"回给前端。服务网格少了几个服务，
        跟"这些服务都健康"在视觉上没有任何区别——不标注就是在骗人（§3.5 第 4 条）。
        """
        org = await _require_aiops_enabled_org(current_user)
        viewable = await ops_store.viewable_connection_ids_for_user(current_user.user_id, org.org_id)
        if viewable is not None and not viewable:
            return OpsLiveOverviewResponse()

        now = time.time()
        day_start = now - 86400.0
        health_req = QueryRequest(kind="service_health", target="",
                                  time_range=TimeRange(start_ts=now - 300.0, end_ts=now))
        alert_req = QueryRequest(kind="alert", target="",
                                 time_range=TimeRange(start_ts=day_start, end_ts=now))
        health_res, alert_res = await asyncio.gather(
            _ops_engine.query(org.org_id, health_req, connection_ids=viewable),
            _ops_engine.query(org.org_id, alert_req, connection_ids=viewable),
        )

        connectors = await ops_store.list_connectors_for_org(org.org_id)
        names = {c.connection_id: c.name for c in connectors}
        # 一次查完全部连接器的阈值覆盖，不逐个查（N+1）。
        overrides = await ops_store.thresholds_by_connection([r.connection_id for r in health_res.results])
        services: List[ServiceHealthEntry] = []
        for result in health_res.results:
            services.extend(
                ServiceHealthEntry(**s.to_dict())
                for s in service_health.points_to_services(
                    # `points` 恒为 `DataPoint`（`QueryResult.points` 的类型），
                    # 这里不做"也许是 dict"的兼容——那种防御只会让真正的类型
                    # 错误变成静默的空结果。
                    [{"ts": p.ts, "value": p.value, "text": p.text, "labels": p.labels}
                     for p in result.points],
                    connection_id=result.connection_id,
                    connector_name=names.get(result.connection_id),
                    overrides=overrides.get(result.connection_id),
                )
            )

        alerts = [
            Alert(alert_id=f"{result.connection_id}:{i}", ts=p.ts,
                  target=(p.labels or {}).get("target", ""), labels=p.labels or {},
                  text=p.text or "", severity=(p.labels or {}).get("severity", "warning"))
            for result in alert_res.results
            for i, p in enumerate(result.points)
        ]
        correlated = correlate_alerts(alerts) if alerts else None

        # 带上连接器 id 的尾巴——**同名连接器是现实**（同一个企业接两套
        # Prometheus 很常见，客户也未必会起不同的名字），只显示名字的话
        # "哪一个不可用"根本分不出来。
        unavailable = sorted({
            f"{names[e.connection_id]}（{e.connection_id[-6:]}）" if e.connection_id in names
            else e.connection_id
            for e in list(health_res.errors) + list(alert_res.errors)
        })
        return OpsLiveOverviewResponse(
            services=services,
            today_alert_count=correlated.original_count if correlated else 0,
            today_incident_count=len(correlated.incidents) if correlated else 0,
            today_noise_reduction=correlated.noise_reduction if correlated else None,
            unavailable=unavailable,
        )

    @router.get("/api/v1/admin/ops/postmortems", response_model=List[PostmortemEntryResponse])
    async def admin_list_postmortems(
        limit: int = 100,
        current_user: AuthenticatedUser = Depends(get_current_user),
    ) -> List[PostmortemEntryResponse]:
        """§9.2"事后复盘聚合视图"的最小可行版——设计文档原话："没有这个视图，
        本模块的自动修复到底有没有用将无法被回顾评估，是一条真实的遗留风险"。
        列出全部终态动作（completed/failed），附上触发它的分析摘要（如果提议
        时链接过）。权限跟其余总览类端点同一套。"""
        org = await _require_aiops_enabled_org(current_user)
        viewable = await ops_store.viewable_connection_ids_for_user(current_user.user_id, org.org_id)
        entries = await ops_store.list_postmortems(org.org_id, connection_ids=viewable, limit=limit)
        return [
            PostmortemEntryResponse(
                action=_remediation_action_response(e.action), linked_summary=e.linked_summary,
            )
            for e in entries
        ]

    @router.post(
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

    @router.post(
        "/api/v1/admin/ops/remediation-actions/{action_id}/execute",
        response_model=RemediationActionResponse,
    )
    async def admin_execute_remediation_action(
        action_id: str,
        current_user: AuthenticatedUser = Depends(get_current_user),
    ) -> RemediationActionResponse:
        """把一条**已批准**的修复动作真正下发到客户环境。

        ## 为什么需要这个端点

        在它之前，这个模块**唯一的执行通路是 LLM 工具**——审批通过之后，人在
        运维塔台上没有任何办法让它执行，只能去对话里跟模型说一句，指望模型把
        `action_id` 传对。实测两次都没传对（一次被意图路由判成了别的类型、
        一次参数名错），动作永远停在 `approved`。
        业界的运维审批台都是「批准 → 人点执行」，没有哪个是"批准完去跟聊天
        机器人说一声"。

        ## 跟 LLM 那条路走的是同一段代码

        直接调 `ops_toolset.execute_approved_remediation`，**不另写一份执行
        逻辑**——四道检查（跨 org / 状态必须是 approved / 白名单复查 / 下发
        失败也要落到 failed）两条路径完全一致，不会随时间漂移出差别。
        一个"给人用的快捷入口"如果比 AI 那条路少一道检查，它就是这个模块最大
        的漏洞，而不是一个便利功能。

        ## 权限

        跟批准同一档（`can_approve`）。V1 不区分"批准人"和"执行人"——设计文档
        没有定义过第三档权限，凭空造一档不如留给真实需求出现时再定。
        """
        org = await _require_aiops_enabled_org(current_user)
        action = await _get_owned_action(org.org_id, action_id)
        await _require_can_approve(current_user.user_id, action.connection_id)

        outcome = await ops_toolset.execute_approved_remediation(
            org.org_id, action.action_id,
            # 从落库的 plan 里取，不让调用方传——调用方能传就意味着能传错，
            # 而传错的后果是白名单复查被跳过（见上面提议处的说明）。
            action_type=(action.plan or {}).get("action_type"),
        )
        await _audit_log(
            current_user.user_id, "execute_remediation_action", "remediation_action",
            action.action_id, {"ok": outcome.ok},
        )
        if outcome.refused:
            # 被四道检查挡下 = 业务规则冲突，不是服务器错误，调用方要能区分。
            raise HTTPException(status_code=409, detail=outcome.message)
        refreshed = await ops_store.get_action(action.action_id)
        return _remediation_action_response(refreshed or action)

    @router.post(
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

    @router.post(
        "/api/v1/admin/ops/remediation-actions/{action_id}/outcome",
        response_model=RemediationActionResponse,
    )
    async def admin_set_remediation_outcome(
        action_id: str,
        request: SetOutcomeEffectiveRequest,
        current_user: AuthenticatedUser = Depends(get_current_user),
    ) -> RemediationActionResponse:
        """§10.5"事后有效性"指标的数据来源——人工事后补标"这次修复是否真的
        解决了问题"，`ops_store.set_outcome_effective` 早就写好了但从来没有
        端点调用过（真实存在过的死代码，不是这次顺手加的）。权限跟批准/拒绝
        同一档：能拍板批不批的人，也是自然该来判断"批准的这次到底有没有用"
        的人。不限制当前状态——`set_outcome_effective` 本身"不做状态限制"是
        既有的设计决定（见它的类内说明），这里不额外收紧。"""
        org = await _require_aiops_enabled_org(current_user)
        action = await _get_owned_action(org.org_id, action_id)
        await _require_can_approve(current_user.user_id, action.connection_id)
        action = await ops_store.set_outcome_effective(action.action_id, request.effective)
        await _audit_log(
            current_user.user_id, "set_remediation_outcome", "remediation_action", action.action_id,
            {"effective": request.effective},
        )
        return _remediation_action_response(action)

    return router
