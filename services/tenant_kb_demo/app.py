"""企业知识库微服务 —— 参考实现（用于模拟"企业自建知识库微服务"）。

这是 `knowledge-base-tenant-federation.md` 第 4 节统一 HTTP 契约的一份参考实现，
演示"任何企业只要照着契约实现一个 HTTP 端点就能接入"这句话——本文件不是主系统
的一部分，是一个完全独立、可单独部署的进程，主系统只通过 `POST /v1/search` 跟它
打交道，内部用什么向量库/检索算法对主系统透明。

复用主系统已经验证过的 ChromaStore + HybridSearch（决策：见该文档 §1.5 决策 5），
但每个租户进程用自己独立的持久化目录（`TENANT_DATA_DIR`）和 collection，物理上
跟主系统、跟别的租户完全不共享存储。

启动（同一份代码，用环境变量区分"我是哪家公司"）：
    TENANT_NAME=acme TENANT_ORG_ID=org_acme TENANT_TOKEN=acme-demo-token \\
    TENANT_DATA_DIR=data/tenant_demo/acme TENANT_PORT=9101 \\
    uvicorn services.tenant_kb_demo.app:app --port 9101

    TENANT_NAME=globex TENANT_ORG_ID=org_globex TENANT_TOKEN=globex-demo-token \\
    TENANT_DATA_DIR=data/tenant_demo/globex TENANT_PORT=9102 \\
    uvicorn services.tenant_kb_demo.app:app --port 9102
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from src.core.settings import load_settings, resolve_path

logger = logging.getLogger("tenant_kb_demo")
logging.basicConfig(level=logging.INFO)

TENANT_NAME = os.getenv("TENANT_NAME", "demo")
TENANT_ORG_ID = os.getenv("TENANT_ORG_ID", "")
TENANT_TOKEN = os.getenv("TENANT_TOKEN", "")
TENANT_DATA_DIR = os.getenv("TENANT_DATA_DIR", f"data/tenant_demo/{TENANT_NAME}")
# collection 名必须租户唯一——`IngestionPipeline`（种子脚本用来灌数据的那条路）
# 内部把 BM25 索引目录写死成仓库根目录下的 `data/db/bm25/{collection}`（不支持按
# persist_directory 覆盖，见 `src/ingestion/pipeline.py`），所以 BM25 这一层的物理
# 隔离只能靠 collection 名唯一来保证；Chroma 向量库这一层则额外用 `TENANT_DATA_DIR`
# 做了持久化目录级别的物理隔离（见下方 `_ensure_ready`）。
TENANT_COLLECTION = os.getenv("TENANT_COLLECTION", f"tenant_{TENANT_NAME}_kb")

# 演示语料按 kb_corpus/<category>/*.txt 分目录组织（见
# scripts/generate_tenant_kb_corpus.py），摄入时这个目录名原样落进每个 chunk
# 的 source_path 里——这里从命中结果的 source_path 反推出它属于哪个子类目，
# 转成人话标签通过 metadata.kb_name 报给平台。这是第 4.2 节契约里的可选字段：
# 平台自己不认识"任何企业内部知识库该怎么分类"，只能由企业自己的服务（最懂自己
# 内部结构的一方）决定要不要、以及怎么把这层粒度透出来；不提供这个字段的企业
# （契约的必选部分只有 content/score/source）跟现在完全一样，UI 上退回展示笼统
# 的"本企业知识库"，不会因为这个可选字段缺失而报错或降级。
#
# 2026-08-22 第二版：6 个类目改成跟平台之前本地部门知识库同一套命名（人力资源
# 与行政/财务与报销制度/IT支持与技术运维/销售话术与市场/研发与产品代码/客户
# 成功与售后服务），不再是 Acme/Globex 各自一套互不相同的分类——这样
# query_knowledge_hub.py 的 DEPARTMENT_ROLE_TO_REMOTE_CATEGORIES 才能用同一份
# 角色->类目映射同时管两家企业，不用每接入一家新企业就单独维护一份映射表。
CATEGORY_LABELS: Dict[str, str] = {
    "hr_admin": "人力资源与行政",
    "finance": "财务与报销制度",
    "it_support": "IT支持与技术运维",
    "sales_marketing": "销售话术与市场",
    "rd_product": "研发与产品代码",
    "customer_success": "客户成功与售后服务",
}


def _category_label(source_path: str) -> str:
    """从 `.../kb_corpus/<category>/文件名` 反推 category，查不到已知标签时
    原样返回目录名（好过完全不展示）；source_path 为空或不含 kb_corpus 时
    返回空字符串，调用方据此决定要不要省略 kb_name 字段。只用于种子语料
    （目录结构里带 category），委托写入契约上传的文档走 `_metadata_kb_name`。"""
    if not source_path:
        return ""
    parts = Path(source_path).parts
    if "kb_corpus" not in parts:
        return ""
    idx = parts.index("kb_corpus")
    if idx + 1 >= len(parts):
        return ""
    category = parts[idx + 1]
    return CATEGORY_LABELS.get(category, category)


def _metadata_kb_name(metadata: Dict[str, Any]) -> str:
    """优先用 chunk metadata 里显式的 `category`（委托写入契约 `/v1/vectors`
    上传时员工选的子库，见 `_HybridSearchEngine.upsert`），查不到已知标签时
    原样返回这个值；没有 `category` 字段时退回从 `source_path` 目录结构反推
    （种子语料摄入路径，见 `_category_label`）。"""
    category = metadata.get("category")
    if category:
        return CATEGORY_LABELS.get(category, category)
    source_path = metadata.get("source_path", metadata.get("source", ""))
    return _category_label(source_path)


class SearchRequest(BaseModel):
    query: str
    top_k: int = 5
    collection: Optional[str] = None
    filters: Dict[str, Any] = Field(default_factory=dict)


class SearchResultItem(BaseModel):
    content: str
    score: float
    source: str = ""
    page: Optional[int] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class SearchResponse(BaseModel):
    results: List[SearchResultItem]


# 第 4.4 节委托写入契约（方案 2："平台代算，企业只存储"）：平台已经切好块、
# 算好稠密向量 + 稀疏统计，这边只管存，不跑任何 embedding 模型——契约细节见
# knowledge-base-tenant-federation.md 第 4.4 节。

class IngestChunkItem(BaseModel):
    chunk_id: str
    text: str
    vector: List[float]
    sparse_stats: Dict[str, Any]
    metadata: Dict[str, Any] = Field(default_factory=dict)


class IngestVectorsRequest(BaseModel):
    doc_id: str
    chunks: List[IngestChunkItem]
    category: Optional[str] = None


class IngestVectorsResponse(BaseModel):
    chunk_count: int


# 第 7 节"检索质量完全依赖企业自己的实现"这条设计决策的直接后果：平台的
# `_execute_remote` 只负责转发、从不重排/从不判断这边结果是否真的相关（见
# query_knowledge_hub.py `_execute_remote` 的说明）——质量控制的责任在这一侧。
# 这里"企业自己的实现"就是这个参考实现本身，所以真实踩过的坑（问一句跟语料
# 毫不相关的话，比如"iPhone16 是否配套充电器"，混合检索依然会凑出几条"矬子里
# 最高"的候选，score 看起来还不低）要在这一层堵住，不能指望平台那边兜底。
# 阈值/理由跟平台本地部门知识库用的是同一套校准（query_knowledge_hub.py 的
# MIN_RELEVANCE_SCORE 旁有完整说明）：cross-encoder 重排后，真正相关的命中
# 稳定在 0.13 以上，完全不相关的问题稳定卡在 0.03 以下，0.1 是两者之间有
# 安全余量的分界线。
MIN_RELEVANCE_SCORE = 0.1


class _HybridSearchEngine:
    """懒加载封装：首次查询时才建 embedding client / vector store / BM25 索引 / reranker。"""

    def __init__(self, data_dir: str, collection: str) -> None:
        self._data_dir = data_dir
        self._collection = collection
        self._hybrid_search = None
        self._reranker = None
        self._vector_store = None
        self._bm25_indexer = None
        self._vector_upserter = None

    def _ensure_ready(self) -> None:
        if self._hybrid_search is not None:
            return

        from src.core.query_engine.dense_retriever import create_dense_retriever
        from src.core.query_engine.hybrid_search import create_hybrid_search
        from src.core.query_engine.query_processor import QueryProcessor
        from src.core.query_engine.sparse_retriever import create_sparse_retriever
        from src.core.query_engine.reranker import create_core_reranker
        from src.ingestion.storage.bm25_indexer import BM25Indexer
        from src.libs.embedding.embedding_factory import EmbeddingFactory
        from src.libs.vector_store.vector_store_factory import VectorStoreFactory

        settings = load_settings()
        embedding_client = EmbeddingFactory.create(settings)
        vector_store = VectorStoreFactory.create(
            settings,
            persist_directory=f"{self._data_dir}/chroma",
            collection_name=self._collection,
        )
        dense_retriever = create_dense_retriever(
            settings=settings, embedding_client=embedding_client, vector_store=vector_store,
        )
        # 跟 `IngestionPipeline` 写入时用的路径保持一致（见上面 TENANT_COLLECTION 的注释）。
        bm25_indexer = BM25Indexer(index_dir=str(resolve_path(f"data/db/bm25/{self._collection}")))
        sparse_retriever = create_sparse_retriever(
            settings=settings, bm25_indexer=bm25_indexer, vector_store=vector_store,
        )
        sparse_retriever.default_collection = self._collection

        self._hybrid_search = create_hybrid_search(
            settings=settings,
            query_processor=QueryProcessor(),
            dense_retriever=dense_retriever,
            sparse_retriever=sparse_retriever,
        )
        self._reranker = create_core_reranker(settings=settings)
        # count/list/clear 这几个管理操作只需要直接碰向量库，不用走完整的
        # hybrid search 链路——留一份引用，见下面 count()/list_chunks()/clear()。
        self._vector_store = vector_store
        self._bm25_indexer = bm25_indexer

    def search(self, query: str, top_k: int) -> List["Any"]:
        self._ensure_ready()
        # 多召回一些候选给 reranker 挑，跟平台本地路径的 initial_top_k 是
        # 同一个思路（query_knowledge_hub.py _search_with）。
        results = self._hybrid_search.search(query=query, top_k=top_k * 2, filters=None, return_details=False)
        results = results if isinstance(results, list) else results.results
        if not results:
            return results

        # 候选只有 1 条时，CoreReranker.rerank() 会走它自己的 len(results)==1
        # 短路分支，原样把这条结果（原始 hybrid 融合分数，量级是 0.01~0.05）
        # 传回来，`used_fallback` 还是 False——下面的 MIN_RELEVANCE_SCORE 过滤
        # 是按 cross-encoder 分数量级校准的，会把这条其实真正相关的结果误杀
        # 成"不相关"。跟平台侧 query_knowledge_hub.py `_apply_rerank` 同一个
        # 坑、同一个修法：候选数量 <= 1 时直接跳过重排/过滤。委托写入契约上线
        # 后，企业知识库刚起步、库里只有一两篇文档时最容易踩中这个边界。
        if len(results) <= 1:
            return results[:top_k]

        if self._reranker is not None and self._reranker.is_enabled:
            try:
                rerank_result = self._reranker.rerank(query=query, results=results, top_k=top_k)
                if not rerank_result.used_fallback:
                    return [r for r in rerank_result.results if r.score >= MIN_RELEVANCE_SCORE]
                results = rerank_result.results
            except Exception:
                pass

        return results[:top_k]

    def upsert(self, doc_id: str, chunks: List[Dict[str, Any]], category: Optional[str] = None) -> int:
        """写入平台代算好的 chunk + 向量 + 稀疏统计（第 4.4 节委托写入契约
        `/v1/vectors` 的落地方法）——这里不做任何 embedding 计算，纯粹是
        存储层的 upsert，复用主系统同一套 `VectorUpserter`/`BM25Indexer`
        （见文件顶部"复用主系统已经验证过的 ChromaStore + HybridSearch"），
        物理上仍然只写自己这份独立的 `TENANT_DATA_DIR`，跟平台、跟别的租户
        互不影响。

        `category`：员工上传时选的子库/分类（6 个固定类目之一，见
        `CATEGORY_LABELS`），原样写进每个 chunk 的 metadata，供 `search()`
        组装 `kb_name` 时直接读取——不需要再像种子语料那样从 `source_path`
        目录结构反推分类。"""
        self._ensure_ready()
        from src.core.types import Chunk
        from src.ingestion.storage.vector_upserter import VectorUpserter

        if self._vector_upserter is None:
            self._vector_upserter = VectorUpserter(
                load_settings(),
                collection_name=self._collection,
                persist_directory=f"{self._data_dir}/chroma",
            )

        chunk_objs = []
        for c in chunks:
            metadata = dict(c.get("metadata") or {})
            if category:
                metadata["category"] = category
            chunk_objs.append(Chunk(id=c["chunk_id"], text=c["text"], metadata=metadata))
        vectors = [c["vector"] for c in chunks]
        vector_ids = self._vector_upserter.upsert(chunk_objs, vectors)

        # BM25 索引要求 term_stats 里的 chunk_id 跟向量库真正生成的 ID 对齐
        # （VectorUpserter 内部会重新生成确定性 ID，不是原样用调用方传来的
        # chunk_id），跟主系统 pipeline.py 阶段 6b 前那一次 zip 对齐是同一个
        # 做法。
        sparse_stats = [dict(c["sparse_stats"]) for c in chunks]
        for stat, vid in zip(sparse_stats, vector_ids):
            stat["chunk_id"] = vid
        self._bm25_indexer.add_documents(sparse_stats, collection=self._collection, doc_id=doc_id)

        # hybrid_search 内部的 retriever 是围绕 `_ensure_ready()` 那一刻的
        # vector_store/bm25_indexer 建的，新写入的内容能否被下一次 search()
        # 看到取决于这两个组件是不是"live"读盘（不是查询时重建整个对象）——
        # 不确定这一点在两边组件实现里是否总是成立，稳妥起见强制下一次
        # search() 重新 `_ensure_ready()`，用一次可忽略的冷启动开销换"刚
        # upsert 完立刻能查到"这个确定性保证。
        self._hybrid_search = None

        return len(vector_ids)

    # ---- 内部 QA 测试用的管理操作（见 /v1/collection/* 端点顶部说明） ----

    def _all_raw(self) -> Dict[str, Any]:
        """管理端点（分类统计/查看/清空）共用的"取全部记录"——数据量小（参考
        语料每个租户就 120 条），不值得为分页/游标搭一套机制，客户端过滤比
        照着 Chroma `where` 精确匹配 metadata.category 简单，且不要求种子语料
        也补一份显式 category 字段（种子语料走 `_category_label` 从
        source_path 反推，见文件顶部说明）。"""
        self._ensure_ready()
        return self._vector_store.collection.get(include=["metadatas", "documents"])

    def list_chunks(self, limit: int, category: Optional[str] = None) -> List[Dict[str, Any]]:
        raw = self._all_raw()
        items = []
        for i, chunk_id in enumerate(raw.get("ids", [])):
            metadata = (raw.get("metadatas") or [{}])[i] or {}
            kb_name = _metadata_kb_name(metadata) or None
            if category is not None and kb_name != category:
                continue
            document = (raw.get("documents") or [""])[i] or ""
            items.append({
                "chunk_id": chunk_id,
                "text": document,
                "source_path": metadata.get("source_path", ""),
                "kb_name": kb_name,
            })
            if len(items) >= limit:
                break
        return items

    def stats_by_category(self) -> List[Dict[str, Any]]:
        """按 kb_name（人力资源与行政/财务与报销制度等 6 个类目）分组统计
        chunk 数——平台知识库测试页按分类列出该企业知识库（而不是笼统一条
        "本企业知识库"），需要用这个替代 `count()` 的整库汇总。没上报/推断出
        分类的 chunk 归到"未分类"，不丢弃，方便测试页发现这类数据。"""
        raw = self._all_raw()
        counts: Dict[str, int] = {}
        for metadata in (raw.get("metadatas") or []):
            kb_name = _metadata_kb_name(metadata or {}) or "未分类"
            counts[kb_name] = counts.get(kb_name, 0) + 1
        return [{"category": k, "chunk_count": v} for k, v in sorted(counts.items())]

    def clear_category(self, category: str) -> int:
        """只清空某一个分类下的 chunk（向量 + 对应 BM25 posting），不动同一个
        collection 里其他分类的内容——测试页现在能单独查看/清空某个类目，
        清空动作的粒度也要跟着收窄到类目级别，不能像 `clear()` 那样把整个
        企业的 6 个类目一锅端。"""
        raw = self._all_raw()
        matching_ids = [
            chunk_id for i, chunk_id in enumerate(raw.get("ids", []))
            if (_metadata_kb_name((raw.get("metadatas") or [{}])[i] or {}) or "未分类") == category
        ]
        if not matching_ids:
            return 0
        self._vector_store.collection.delete(ids=matching_ids)
        # remove_document 按"chunk_id 以 doc_id 为前缀"匹配 posting——这里传
        # 完整的 chunk_id 本身当 doc_id，等价于精确删除这一条 posting，不需要
        # BM25Indexer 另外提供一个按 id 列表精确删除的方法。
        for chunk_id in matching_ids:
            try:
                self._bm25_indexer.remove_document(chunk_id, collection=self._collection)
            except Exception:
                logger.warning(f"[{TENANT_NAME}] BM25 remove_document failed for {chunk_id}", exc_info=True)
        # 底层数据已经就地改了（不是像 clear() 那样整个 collection 被删掉重建），
        # vector_store/bm25_indexer 引用仍然有效，只需要让下一次 search() 重新
        # 走一遍 _ensure_ready 之外的检索链路缓存失效，跟 upsert() 后的处理一致。
        self._hybrid_search = None
        return len(matching_ids)

    def clear(self) -> int:
        """清空这个 collection 的全部向量 + BM25 索引，登记信息（TENANT_COLLECTION
        这个名字本身）不受影响——下次摄入会用 get_or_create 语义重新建一个空的。"""
        import shutil

        self._ensure_ready()
        cleared = self._vector_store.get_collection_stats()["count"]
        self._vector_store.client.delete_collection(self._vector_store.collection_name)
        if self._bm25_indexer.index_dir.exists():
            shutil.rmtree(self._bm25_indexer.index_dir)
        # 强制下一次调用重新初始化——delete_collection 之后原来那个 self._vector_store.collection
        # 引用已经指向一个不存在的 collection，继续用会报错。
        self._hybrid_search = None
        self._reranker = None
        self._vector_store = None
        self._bm25_indexer = None
        self._vector_upserter = None
        return cleared


_engine = _HybridSearchEngine(TENANT_DATA_DIR, TENANT_COLLECTION)

app = FastAPI(title=f"Tenant KB Demo — {TENANT_NAME}")


@app.get("/healthz")
def healthz() -> Dict[str, str]:
    return {"status": "ok", "tenant": TENANT_NAME}


@app.post("/v1/search", response_model=SearchResponse)
def search(
    request: SearchRequest,
    authorization: str = Header(default=""),
    x_organization_id: str = Header(default="", alias="X-Organization-Id"),
) -> SearchResponse:
    """第 4 节统一 HTTP 契约的参考实现：鉴权 + hybrid search + 组装响应。"""
    _check_token(authorization)
    # 双重校验：请求头里的组织必须跟 token 绑定的组织一致，token 泄露到别的租户时
    # 多一层防护（见第 2 节鉴权设计）。
    if x_organization_id != TENANT_ORG_ID:
        raise HTTPException(status_code=403, detail="X-Organization-Id does not match this connector's organization")

    t0 = time.monotonic()
    raw_results = _engine.search(request.query, request.top_k)
    elapsed_ms = (time.monotonic() - t0) * 1000.0
    logger.info(
        f"[{TENANT_NAME}] query={request.query!r} top_k={request.top_k} "
        f"-> {len(raw_results)} results in {elapsed_ms:.0f}ms"
    )

    items = []
    for r in raw_results:
        source_path = r.metadata.get("source_path", r.metadata.get("source", ""))
        result_metadata: Dict[str, Any] = {"title": r.metadata.get("title", ""), "tenant": TENANT_NAME}
        kb_name = _metadata_kb_name(r.metadata)
        if kb_name:
            result_metadata["kb_name"] = kb_name
        items.append(SearchResultItem(
            content=r.text or "",
            score=float(r.score),
            source=source_path,
            page=r.metadata.get("page") or r.metadata.get("page_num"),
            metadata=result_metadata,
        ))
    return SearchResponse(results=items)


@app.post("/v1/vectors", response_model=IngestVectorsResponse)
def ingest_vectors(
    request: IngestVectorsRequest,
    authorization: str = Header(default=""),
    x_organization_id: str = Header(default="", alias="X-Organization-Id"),
) -> IngestVectorsResponse:
    """第 4.4 节委托写入契约的参考实现：鉴权（跟 /v1/search 同一套）+ 存储
    平台已经算好的 chunk/向量/稀疏统计，不做任何 embedding 计算。"""
    _check_token(authorization)
    if x_organization_id != TENANT_ORG_ID:
        raise HTTPException(status_code=403, detail="X-Organization-Id does not match this connector's organization")

    if not request.chunks:
        return IngestVectorsResponse(chunk_count=0)

    chunk_dicts = [c.model_dump() for c in request.chunks]
    t0 = time.monotonic()
    count = _engine.upsert(request.doc_id, chunk_dicts, category=request.category)
    elapsed_ms = (time.monotonic() - t0) * 1000.0
    logger.info(f"[{TENANT_NAME}] ingested doc_id={request.doc_id} chunks={count} in {elapsed_ms:.0f}ms")
    return IngestVectorsResponse(chunk_count=count)


def _check_token(authorization: str) -> None:
    token = authorization.removeprefix("Bearer ").strip()
    if not token or token != TENANT_TOKEN:
        raise HTTPException(status_code=401, detail="invalid or missing bearer token")


# ==================== 内部 QA 测试用管理端点（非契约的一部分） ====================
# 第 4 节统一 HTTP 契约只定义了 /v1/search 这一个查询端点——这三个是额外加的，
# 只给平台后台"知识库测试页面"（仅 super_admin + RAGENT_DEBUG=true）用，方便
# 反复测试"导入知识库 -> 查询知识库"这条链路时不用手动清数据。真实客户接入的
# 知识库微服务不需要实现这三个端点，平台正常的检索/委托流程也完全不依赖它们。

@app.get("/v1/collection/stats")
def collection_stats(authorization: str = Header(default="")) -> Dict[str, Any]:
    """`categories` 是平台知识库测试页按分类列出该企业知识库的数据源——一个
    委托企业内部实际上是 6 个类目共享一个物理 collection（見文件顶部说明），
    测试页原来只能看到"本企业知识库"这一整条，看不出某个具体类目有没有数据、
    也没法单独清空某个类目，加这个分组统计就是为了补上这一层粒度。
    `chunk_count` 保留（=各分类之和），不依赖它的调用方不受影响。"""
    _check_token(authorization)
    categories = _engine.stats_by_category()
    return {
        "collection": TENANT_COLLECTION,
        "chunk_count": sum(c["chunk_count"] for c in categories),
        "categories": categories,
    }


@app.get("/v1/collection/chunks")
def collection_chunks(
    limit: int = 50, category: str = "", authorization: str = Header(default=""),
) -> Dict[str, Any]:
    _check_token(authorization)
    return {"chunks": _engine.list_chunks(limit, category=category or None)}


@app.delete("/v1/collection")
def clear_collection(category: str = "", authorization: str = Header(default="")) -> Dict[str, Any]:
    """不传 `category` 时保留原有行为——清空整个租户 collection；传了就只清
    这一个分类下的内容，其他分类不受影响（见 `_HybridSearchEngine.clear_category`）。"""
    _check_token(authorization)
    if category:
        cleared = _engine.clear_category(category)
        logger.info(f"[{TENANT_NAME}] category '{category}' cleared, {cleared} chunks removed")
    else:
        cleared = _engine.clear()
        logger.info(f"[{TENANT_NAME}] collection cleared, {cleared} chunks removed")
    return {"cleared_chunks": cleared}
