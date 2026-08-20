"""MCP Tool: query_knowledge_hub

This tool provides knowledge retrieval capabilities through the MCP protocol.
It combines HybridSearch (Dense + Sparse + RRF Fusion) with optional Reranking
to find relevant documents and return formatted results with citations.

Usage via MCP:
    Tool name: query_knowledge_hub
    Input schema:
        - query (string, required): The search query
        - top_k (integer, optional): Number of results to return (default: 5)
        - collection (string, optional): Limit search to specific collection
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from mcp import types

from src.core.response.response_builder import ResponseBuilder, MCPToolResponse
from src.core.settings import load_settings, resolve_path, Settings
from src.core.trace import TraceContext, TraceCollector
from src.core.types import RetrievalResult

if TYPE_CHECKING:
    from src.core.query_engine.hybrid_search import HybridSearch
    from src.core.query_engine.reranker import CoreReranker
    from src.ragent_backend.user_store import UserStore
    from src.ragent_backend.org_store import OrgStore
    from src.ragent_backend.tenant_connector_store import TenantConnectorStore, TenantConnector
    from src.ragent_backend.collection_store import OrgCollectionStore

logger = logging.getLogger(__name__)

# 委托超时阈值，跟 attendance-tenant-federation.md 的考勤委托路径用同一个数值，
# 保持"企业系统响应多久算超时"的产品预期一致。
REMOTE_SEARCH_TIMEOUT_SECONDS = 8.0

# 平台自己组织（internal_chroma 本地检索）的固定部门知识库清单——原来没有这个
# 固定清单时，LLM 调用工具从不主动填 collection 参数，`effective_collection`
# 永远回退到硬编码的 "default"（一个开发阶段误摄入了项目自己 README 的
# collection，不是任何业务知识库），导致本地检索的用户不管问什么、不管角色
# 实际关联了哪些知识库，查的都是同一个不相关的库。修复方式不是让 LLM 更准地
# 猜该填哪个 collection（同一个本地小模型在意图分类那几轮已经证明不可靠，
# 见 intent.py/workflow.py 里几处"分类器判不准"的修复记录），而是压根不依赖
# 它选：调用方不显式指定 collection 时，直接对用户角色关联的全部部门知识库
# 做"全库混合召回 + 重排"（见 execute() 里的 _execute_local_multi）——每个库
# 并行跑一次 dense+sparse 混合检索，候选结果合并后统一过一次 cross-encoder
# 重排，取最终 top_k，不用猜"该查哪个库"，让检索结果自己说话。
DEPARTMENT_KB_COLLECTIONS: Dict[str, str] = {
    "hr_admin_kb": "人力资源与行政知识库",
    "finance_kb": "财务与报销制度知识库",
    "it_support_kb": "IT 支持与技术运维知识库",
    "sales_marketing_kb": "销售话术与市场知识库",
    "rd_product_kb": "研发与产品代码知识库",
    "customer_success_kb": "客户成功与售后服务知识库",
}

# cross-encoder 重排分数的相关性下限——向量/BM25 混合检索的 top_k 本质是"矬子
# 里拔将军"：不管问题跟语料实际有多不相关，永远会返回 k 个"矬子里最高"的结果，
# 分数不会自动归零。真实踩过的坑：问一句跟任何库都毫不相关的话（"iPhone16 是否
# 配套充电器"），6 个部门库依然会凑出几条"看起来像结果"的候选，reranker 打分
# 后普遍落在 0.03 以下（本地实测最高 0.0312），而真正相关的问题即使是模糊命中
# 也稳定在 0.13 以上——这两簇之间有明显断层，0.1 卡在中间，足够把"矬子"过滤掉、
# 又不会误伤真正相关但没那么强的命中。只在 reranker 真的跑起来、拿到 cross-encoder
# 量级的分数时才适用（_apply_rerank 返回的 `scored` 标志位表示这一点）——reranker
# 禁用/降级 fallback 时的分数量级跟这个不是一回事，不能拿这个阈值卡。
MIN_RELEVANCE_SCORE: float = 0.1


# Tool metadata
TOOL_NAME = "query_knowledge_hub"
TOOL_DESCRIPTION = """Search the enterprise knowledge base for relevant documents.

This tool uses hybrid search (semantic + keyword) to find the most relevant
documents matching your query, then reranks them. Results include source
citations for reference.

The knowledge base is organized into fixed department libraries (HR & Admin,
Finance & Reimbursement, IT Support & Ops, Sales & Marketing, R&D & Product,
Customer Success & After-sales). You do NOT need to pick which one to search —
leave `collection` unset and the tool searches across all department libraries
the caller has access to in parallel, merges the candidates, and reranks them.
Only set `collection` if you already know the exact internal collection name
and want to restrict the search to just that one.

