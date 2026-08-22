"""委托模式知识库的"计算侧"——只切块 + 编码，不落任何本地存储。

方案 2（"平台代算，企业只存储"）：员工上传文档时，平台复用自己已有的切块/
embedding 组件算出 chunk + 向量 + 稀疏统计，通过委托写入契约（见
knowledge-base-tenant-federation.md 第 4.4 节）推给企业自己的知识库微服务，由
对方决定怎么存（ChromaDB、其他向量库都行）——企业那边不需要跑任何 embedding
模型，只需要实现一个接收 {text, vector, metadata} 的 upsert 接口。

跟 `IngestionPipeline`（本地检索模式用的那条完整流水线，见 pipeline.py）的
区别：不复用 `IngestionPipeline.run()`——那个方法从构造起就跟本地存储
（VectorUpserter/BM25Indexer/ImageStorage/文档摘要 collection）深度耦合，
把它拆成"算完不存"和"算完就存"两条路径的改动面/风险都远大于直接调用它内部
真正复用的那几个无状态组件（loader/chunker/dense+sparse 编码器）。代价是
这里跳过了 chunk 精炼、元数据增强、图片描述、片段级去重、文档级摘要这几个
本地模式独有的增强步骤——核心的"切块 + 向量化"逻辑跟本地模式完全一致，
只是没有这几个锦上添花的环节，属于这个委托写入通道的已知范围，不是遗漏。
"""

from __future__ import annotations

from typing import Any, Dict, List

from src.core.settings import Settings
from src.ingestion.chunking.document_chunker import DocumentChunker
from src.ingestion.embedding.batch_processor import BatchProcessor
from src.ingestion.embedding.dense_encoder import DenseEncoder
from src.ingestion.embedding.sparse_encoder import SparseEncoder
from src.libs.embedding.embedding_factory import EmbeddingFactory
from src.libs.loader.universal_loader import UniversalLoader


def compute_chunks_for_delegation(settings: Settings, file_path: str) -> Dict[str, Any]:
    """加载 + 切块 + 编码一份文档，返回可直接推给委托契约 `/v1/vectors` 的
    payload：`{"doc_id", "chunks": [{"chunk_id", "text", "metadata", "vector",
    "sparse_stats"}, ...]}`。不触碰任何本地向量库/BM25 索引/图片存储。

    `extract_images=False`：委托写入通道目前只处理正文文本，图片摘要/图片
    索引留在本地检索模式那条完整流水线里，不在这个范围内。
    """
    loader = UniversalLoader(settings=settings, extract_images=False)
    chunker = DocumentChunker(settings)

    document = loader.load(file_path)
    chunks = chunker.split_document(document)
    if not chunks:
        return {"doc_id": document.id, "chunks": []}

    embedding = EmbeddingFactory.create(settings)
    batch_size = settings.ingestion.batch_size if settings.ingestion else 100
    dense_encoder = DenseEncoder(embedding, batch_size=batch_size)
    sparse_encoder = SparseEncoder()
    batch_processor = BatchProcessor(
        dense_encoder=dense_encoder, sparse_encoder=sparse_encoder, batch_size=batch_size,
    )
    batch_result = batch_processor.process(chunks)

    return {
        "doc_id": document.id,
        "chunks": [
            {
                "chunk_id": c.id,
                "text": c.text,
                "metadata": c.metadata,
                "vector": batch_result.dense_vectors[i],
                "sparse_stats": batch_result.sparse_stats[i],
            }
            for i, c in enumerate(chunks)
        ],
    }
