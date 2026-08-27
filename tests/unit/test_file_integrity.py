"""Unit tests for file integrity checker module.

Tests cover:
- SHA256 hash computation consistency
- Skip logic (should_skip returns True after mark_success)
- Database creation and persistence
- Concurrent write support (SQLite WAL mode)
- Error handling for invalid files
- Idempotent operations
"""

import hashlib
import sqlite3
import tempfile
from pathlib import Path

import pytest

from src.libs.loader.file_integrity import (
    FileIntegrityChecker,
    SQLiteIntegrityChecker,
)


@pytest.fixture
def temp_db():
    """Create a temporary database for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        yield str(db_path)


@pytest.fixture
def temp_file():
    """Create a temporary test file."""
    with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".txt") as f:
        f.write("Test content for hashing")
        temp_path = f.name
    
    yield temp_path
    
    # Cleanup
    Path(temp_path).unlink(missing_ok=True)


@pytest.fixture
def checker(temp_db):
    """Create a file integrity checker instance."""
    return SQLiteIntegrityChecker(db_path=temp_db)


class TestSQLiteIntegrityChecker:
    """Test suite for SQLiteIntegrityChecker."""
    
    def test_init_creates_database(self, temp_db):
        """Test that initialization creates database file."""
        checker = SQLiteIntegrityChecker(db_path=temp_db)
        
        assert Path(temp_db).exists()
        assert Path(temp_db).is_file()
    
    def test_init_creates_parent_directories(self):
        """Test that initialization creates parent directories."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "subdir" / "nested" / "test.db"
            checker = SQLiteIntegrityChecker(db_path=str(db_path))
            
            assert db_path.exists()
            assert db_path.parent.exists()
    
    def test_database_schema_created(self, temp_db, checker):
        """Test that database schema is properly initialized."""
        conn = sqlite3.connect(temp_db)
        try:
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='ingestion_history'"
            )
            assert cursor.fetchone() is not None
            
            # Check WAL mode is enabled
            cursor = conn.execute("PRAGMA journal_mode")
            mode = cursor.fetchone()[0]
            assert mode.lower() == "wal"
        finally:
            conn.close()
    
    def test_compute_sha256_consistency(self, checker, temp_file):
        """Test that computing hash twice gives same result."""
        hash1 = checker.compute_sha256(temp_file)
        hash2 = checker.compute_sha256(temp_file)
        
        assert hash1 == hash2
        assert len(hash1) == 64  # SHA256 produces 64 hex characters
        assert isinstance(hash1, str)
    
    def test_compute_sha256_matches_hashlib(self, checker, temp_file):
        """Test that computed hash matches direct hashlib computation."""
        # Compute using checker
        checker_hash = checker.compute_sha256(temp_file)
        
        # Compute using hashlib directly
        with open(temp_file, "rb") as f:
            content = f.read()
            expected_hash = hashlib.sha256(content).hexdigest()
        
        assert checker_hash == expected_hash
    
    def test_compute_sha256_file_not_found(self, checker):
        """Test that computing hash of non-existent file raises error."""
        with pytest.raises(FileNotFoundError, match="File not found"):
            checker.compute_sha256("/nonexistent/file.txt")
    
    def test_compute_sha256_directory_raises_error(self, checker, temp_db):
        """Test that computing hash of directory raises error."""
        dir_path = Path(temp_db).parent
        
        with pytest.raises(IOError, match="Path is not a file"):
            checker.compute_sha256(str(dir_path))
    
    def test_should_skip_new_file(self, checker):
        """Test that new file hash returns False for should_skip."""
        fake_hash = "a" * 64
        assert checker.should_skip(fake_hash) is False
    
    def test_mark_success_and_should_skip(self, checker, temp_file):
        """Test that marking success causes should_skip to return True."""
        file_hash = checker.compute_sha256(temp_file)
        
        # Initially should not skip
        assert checker.should_skip(file_hash) is False
        
        # Mark success
        checker.mark_success(file_hash, temp_file)
        
        # Now should skip
        assert checker.should_skip(file_hash) is True
    
    def test_mark_success_with_collection(self, checker, temp_file):
        """Test marking success with collection name."""
        file_hash = checker.compute_sha256(temp_file)
        collection = "test_collection"
        
        checker.mark_success(file_hash, temp_file, collection=collection)
        
        # Verify collection is stored
        conn = sqlite3.connect(checker.db_path)
        try:
            cursor = conn.execute(
                "SELECT collection FROM ingestion_history WHERE file_hash = ?",
                (file_hash,)
            )
            result = cursor.fetchone()
            assert result is not None
            assert result[0] == collection
        finally:
            conn.close()
    
    def test_mark_success_idempotent(self, checker, temp_file):
        """Test that marking success multiple times is safe."""
        file_hash = checker.compute_sha256(temp_file)
        
        # Mark success three times
        checker.mark_success(file_hash, temp_file)
        checker.mark_success(file_hash, temp_file)
        checker.mark_success(file_hash, temp_file)
        
        # Should still skip
        assert checker.should_skip(file_hash) is True
        
        # Verify only one row exists
        conn = sqlite3.connect(checker.db_path)
        try:
            cursor = conn.execute(
                "SELECT COUNT(*) FROM ingestion_history WHERE file_hash = ?",
                (file_hash,)
            )
            count = cursor.fetchone()[0]
            assert count == 1
        finally:
            conn.close()
    
    def test_mark_failed_does_not_skip(self, checker, temp_file):
        """Test that marking as failed does not cause skip."""
        file_hash = checker.compute_sha256(temp_file)
        
        checker.mark_failed(file_hash, temp_file, "Test error")
        
        # Should NOT skip failed files (allow retry)
        assert checker.should_skip(file_hash) is False
    
    def test_mark_failed_stores_error_message(self, checker, temp_file):
        """Test that error message is stored for failed files."""
        file_hash = checker.compute_sha256(temp_file)
        error_msg = "Test error message"
        
        checker.mark_failed(file_hash, temp_file, error_msg)
        
        # Verify error message is stored
        conn = sqlite3.connect(checker.db_path)
        try:
            cursor = conn.execute(
                "SELECT error_msg, status FROM ingestion_history WHERE file_hash = ?",
                (file_hash,)
            )
            result = cursor.fetchone()
            assert result is not None
            assert result[0] == error_msg
            assert result[1] == "failed"
        finally:
            conn.close()
    
    def test_mark_success_after_failure_clears_error(self, checker, temp_file):
        """Test that marking success after failure clears error message."""
        file_hash = checker.compute_sha256(temp_file)
        
        # First mark as failed
        checker.mark_failed(file_hash, temp_file, "Initial error")
        assert checker.should_skip(file_hash) is False
        
        # Then mark as success
        checker.mark_success(file_hash, temp_file)
        assert checker.should_skip(file_hash) is True
        
        # Verify error is cleared and status is success
        conn = sqlite3.connect(checker.db_path)
        try:
            cursor = conn.execute(
                "SELECT error_msg, status FROM ingestion_history WHERE file_hash = ?",
                (file_hash,)
            )
            result = cursor.fetchone()
            assert result is not None
            assert result[0] is None  # error_msg should be NULL
            assert result[1] == "success"
        finally:
            conn.close()
    
    def test_timestamps_are_recorded(self, checker, temp_file):
        """Test that processed_at and updated_at timestamps are recorded."""
        file_hash = checker.compute_sha256(temp_file)
        
        checker.mark_success(file_hash, temp_file)
        
        conn = sqlite3.connect(checker.db_path)
        try:
            cursor = conn.execute(
                "SELECT processed_at, updated_at FROM ingestion_history WHERE file_hash = ?",
                (file_hash,)
            )
            result = cursor.fetchone()
            assert result is not None
            assert result[0] is not None  # processed_at
            assert result[1] is not None  # updated_at
        finally:
            conn.close()
    
    def test_multiple_files_independent(self, checker):
        """Test that multiple files can be tracked independently."""
        # Create multiple temp files
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as f1:
            f1.write("Content 1")
            file1 = f1.name
        
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as f2:
            f2.write("Content 2")
            file2 = f2.name
        
        try:
            hash1 = checker.compute_sha256(file1)
            hash2 = checker.compute_sha256(file2)
            
            # Hashes should be different
            assert hash1 != hash2
            
            # Mark only first file as success
            checker.mark_success(hash1, file1)
            
            # Check skip status
            assert checker.should_skip(hash1) is True
            assert checker.should_skip(hash2) is False
        finally:
            Path(file1).unlink(missing_ok=True)
            Path(file2).unlink(missing_ok=True)
    
    def test_compute_sha256_large_file(self, checker):
        """Test that large files are handled correctly (chunked reading)."""
        with tempfile.NamedTemporaryFile(mode="wb", delete=False) as f:
            # Write 1MB of data
            data = b"x" * (1024 * 1024)
            f.write(data)
            large_file = f.name
        
        try:
            # Should not raise memory error
            file_hash = checker.compute_sha256(large_file)
            
            # Verify hash
            expected_hash = hashlib.sha256(data).hexdigest()
            assert file_hash == expected_hash
        finally:
            Path(large_file).unlink(missing_ok=True)
    
    def test_database_persists_across_instances(self, temp_db, temp_file):
        """Test that data persists when creating new checker instances."""
        # Create first checker and mark file
        checker1 = SQLiteIntegrityChecker(db_path=temp_db)
        file_hash = checker1.compute_sha256(temp_file)
        checker1.mark_success(file_hash, temp_file)
        
        # Create new checker instance
        checker2 = SQLiteIntegrityChecker(db_path=temp_db)
        
        # Should still skip
        assert checker2.should_skip(file_hash) is True
    
    def test_abstract_base_class_cannot_instantiate(self):
        """Test that FileIntegrityChecker abstract class cannot be instantiated."""
        with pytest.raises(TypeError):
            FileIntegrityChecker()
    
    def test_concurrent_writes_supported(self, checker, temp_file):
        """Test that WAL mode allows concurrent operations (basic check)."""
        file_hash = checker.compute_sha256(temp_file)
        
        # Multiple operations in sequence should work
        checker.mark_success(file_hash, temp_file)
        result1 = checker.should_skip(file_hash)
        checker.mark_success(file_hash, temp_file, collection="test")
        result2 = checker.should_skip(file_hash)
        
        assert result1 is True
        assert result2 is True