Parameters:
- query: Your search question or keywords
- top_k: Maximum number of results (default: 5)
- collection: Optional — restrict to one specific collection by its exact name; leave unset to search all accessible department libraries
"""

TOOL_INPUT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "The search query or question to find relevant documents for.",
        },
        "top_k": {
            "type": "integer",
            "description": "Maximum number of results to return.",
            "default": 5,
            "minimum": 1,
            "maximum": 20,
        },
        "collection": {
            "type": "string",
            "description": "Optional collection name to limit the search scope.",
        },
    },
    "required": ["query"],
}


@dataclass
class QueryKnowledgeHubConfig:
    """Configuration for query_knowledge_hub tool.
    
    Attributes:
        default_top_k: Default number of results if not specified
        max_top_k: Maximum allowed top_k value
        default_collection: Default collection if not specified
        enable_rerank: Whether to apply reranking
    """
    default_top_k: int = 5
    max_top_k: int = 20
    default_collection: str = "default"
    enable_rerank: bool = True


class QueryKnowledgeHubTool:
    """MCP Tool for knowledge base queries.
    
    This class encapsulates the query_knowledge_hub tool logic,
    coordinating HybridSearch and Reranker to produce formatted results.
    
    Design Principles:
    - Lazy initialization: Components created on first use
    - Error resilience: Graceful handling of search/rerank failures
    - Configurable: All parameters from settings.yaml
    
    Example:
        >>> tool = QueryKnowledgeHubTool(settings)
        >>> result = await tool.execute(query="Azure 配置", top_k=5)
        >>> print(result.content)
    """
    
    def __init__(
        self,
        settings: Optional[Settings] = None,
        config: Optional[QueryKnowledgeHubConfig] = None,
        hybrid_search: Optional[HybridSearch] = None,
        reranker: Optional[CoreReranker] = None,
        response_builder: Optional[ResponseBuilder] = None,
        user_store: Optional["UserStore"] = None,
        org_store: Optional["OrgStore"] = None,
        tenant_connector_store: Optional["TenantConnectorStore"] = None,
    ) -> None:
        """Initialize QueryKnowledgeHubTool.

        Args:
            settings: Application settings. If None, loaded from default path.
            config: Tool configuration. If None, uses defaults.
            hybrid_search: Optional pre-configured HybridSearch instance.
            reranker: Optional pre-configured CoreReranker instance.
            response_builder: Optional pre-configured ResponseBuilder instance.
            user_store: Optional pre-configured UserStore, used to look up the
                caller's allowed_collections for ACL checks. If None, lazily
                creates its own (see `user_store` property).
            org_store: Optional pre-configured OrgStore, used to resolve which
                organization the caller belongs to (see
                `knowledge-base-tenant-federation.md` 第 5.1 节). If None,
                lazily creates its own.
            tenant_connector_store: Optional pre-configured TenantConnectorStore,
                used to look up whether the caller's organization delegates
                knowledge-base search to its own microservice. If None, lazily
                creates its own.
        """
        self._settings = settings
        self.config = config or QueryKnowledgeHubConfig()
        self._hybrid_search = hybrid_search
        self._reranker = reranker
        self._embedding_client = None
        self._response_builder = response_builder or ResponseBuilder()
        self._user_store = user_store
        self._org_store = org_store
        self._tenant_connector_store = tenant_connector_store
        self._org_collection_store: Optional["OrgCollectionStore"] = None

        # Track initialization state
        self._initialized = False
        self._current_collection: Optional[str] = None

    @property
    def user_store(self) -> "UserStore":
        """Get the UserStore used for ACL lookups, creating one if necessary."""
        if self._user_store is None:
            from src.ragent_backend.user_store import UserStore
            self._user_store = UserStore()
        return self._user_store

    @property
    def org_store(self) -> "OrgStore":
        """Get the OrgStore used to resolve caller org, creating one if necessary."""
        if self._org_store is None:
            from src.ragent_backend.org_store import OrgStore
            self._org_store = OrgStore()
        return self._org_store

    @property
    def tenant_connector_store(self) -> "TenantConnectorStore":
        """Get the TenantConnectorStore, creating one if necessary."""
        if self._tenant_connector_store is None:
            from src.ragent_backend.tenant_connector_store import TenantConnectorStore
            self._tenant_connector_store = TenantConnectorStore()
        return self._tenant_connector_store

    @property
    def org_collection_store(self) -> "OrgCollectionStore":
        """Get the OrgCollectionStore (企业自建知识库归属), creating one if necessary."""
        if self._org_collection_store is None:
            from src.ragent_backend.collection_store import OrgCollectionStore
            self._org_collection_store = OrgCollectionStore()
        return self._org_collection_store

    async def _org_owned_collections(self, org: Optional[Any]) -> List[str]:
        """这个组织"自己名下"、本地检索能碰的 collection 全集——是"全库并行召回"
        候选集、以及显式指定 collection 时 ACL 校验的公共基准（见 execute() 里
        两处调用），确保两条路径的"我能查哪些库"口径完全一致。

        - `org` 是平台自己的组织（org_platform）：固定就是
          DEPARTMENT_KB_COLLECTIONS 那 6 个（跟改造前行为完全一致，不受这次
          企业自建知识库改造影响）。
        - `org` 是别的（本地检索）企业：`org_collections` 表里登记的、这家企业
          自己创建的知识库（见 collection_store.py）——不会把平台的 6 个部门库
          或者别的企业自己建的库混进来，这正是"平台管理员/别的企业看不到、也
          查不到这家企业知识库内容"（见 knowledge-base-tenant-federation.md 相关
          权限边界讨论）在检索层的落地。
        - 没有 user_id/查不到 org（老的独立 MCP server 调用方，没有身份概念）：
          退回 DEPARTMENT_KB_COLLECTIONS，保留改造前的行为，不引入新的隐式收紧。
        """
        if org is None:
            return list(DEPARTMENT_KB_COLLECTIONS)
        if org.is_platform:
            return list(DEPARTMENT_KB_COLLECTIONS)
        owned = await self.org_collection_store.list_for_org(org.org_id)
        return [c.collection_name for c in owned]

    @property
    def settings(self) -> Settings:
        """Get settings, loading if necessary."""
        if self._settings is None:
            self._settings = load_settings()
        return self._settings
    
    def _build_hybrid_search_for(self, collection: str) -> "HybridSearch":
        """为单个 collection 现建一个独立的 HybridSearch 实例，不读也不写
        self._hybrid_search/self._current_collection——"全库混合召回"
        （_execute_local_multi）要在多个 collection 上并发跑检索，如果沿用
        原来那种"挂在 self 上、切 collection 就整体重建"的单实例缓存方式，
        并发跑多个 collection 时会互相踩，A 库还没查完，B 库的初始化就把
        self._hybrid_search 替换成了自己的，最后 A 库查到的其实是 B 库的
        索引——所以这里只把 embedding client / reranker 这两个真正无状态、
        可以跨 collection 共享的部分挂在 self 上复用，vector_store/检索器
        这些跟 collection 绑定的部分每次都建新的局部变量返回，调用方自己
        持有，不留在实例状态里。"""
        logger.info(f"Building hybrid search for collection: {collection}")

        from src.core.query_engine.query_processor import QueryProcessor
        from src.core.query_engine.hybrid_search import create_hybrid_search
        from src.core.query_engine.dense_retriever import create_dense_retriever
        from src.core.query_engine.sparse_retriever import create_sparse_retriever
        from src.core.query_engine.reranker import create_core_reranker
        from src.ingestion.storage.bm25_indexer import BM25Indexer
        from src.libs.embedding.embedding_factory import EmbeddingFactory
        from src.libs.vector_store.vector_store_factory import VectorStoreFactory

        # === 真正无状态、可跨 collection 共享的部分：挂在 self 上复用 ===
        if self._embedding_client is None:
            self._embedding_client = EmbeddingFactory.create(self.settings)

        if self._reranker is None:
            self._reranker = create_core_reranker(settings=self.settings)

        # === 跟 collection 绑定的部分：每次现建，不缓存在 self 上 ===
        # ChromaDB PersistentClient 底层是 SQLite，并发读是安全的（多个
        # collection 各自开自己的 client，互不干扰）。
        vector_store = VectorStoreFactory.create(
            self.settings,
            collection_name=collection,
        )

        dense_retriever = create_dense_retriever(
            settings=self.settings,
            embedding_client=self._embedding_client,
            vector_store=vector_store,
        )

        bm25_indexer = BM25Indexer(index_dir=str(resolve_path(f"data/db/bm25/{collection}")))
        sparse_retriever = create_sparse_retriever(
            settings=self.settings,
            bm25_indexer=bm25_indexer,
            vector_store=vector_store,
        )
        sparse_retriever.default_collection = collection

        query_processor = QueryProcessor()
        return create_hybrid_search(
            settings=self.settings,
            query_processor=query_processor,
            dense_retriever=dense_retriever,
            sparse_retriever=sparse_retriever,
        )

    def _ensure_initialized(self, collection: str) -> None:
        """单 collection 场景（调用方显式指定了 collection，或者委托远程结果
        解析等老路径）用的缓存包装——只有 collection 变了才重建，行为跟改造
        前一致。"全库混合召回"的新路径（_execute_local_multi）不走这个方法，
        直接调 _build_hybrid_search_for，原因见该方法的说明。"""
        self._hybrid_search = self._build_hybrid_search_for(collection)
        self._current_collection = collection
        self._initialized = True
        logger.info(f"Query components initialized for collection: {collection}")
    
    async def execute(
        self,
        query: str,
        top_k: Optional[int] = None,
        collection: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> MCPToolResponse:
        """Execute the query_knowledge_hub tool.

        Args:
            query: Search query string.
            top_k: Maximum results to return.
            collection: Target collection name.
            user_id: Caller identity for ACL checks. Only trust values that came
                from the server-side request/state, never from LLM-supplied tool
                arguments (see tool_agent/subgraph.py). None skips the check
                entirely — used by callers (e.g. the standalone MCP server) that
                have no caller identity to check against.

        Returns:
            MCPToolResponse with formatted content and citations.

        Raises:
            ValueError: If query is empty or invalid.
        """
        # Validate query
        if not query or not query.strip():
            raise ValueError("Query cannot be empty")

        # Apply defaults
        effective_top_k = min(
            top_k or self.config.default_top_k,
            self.config.max_top_k
        )

        # 路由决策：这个用户属于哪家企业，这家企业的知识库能力有没有配置委托连接器
        # （knowledge-base-tenant-federation.md 第 5.1 节）。没有 user_id 的调用方
        # （如独立跑的 MCP server）跳过整个路由和 ACL 判断，直接走本地检索——保留
        # 原有行为。
        # 延迟导入：顶层导入 tenant_connector_store 会触发 `src.ragent_backend`
        # 包初始化，而该包的 `workflow.py` 又导入本模块，形成循环导入。
        from src.ragent_backend.tenant_connector_store import (
            CAPABILITY_KNOWLEDGE_BASE,
            CONNECTOR_TYPE_HTTP_API,
            CONNECTOR_TYPE_INTERNAL_CHROMA,
        )

        connector = None
        allowed_collections: Optional[List[str]] = None
        org = None
        if user_id is not None:
            org = await self.org_store.get_org_for_user(user_id)
            if org is not None:
                connector = await self.tenant_connector_store.get(org.org_id, CAPABILITY_KNOWLEDGE_BASE)
            if connector is None or connector.connector_type == CONNECTOR_TYPE_INTERNAL_CHROMA:
                allowed_collections = await self.user_store.get_allowed_collections(user_id)

        is_remote = connector is not None and connector.connector_type == CONNECTOR_TYPE_HTTP_API

        # 委托模式（企业自己的知识库微服务）：这几个固定部门知识库是本地
        # internal_chroma 专属的概念，委托出去的查询不受影响，沿用老逻辑——
        # 单一 collection，默认值取 "default"（企业微服务自己决定怎么理解
        # 这个值，见 knowledge-base-tenant-federation.md 第 5.2 节，委托模式下
        # 细粒度权限本来就转移给对方了）。
        if is_remote:
            # 委托模式下这个值只用来展示（塞进请求体的 collection 字段企业自己
            # 的知识库微服务压根不读，见 services/tenant_kb_demo/app.py——它
            # 固定查自己环境变量配的 TENANT_COLLECTION，不受这个字段影响），
            # 不能沿用 self.config.default_collection（字面量 "default"，本来
            # 是本地共享 Chroma 那个"没人管的历史遗留 collection"专用的默认值，
            # 见 DEPARTMENT_KB_COLLECTIONS 旁的说明），沿用它会导致 UI 上企业
            # 员工问的每一句话，来源角标都显示成语义完全不对的"通用知识库"
            # ——用户会以为查的是某个真实存在的公共库，但这个库根本不存在。
            # 改成 tenant_{org_id}_kb 这个形状，跟前端 kbMeta() 已经认识的
            # tenant_*_kb 命名规则对上，会展示成"本企业知识库"，准确反映
            # "这是委托到该企业自己知识库的查询"这个事实。
            effective_collection = collection or f"tenant_{connector.org_id}_kb"
            trace = TraceContext(trace_type="query")
            trace.metadata["query"] = query[:200]
            trace.metadata["top_k"] = effective_top_k
            trace.metadata["collection"] = effective_collection
            trace.metadata["source"] = "mcp"
            trace.metadata["connector_type"] = connector.connector_type
            return await self._execute_remote(query, effective_top_k, effective_collection, connector, trace)

        # 本地 internal_chroma：调用方显式指定了 collection——老的单 collection
        # 路径原样保留（ACL 校验 + tenant_ 前缀硬拦截，见下面注释），不受"全库
        # 混合召回"改造影响。
        if collection:
            effective_collection = collection
            if allowed_collections is not None:
                from src.ragent_backend.acl import is_collection_allowed
                if not is_collection_allowed(effective_collection, allowed_collections):
                    logger.warning(
                        f"ACL denied: user_id={user_id} tried to query collection={effective_collection}"
                    )
                    return self._build_access_denied_response(query, effective_collection)

            # 显式兜底：不管上面 ACL 判断结果如何（包括平台管理员的
            # allowed_collections 通配符 "*"），本地检索都不允许碰
            # `tenant_{name}_kb` 这个命名——这是各企业委托微服务专属的 collection
            # 命名约定（scripts/ingest_tenant_kb_corpus.py），这些企业的知识库
            # 物理上根本没有摄入到本地共享 Chroma 库里（不同 persist_directory），
            # 本来就查不到；这层检查把"物理隔离导致查不到"变成"显式拒绝并留
            # 日志"，防止未来有人改动存储布局时悄悄破坏这个保证——平台运营方
            # 账号（super_admin/admin）不该、也不能查到任何客户企业的知识库内容。
            if effective_collection.startswith("tenant_") and effective_collection.endswith("_kb"):
                logger.warning(
                    f"Blocked local access to tenant-reserved collection '{effective_collection}' "
                    f"(user_id={user_id})"
                )
                return self._build_access_denied_response(query, effective_collection)

            # 第二层兜底，跟上面的 tenant_ 前缀拦截同一个用意：ACL 的 allowed_collections
            # 通配符 "*" 现在也会被 org_admin 隐式触发（role_store.py
            # get_allowed_collections_for_user），如果只靠上面那次 ACL 判断，
            # 一个通配符用户显式传一个别的企业自建知识库的 collection 名（哪怕
            # 只是猜出来的）也能查到——这里用"这个 collection 是不是我自己企业
            # 名下的"再收紧一次，跟"全库并行召回"用的是同一个基准
            # （_org_owned_collections），保证两条路径口径一致。user_id 为 None
            # （没有身份概念的独立调用方）跳过这层，保留原有行为。
            if user_id is not None:
                org_owned = await self._org_owned_collections(org)
                if effective_collection not in org_owned:
                    logger.warning(
                        f"Blocked cross-org local access to collection '{effective_collection}' "
                        f"(user_id={user_id})"
                    )
                    return self._build_access_denied_response(query, effective_collection)

            logger.info(
                f"Executing query_knowledge_hub: query='{query[:50]}...', "
                f"top_k={effective_top_k}, collection={effective_collection}, connector_type=internal_chroma"
            )
            trace = TraceContext(trace_type="query")
            trace.metadata["query"] = query[:200]
            trace.metadata["top_k"] = effective_top_k
            trace.metadata["collection"] = effective_collection
            trace.metadata["source"] = "mcp"
            trace.metadata["connector_type"] = "internal_chroma"
            return await self._execute_local_single(query, effective_top_k, effective_collection, trace)

        # 本地 internal_chroma、调用方没指定 collection（绝大多数场景——LLM 从
        # 不主动填这个参数，见模块顶部 DEPARTMENT_KB_COLLECTIONS 旁的说明）：
        # "全库混合召回 + 重排"，候选集是"这个用户自己企业名下的 collection"
        # （_org_owned_collections——平台自己是固定 6 个部门库，别的企业是
        # org_collections 表里登记的自建知识库，见该方法的说明）跟用户角色关联
        # 的 collection 取交集；allowed_collections 里有通配符 "*"（或者压根
        # 没有 user_id、跳过了 ACL）就是这个用户自己企业名下的全部知识库都能查
        # ——不是"全平台任何 collection"，这是这次改造要收紧的点：改造前候选集
        # 固定写死 DEPARTMENT_KB_COLLECTIONS，企业自建知识库上线后如果还按老
        # 逻辑，通配符用户（企业管理员现在也隐式通配符，见 role_store.py）反而
        # 会被限制在平台的 6 个部门库里，查不到自己企业刚建的库；换成
        # _org_owned_collections 之后，两种身份（平台自己 / 本地检索企业）都各自
        # 对应到正确的候选集，互不越界。
        org_owned = await self._org_owned_collections(org)
        if allowed_collections is not None and "*" not in allowed_collections:
            candidate_collections = [c for c in org_owned if c in allowed_collections]
        else:
            candidate_collections = org_owned

        if not candidate_collections:
            logger.warning(f"ACL denied: user_id={user_id} has no accessible department KB")
            return self._build_access_denied_response(query, "(部门知识库)")

        logger.info(
            f"Executing query_knowledge_hub (parallel recall): query='{query[:50]}...', "
            f"top_k={effective_top_k}, candidates={candidate_collections}"
        )
        trace = TraceContext(trace_type="query")
        trace.metadata["query"] = query[:200]
        trace.metadata["top_k"] = effective_top_k
        trace.metadata["candidate_collections"] = candidate_collections
        trace.metadata["source"] = "mcp"
        trace.metadata["connector_type"] = "internal_chroma"
        try:
            response = await self._execute_local_multi(query, effective_top_k, candidate_collections, trace)
            TraceCollector().collect(trace)
            return response
        except Exception as e:
            logger.exception(f"query_knowledge_hub (parallel recall) failed: {e}")
            TraceCollector().collect(trace)
            return self._build_error_response(query, ",".join(candidate_collections), str(e))

    async def _execute_local_single(
        self, query: str, effective_top_k: int, effective_collection: str, trace: TraceContext,
    ) -> MCPToolResponse:
        """单 collection 本地检索——调用方显式指定 collection 时的老路径，从
        execute() 里搬出来，逻辑不变。"""
        try:
            # Initialize components for collection
            # Run blocking I/O (embedding API, ChromaDB, BM25) in a thread
            # to avoid blocking the async event loop / MCP stdio transport
            import time as _time
            _init_t0 = _time.monotonic()
            await asyncio.to_thread(self._ensure_initialized, effective_collection)
            _init_elapsed = (_time.monotonic() - _init_t0) * 1000.0
            trace.record_stage("initialization", {
                "collection": effective_collection,
                "cold_start": _init_elapsed > 500,  # >500ms ≈ cold
            }, elapsed_ms=_init_elapsed)

            # Perform hybrid search (blocking: embedding API + DB queries)
            results = await asyncio.to_thread(
                self._perform_search, query, effective_top_k, trace,
            )

            # Apply reranking if enabled (may call LLM API)
            if self.config.enable_rerank and results:
                results, scored = await asyncio.to_thread(
                    self._apply_rerank, query, results, effective_top_k, trace,
                )
                if scored:
                    results = self._filter_by_relevance(results)

            # Build response
            response = self._response_builder.build(
                results=results,
                query=query,
                collection=effective_collection,
            )
            
            # Store final results in trace for dashboard display
            trace.metadata["final_results"] = [
                {
                    "chunk_id": r.chunk_id,
                    "score": round(r.score, 4),
                    "text": r.text or "",
                    "source": r.metadata.get("source_path", r.metadata.get("source", "")),
                    "title": r.metadata.get("title", ""),
                }
                for r in results
            ]

            logger.info(
                f"query_knowledge_hub completed: {len(results)} results, "
                f"is_empty={response.is_empty}"
            )
            
            TraceCollector().collect(trace)
            return response
            
        except Exception as e:
            logger.exception(f"query_knowledge_hub failed: {e}")
            TraceCollector().collect(trace)
            # Return error response
            return self._build_error_response(query, effective_collection, str(e))
    
    def _perform_search(
        self,
        query: str,
        top_k: int,
        trace: Optional[Any] = None,
    ) -> List[RetrievalResult]:
        """Perform hybrid search.
        
        Args:
            query: Search query.
            top_k: Maximum results.
            trace: Optional TraceContext for observability.
            
        Returns:
            List of RetrievalResult.
        """
        if self._hybrid_search is None:
            raise RuntimeError("HybridSearch not initialized")
        return self._search_with(self._hybrid_search, query, top_k, trace)

    def _search_with(
        self,
        hybrid_search: "HybridSearch",
        query: str,
        top_k: int,
        trace: Optional[Any] = None,
    ) -> List[RetrievalResult]:
        """跟 _perform_search 逻辑一样，但接收显式传入的 HybridSearch 实例，
        不读 self._hybrid_search——"全库混合召回"并发查多个 collection 时，
        每个 collection 自己的 HybridSearch 局部变量互不干扰，靠的就是这个
        方法不碰共享的 self 状态（见 _build_hybrid_search_for 的说明）。"""
        # Use a larger initial retrieval for reranking
        initial_top_k = top_k * 2 if self.config.enable_rerank else top_k

        try:
            results = hybrid_search.search(
                query=query,
                top_k=initial_top_k,
                filters=None,
                trace=trace,
                return_details=False,
            )
            return results if isinstance(results, list) else results.results
        except Exception as e:
            logger.warning(f"Hybrid search failed: {e}")
            return []

    def _apply_rerank(
        self,
        query: str,
        results: List[RetrievalResult],
        top_k: int,
        trace: Optional[Any] = None,
    ) -> tuple[List[RetrievalResult], bool]:
        """Apply reranking to search results.

        Args:
            query: Original query.
            results: Search results to rerank.
            top_k: Final number of results.
            trace: Optional TraceContext for observability.

        Returns:
            (results, scored) — `scored` is True only when the returned scores are
            genuinely on the cross-encoder scale (reranker actually ran, no
            fallback), which is the only case MIN_RELEVANCE_SCORE is calibrated
            against. Callers must not threshold-filter on `scored=False` results
            (raw hybrid-fusion scores are a different, much smaller scale — see
            MIN_RELEVANCE_SCORE's comment).
        """
        if self._reranker is None or not self._reranker.is_enabled:
            return results[:top_k], False

        # 真实踩过的坑：候选只有 1 条时，CoreReranker.rerank() 会走它自己的
        # len(results)==1 短路分支，原样把这条结果（原始 hybrid 融合分数，量级
        # 是 0.01~0.05）传回来，`used_fallback` 还是 False——单文档的知识库
        # （典型场景：企业管理员刚新建的自建知识库，只摄入了一两篇文档）候选
        # 检索经常就是 1 条，如果只看 used_fallback 判断"是不是真分数"，会把这
        # 条其实真正相关的结果，当成 cross-encoder 量级去跟 MIN_RELEVANCE_SCORE
        # 比，误杀成"不相关"——表现为一个知识库明明有文档、问的问题也确实是
        # 文档里的内容，回答却说"未找到相关内容"。真正过了 cross-encoder 的
        # 前提是候选数量 > 1，这里在调用前就拦掉，不依赖 rerank_result 自己
        # 报的字段（更贴近 reranker.py 那个分支的真实触发条件，不用假设它未来
        # 会不会换个方式报告"我短路了"）。
        if len(results) <= 1:
            return results[:top_k], False

        try:
            rerank_result = self._reranker.rerank(
                query=query,
                results=results,
                top_k=top_k,
                trace=trace,
            )

            if rerank_result.used_fallback:
                logger.warning(
                    f"Reranker fallback: {rerank_result.fallback_reason}"
                )

            return rerank_result.results, not rerank_result.used_fallback
        except Exception as e:
            logger.warning(f"Reranking failed, using original order: {e}")
            return results[:top_k], False

    @staticmethod
    def _filter_by_relevance(results: List[RetrievalResult]) -> List[RetrievalResult]:
        """丢掉重排分数低于 MIN_RELEVANCE_SCORE 的结果——只在调用方确认分数是
        真实 cross-encoder 量级（`_apply_rerank` 返回的 `scored=True`）时才能调用，
        见 MIN_RELEVANCE_SCORE 旁的说明。全部被过滤掉时返回空列表，调用方直接
        走 ResponseBuilder 已有的空结果分支，不需要额外处理。"""
        return [r for r in results if r.score >= MIN_RELEVANCE_SCORE]

    async def _execute_local_multi(
        self,
        query: str,
        top_k: int,
        candidate_collections: List[str],
        trace: TraceContext,
    ) -> MCPToolResponse:
        """"全库混合召回 + 重排"：调用方没有显式指定 collection 时的默认路径
        （见 execute() 里的分支、以及模块顶部 DEPARTMENT_KB_COLLECTIONS 旁的
        说明）。对 `candidate_collections`（用户角色关联的、且在固定部门知识库
        清单里的那几个）逐个并发跑一次 dense+sparse 混合检索，候选结果合并后
        统一过一次 cross-encoder 重排，取最终 top_k——不用猜"该查哪个库"，也
        不需要 LLM 参与这个决策。
        """
        # 先把每个 collection 的 HybridSearch（内部会各自新建一个指向同一个
        # persist_directory 的 chromadb.PersistentClient）串行建好，再并行跑
        # 查询——实测踩过坑：6 个 collection 各自在不同线程里并发 new 一个
        # PersistentClient 指向同一个目录时，ChromaDB 的 Rust binding 初始化
        # 不是线程安全的，会报"'RustBindingsAPI' object has no attribute
        # 'bindings'" / "Could not connect to tenant default_tenant" 这类
        # 看起来毫不相关、实则是同一个根因（并发 bootstrap）的错误，而且是
        # 偶发的，不是每次都炸，很容易被当成"这次网络抖了一下"糊弄过去。
        # client 一旦建好之后，用已建好的 client 并发查询是安全的（SQLite
        # WAL 模式支持并发读），所以只把"建 client"这一步收窄成串行，真正
        # 耗时的 embedding + 检索这部分保留并行，不牺牲全库并行召回的速度。
        def _build_all_sync() -> Dict[str, "HybridSearch"]:
            return {c: self._build_hybrid_search_for(c) for c in candidate_collections}

        hybrid_searches = await asyncio.to_thread(_build_all_sync)

        def _search_one_sync(collection: str) -> List[RetrievalResult]:
            hybrid_search = hybrid_searches[collection]
            initial_top_k = top_k * 2 if self.config.enable_rerank else top_k
            results = self._search_with(hybrid_search, query, initial_top_k, trace=None)
            # 打上来源标记，供合并后统计"最终结果实际来自哪几个库"（response
            # metadata 的 collections 字段，UI 来源角标用），以及排查问题时
            # 一眼看出某条结果是从哪个库召回的。
            for r in results:
                r.metadata = dict(r.metadata or {})
                r.metadata["collection"] = collection
            return results

        per_collection_results = await asyncio.gather(
            *[asyncio.to_thread(_search_one_sync, c) for c in candidate_collections],
            return_exceptions=True,
        )

        merged: List[RetrievalResult] = []
        for collection, sub_results in zip(candidate_collections, per_collection_results):
            if isinstance(sub_results, Exception):
                logger.warning(f"Search failed for collection '{collection}': {sub_results}")
                continue
            merged.extend(sub_results)

        trace.record_stage("parallel_recall", {
            "candidate_collections": candidate_collections,
            "merged_candidate_count": len(merged),
        })

        if self.config.enable_rerank and merged:
            merged, scored = await asyncio.to_thread(self._apply_rerank, query, merged, top_k, trace)
            if scored:
                # 全库并行召回对"不相关问题也能凑出候选"格外敏感——每个候选库
                # 都会各自返回自己"矬子里最高"的几条，6 个库凑在一起，合并候选
                # 集比单库场景更容易看着"有内容"，实际全是噪音（见
                # MIN_RELEVANCE_SCORE 旁的真实案例）。重排后过滤跟单库路径
                # （_execute_local_single）用的是同一个阈值/同一个理由。
                merged = self._filter_by_relevance(merged)
        else:
            merged = sorted(merged, key=lambda r: r.score, reverse=True)[:top_k]

        contributing_collections = sorted({
            r.metadata.get("collection") for r in merged if r.metadata.get("collection")
        })
        trace.record_stage("rerank_merge", {
            "final_result_count": len(merged),
            "contributing_collections": contributing_collections,
        })

        # merged 为空时 ResponseBuilder.build() 内部会自己路由到它自己的
        # _build_empty_response（那个方法长在 ResponseBuilder 上，不是长在
        # QueryKnowledgeHubTool 上——这里之前直接写 self._build_empty_response(...)
        # 是个真实踩过的坑：这个类根本没有这个方法，AttributeError 被下面
        # execute() 的 except 兜底吞掉，表现成一个跟真正原因毫不相关的错误
        # 响应，很容易被误判成别的问题），不用在这里单独分支处理。
        return self._response_builder.build(
            results=merged,
            query=query,
            collection=contributing_collections or candidate_collections,
        )
    
    async def _execute_remote(
        self,
        query: str,
        top_k: int,
        collection: str,
        connector: "TenantConnector",
        trace: TraceContext,
    ) -> MCPToolResponse:
        """委托到企业自己的知识库微服务（统一 HTTP 契约，见
        `knowledge-base-tenant-federation.md` 第 4 节）。

        本地 hybrid search / rerank 完全不跑——企业自己的服务已经返回排好序的结果，
        我们只负责转发请求、把响应转成 `MCPToolResponse`。
        """
        import httpx
        import time as _time

        org_id = connector.org_id
        token = connector.auth_config.get("token", "")
        t0 = _time.monotonic()

        async def _record(success: bool, error: Optional[str] = None) -> float:
            elapsed = (_time.monotonic() - t0) * 1000.0
            # 网关监控页的调用/失败计数来源——每条退出路径都要记，不只是成功路径
            # （见 tenant_connector_store.record_call 的 docstring）。指标记录失败
            # 不应该影响本次查询结果，静默吞掉。
            try:
                await self.tenant_connector_store.record_call(connector.connector_id, success, elapsed, error)
            except Exception:
                logger.warning("record_call failed", exc_info=True)
            return elapsed

        try:
            async with httpx.AsyncClient(timeout=REMOTE_SEARCH_TIMEOUT_SECONDS) as client:
                resp = await client.post(
                    f"{connector.endpoint.rstrip('/')}/v1/search",
                    json={"query": query, "top_k": top_k, "collection": collection, "filters": {}},
                    headers={
                        "Authorization": f"Bearer {token}",
                        "X-Organization-Id": org_id,
                        "Content-Type": "application/json",
                    },
                )

            if resp.status_code in (401, 403):
                elapsed_ms = await _record(False, f"HTTP {resp.status_code}")
                trace.record_stage("remote_search", {"status": resp.status_code, "org_id": org_id}, elapsed_ms=elapsed_ms)
                TraceCollector().collect(trace)
                return self._build_remote_error_response(
                    query, collection, "知识库鉴权失败，请联系管理员检查连接器配置。"
                )
            resp.raise_for_status()

            results = self._parse_remote_results(resp.json())
            elapsed_ms = await _record(True)
            trace.record_stage("remote_search", {
                "status": resp.status_code, "org_id": org_id, "result_count": len(results),
            }, elapsed_ms=elapsed_ms)

            # 委托模式下每条结果打的 collection 标签——企业自己的知识库服务
            # 按第 4.2 节契约在 metadata.kb_name 里报了"这条结果来自它内部哪个
            # 子库"（人话标签，比如"人力资源"）时，就用 "{tenant_collection}:{kb_name}"
            # 这个更细的标签，让 UI 角标能显示"本企业知识库 · 人力资源"而不是笼统
            # 的"本企业知识库"；没报这个可选字段的企业（真实客户接入时最常见的
            # 情况——契约必选部分只有 content/score/source）就还是整体一个
            # `collection`，跟改造前完全一样，不受影响。跟 _execute_local_multi
            # 给每个 chunk 打 collection 来源标记（供合并结果统计"最终结果实际
            # 来自哪几个库"）是同一个套路，只是这边的"库"是企业自己上报的，不是
            # 平台自己知道的固定部门清单。
            for r in results:
                kb_name = r.metadata.get("remote_kb_name")
                r.metadata["collection"] = f"{collection}:{kb_name}" if kb_name else collection
            contributing = sorted({r.metadata["collection"] for r in results}) or [collection]

            response = self._response_builder.build(results=results, query=query, collection=contributing)
            trace.metadata["final_results"] = [
                {"chunk_id": r.chunk_id, "score": round(r.score, 4), "text": r.text or "",
                 "source": r.metadata.get("source_path", ""), "title": r.metadata.get("title", "")}
                for r in results
            ]
            TraceCollector().collect(trace)
            return response

        except (httpx.TimeoutException, httpx.ConnectError) as e:
            elapsed_ms = await _record(False, "timeout_or_unreachable")
            trace.record_stage("remote_search", {"error": "timeout_or_unreachable", "org_id": org_id}, elapsed_ms=elapsed_ms)
            TraceCollector().collect(trace)
            return self._build_remote_error_response(
                query, collection, "该企业知识库暂时无法访问，请稍后再试。"
            )
        except httpx.HTTPStatusError as e:
            elapsed_ms = await _record(False, str(e))
            trace.record_stage("remote_search", {"error": str(e), "org_id": org_id}, elapsed_ms=elapsed_ms)
            TraceCollector().collect(trace)
            return self._build_remote_error_response(
                query, collection, "该企业知识库暂时无法访问，请稍后再试。"
            )
        except Exception as e:
            logger.exception(f"query_knowledge_hub remote delegation failed: {e}")
            elapsed_ms = await _record(False, str(e))
            trace.record_stage("remote_search", {"error": str(e), "org_id": org_id}, elapsed_ms=elapsed_ms)
            TraceCollector().collect(trace)
            return self._build_error_response(query, collection, str(e))

    @staticmethod
    def _parse_remote_results(payload: Dict[str, Any]) -> List[RetrievalResult]:
        """把企业知识库微服务的响应（第 4.2 节契约）转成本地 `RetrievalResult`。"""
        results: List[RetrievalResult] = []
        for i, item in enumerate(payload.get("results", [])):
            metadata = dict(item.get("metadata") or {})
            # CitationGenerator 认的字段名是 `source_path`（见 citation_generator.py），
            # 不是契约响应体里的 `source`——这里做一次字段名转译。
            metadata.setdefault("source_path", item.get("source", ""))
            if item.get("page") is not None:
                metadata.setdefault("page", item["page"])
            # 第 4.2 节契约的可选字段——企业自己的知识库服务上报"这条结果属于它
            # 内部哪个子库"的人话标签；改名成 `remote_kb_name`（不直接叫
            # `kb_name`）是为了跟本地 `_execute_local_multi` 已经在用
            # `metadata["collection"]` 表示"来源集合"的约定分开，避免这里
            # 塞进去的值被下游当成本地 collection 名误用（比如反过来传回
            # execute() 的 collection 参数——那样会撞上 ACL/tenant_ 前缀拦截，
            # 见 execute() 里的相关检查）。_execute_remote 读到这个字段后才会
            # 组装出真正写进 RetrievalResult.metadata["collection"] 的展示用
            # 标签。
            kb_name = item.get("metadata", {}).get("kb_name") if isinstance(item.get("metadata"), dict) else None
            if kb_name:
                metadata["remote_kb_name"] = kb_name
            results.append(RetrievalResult(
                chunk_id=f"remote_{i}",
                score=float(item.get("score", 0.0)),
                text=item.get("content", ""),
                metadata=metadata,
            ))
        return results

    def _build_remote_error_response(self, query: str, collection: str, message: str) -> MCPToolResponse:
        """企业知识库微服务不可用/鉴权失败时的统一降级响应（第 4.3 节）。"""
        content = f"## 知识库暂不可用\n\n查询: **{query}**\n集合: `{collection}`\n\n{message}\n"
        return MCPToolResponse(
            content=content,
            citations=[],
            metadata={"query": query, "collection": collection, "error": "remote_unavailable"},
            is_empty=True,
        )

    def _build_access_denied_response(
        self,
        query: str,
        collection: str,
    ) -> MCPToolResponse:
        """Build response for an ACL-denied query.

        Args:
            query: Original query.
            collection: Collection the caller was denied access to.

        Returns:
            MCPToolResponse indicating the access was denied.
        """
        content = f"## 无权访问\n\n"
        content += f"查询: **{query}**\n"
        content += f"集合: `{collection}`\n\n"
        content += "你没有权限访问这个知识库集合，如需访问请联系管理员。\n"

        return MCPToolResponse(
            content=content,
            citations=[],
            metadata={
                "query": query,
                "collection": collection,
                "error": "access_denied",
            },
            is_empty=True,
        )

    def _build_error_response(
        self,
        query: str,
        collection: str,
        error_message: str,
    ) -> MCPToolResponse:
        """Build error response.
        
        Args:
            query: Original query.
            collection: Target collection.
            error_message: Error description.
            
        Returns:
            MCPToolResponse indicating error.
        """
        content = f"## 查询失败\n\n"
        content += f"查询: **{query}**\n"
        content += f"集合: `{collection}`\n\n"
        content += f"**错误信息:** {error_message}\n\n"
        content += "请检查:\n"
        content += "- 数据库连接是否正常\n"
        content += "- 集合是否已创建并包含数据\n"
        content += "- 配置文件是否正确\n"
        
        return MCPToolResponse(
            content=content,
            citations=[],
            metadata={
                "query": query,
                "collection": collection,
                "error": error_message,
            },
            is_empty=True,
        )


# Module-level tool instance (lazy-initialized)
_tool_instance: Optional[QueryKnowledgeHubTool] = None


def get_tool_instance(settings: Optional[Settings] = None) -> QueryKnowledgeHubTool:
    """Get or create the tool instance.
    
    Args:
        settings: Optional settings to use for initialization.
        
    Returns:
        QueryKnowledgeHubTool instance.
    """
    global _tool_instance
    if _tool_instance is None:
        _tool_instance = QueryKnowledgeHubTool(settings=settings)
    return _tool_instance


async def query_knowledge_hub_handler(
    query: str,
    top_k: int = 5,
    collection: Optional[str] = None,
) -> types.CallToolResult:
    """Handler function for MCP tool registration.
    
    This function is registered with the ProtocolHandler and called
    when the MCP client invokes the query_knowledge_hub tool.
    
    Supports multimodal responses - if search results contain images,
    the response will include ImageContent blocks alongside TextContent.
    
    Args:
        query: Search query string.
        top_k: Maximum number of results.
        collection: Optional collection name.
        
    Returns:
        MCP CallToolResult with content blocks (text and optionally images).
    """
    tool = get_tool_instance()
    
    try:
        response = await tool.execute(
            query=query,
            top_k=top_k,
            collection=collection,
        )
        
        # Use to_mcp_content() which handles multimodal (text + images)
        content_blocks = response.to_mcp_content()
        
        return types.CallToolResult(
            content=content_blocks,
            isError=response.is_empty and "error" in response.metadata,
        )
        
    except ValueError as e:
        # Invalid parameters
        return types.CallToolResult(
            content=[
                types.TextContent(
                    type="text",
                    text=f"参数错误: {e}",
                )
            ],
            isError=True,
        )
    except Exception as e:
        # Internal error
        logger.exception(f"query_knowledge_hub handler error: {e}")
        return types.CallToolResult(
            content=[
                types.TextContent(
                    type="text",
                    text=f"内部错误: 查询处理失败",
                )
            ],
            isError=True,
        )


def register_tool(protocol_handler) -> None:
    """Register query_knowledge_hub tool with the protocol handler.
    
    Args:
        protocol_handler: ProtocolHandler instance to register with.
    """
    protocol_handler.register_tool(
        name=TOOL_NAME,
        description=TOOL_DESCRIPTION,
        input_schema=TOOL_INPUT_SCHEMA,
        handler=query_knowledge_hub_handler,
    )
    logger.info(f"Registered MCP tool: {TOOL_NAME}")
