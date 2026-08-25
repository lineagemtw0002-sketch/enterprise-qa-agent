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
import time
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

# 平台自己组织（org_platform）不再有任何本地业务知识库——2026-08-22 起平台
# 运营方只负责用户/角色/审计/运营仪表盘这类系统管理，不代表任何一家具体企业，
# 挂着"财务政策""销售话术"这类业务知识库本身就名不正言不顺。原来这里有一份
# `DEPARTMENT_KB_COLLECTIONS`（6 个固定部门库，物理上是平台自己本地共享的
# Chroma collection），已经连同角色关联、BM25 索引一并下线——那 6 个知识库
# 分组名字（hr_admin_kb/finance_kb/...）还在，但现在只用于委托模式企业的
# 类目过滤（见下面 DEPARTMENT_KB_GROUP_TO_REMOTE_CATEGORIES），跟"平台自己
# 有没有本地库"是两件不相关的事。之前误摄入 README 的 "default" collection、
# 以及迁移前的 4 个老单文档部门库（it_kb/attendance_kb/logistics_kb/legal_kb）
# 也是同一天一起下线的。`it_dept`/`attendance_dept`/`logistics_dept`/`legal_dept`
# 这几个系统角色本身还在（没有下线角色），持有它们的用户本地检索这条路径查不到
# 任何部门库，这是预期结果，不是 bug——平台压根没有本地部门库可查了。

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

# 委托模式（企业自己的知识库微服务，如 Acme/Globex）下的部门级过滤——2026-08-22
# 从 bob_acme（IT部）问出财务/供应商发票内容这个真实案例排查出来的缺口：委托
# 模式一旦命中 `_execute_remote`，之前完全没有任何按角色/部门的过滤，同一家
# 企业不同部门的人查到的内容是一样的（`collection` 参数对委托模式下的访问控制
# 毫无作用，只影响转发到哪个企业自己的服务，不影响返回什么内容）。
#
# 这里用企业自己知识库微服务上报的可选字段 metadata.kb_name（见
# services/tenant_kb_demo/app.py `_category_label`，参考实现确实上报了，真实
# 客户接入时不一定会）在我们这一侧做二次过滤——不是本地检索那套 collection
# 级 ACL（那套只认本地 collection 名，管不到委托企业自己的分类体系）。
#
# 这份知识库分组 -> 可见类目中文标签的映射是我们自己按"分组名字面意思 +
# Acme/Globex 演示语料的类目名"推断出来的默认策略，不是企业自己配的（后台还
# 没有让企业管理员自定义这份映射的入口，后续要加真实客户接入时再补）。
# org_admin 不查这份映射，视为企业内部无限制（跟本地检索路径
# `get_allowed_collections_for_user` 对 org_admin 的特判是同一个语义："企业
# 管理员=企业内全部知识库"）。
#
# 没有上报 kb_name 的结果、或者用户不属于这份映射里的任何分组（包括压根没有
# 知识库分组的账号）一律拦下（fail-closed）而不是放行——委托企业如果压根不
# 上报分类，非管理员员工在这条路径上会看到"无权访问"而不是内容，这是刻意的：
# 宁可员工发现自己好像用不了、去找管理员，也不要在权限判断不出来的时候默认放行。
#
# 这 6 个分组名字就是角色名（role_store.py）——2026-08-23 起角色和知识库分组
# 合并回一套（角色直接携带知识库权限，见 role_store.py 文件顶部说明），这里
# 改成直接读 `RoleStore.get_user_roles` 的角色名集合做匹配，不再单独查一次
# 知识库分组。跟平台之前那 6 个本地部门知识库同一套分类（人力资源与行政/
# 财务与报销制度/IT支持与技术运维/销售话术与市场/研发与产品代码/客户成功
# 与售后服务，见 services/tenant_kb_demo/app.py 的 CATEGORY_LABELS、
# scripts/generate_tenant_kb_corpus.py），跟角色名是严格一对一。
# legal_dept/attendance_dept/logistics_dept/it_dept 这几个纯工作流审批路由
# 用的部门角色（在这家委托企业没有另外配置知识库关联）不在这份映射里，持有
# 它们的委托企业员工在这条路径上会查不到任何内容（fail-closed 的自然结果，
# 不是 bug）。
DEPARTMENT_KB_GROUP_TO_REMOTE_CATEGORIES: Dict[str, List[str]] = {
    "hr_admin_kb": ["人力资源与行政"],
    "finance_kb": ["财务与报销制度"],
    "it_support_kb": ["IT支持与技术运维"],
    "sales_marketing_kb": ["销售话术与市场"],
    "rd_product_kb": ["研发与产品代码"],
    "customer_success_kb": ["客户成功与售后服务"],
}


