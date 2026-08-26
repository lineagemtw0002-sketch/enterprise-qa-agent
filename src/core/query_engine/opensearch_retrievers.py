"""OpenSearch 版的 sparse / dense retriever —— 迁移设计阶段 3（切读）。

**这是读路径的替换实现，不是包装层。**

设计文档 §1 的决策是"不做适配层把 OpenSearch 包装成现有抽象的替身"。
这里仍然实现 `retrieve()` 是因为 **`HybridSearch` 是复用的那一层**：
按已定的收窄范围（§4），本次只换检索存储，RRF 融合与重排维持现状。
所以接缝就落在 retriever 上——对齐的是**返回契约**（`RetrievalResult`），
不是把 OpenSearch 塞进 `BaseVectorStore` 那种会掩盖它原生能力的抽象。

## 与旧实现的行为差异（已实测，不是推断）

| | 旧 | 新 |
|---|---|---|
| sparse 命中集合 | — | 黄金集 recall@10 **83.0% vs 83.0%**，未回退 |
| dense 命中集合 | — | 与 Chroma 平均重叠 **88%** |
| 摘要层 | — | 平均重叠 **91%** |

dense 侧重叠不是 100% 属正常：Chroma 与 OpenSearch 是不同的 ANN 实现，
两边都是**近似**最近邻，本来就不保证返回同一批。

## 一个不能省的细节：sparse 侧的查询必须先分词

索引侧 `content` 用的是 `whitespace` 分析器，写入的是 jieba 分好的文本
（见 `opensearch_store` 的 mapping 注释）。查询串不先分词，整句会被当成
一个 token，什么都匹配不上——而且这个错误在小语料上不一定暴露。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from src.core.types import RetrievalResult

logger = logging.getLogger(__name__)


class OpenSearchSparseRetriever:
    """BM25 检索，走 OpenSearch。"""

    def __init__(
        self,
        collection: str,
        store: Any,
        default_top_k: int = 10,
        org_id: Optional[str] = None,
        owner_user_id: Optional[str] = None,
    ):
        self.collection = collection
        self.default_collection = collection
        self._store = store
        self.default_top_k = default_top_k
        # 仅 conv_* 对话私有库需要：它在 OpenSearch 侧是"每企业一个 index +
        # 按所有者过滤"，这两个值决定查哪个 index、以及能看到谁的文档。
        self.org_id = org_id
        self.owner_user_id = owner_user_id

    def retrieve(
        self,
        keywords: List[str],
        top_k: Optional[int] = None,
        collection: Optional[str] = None,
        trace: Optional[Any] = None,
    ) -> List[RetrievalResult]:
        """签名与 `SparseRetriever.retrieve` 对齐 —— 理由见
        `OpenSearchDenseRetriever.retrieve` 的说明（静默降级）。"""
        if not keywords:
            return []
        col = collection or self.default_collection
        k = top_k or self.default_top_k
        if col.startswith("conv_"):
            # 调用方（_build_hybrid_search_for）在缺身份时已经回退旧链路了，
            # 走到这里两个值必然都有。断言而不是静默降级 —— 少了过滤条件
            # 就是越权返回，宁可炸也不能悄悄查到别人的文档。
            assert self.org_id and self.owner_user_id, (
                "conv_ 检索缺 org_id/owner_user_id —— 这会导致越权"
            )
            hits = self._store.search_conv(
                self.org_id,
                col[len("conv_"):],
                self.owner_user_id,
                "",
                top_k=k,
                query_tokens=keywords,
            )
        else:
            hits = self._store.search_kb(
                col,
                "",
                top_k=k,
                # keywords 已经是 QueryProcessor 分好的词条，不要再过一遍分词器
                query_tokens=keywords,
            )
        return _to_results(hits)


class OpenSearchDenseRetriever:
    """向量检索，走 OpenSearch 的 kNN。"""

    def __init__(
        self,
        collection: str,
        store: Any,
        embedding_client: Any,
        default_top_k: int = 10,
        org_id: Optional[str] = None,
        owner_user_id: Optional[str] = None,
    ):
        self.collection = collection
        self.default_collection = collection
        self._store = store
        self._embedding = embedding_client
        self.default_top_k = default_top_k
        self.org_id = org_id
        self.owner_user_id = owner_user_id

    def retrieve(
        self,
        query: str,
        top_k: Optional[int] = None,
        filters: Optional[Dict[str, Any]] = None,
        trace: Optional[Any] = None,
        query_vector: Optional[List[float]] = None,
    ) -> List[RetrievalResult]:
        """⚠️ 签名必须与 `DenseRetriever.retrieve` **逐字对齐**，包括 `filters`。

        少一个参数不会报错到用户面前 —— `HybridSearch._dense_search` 会捕获
        TypeError 并打一行 "Dense retrieval failed, using sparse only"，
        然后**静默退化成只有稀疏检索**。检索照常返回结果，只是少了一半召回，
        谁也不会发现。第一版就漏了 `filters`，是端到端对照时结果对不上才查出来的。

        `filters` 也**不能只接受不实现** —— 那样元数据过滤会静默失效，
        同样是"能跑但结果错"。

        `query_vector` 同理：**接受了就要真的用**。只接受不实现不会算错结果，
        但会白白多打一次 embedding 往返（Ollama 默认 NUM_PARALLEL=1 下是串行的，
        见 CLAUDE.md §4 第 3 条），而调用方以为自己已经省掉了——
        同样是"能跑但你以为的和实际发生的不是一回事"。
        """
        if not query or not query.strip():
            return []
        col = self.default_collection
        vector = query_vector if query_vector is not None else self._embedding.embed([query])[0]
        k = top_k or self.default_top_k
        if col.startswith("conv_"):
            assert self.org_id and self.owner_user_id, (
                "conv_ 检索缺 org_id/owner_user_id —— 这会导致越权"
            )
            hits = self._store.knn_conv(
                self.org_id, col[len("conv_"):], self.owner_user_id, vector, top_k=k
            )
        else:
            hits = self._store.knn_kb(col, vector, top_k=k, filters=filters)
        return _to_results(hits)


def _to_results(hits: List[Dict[str, Any]]) -> List[RetrievalResult]:
    """统一转成 `RetrievalResult`。

    ⚠️ 跳过 `chunk_id` 为空的命中而不是让它抛异常：`RetrievalResult.__post_init__`
    对空 chunk_id 会 raise，而检索路径上一条坏数据不该让整次查询失败。
    但要留日志——静默丢结果是最难查的那种问题。
    """
    out: List[RetrievalResult] = []
    for h in hits:
        cid = h.get("chunk_id")
        if not cid:
            logger.warning(
                "OpenSearch 命中缺少 chunk_id，已跳过 source_path=%s",
                h.get("source_path"),
                extra={"event": "opensearch.hit_missing_chunk_id"},
            )
            continue
        out.append(
            RetrievalResult(
                chunk_id=cid,
                score=float(h.get("score", 0.0)),
                text=h.get("content", ""),
                # ⚠️ 用 store 给的完整 metadata，**不要在这里另挑字段**。
                # HybridSearch 的 metadata_filter_post 会在融合后按 metadata
                # 再过滤一次；少一个字段（比如层次化收窄用的 source_ref）
                # 就会让结果被整片丢掉，而检索层看起来完全正常。
                metadata=h.get("metadata") or {"source_path": h.get("source_path", "")},
            )
        )
    return out
