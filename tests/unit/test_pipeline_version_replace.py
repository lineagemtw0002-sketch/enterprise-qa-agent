"""`IngestionPipeline._replace_old_versions` —— CLAUDE.md §4 P0 第 1 条
「文档更新后旧版本片段永久残留」的修复。

不跑真实摄入流水线（那需要真实 embedding/LLM，见
`tests/integration/test_ingestion_pipeline.py`），直接对这一个方法做隔离
测试：给一个鸭子类型的假 pipeline 对象（跟 `test_pipeline_progress.py`
同一个模式）打上必要的桩，验证"找到旧版本就删、删不掉/查不到都不炸掉
本次摄入"这条非致命清理的语义。

判别力核心：`test_calls_delete_document_with_correct_args` 直接断言
`DocumentManager.delete_document` 被调用时传的 `source_hash` 是旧版本的
哈希、不是这次刚成功的新哈希——如果实现反过来删错了哈希（把新版本删掉、
留着旧版本），这条测试会失败但 `test_no_old_versions_is_a_noop` 这类正向
测试不会。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.ingestion.document_manager import DeleteResult
from src.ingestion.pipeline import IngestionPipeline


def _make_fake_pipeline() -> object:
    """跟 `test_pipeline_progress.py::_make_fake_pipeline` 同一个模式：
    鸭子类型假件，只桩 `_replace_old_versions` 用得到的属性。"""

    class FP:
        collection = "kb1"

    fp = FP()
    fp.integrity_checker = MagicMock()
    fp.vector_upserter = SimpleNamespace(vector_store=MagicMock())
    fp.bm25_indexer = MagicMock()
    fp.image_storage = MagicMock()
    return fp


def test_no_old_versions_is_a_noop():
    fp = _make_fake_pipeline()
    fp.integrity_checker.find_other_versions.return_value = []
    stages: dict = {}

    with patch("src.ingestion.pipeline.DocumentManager") as MockManager:
        IngestionPipeline._replace_old_versions(fp, "hash_new", "report.pdf", stages)

    MockManager.assert_not_called()
    assert stages["version_replace"] == {"old_versions_removed": 0}


def test_calls_delete_document_with_correct_args():
    """核心判别式：删的必须是旧哈希，不是刚摄入成功的新哈希。"""
    fp = _make_fake_pipeline()
    fp.integrity_checker.find_other_versions.return_value = [
        {"file_hash": "hash_old", "file_path": "/uploads/aaa_report.pdf"},
    ]
    stages: dict = {}

    with patch("src.ingestion.pipeline.DocumentManager") as MockManager:
        instance = MockManager.return_value
        instance.delete_document.return_value = DeleteResult(
            success=True, chunks_deleted=3, bm25_removed=True,
        )
        IngestionPipeline._replace_old_versions(fp, "hash_new", "report.pdf", stages)

    instance.delete_document.assert_called_once_with(
        "/uploads/aaa_report.pdf", collection="kb1", source_hash="hash_old"
    )
    assert stages["version_replace"] == {
        "old_versions_found": 1, "old_versions_removed": 1, "errors": [],
    }


def test_multiple_old_versions_all_deleted():
    fp = _make_fake_pipeline()
    fp.integrity_checker.find_other_versions.return_value = [
        {"file_hash": "hash_v1", "file_path": "/uploads/aaa_report.pdf"},
        {"file_hash": "hash_v2", "file_path": "/uploads/bbb_report.pdf"},
    ]
    stages: dict = {}

    with patch("src.ingestion.pipeline.DocumentManager") as MockManager:
        instance = MockManager.return_value
        instance.delete_document.return_value = DeleteResult(success=True)
        IngestionPipeline._replace_old_versions(fp, "hash_new", "report.pdf", stages)

    assert instance.delete_document.call_count == 2
    deleted_hashes = {c.kwargs["source_hash"] for c in instance.delete_document.call_args_list}
    assert deleted_hashes == {"hash_v1", "hash_v2"}
    assert stages["version_replace"]["old_versions_removed"] == 2


def test_lookup_failure_is_non_fatal():
    """查旧版本本身就失败（比如 SQLite 抖动）不该让这次已经成功的新版本
    摄入被判失败——跟 `_rollback_storage` 的"非致命清理"是同一条原则。"""
    fp = _make_fake_pipeline()
    fp.integrity_checker.find_other_versions.side_effect = RuntimeError("db locked")
    stages: dict = {}

    with patch("src.ingestion.pipeline.DocumentManager") as MockManager:
        IngestionPipeline._replace_old_versions(fp, "hash_new", "report.pdf", stages)

    MockManager.assert_not_called()
    assert "error" in stages["version_replace"]


def test_partial_delete_failure_does_not_stop_other_deletions():
    """一个旧版本删失败，不该连累其它旧版本也不去删。"""
    fp = _make_fake_pipeline()
    fp.integrity_checker.find_other_versions.return_value = [
        {"file_hash": "hash_v1", "file_path": "/uploads/aaa_report.pdf"},
        {"file_hash": "hash_v2", "file_path": "/uploads/bbb_report.pdf"},
    ]
    stages: dict = {}

    with patch("src.ingestion.pipeline.DocumentManager") as MockManager:
        instance = MockManager.return_value
        instance.delete_document.side_effect = [
            DeleteResult(success=False, errors=["ChromaDB delete failed: boom"]),
            DeleteResult(success=True),
        ]
        IngestionPipeline._replace_old_versions(fp, "hash_new", "report.pdf", stages)

    assert instance.delete_document.call_count == 2
    assert stages["version_replace"]["old_versions_found"] == 2
    assert stages["version_replace"]["old_versions_removed"] == 1
    assert len(stages["version_replace"]["errors"]) == 1
