"""片段级去重（chunk-level dedup）。

跟 `src/libs/loader/file_integrity.py` 的文件级去重是两个不同粒度的问题：
文件级去重认的是"这一整份文件之前有没有摄入过"（同一份文件重复上传不会重复入库）；
这里认的是"这段切分/精炼后的文本，在同一个 collection 里之前有没有出现过"——
同一份长文档里重复的页眉页脚、免责声明，或者两份不同文档里雷同的公司政策
条款，都会被切成内容相同的 chunk，白白占用 embedding 调用和向量库空间，
还会在检索结果里挤占同一批候选、降低召回多样性。file_integrity 那层的文件
哈希覆盖不到这种情况——两份文件本身内容不同，文件哈希自然不同，但切出来的
某几个 chunk 可能逐字相同。

按 collection 隔离（不做跨库全局去重）：同一段话出现在两个不同知识库里是
正常业务场景（比如两家企业各自的合同模板都引用了同一段行业通用条款），
不该互相影响。

哈希用的是精炼后文本（去噪/合并之后的最终版本，不是切分刚出来的原始文本）——
两个 chunk 精炼前哪怕字符级不同（空白、页码水印），精炼后如果收敛成同一段
内容，就该被认成同一条，不然去重形同虚设。
"""

import hashlib
import re
import sqlite3
from pathlib import Path
from typing import Optional


def compute_content_hash(text: str) -> str:
    """对 chunk 正文算指纹——先做一次极简的空白归一化（连续空白/换行压缩成
    单个空格），避免"内容完全一样，只是换行位置不同"这种无意义差异被误判成
    不重复。跟 DocumentChunker._generate_chunk_id 的哈希算法一致（SHA256），
    但输入是归一化后的全文而不是原始 chunk 文本，两者用途不同，不能混用。"""
    normalized = re.sub(r"\s+", " ", text).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


class ChunkDedupIndex:
    """基于 SQLite 的 chunk 内容指纹登记表。跟 `SQLiteIntegrityChecker` 共用
    同一个 db 文件（`data/db/ingestion_history.db`），职责不同所以是独立的表，
    不复用它的 schema。"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._ensure_database()

    def _ensure_database(self) -> None:
        db_file = Path(self.db_path)
        db_file.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS chunk_content_index (
                    content_hash TEXT NOT NULL,
                    collection   TEXT NOT NULL,
                    chunk_id     TEXT NOT NULL,
                    source_doc_id TEXT,
                    first_seen_at TEXT NOT NULL,
                    PRIMARY KEY (content_hash, collection)
                )
                """
            )
            conn.commit()
        finally:
            conn.close()

    def find_existing(self, content_hash: str, collection: str) -> Optional[str]:
        """已经登记过就返回最早那条的 chunk_id，没有则返回 None。"""
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.execute(
                "SELECT chunk_id FROM chunk_content_index WHERE content_hash = ? AND collection = ?",
                (content_hash, collection),
            )
            row = cursor.fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    def register(self, content_hash: str, collection: str, chunk_id: str, source_doc_id: str) -> None:
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """
                INSERT OR IGNORE INTO chunk_content_index
                    (content_hash, collection, chunk_id, source_doc_id, first_seen_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (content_hash, collection, chunk_id, source_doc_id, now),
            )
            conn.commit()
        finally:
            conn.close()
