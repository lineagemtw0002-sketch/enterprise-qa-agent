"""跨域共用的端点辅助函数——`create_app()` 分层的第一步（批次 0）。

## 为什么先做这件事

`docs/app_layering_design.md` 原来把批次 0 写成"建骨架、不搬端点"。2026-08-27
对 `create_app()` 做了一次 AST 调用图分析后改成了现在这样：**先把跨域共用的
辅助函数提取出来**。

实测发现 45 个辅助函数里有 **7 个被多个域调用**（`_audit_log` 被 9 个域用）。
按 URL 前缀切批次的话，第一批搬 `admin/ops` 就会把 `_get_owned_connector`
一起带走，而 `admin/roles` 下的 ops-permissions 端点还在用它——**要么编译不过，
要么有人顺手复制一份，两个副本从此各自演化**。所以这一步必须在任何端点搬迁
之前完成。

## 为什么 Store 改成显式参数而不是继续闭包捕获

闭包捕获正是 CLAUDE.md 记的那条"后端 12,200 行零测试的结构性原因"：依赖藏在
`create_app()` 的局部作用域里，测一个函数就得先构造整个 app。改成显式参数之后
这些函数可以直接用假件测——本模块配套的
`tests/unit/test_api_helpers.py` 就是不建 app 测出来的。

⚠️ **`create_app()` 里仍然保留同名的薄包装**，把 Store 绑上去。这样 93 个端点
的调用点一个字都不用改，本次是**纯提取、零行为变化**——这是批次 0 的全部要求。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import HTTPException

logger = logging.getLogger(__name__)


# ==================== 纯函数（不依赖任何 Store） ====================

def role_ops_permission_response(p: Any):
    from src.ragent_backend.schemas import RoleOpsPermissionResponse

    return RoleOpsPermissionResponse(
        role_id=p.role_id, connection_id=p.connection_id,
        can_view=p.can_view, can_approve=p.can_approve,
    )


def workflow_template_response(template: Any):
    from src.ragent_backend.schemas import WorkflowTemplateResponse

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


# ==================== 需要 Store 的（Store 走显式参数） ====================

async def audit_log(
    *, audit_store: Any, org_store: Any, user_store: Any,
    user_id: Optional[str], action: str, resource_type: str,
    resource_id: Optional[str], detail: dict, success: bool = True,
) -> None:
    """审计日志回调：补上 org_id/username 再落库。传给 RAGWorkflow（工具
    调用审计）和各管理端点（管理操作审计）共用同一个函数，保证两类事件
    落进同一张表、同一套字段。org_store/user_store 这里都能查——跟工具
    调用发生在同一个进程内，不需要额外的服务间调用。

    ⚠️ **异常一律吞掉只记日志**：审计写失败绝不能让业务操作跟着失败——
    一次批准动作因为审计表写不进去而回滚，比丢一条审计记录严重得多。
    """
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
        logger.exception("failed to record audit log",
                         extra={"action": action, "resource_type": resource_type})


async def require_conversation_owner(*, conversation_store: Any,
                                     conversation_id: str, current_user: Any):
    """校验对话存在且属于当前登录用户；不存在 404，存在但不是自己的 403。"""
    conv = await conversation_store.get_conversation(conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if conv.user_id != current_user.user_id:
        raise HTTPException(status_code=403, detail="无权访问该对话")
    return conv


async def require_aiops_enabled_org(*, org_store: Any, ops_store: Any, current_user: Any):
    """模块开关叠加在角色/ACL 校验之前的新一层（§4.1），不是替代——
    `_require_org_admin` 这层 Depends 已经先过了，这里再加一道"这家企业
    开没开通"的业务闸。跟 `require_local_retrieval_org` 是同一个模式：
    返回调用方所属的 Organization，避免调用方再查一次。"""
    org = await org_store.get_org_for_user(current_user.user_id)
    if org is None:
        raise HTTPException(status_code=403, detail="账号未关联任何企业")
    if not await ops_store.is_module_enabled(org.org_id):
        raise HTTPException(status_code=403, detail="智能运维模块未对本企业开通，请联系平台管理员")
    return org


async def get_owned_connector(*, ops_store: Any, org_id: str, connection_id: str):
    """404 不是 403——不能让"这个连接器存在但不是你的"这个信息泄露给
    跨企业的调用方，跟 admin_delete_collection 的既有约定一致。"""
    connector = await ops_store.get_connector(connection_id)
    if connector is None or connector.org_id != org_id:
        raise HTTPException(status_code=404, detail="连接器不存在")
    return connector


async def require_local_retrieval_org(*, org_store: Any, tenant_connector_store: Any,
                                      current_user: Any):
    """企业自建知识库只对"走本地 Chroma 检索"的企业开放（跟平台自己的 6 个
    部门库同一套检索机制）——像 Acme/Globex 这类把 knowledge_base 能力委托
    给自己微服务的企业（`tenant_connectors` 里配了 http_api 连接器），本地
    新建/关联的 collection 对它们的实际问答毫无意义（`query_knowledge_hub.py`
    的 `is_remote` 分支完全绕开本地检索，见该模块说明），所以在这里就地
    拒绝，报出清楚的原因，而不是让管理员建了一堆库、配了半天角色，结果
    员工提问永远用不上，自己也不知道为什么。返回调用方所属的 Organization，
    避免调用方再查一次。"""
    from src.ragent_backend.tenant_connector_store import (
        CAPABILITY_KNOWLEDGE_BASE, CONNECTOR_TYPE_HTTP_API,
    )

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
