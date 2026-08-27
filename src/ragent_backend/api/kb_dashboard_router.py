"""知识库 / 运营仪表盘 / 通知的路由模块——`create_app()` 分层的批次 2。

做法与批次 1 完全一致（见 `api/ops_router.py` 顶部说明）：**函数体逐字搬迁**，
只把装饰器换成 `@router.xxx`，依赖走显式参数并在工厂开头绑回原名，
于是"行为没变"可以用 OpenAPI 逐条比对来机器验证。

## ⚠️ 两个共享可变状态必须注入，不能在这里另建

- `ingest_semaphore`（`asyncio.Semaphore(2)`）限制并发摄入。router 里另建一个
  等于**限制悄悄翻倍**，而且不会有任何地方报错——摄入变慢或 OOM 时没人会想到
  这里。
- `upload_progress` 是上传进度的内存字典。另建一份的话，写进度的那一半和读
  进度的那一半会看不见彼此，前端轮询永远拿到"处理中"。

这跟批次 1 里 WebSocket 注册表是同一类风险（见 `docs/app_layering_design.md` §7）。
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile

from fastapi import Depends
from fastapi import File
from fastapi import HTTPException
from fastapi import UploadFile
from pathlib import Path
from src.core.settings import resolve_path
from src.ingestion.pipeline import IngestionPipeline
from src.ragent_backend import api_helpers
from src.ragent_backend.auth import AuthenticatedUser
from src.ragent_backend.auth import get_current_user
from src.ragent_backend.auth import require_platform_admin
from src.ragent_backend.schemas import AdminKbChunkPreview
from src.ragent_backend.schemas import CollectionCatalogEntry
from src.ragent_backend.schemas import CollectionResponse
from src.ragent_backend.schemas import CostOverviewResponse
from src.ragent_backend.schemas import CreateCollectionRequest
from src.ragent_backend.schemas import DashboardOverviewResponse
from src.ragent_backend.schemas import DashboardTrendPointResponse
from src.ragent_backend.schemas import DashboardTrendResponse
from src.ragent_backend.schemas import NotificationResponse
from src.ragent_backend.schemas import UploadProgressResponse
from src.ragent_backend.schemas import UploadStartedResponse
from typing import Dict
from typing import List
from typing import Optional
import asyncio
import asyncpg
import uuid


def build_kb_dashboard_router(
    *,
    org_store: Any,
    user_store: Any,
    role_store: Any,
    workflow_store: Any,
    org_collection_store: Any,
    tenant_connector_store: Any,
    dashboard_stats_service: Any,
    kb_management_tool: Any,
    settings: Any,
    model_price_per_1m_usd: Dict[str, Any],
    ingest_semaphore: Any,
    upload_progress: Dict[str, Any],
    allowed_extensions: Any,
    get_current_user: Callable,
    require_org_admin: Callable,
    require_platform_tier: Callable,
    audit_log: Callable,
) -> APIRouter:
    """把知识库 / 仪表盘 / 通知这 15 个端点装进一个 `APIRouter` 并返回。

    ⚠️ **函数体逐字来自 `create_app()`**，只改了装饰器。
    """
    router = APIRouter()

    # 绑回搬迁前的原名——函数体因此一个字都不用动（同批次 1）。
    _MODEL_PRICE_PER_1M_USD = model_price_per_1m_usd
    _kb_management_tool = kb_management_tool
    _require_org_admin = require_org_admin
    _require_platform_tier = require_platform_tier
    _upload_progress = upload_progress
    _audit_log = audit_log
    INGEST_SEMAPHORE = ingest_semaphore
    ALLOWED_EXTENSIONS = allowed_extensions
    logger = logging.getLogger(__name__)

    def _estimate_cost_usd(prompt_tokens: int, completion_tokens: int) -> Optional[float]:
        # 本地 Ollama 模型没有按 token 计费的推理成本，直接是 0，不是"未知"。
        if settings.llm.provider == "ollama":
            return 0.0
        model_name = (settings.llm.model or "").lower()
        for key, (price_in, price_out) in _MODEL_PRICE_PER_1M_USD.items():
            if key in model_name:
                return round(prompt_tokens / 1_000_000 * price_in + completion_tokens / 1_000_000 * price_out, 4)
        return None

    def _pct_change(current: Optional[float], previous: Optional[float]) -> Optional[float]:
        if current is None or not previous:
            return None
        return round((current - previous) / previous * 100, 1)

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

    async def _require_local_retrieval_org(current_user: AuthenticatedUser):
        """薄包装，实现见 `api_helpers.require_local_retrieval_org`。"""
        return await api_helpers.require_local_retrieval_org(
            org_store=org_store, tenant_connector_store=tenant_connector_store,
            current_user=current_user,
        )

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
                # version_key 显式传原始文件名，不能让它默认落回 dest_path——
                # dest_path 每次上传都带一个随机 UUID 前缀（见上面 upload
                # 端点的 safe_name），默认值在这条路径上永远不会撞上旧版本，
                # 等于 P0"旧版本片段永久残留"完全没有被修（CLAUDE.md §4 第 1 条）。
                result = await asyncio.to_thread(
                    pipeline.run, file_path=str(dest_path), on_progress=on_progress,
                    version_key=original_name,
                )

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

    @router.get("/api/v1/admin/dashboard/overview", response_model=DashboardOverviewResponse)
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

    @router.get("/api/v1/admin/dashboard/trend", response_model=DashboardTrendResponse)
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

    @router.get("/api/v1/admin/dashboard/cost-overview", response_model=CostOverviewResponse)
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

    @router.get("/api/v1/admin/dashboard/cost-trend", response_model=DashboardTrendResponse)
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

    @router.get("/api/v1/admin/collections", response_model=List[CollectionResponse])
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

    @router.post("/api/v1/admin/collections", response_model=CollectionResponse)
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

    @router.get("/api/v1/admin/collections/{collection_name}/chunks", response_model=List[AdminKbChunkPreview])
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

    @router.delete("/api/v1/admin/collections/{collection_name}")
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

    @router.get("/api/v1/collections/catalog", response_model=List[CollectionCatalogEntry])
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

    @router.post("/api/v1/collections/{collection_name}/documents", response_model=UploadStartedResponse)
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

    @router.get("/api/v1/collections/uploads/{upload_id}", response_model=UploadProgressResponse)
    async def get_upload_progress(
        upload_id: str,
        current_user: AuthenticatedUser = Depends(get_current_user),
    ) -> UploadProgressResponse:
        state = _upload_progress.get(upload_id)
        if state is None:
            raise HTTPException(status_code=404, detail="上传任务不存在或已过期")
        return UploadProgressResponse(upload_id=upload_id, **state)

    @router.get("/api/v1/notifications", response_model=List[NotificationResponse])
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

    @router.get("/api/v1/notifications/unread-count")
    async def get_unread_notification_count(
        current_user: AuthenticatedUser = Depends(get_current_user),
    ) -> dict:
        count = await workflow_store.unread_count(current_user.user_id)
        return {"count": count}

    @router.post("/api/v1/notifications/{notification_id}/read")
    async def mark_notification_read(
        notification_id: str,
        current_user: AuthenticatedUser = Depends(get_current_user),
    ) -> dict:
        found = await workflow_store.mark_read(notification_id, current_user.user_id)
        if not found:
            raise HTTPException(status_code=404, detail="通知不存在")
        return {"success": True}

    @router.post("/api/v1/notifications/mark-all-read")
    async def mark_all_notifications_read(
        current_user: AuthenticatedUser = Depends(get_current_user),
    ) -> dict:
        count = await workflow_store.mark_all_read(current_user.user_id)
        return {"success": True, "count": count}

    return router