class TestHashConsistency:
    """Tests for hash computation consistency."""
    
    def test_empty_file_hash(self, checker):
        """Test hashing empty file."""
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as f:
            empty_file = f.name
        
        try:
            file_hash = checker.compute_sha256(empty_file)
            
            # Empty file should have consistent hash
            expected = hashlib.sha256(b"").hexdigest()
            assert file_hash == expected
        finally:
            Path(empty_file).unlink(missing_ok=True)
    
    def test_same_content_different_files(self, checker):
        """Test that files with same content produce same hash."""
        content = "Identical content"
        
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as f1:
            f1.write(content)
            file1 = f1.name
        
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as f2:
            f2.write(content)
            file2 = f2.name
        
        try:
            hash1 = checker.compute_sha256(file1)
            hash2 = checker.compute_sha256(file2)
            
            assert hash1 == hash2
        finally:
            Path(file1).unlink(missing_ok=True)
            Path(file2).unlink(missing_ok=True)
    
    def test_different_content_different_hash(self, checker):
        """Test that different content produces different hashes."""
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as f1:
            f1.write("Content A")
            file1 = f1.name
        
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as f2:
            f2.write("Content B")
            file2 = f2.name
        
        try:
            hash1 = checker.compute_sha256(file1)
            hash2 = checker.compute_sha256(file2)
            
            assert hash1 != hash2
        finally:
            Path(file1).unlink(missing_ok=True)
            Path(file2).unlink(missing_ok=True)