# Tool metadata
TOOL_NAME = "query_knowledge_hub"
TOOL_DESCRIPTION = """Search the enterprise knowledge base for relevant documents.

This tool uses hybrid search (semantic + keyword) to find the most relevant
documents matching your query, then reranks them. Results include source
citations for reference.

Your organization's knowledge base may be split into several department
libraries (e.g. HR & Admin, Finance & Reimbursement, IT Support & Ops, Sales &
Marketing, R&D & Product, Customer Success & After-sales). You do NOT need to
pick which one to search — leave `collection` unset and the tool searches
across all department libraries the caller has access to in parallel, merges
the candidates, and reranks them. Only set `collection` if you already know
the exact internal collection name and want to restrict the search to just
that one.

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

        - `org` 是平台自己的组织（org_platform）：空列表——平台运营方不代表任何
          一家具体企业，2026-08-22 起不再有任何本地业务知识库（原来的 6 个部门
          库连同更早下线的老 5 个部门库，都已经物理删除，见模块顶部说明）。
        - `org` 是别的（本地检索）企业：`org_collections` 表里登记的、这家企业
          自己创建的知识库（见 collection_store.py）——不会把别的企业自己建的
          库混进来，这正是"别的企业看不到、也查不到这家企业知识库内容"（见
          knowledge-base-tenant-federation.md 相关权限边界讨论）在检索层的落地。
        - 没有 user_id/查不到 org（老的独立 MCP server 调用方，没有身份概念）：
          同样返回空列表——没有身份就无法判断"该给哪家企业的库"，不能再假定
          是平台的库（平台现在压根没有本地库）。
        """
        if org is None or org.is_platform:
            return []
        owned = await self.org_collection_store.list_for_org(org.org_id)
        return [c.collection_name for c in owned]

    @property
    def settings(self) -> Settings:
        """Get settings, loading if necessary."""
        if self._settings is None:
            self._settings = load_settings()
        return self._settings

    def _ensure_shared_clients(self) -> None:
        """把 embedding_client/reranker 这两个跨 collection 共享、只需要建一次
        的对象准备好——`_build_hybrid_search_for` 和 `_narrow_by_document_summary`
        原来各自写了一遍一模一样的 `if self._x is None: ...` 判断，这里抽成
        一个方法，两处都改成调它，同时也是 `preload_models()`（见下面）在
        应用启动阶段"预热"时调的同一个入口——保证"预热时走的代码路径"和
        "真实请求第一次用到时走的代码路径"完全是同一段逻辑，不会出现预热
        逻辑跟正式逻辑各写一份、以后改了一处忘了改另一处的问题。"""
        from src.libs.embedding.embedding_factory import EmbeddingFactory
        from src.core.query_engine.reranker import create_core_reranker

        if self._embedding_client is None:
            self._embedding_client = EmbeddingFactory.create(self.settings)
        if self._reranker is None:
            self._reranker = create_core_reranker(settings=self.settings)

    async def preload_models(self) -> None:
        """应用启动阶段主动预热——见 docs/optimization_tracking.md 耗时优化
        任务"知识库检索为什么要 7 秒"那次排查：真正的大头不是检索/重排本身
        （几十到几百毫秒），是 `self._reranker`（本地 cross-encoder
        `BAAI/bge-reranker-base`）第一次被用到时才现场加载模型，撞上这次
        加载的是"运气不好、第一个问知识库问题的真实用户"，代价被摊派给了
        它，而不是摊派给"服务启动"这个所有用户都不会感知到的阶段。

        `_ensure_shared_clients()` 建好 embedding_client/reranker 两个对象后，
        这里再额外发一次真实调用（不只是建对象），两个都要跑，理由不完全
        一样：
        1. **embedding_client**：Ollama 那边的 embedding 模型本身也有独立的
           冷启动（同一次排查里 embed_query 这一步从 587ms 降到 27ms，降的
           就是这个），只建 client 对象不会触发它加载，必须真的调一次。
        2. **reranker**：这是后来又发现的第二层、更小的冷启动——权重加载
           好了不代表"针对某个具体输入形状的计算图"也编译好了，本地跑的是
           PyTorch MPS（Metal GPU）后端，**第一次真正调用推理**时还要为这
           个输入形状现场编译一次计算核心，实测同一批候选、同一个进程内，
           第一次调用 573ms、第二次只要 32ms，差了将近 20 倍。只创建
           reranker 对象（`_ensure_shared_clients`）不会触发这层编译，必须
           真的跑一次 `rerank()`（而不是空转），且候选数量要 ≥ 2——
           `CoreReranker.rerank()` 对 0/1 条候选有专门的短路分支，根本不会
           进真正的 cross-encoder 推理，预热不到点上。

        查询文本/候选内容随便传占位符即可，这次调用的结果不使用，只为了
        触发这两层各自的模型加载/计算图编译。

        调用方（app.py lifespan）需要对每一个真实持有的 `QueryKnowledgeHubTool`
        实例都单独调一次——`self._reranker`/`self._embedding_client` 是实例
        级别的缓存，不跨实例共享（当前项目里这个工具类被实例化了不止一处，
        各自预热互不影响，也互不能替对方省下这次加载）。"""
        self._ensure_shared_clients()
        try:
            await asyncio.to_thread(self._embedding_client.embed, ["预热"])
        except Exception as e:
            logger.warning(f"[Preload] embedding warm-up call failed (non-fatal): {e}")

        if self._reranker is not None and self._reranker.is_enabled:
            try:
                dummy = [
                    RetrievalResult(chunk_id="preload_1", score=0.0, text="预热占位文本一"),
                    RetrievalResult(chunk_id="preload_2", score=0.0, text="预热占位文本二"),
                ]
                await asyncio.to_thread(self._reranker.rerank, "预热", dummy, 2)
            except Exception as e:
                logger.warning(f"[Preload] reranker warm-up call failed (non-fatal): {e}")

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
        from src.ingestion.storage.bm25_indexer import BM25Indexer
        from src.libs.vector_store.vector_store_factory import VectorStoreFactory

        # === 真正无状态、可跨 collection 共享的部分：挂在 self 上复用 ===
        self._ensure_shared_clients()

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

        # 委托模式下按知识库分组过滤的可见类目集合——只在这里（remote 分支）用，
        # 跟上面 `allowed_collections`（本地 collection ACL）是两个不同的概念，
        # 不复用同一个变量：None 表示"不过滤"（org_admin，或没有 user_id 的老
        # 调用方，保留原有行为），空集合表示"这个人没有任何知识库分组能匹配上
        # 已知类目，过滤到一条不剩"，不是"跳过过滤"。见
        # DEPARTMENT_KB_GROUP_TO_REMOTE_CATEGORIES 旁边的完整说明。
        remote_allowed_categories: Optional[set] = None
        if is_remote and user_id is not None:
            from src.ragent_backend.role_store import RoleStore, ROLE_ORG_ADMIN

            role_names = {r.name for r in await RoleStore().get_user_roles(user_id)}
            if ROLE_ORG_ADMIN not in role_names:
                remote_allowed_categories = set()
                for role_name in role_names:
                    remote_allowed_categories.update(DEPARTMENT_KB_GROUP_TO_REMOTE_CATEGORIES.get(role_name, []))

        # 委托模式（企业自己的知识库微服务）：这几个固定部门知识库是本地
        # internal_chroma 专属的概念，委托出去的查询不受影响，沿用老逻辑——
        # 单一 collection，默认值取 "default"（企业微服务自己决定怎么理解
        # 这个值，见 knowledge-base-tenant-federation.md 第 5.2 节）。检索质量
        # /召回策略仍然完全交给对方（我们不重排、不判断相关性，见
        # `_execute_remote` 说明），但访问控制不再是"委托模式=我们完全不管"——
        # 上面算出的 `remote_allowed_categories` 会在 `_execute_remote` 里按
        # 对方上报的 metadata.kb_name 做二次过滤，见该方法内的说明。
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
            return await self._execute_remote(
                query, effective_top_k, effective_collection, connector, trace, remote_allowed_categories,
            )

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

    # ============================================================
    # 【测试专用，正式上线前删除】管理员知识库测试查询
    # ============================================================
    async def execute_admin_bypass(
        self, query: str, org_id: str, top_k: Optional[int] = None,
    ) -> MCPToolResponse:
        """管理员测试页专用（app.py `admin_test_query_knowledge_base`）——绕过任何
        用户级 ACL，直接对指定企业名下的知识库能力做一次查询，用来验证"这家企业
        的知识库到底能查到什么、内容对不对"，不代表任何真实用户的实际可见范围。

        调用方必须在路由层用 super_admin 权限守住，这个方法本身不做任何权限
        判断——它存在的意义就是绕开 execute() 里那一整套 ACL，仅供内部测试工具
        使用，不能注册成 MCP 工具或暴露给任何非管理员路径。

        跟 execute() 共用同一套本地/委托检索实现（_org_owned_collections /
        _execute_local_multi / _execute_remote），只是候选集直接取"这家企业名下
        全部 collection"，不跟任何用户的 allowed_collections 取交集。

        这是一次性测试工具，正式上线前应当整体删除：本方法、app.py 里对应的
        /api/v1/admin/test/knowledge-query 端点、schemas.py 里的
        AdminTestKBQueryRequest/Response、前端 KnowledgeBaseTestQuery.jsx 及其
        在 OperationsDashboard.jsx 里的入口。
        """
        from src.ragent_backend.tenant_connector_store import (
            CAPABILITY_KNOWLEDGE_BASE,
            CONNECTOR_TYPE_HTTP_API,
        )

        if not query or not query.strip():
            raise ValueError("Query cannot be empty")

        effective_top_k = min(top_k or self.config.default_top_k, self.config.max_top_k)

        org = await self.org_store.get_organization(org_id)
        if org is None:
            raise ValueError(f"Organization '{org_id}' not found")

        connector = await self.tenant_connector_store.get(org_id, CAPABILITY_KNOWLEDGE_BASE)
        if connector is not None and connector.connector_type == CONNECTOR_TYPE_HTTP_API:
            effective_collection = f"tenant_{org_id}_kb"
            trace = TraceContext(trace_type="query")
            trace.metadata["query"] = query[:200]
            trace.metadata["top_k"] = effective_top_k
            trace.metadata["collection"] = effective_collection
            trace.metadata["source"] = "admin_test_bypass"
            trace.metadata["connector_type"] = connector.connector_type
            return await self._execute_remote(query, effective_top_k, effective_collection, connector, trace)

        candidate_collections = await self._org_owned_collections(org)
        if not candidate_collections:
            return self._build_empty_response_for_org(query, org_id)

        trace = TraceContext(trace_type="query")
        trace.metadata["query"] = query[:200]
        trace.metadata["top_k"] = effective_top_k
        trace.metadata["candidate_collections"] = candidate_collections
        trace.metadata["source"] = "admin_test_bypass"
        trace.metadata["connector_type"] = "internal_chroma"
        try:
            response = await self._execute_local_multi(query, effective_top_k, candidate_collections, trace)
            TraceCollector().collect(trace)
            return response
        except Exception as e:
            logger.exception(f"execute_admin_bypass (parallel recall) failed: {e}")
            TraceCollector().collect(trace)
            return self._build_error_response(query, ",".join(candidate_collections), str(e))

    # ==================== 内部 QA 测试用：清空/查看某企业知识库 ====================
    # 跟 execute_admin_bypass 是同一批"绕过正常权限边界的内部工具"，调用方（app.py）
    # 已经用 super_admin + platform_admin + RAGENT_DEBUG=true 三层守住，这里不
    # 重复做权限判断。本地检索企业直接读写共享 Chroma/BM25；委托模式企业代理到
    # 企业自己知识库微服务的管理端点（见 services/tenant_kb_demo/app.py 的
    # /v1/collection/*，那几个端点本身也不是统一契约的一部分，只是参考实现
    # 额外加的测试入口，真实客户接入的服务不需要实现它们）。

    async def _resolve_org_and_connector(self, org_id: str):
        from src.ragent_backend.tenant_connector_store import CAPABILITY_KNOWLEDGE_BASE, CONNECTOR_TYPE_HTTP_API

        org = await self.org_store.get_organization(org_id)
        if org is None:
            raise ValueError(f"Organization '{org_id}' not found")
        connector = await self.tenant_connector_store.get(org_id, CAPABILITY_KNOWLEDGE_BASE)
        is_remote = connector is not None and connector.connector_type == CONNECTOR_TYPE_HTTP_API
        return org, (connector if is_remote else None)

    async def list_org_collection_stats(self, org_id: str) -> List[Dict[str, Any]]:
        org, connector = await self._resolve_org_and_connector(org_id)
        if connector is not None:
            stats = await self._remote_collection_stats(connector)
            categories = stats.get("categories") or []
            if not categories:
                # 企业没上报分类信息（categories 为空，比如接的是没实现这个
                # 可选统计端点的老版本参考实现）时的兜底：退回整个 collection
                # 一条，保留改这次之前的行为，不因为这个可选字段缺失就什么都
                # 看不到。真的没有任何数据（chunk_count=0）时不展示这一条。
                return [{
                    "collection_name": stats["collection"], "display_name": "本企业委托知识库",
                    "source": "delegated", "chunk_count": stats.get("chunk_count", 0),
                }] if stats.get("chunk_count", 0) else []
            # 每个类目一行——`collection_name` 编成 "{tenant_collection}:{分类}"
            # 这个形状（跟 `_execute_remote` 给结果打 collection 标签用的是
            # 同一个约定），查看 chunk / 清空时原样传回来，靠这个拆出具体分类，
            # 不需要另外发明一套"分类 id"。
            return [{
                "collection_name": f"{stats['collection']}:{c['category']}",
                "display_name": c["category"],
                "source": "delegated", "chunk_count": c["chunk_count"],
            } for c in categories]
        if org.is_platform:
            return []
        owned = await self.org_collection_store.list_for_org(org_id)
        results = []
        for c in owned:
            results.append({
                "collection_name": c.collection_name, "display_name": c.display_name,
                "source": "local", "chunk_count": await self._local_collection_count(c.collection_name),
            })
        return results

    @staticmethod
    def _split_remote_category(collection: str) -> Optional[str]:
        """从 list_org_collection_stats 拼出的 "{tenant_collection}:{分类}"
        里取出分类部分——没有冒号时说明是"没分类信息"兜底的整库那一条，
        返回 None 表示不按分类过滤/清空，行为等同于改这次之前。"""
        return collection.split(":", 1)[1] if ":" in collection else None

    async def list_org_collection_chunks(
        self, org_id: str, collection: str, limit: int = 50, offset: int = 0,
    ) -> List[Dict[str, Any]]:
        # offset 只对本地检索生效——委托模式的参考实现语料量很小，压根没有
        # 分页概念（见 services/tenant_kb_demo/app.py list_chunks 旁的说明），
        # 传了也会被忽略，这里干脆不传，避免造成"这个参数在委托模式下也生效"
        # 的错觉。企业管理员的「知识库权限」自助分页查数据功能本来就只对本地
        # 检索企业开放（跟"新增知识库"同一条边界，见 _require_local_retrieval_org）。
        org, connector = await self._resolve_org_and_connector(org_id)
        if connector is not None:
            category = self._split_remote_category(collection)
            return await self._remote_collection_chunks(connector, limit, category=category)
        await self._assert_local_collection_owned(org, collection)
        return await self._local_collection_chunks(collection, limit, offset=offset)

    async def clear_org_collection(self, org_id: str, collection: str) -> int:
        org, connector = await self._resolve_org_and_connector(org_id)
        if connector is not None:
            category = self._split_remote_category(collection)
            return await self._remote_collection_clear(connector, category=category)
        await self._assert_local_collection_owned(org, collection)
        return await self._local_collection_clear(collection)

    async def _assert_local_collection_owned(self, org, collection: str) -> None:
        """防止拼一个别的企业的 collection 名字，用这家企业的 org_id 清空/查看
        到别人的知识库——跟 execute() 里 tenant_ 前缀硬拦截、org_owned 二次校验
        是同一个"企业边界不能靠调用方老实"的思路。"""
        owned_names = {c.collection_name for c in await self.org_collection_store.list_for_org(org.org_id)}
        if collection not in owned_names:
            raise ValueError(f"'{collection}' 不属于企业 '{org.org_id}'")

    @staticmethod
    async def _local_collection_count(collection_name: str) -> int:
        from src.libs.vector_store.vector_store_factory import VectorStoreFactory

        def _sync() -> int:
            store = VectorStoreFactory.create(load_settings(), collection_name=collection_name)
            return store.get_collection_stats()["count"]

        return await asyncio.to_thread(_sync)

    @staticmethod
    async def _local_collection_chunks(collection_name: str, limit: int, offset: int = 0) -> List[Dict[str, Any]]:
        from src.libs.vector_store.vector_store_factory import VectorStoreFactory

        def _sync() -> List[Dict[str, Any]]:
            store = VectorStoreFactory.create(load_settings(), collection_name=collection_name)
            raw = store.collection.get(limit=limit, offset=offset, include=["metadatas", "documents"])
            items = []
            for i, chunk_id in enumerate(raw.get("ids", [])):
                metadata = (raw.get("metadatas") or [{}])[i] or {}
                document = (raw.get("documents") or [""])[i] or ""
                items.append({
                    "chunk_id": chunk_id, "text": document,
                    "source_path": metadata.get("source_path", ""), "kb_name": None,
                })
            return items

        return await asyncio.to_thread(_sync)

    @staticmethod
    async def _local_collection_clear(collection_name: str) -> int:
        """跟这次会话里手动清理老部门库用的是同一套三步（Chroma collection +
        BM25 索引目录 + ingestion_history/chunk_content_index 记录），这里把它
        收进代码而不是留在临时脚本里，供页面反复调用。"""
        import shutil

        from src.libs.vector_store.vector_store_factory import VectorStoreFactory

        def _sync() -> int:
            store = VectorStoreFactory.create(load_settings(), collection_name=collection_name)
            cleared = store.get_collection_stats()["count"]
            store.client.delete_collection(collection_name)

            bm25_dir = resolve_path(f"data/db/bm25/{collection_name}")
            if bm25_dir.exists():
                shutil.rmtree(bm25_dir)

            import sqlite3
            history_db = resolve_path("data/db/ingestion_history.db")
            if history_db.exists():
                conn = sqlite3.connect(str(history_db))
                try:
                    conn.execute("DELETE FROM ingestion_history WHERE collection = ?", (collection_name,))
                    conn.execute("DELETE FROM chunk_content_index WHERE collection = ?", (collection_name,))
                    conn.commit()
                finally:
                    conn.close()
            return cleared

        return await asyncio.to_thread(_sync)

    async def _remote_collection_stats(self, connector: "TenantConnector") -> Dict[str, Any]:
        import httpx

        async with httpx.AsyncClient(timeout=REMOTE_SEARCH_TIMEOUT_SECONDS) as client:
            resp = await client.get(
                f"{connector.endpoint.rstrip('/')}/v1/collection/stats",
                headers={"Authorization": f"Bearer {connector.auth_config.get('token', '')}"},
            )
        resp.raise_for_status()
        return resp.json()

    async def _remote_collection_chunks(
        self, connector: "TenantConnector", limit: int, category: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        import httpx

        params: Dict[str, Any] = {"limit": limit}
        if category:
            params["category"] = category
        async with httpx.AsyncClient(timeout=REMOTE_SEARCH_TIMEOUT_SECONDS) as client:
            resp = await client.get(
                f"{connector.endpoint.rstrip('/')}/v1/collection/chunks",
                params=params,
                headers={"Authorization": f"Bearer {connector.auth_config.get('token', '')}"},
            )
        resp.raise_for_status()
        return resp.json().get("chunks", [])

    async def _remote_collection_clear(
        self, connector: "TenantConnector", category: Optional[str] = None,
    ) -> int:
        import httpx

        params: Dict[str, Any] = {"category": category} if category else {}
        async with httpx.AsyncClient(timeout=REMOTE_SEARCH_TIMEOUT_SECONDS) as client:
            resp = await client.delete(
                f"{connector.endpoint.rstrip('/')}/v1/collection",
                params=params,
                headers={"Authorization": f"Bearer {connector.auth_config.get('token', '')}"},
            )
        resp.raise_for_status()
        return resp.json().get("cleared_chunks", 0)

    @staticmethod
    def _build_empty_response_for_org(query: str, org_id: str) -> MCPToolResponse:
        """管理员测试页专用：这家企业压根没有登记任何知识库 collection 时的提示，
        跟 _build_access_denied_response 语义不同（不是权限问题，是这家企业还
        没建过库）。"""
        content = f"## 该企业暂无知识库\n\n查询: **{query}**\n企业: `{org_id}`\n\n这家企业名下还没有登记任何知识库 collection。\n"
        return MCPToolResponse(
            content=content,
            citations=[],
            metadata={"query": query, "org_id": org_id, "error": "no_collections"},
            is_empty=True,
        )

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
            # 提示词注入防护，见 _filter_injected_chunks 旁的说明——重排之前拦。
            results = self._filter_injected_chunks(results, trace)

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
        filters: Optional[Dict[str, Any]] = None,
    ) -> List[RetrievalResult]:
        """跟 _perform_search 逻辑一样，但接收显式传入的 HybridSearch 实例，
        不读 self._hybrid_search——"全库混合召回"并发查多个 collection 时，
        每个 collection 自己的 HybridSearch 局部变量互不干扰，靠的就是这个
        方法不碰共享的 self 状态（见 _build_hybrid_search_for 的说明）。

        filters 透传给 HybridSearch.search()——层次化检索粗筛后按
        {"source_ref": [doc_id, ...]} 收窄到摘要层选中的那几份文档时用
        （见 _execute_local_multi），默认 None 保持原有的"整个 collection
        都是候选池"行为不变。"""
        # Use a larger initial retrieval for reranking
        initial_top_k = top_k * 2 if self.config.enable_rerank else top_k

        try:
            results = hybrid_search.search(
                query=query,
                top_k=initial_top_k,
                filters=filters,
                trace=trace,
                return_details=False,
            )
            return results if isinstance(results, list) else results.results
        except Exception as e:
            logger.warning(f"Hybrid search failed: {e}")
            return []

    def _filter_injected_chunks(
        self, results: List[RetrievalResult], trace: Optional[TraceContext] = None,
    ) -> List[RetrievalResult]:
        """检索时的提示词注入防护（docs/prompt_injection_remediation_plan.md
        问题2 P0）——摄入时的 `detect_document_injection` 检测（见
        pipeline.py）只挡新上传的文档，挡不住这个功能上线之前就已经在库里的
        老数据；而且检索到的投毒 chunk 会跟着"全库混合召回"混进跟它毫不相关
        的问题的上下文（安全复测发现：一句问数据库连接串的越狱话术，也能把
        `product_req_kb` 里的投毒文档钓鱼话术带出来），说明不能只在摄入时
        挡一次，检索到的内容每次也要重新过一遍——不管这条数据是什么时候
        进的库。

        在重排之前调用（`_execute_local_single`/`_execute_local_multi` 拿到
        原始候选集之后），而不是等重排完、或者等模型生成完之后再检查：
        1. 不依赖模型"听不听话"——这层是确定性代码，命中就直接从候选集里
           拿掉，模型压根没有机会接触这段内容，不用赌它会不会把内容说出来。
        2. 不给重排机会——投毒内容如果留到重排阶段，可能拿到一个不低的
           cross-encoder 分数，占用最终 top_k 里的一个名额、挤掉真正相关
           的结果；摄入时就问对了."""
        from src.security.prompt_guard import detect_document_injection

        filtered: List[RetrievalResult] = []
        dropped = 0
        for r in results:
            hit = detect_document_injection(r.text or "")
            if hit:
                dropped += 1
                logger.warning(
                    f"[InjectionGuard] Dropped retrieved chunk suspected of prompt "
                    f"injection: {hit!r} (chunk_id={getattr(r, 'chunk_id', '?')})"
                )
                continue
            filtered.append(r)

        if dropped and trace is not None:
            trace.record_stage("injection_filter", {
                "dropped_count": dropped, "remaining_count": len(filtered),
            })
        return filtered

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

    async def _narrow_by_document_summary(
        self, query: str, candidate_collections: List[str], trace: Optional["TraceContext"] = None,
    ) -> Dict[str, List[str]]:
        """层次化检索的"粗筛"阶段——见 ingestion/hierarchy/doc_summary.py 顶部
        说明。在每个候选 collection 的 `{collection}__summary` 摘要层各查一次
        （只做向量相似度，不跑 BM25/rerank，这一层要的是"大致相关"的快速信号，
        不是精确排序），把全部候选 collection 的摘要命中合并、按分数取整体
        前 N 份文档，按它们各自所属的 collection 分组返回。

        返回空 dict 表示"摘要层没有可用信号"（可能是这几个 collection 都还
        没有任何文档摘要——比如这个功能上线前就已经摄入的老数据），调用方
        （_execute_local_multi）据此决定要不要整体退回原来的平铺检索，不是
        把空结果当成"真的查无相关文档"处理。

        `trace` 是本次诊断"知识库检索为什么要 7 秒"时临时加的分段计时——把
        "建 6 个摘要 store（当前是串行）""embed 一次查询向量""并行查各 store"
        这三步拆开各自计时，写进 `trace.record_stage("narrow_detail", ...)`，
        跟正式的 rerank 计时用同一套机制，方便直接从 logs/traces.jsonl 里
        对比哪一步才是真正的大头。不影响任何检索结果，只是多记了几条日志。
        """
        from src.ingestion.hierarchy.doc_summary import summary_collection_name
        from src.libs.vector_store.vector_store_factory import VectorStoreFactory

        self._ensure_shared_clients()

        top_docs = getattr(getattr(self.settings, "ingestion", None), "doc_summary", None) or {}
        top_docs = top_docs.get("top_docs", 5)

        # 跟 _execute_local_multi 建 HybridSearch 时同一个坑：并发 new 多个
        # PersistentClient 指向同一个 persist_directory 不是线程安全的，必须
        # 先串行建好每个 collection 的 client，再并行查询。
        def _build_stores_sync() -> Dict[str, Any]:
            stores = {}
            for c in candidate_collections:
                try:
                    stores[c] = VectorStoreFactory.create(self.settings, collection_name=summary_collection_name(c))
                except Exception as e:
                    logger.warning(f"Failed to open summary store for '{c}': {e}")
            return stores

        _t0 = time.monotonic()
        summary_stores = await asyncio.to_thread(_build_stores_sync)
        _t_build = (time.monotonic() - _t0) * 1000.0
        if trace is not None:
            trace.record_stage("narrow_detail", {
                "step": "build_summary_stores", "collection_count": len(candidate_collections),
            }, elapsed_ms=_t_build)
        if not summary_stores:
            return {}

        _t0 = time.monotonic()
        query_vector = (await asyncio.to_thread(self._embedding_client.embed, [query]))[0]
        _t_embed = (time.monotonic() - _t0) * 1000.0
        if trace is not None:
            trace.record_stage("narrow_detail", {"step": "embed_query"}, elapsed_ms=_t_embed)

        def _query_one_sync(collection: str) -> List[Dict[str, Any]]:
            try:
                hits = summary_stores[collection].query(vector=query_vector, top_k=top_docs)
            except Exception as e:
                logger.warning(f"Summary query failed for '{collection}': {e}")
                return []
            for h in hits:
                h["_collection"] = collection
            return hits

        _t0 = time.monotonic()
        per_collection_hits = await asyncio.gather(
            *[asyncio.to_thread(_query_one_sync, c) for c in summary_stores],
        )
        _t_query = (time.monotonic() - _t0) * 1000.0
        if trace is not None:
            trace.record_stage("narrow_detail", {
                "step": "query_summary_stores", "store_count": len(summary_stores),
            }, elapsed_ms=_t_query)
        all_hits = [h for sub in per_collection_hits for h in sub]
        if not all_hits:
            return {}

        all_hits.sort(key=lambda h: h.get("score", 0.0), reverse=True)
        narrowed: Dict[str, List[str]] = {}
        for h in all_hits[:top_docs]:
            doc_id = h.get("metadata", {}).get("doc_id") or h.get("id")
            if not doc_id:
                continue
            narrowed.setdefault(h["_collection"], []).append(doc_id)
        return narrowed

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

        在真正的全量并行召回之前，先过一遍层次化检索的文档级粗筛（见
        `_narrow_by_document_summary`）——摘要层有信号时，把接下来的
        hybrid search 收窄到"只在这几份文档范围内"（通过 source_ref 过滤，
        见下面 _search_one_sync），减少候选池里跟查询没关系的文档稀释精排
        结果；摘要层没信号（老数据没有摘要）就整体退回原来的行为，不做任何
        收窄，保证这个功能是纯增量的，不会让还没补摘要的旧数据查不到东西。
        """
        _t0 = time.monotonic()
        narrowed = await self._narrow_by_document_summary(query, candidate_collections, trace=trace)
        _t_narrow_total = (time.monotonic() - _t0) * 1000.0
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
        # 摘要层有信号就只在命中的那几个 collection 里查（且带 source_ref
        # 过滤，收窄到具体那几份文档）；没信号（narrowed 为空）就是老行为——
        # 全部候选 collection、不加文档级过滤。
        search_collections = list(narrowed.keys()) if narrowed else candidate_collections
        # elapsed_ms 用整个 _narrow_by_document_summary() 的墙钟时间（含它
        # 内部三个子步骤），narrow_detail 那几条已经拆得更细，这里是总览。
        trace.record_stage("hierarchy_narrow", {
            "narrowed_collections": {c: docs for c, docs in narrowed.items()},
        }, elapsed_ms=_t_narrow_total)

        def _build_all_sync() -> Dict[str, "HybridSearch"]:
            return {c: self._build_hybrid_search_for(c) for c in search_collections}

        _t0 = time.monotonic()
        hybrid_searches = await asyncio.to_thread(_build_all_sync)
        _t_build_hybrid = (time.monotonic() - _t0) * 1000.0
        trace.record_stage("build_hybrid_searches", {
            "collection_count": len(search_collections),
        }, elapsed_ms=_t_build_hybrid)

        def _search_one_sync(collection: str) -> List[RetrievalResult]:
            _t0_one = time.monotonic()
            hybrid_search = hybrid_searches[collection]
            initial_top_k = top_k * 2 if self.config.enable_rerank else top_k
            doc_filter = {"source_ref": narrowed[collection]} if collection in narrowed else None
            results = self._search_with(hybrid_search, query, initial_top_k, trace=None, filters=doc_filter)
            # 打上来源标记，供合并后统计"最终结果实际来自哪几个库"（response
            # metadata 的 collections 字段，UI 来源角标用），以及排查问题时
            # 一眼看出某条结果是从哪个库召回的。
            for r in results:
                r.metadata = dict(r.metadata or {})
                r.metadata["collection"] = collection
            # 诊断用：每个 collection 自己的 HybridSearch.search() 内部会不会
            # 重新 embed 一次 query（跟 narrow 阶段那次 embedding 是否重复），
            # 这里先只测"这个 collection 整体花了多久"，跟 narrow_detail 的
            # embed_query 那条对比数量级——如果好几个 collection 各自都接近
            # 那个数字，基本能坐实"重复 embedding"这个猜测。
            trace.record_stage("search_one_collection", {
                "collection": collection, "result_count": len(results),
            }, elapsed_ms=(time.monotonic() - _t0_one) * 1000.0)
            return results

        _t0 = time.monotonic()
        per_collection_results = await asyncio.gather(
            *[asyncio.to_thread(_search_one_sync, c) for c in search_collections],
            return_exceptions=True,
        )
        _t_gather = (time.monotonic() - _t0) * 1000.0

        merged: List[RetrievalResult] = []
        for collection, sub_results in zip(search_collections, per_collection_results):
            if isinstance(sub_results, Exception):
                logger.warning(f"Search failed for collection '{collection}': {sub_results}")
                continue
            merged.extend(sub_results)

        trace.record_stage("parallel_recall", {
            "candidate_collections": candidate_collections,
            "merged_candidate_count": len(merged),
        }, elapsed_ms=_t_gather)

        # 提示词注入防护，见 _filter_injected_chunks 旁的说明——重排之前拦，
        # 不给投毒 chunk 机会拿到一个不低的重排分数、挤掉真正相关的结果。
        merged = self._filter_injected_chunks(merged, trace)

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
            collection=contributing_collections or search_collections,
        )
    
    async def _execute_remote(
        self,
        query: str,
        top_k: int,
        collection: str,
        connector: "TenantConnector",
        trace: TraceContext,
        remote_allowed_categories: Optional[set] = None,
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

            # 部门级过滤——见 DEPARTMENT_KB_GROUP_TO_REMOTE_CATEGORIES 旁边的说明。
            # None 表示不过滤（org_admin 或没有 user_id 的老调用方）；否则只保留
            # kb_name 命中允许类目集合的结果，企业没上报 kb_name 的结果一律拦下
            # （fail-closed，不是"看不出类目就放行"）。这一步只发生在委托模式，
            # 不影响本地检索路径的 ACL。
            if remote_allowed_categories is not None:
                before_filter = len(results)
                results = [r for r in results if r.metadata.get("remote_kb_name") in remote_allowed_categories]
                if before_filter and not results:
                    logger.warning(
                        f"Remote KB results filtered out by department scope: org_id={org_id}, "
                        f"allowed_categories={sorted(remote_allowed_categories)}"
                    )
                    TraceCollector().collect(trace)
                    return self._build_access_denied_response(query, collection)

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
