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


class _HybridSearchEngine:
    """懒加载封装：首次查询时才建 embedding client / vector store / BM25 索引。"""

    def __init__(self, data_dir: str, collection: str) -> None:
        self._data_dir = data_dir
        self._collection = collection
        self._hybrid_search = None

    def _ensure_ready(self) -> None:
        if self._hybrid_search is not None:
            return

        from src.core.query_engine.dense_retriever import create_dense_retriever
        from src.core.query_engine.hybrid_search import create_hybrid_search
        from src.core.query_engine.query_processor import QueryProcessor
        from src.core.query_engine.sparse_retriever import create_sparse_retriever
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

    def search(self, query: str, top_k: int) -> List["Any"]:
        self._ensure_ready()
        results = self._hybrid_search.search(query=query, top_k=top_k, filters=None, return_details=False)
        return results if isinstance(results, list) else results.results


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
    token = authorization.removeprefix("Bearer ").strip()
    if not token or token != TENANT_TOKEN:
        raise HTTPException(status_code=401, detail="invalid or missing bearer token")
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

    items = [
        SearchResultItem(
            content=r.text or "",
            score=float(r.score),
            source=r.metadata.get("source_path", r.metadata.get("source", "")),
            page=r.metadata.get("page") or r.metadata.get("page_num"),
            metadata={"title": r.metadata.get("title", ""), "tenant": TENANT_NAME},
        )
        for r in raw_results
    ]
    return SearchResponse(results=items)
