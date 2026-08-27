"""`IngestionPipeline.run()` 必须用 `file_hash`（完整 64 位内容哈希）作为
BM25 `add_documents(doc_id=...)` 的值，不能用 `document.id`。

背景（2026-08-27 真机验证 P0"文档更新后旧版本片段永久残留"修复时发现）：
`UniversalLoader.load()` 里 `document.id = f"doc_{sha256[:16]}"`——只截了
16 位、还带 `doc_` 前缀，是一个跟 `file_hash`/`chunk.metadata["doc_hash"]`
完全不同的标识。Chroma chunk 元数据、图片索引的 `doc_hash` 全部用完整
`file_hash`，唯独 BM25 这一路错传了 `document.id`，导致 `chunk_doc_hash`
映射里存的值格式不对，任何按完整哈希发起的 BM25 删除（管理员删文档、
版本替换）永远找不到匹配，`remove_document` 恒为 no-op 却不报错。

判别力：`test_add_documents_receives_full_file_hash_not_short_document_id`
直接断言 `doc_id` 参数值等于 `file_hash`（64 位）而不是 `document.id`
（`doc_` 前缀 + 16 位）——两者格式一眼可辨，断言写反的实现会在这条测试上
明确失败，不会因为两个值恰好相等而误判通过。
"""

from __future__ import annotations

from src.ingestion.pipeline import IngestionPipeline
from tests.unit.test_pipeline_progress import _make_fake_pipeline


def test_add_documents_receives_full_file_hash_not_short_document_id():
    fp = _make_fake_pipeline()
    # `_make_fake_pipeline` 里的 loader 返回的 Document 没有显式设置 `.id`，
    # `Document` 是否要求这个字段依赖具体实现——这里不依赖假件默认值，
    # 直接读真实调用后 mock 记录的参数来做判别，跟假件的 `.id` 具体是什么
    # 无关，只要它和 `hash123`（compute_sha256 的桩返回值）不同即可验证。
    fp._replace_old_versions = lambda *a, **k: None

    IngestionPipeline.run(fp, "test.pdf")

    assert fp.bm25_indexer.add_documents.called
    call_kwargs = fp.bm25_indexer.add_documents.call_args.kwargs
    assert call_kwargs["doc_id"] == "hash123"
    # 短前缀形式一定不会被当成完整哈希——这条断言防止将来有人把
    # `file_hash` 改回 `document.id` 却因为两者形似而没被发现。
    assert not call_kwargs["doc_id"].startswith("doc_")