class TestErrorHandling:
    """Tests for error handling scenarios."""
    
    def test_should_skip_with_corrupted_db_raises_error(self, temp_db):
        """Test behavior when database is corrupted."""
        # Create valid checker first
        checker = SQLiteIntegrityChecker(db_path=temp_db)
        checker.close()  # Explicitly close connection
        
        # Corrupt the database by writing invalid data
        with open(temp_db, "wb") as f:
            f.write(b"This is not a valid SQLite database")
        
        # Create a new checker with corrupted db - this will fail on init
        with pytest.raises(sqlite3.DatabaseError):
            corrupted_checker = SQLiteIntegrityChecker(db_path=temp_db)
    
    def test_mark_success_with_readonly_db_raises_error(self, temp_db, temp_file):
        """Test error handling when database is read-only."""
        # Create checker and make DB read-only
        checker = SQLiteIntegrityChecker(db_path=temp_db)
        Path(temp_db).chmod(0o444)
        
        try:
            file_hash = checker.compute_sha256(temp_file)

            # Should raise RuntimeError on write
            with pytest.raises(RuntimeError, match="Failed to mark success"):
                checker.mark_success(file_hash, temp_file)
        finally:
            # Restore permissions for cleanup
            Path(temp_db).chmod(0o644)


class TestVersionKey:
    """CLAUDE.md §4 P0 第 1 条：文档更新后旧版本片段永久残留。

    `find_other_versions` 是让调用方（`IngestionPipeline`）能找到"同一份
    文档的旧版本"的唯一入口，这里独立验证它的查询语义，不依赖完整的
    摄入流水线。
    """

    def test_version_key_defaults_to_file_path(self, checker):
        """不传 version_key 时退化成旧行为——CLI/仪表盘这类路径本身稳定
        的调用方不受影响。"""
        checker.mark_success("hash_a", "/docs/report.pdf", "kb1")

        # 同一路径、新内容（新哈希）视为新版本，应该能查到旧的那条。
        others = checker.find_other_versions("/docs/report.pdf", "kb1", exclude_hash="hash_b")
        assert [o["file_hash"] for o in others] == ["hash_a"]

    def test_explicit_version_key_used_instead_of_path(self, checker):
        """上传接口给物理路径加随机前缀时，必须靠显式 version_key 而不是
        物理路径本身找到旧版本——这是这次修复要解决的真实场景。"""
        checker.mark_success(
            "hash_a", "/uploads/aaaa1111_report.pdf", "kb1", version_key="report.pdf"
        )

        # 物理路径不同（每次上传都换一个随机前缀），但 version_key 相同。
        others = checker.find_other_versions("report.pdf", "kb1", exclude_hash="hash_b")
        assert len(others) == 1
        assert others[0]["file_hash"] == "hash_a"
        assert others[0]["file_path"] == "/uploads/aaaa1111_report.pdf"

        # 反过来，物理路径查不到任何东西——证明确实是按 version_key 查，
        # 不是碰巧退化回按路径查。
        assert checker.find_other_versions("/uploads/aaaa1111_report.pdf", "kb1", exclude_hash="hash_b") == []

    def test_excludes_current_hash(self, checker):
        """摄入成功后查自己刚写的哈希不该把自己当成"旧版本"。"""
        checker.mark_success("hash_a", "/docs/report.pdf", "kb1")

        assert checker.find_other_versions("/docs/report.pdf", "kb1", exclude_hash="hash_a") == []

    def test_accumulates_multiple_stale_versions(self, checker):
        """这条 P0 存在的时间够长，同一份文档可能已经积累了两次以上没被
        清理的旧版本——修复必须把它们全部找出来，不能只处理"最近一次"。"""
        checker.mark_success("hash_v1", "/docs/report.pdf", "kb1")
        checker.mark_success("hash_v2", "/docs/report.pdf", "kb1")

        others = checker.find_other_versions("/docs/report.pdf", "kb1", exclude_hash="hash_v3")
        assert {o["file_hash"] for o in others} == {"hash_v1", "hash_v2"}

    def test_scoped_by_collection(self, checker):
        """两个不同知识库里恰好同名的文档是完全不相关的两份文档，不能
        跨库互相当成"旧版本"删掉。"""
        checker.mark_success("hash_a", "/docs/report.pdf", "kb1", version_key="report.pdf")
        checker.mark_success("hash_b", "/docs/report.pdf", "kb2", version_key="report.pdf")

        assert checker.find_other_versions("report.pdf", "kb1", exclude_hash="hash_c") == \
            [{"file_hash": "hash_a", "file_path": "/docs/report.pdf"}]
        assert checker.find_other_versions("report.pdf", "kb2", exclude_hash="hash_c") == \
            [{"file_hash": "hash_b", "file_path": "/docs/report.pdf"}]

    def test_empty_version_key_matches_nothing(self, checker):
        """空 version_key 不构成有效标识，不该意外匹配到库里其它同样是
        空 version_key 的历史记录（迁移前的老数据）。"""
        checker.mark_success("hash_a", "", "kb1", version_key="")

        assert checker.find_other_versions("", "kb1", exclude_hash="hash_b") == []
