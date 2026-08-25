"""BM25 Indexer for building and querying inverted indexes.

This module implements the BM25 indexing component, responsible for:
- Computing IDF (Inverse Document Frequency) scores
- Building inverted index structures
- Persisting and loading indexes from disk
- Supporting incremental updates

Design Principles:
- Idempotent: Rebuild produces same results for same input
- Observable: Accepts TraceContext for future integration
- Persistent: Indexes saved to data/db/bm25/ directory
- Deterministic: Same corpus produces same IDF scores
"""

import json
import logging
import math
import os
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple

logger = logging.getLogger(__name__)


class BM25Indexer:
    """Build and query BM25 inverted indexes.
    
    This indexer receives term statistics from SparseEncoder and constructs
    a queryable BM25 index with IDF scores and posting lists.
    
    Index Structure:
        {
            "metadata": {
                "num_docs": int,
                "avg_doc_length": float,
                "total_terms": int
            },
            "index": {
                "term": {
                    "idf": float,
                    "df": int,  # document frequency
                    "postings": [
                        {"chunk_id": str, "tf": int, "doc_length": int},
                        ...
                    ]
                },
                ...
            }
        }
    
    BM25 IDF Formula:
        IDF(term) = log((N - df + 0.5) / (df + 0.5))
        
        Where:
        - N = total number of documents
        - df = document frequency (number of docs containing term)
    
    Example:
        >>> indexer = BM25Indexer(index_dir="data/db/bm25")
        >>> 
        >>> # Build index from SparseEncoder output
        >>> term_stats = [
        ...     {"chunk_id": "1", "term_frequencies": {"hello": 2, "world": 1}, "doc_length": 3},
        ...     {"chunk_id": "2", "term_frequencies": {"hello": 1, "python": 1}, "doc_length": 2}
        ... ]
        >>> indexer.build(term_stats)
        >>> 
        >>> # Query the index
        >>> results = indexer.query(["hello"], top_k=2)
        >>> len(results) <= 2  # True
    """
    
    def __init__(
        self,
        index_dir: str = "data/db/bm25",
        k1: float = 1.5,
        b: float = 0.75,
    ):
        """Initialize BM25Indexer.
        
        Args:
            index_dir: Directory to store index files (default: data/db/bm25)
            k1: BM25 term frequency saturation parameter (default: 1.5)
            b: BM25 length normalization parameter (default: 0.75)
        
        Raises:
            ValueError: If k1 or b are out of valid ranges
        """
        if k1 <= 0:
            raise ValueError(f"k1 must be > 0, got {k1}")
        if not 0 <= b <= 1:
            raise ValueError(f"b must be in [0, 1], got {b}")
        
        self.index_dir = Path(index_dir)
        self.k1 = k1
        self.b = b

        # In-memory index structure
        self._index: Dict[str, Dict[str, Any]] = {}
        self._metadata: Dict[str, Any] = {}

        # 方案 C 阶段 1：SQLite 影子写。**读路径完全不受影响，仍走 JSON。**
        # 目的是让两种后端的数据先并存，好在切读之前逐条比对打分（设计文档 §6）。
        # 默认开启；`RAGENT_BM25_SQLITE_DUAL_WRITE=false` 可关掉。
        self.dual_write_sqlite = (
            os.getenv("RAGENT_BM25_SQLITE_DUAL_WRITE", "true").strip().lower() != "false"
        )

        # 方案 C 阶段 2：读后端。
        #   auto（默认）—— 有可用的 SQLite 副本就走它，否则回退 JSON
        #   json        —— 强制走 JSON（回滚开关，出问题时不用改代码）
        #   sqlite      —— 强制走 SQLite，缺副本就报错（给迁移验收用，别在生产开）
        self.read_backend = (
            os.getenv("RAGENT_BM25_READ_BACKEND", "auto").strip().lower()
        )
        # `auto` 模式下，JSON 索引小于这个字节数就仍走 JSON —— 小索引上
        # SQLite 的连接固定开销盖过收益。默认 256KB，实测交叉点约 290KB。
        # 详见 `_use_sqlite_for_read` 里的实测表。
        self._sqlite_min_json_bytes = int(
            os.getenv("RAGENT_BM25_SQLITE_MIN_JSON_BYTES", str(256 * 1024))
        )
        # `load()` 判定走 SQLite 之后记在这里；`query()` 据此分流。
        # 为空表示走 JSON 的老路径（`self._index`）。
        self._sqlite_read_collection: Optional[str] = None

    def build(
        self,
        term_stats: List[Dict[str, Any]],
        collection: str = "default",
        trace: Optional[Any] = None,
        doc_hash_by_chunk: Optional[Dict[str, str]] = None,
    ) -> None:
        """Build BM25 index from term statistics.
        
        This method:
        1. Calculates corpus-level statistics (N, avg_doc_length, DF)
        2. Computes IDF for each term
        3. Builds inverted index with posting lists
        4. Persists to disk
        
        Args:
            term_stats: List of statistics from SparseEncoder.encode()
                Each item should have: chunk_id, term_frequencies, doc_length
            collection: Collection name for organizing indexes (default: "default")
            trace: Optional TraceContext for observability
        
        Raises:
            ValueError: If term_stats is empty or has invalid structure
        
        Example:
            >>> term_stats = [
            ...     {
            ...         "chunk_id": "doc1_chunk0",
            ...         "term_frequencies": {"machine": 2, "learning": 1},
            ...         "doc_length": 3
            ...     }
            ... ]
            >>> indexer.build(term_stats, collection="my_docs")
        """
        if not term_stats:
            raise ValueError("Cannot build index from empty term_stats")
        
        # Validate structure
        self._validate_term_stats(term_stats)
        
        # Step 1: Calculate corpus-level statistics
        num_docs = len(term_stats)
        total_length = sum(stat["doc_length"] for stat in term_stats)
        avg_doc_length = total_length / num_docs if num_docs > 0 else 0.0
        
        # Step 2: Invert in a single pass over term_stats.
        #
        # 2026-08-25：原实现是
        #     for term in doc_freq:          # 词表
        #         for stat in term_stats:    # 每篇文档
        #             tf = stat["term_frequencies"].get(term, 0)
        # 即为每个词条把整个语料重扫一遍，复杂度 O(词表 × 文档数)。原型实测
        # 1K/16K/50K 块 = 0.35 / 31.08 / 393.97 s，16K→50K 段 α=2.23；
        # 外推 143K 块约 1.1 小时、716K 块约 41 小时。
        # （`scale_slo_and_priorities.md` 里「首次摄入 1.4–3.2 小时」的估算
        #   没把这条算进去。）
        #
        # 单遍写法把复杂度降到 O(词条出现总次数)，且**与是否换存储后端无关** ——
        # 改完仍写 JSON，50K 块也只要约 5s。
        #
        # 输出必须与旧实现逐字节等价，这依赖两条顺序性质，改动时不要破坏：
        #   1. `index` 的键顺序 = 词条首次出现的顺序。doc_freq 和 postings 都在
        #      同一遍里按 term_stats 顺序填充，dict 保序，因此与旧实现一致。
        #   2. 每个 postings 列表内部 = term_stats 顺序。同样由单遍顺序追加保证。
        # 有测试拿旧实现的逐行复刻当 oracle 对比整棵索引。
        doc_freq: Dict[str, int] = {}
        postings_by_term: Dict[str, List[Dict[str, Any]]] = {}

        for stat in term_stats:
            chunk_id = stat["chunk_id"]
            doc_length = stat["doc_length"]
            for term, tf in stat["term_frequencies"].items():
                # ⚠️ df 统计所有出现过的键，而 postings 只收 tf > 0 —— 两者在
                # tf == 0 时会对不上。这是旧实现的既有语义，此处原样保留：
                # 真实的 SparseEncoder 不产生 tf == 0，要改它是另一个决策，
                # 不该夹带在一次纯性能优化里。有测试钉住这个边界。
                doc_freq[term] = doc_freq.get(term, 0) + 1
                if tf > 0:
                    postings_by_term.setdefault(term, []).append({
                        "chunk_id": chunk_id,
                        "tf": tf,
                        "doc_length": doc_length
                    })

        index: Dict[str, Dict[str, Any]] = {}
        for term, df in doc_freq.items():
            index[term] = {
                "idf": self._calculate_idf(num_docs, df),
                "df": df,
                "postings": postings_by_term.get(term, [])
            }

        # Step 3: Store metadata
        self._metadata = {
            "num_docs": num_docs,
            "avg_doc_length": avg_doc_length,
            "total_terms": len(index),
            "collection": collection,
        }
        
        self._index = index

        # Step 4: Persist to disk
        self._save(collection)
        self._mirror_to_sqlite(collection, doc_hash_by_chunk)
    
    def load(
        self,
        collection: str = "default",
        trace: Optional[Any] = None,
    ) -> bool:
        """Load index from disk.
        
        Args:
            collection: Collection name to load
            trace: Optional TraceContext for observability
        
        Returns:
            True if index loaded successfully, False if not found
        
        Raises:
            ValueError: If index file is corrupted

        方案 C 阶段 2：**这个方法是读路径的分流点。**

        `SparseRetriever._ensure_index_loaded` 每次查询都调它一次，
        它的注释写着"The load is fast (a single JSON file read)" ——
        那正是要修的错误假设：50K 块时这一次 `json.load` 实测要 **1.2–1.3 秒**，
        6 库企业每次提问就是 8 秒，而 TTFT SLO 是 3 秒。

        有可用的 SQLite 副本时，这里**什么都不读**，只记下走 SQLite，
        把实际取数推迟到 `query()` 里按词条做。`self._index` 保持为空 ——
        这正是收益所在，不要"顺手"再把 JSON 载进来。
        """
        if self._use_sqlite_for_read(collection):
            from src.ingestion.storage.bm25_sqlite_store import BM25SQLiteStore

            self._sqlite_read_collection = collection
            # `_index` 保持为空 —— 那是整个改动的收益所在，不要"顺手"把 JSON
            # 载进来。但 `_metadata` 照常填：它只有 4 个标量，从 meta 表读几乎
            # 免费，而填上之后 `load()` 的对外契约基本不变，既有调用方
            # （以及测试）不必区分后端。
            self._index = {}
            with BM25SQLiteStore(self._sqlite_path(collection)) as store:
                self._metadata = store.load_metadata()
            return True

        self._sqlite_read_collection = None
        return self._load_json_index(collection)

    def _load_json_index(self, collection: str) -> bool:
        """把 JSON 索引读进 `self._index`。

        **写路径（`add_documents` / `remove_document`）必须调这个而不是 `load()`**：
        阶段 2 起 `load()` 在有 SQLite 副本时会走读路径分流、刻意不填充
        `self._index`，而写路径的合并重建完全依赖它。用错会导致既有 postings
        全部丢失、整个索引被这一份文档覆盖。
        """
        index_path = self._get_index_path(collection)

        if not index_path.exists():
            return False

        try:
            with open(index_path, 'r', encoding='utf-8') as f:
                data = json.load(f)

            # Validate structure
            if "metadata" not in data or "index" not in data:
                raise ValueError(f"Invalid index file structure: missing metadata or index")

            self._metadata = data["metadata"]
            self._index = data["index"]

            return True

        except json.JSONDecodeError as e:
            raise ValueError(f"Corrupted index file at {index_path}: {e}")
    
    def query(
        self,
        query_terms: List[str],
        top_k: int = 10,
        trace: Optional[Any] = None,
    ) -> List[Dict[str, Any]]:
        """Query the index using BM25 scoring.
        
        Args:
            query_terms: List of terms to search for
            top_k: Maximum number of results to return
            trace: Optional TraceContext for observability
        
        Returns:
            List of results sorted by BM25 score (descending).
            Each result: {"chunk_id": str, "score": float}
        
        Raises:
            ValueError: If index not loaded or query_terms empty
        
        Example:
            >>> indexer.load("my_docs")
            >>> results = indexer.query(["machine", "learning"], top_k=5)
            >>> results[0]["score"] > 0  # True if matches found
        """
        if not query_terms:
            raise ValueError("query_terms cannot be empty")

        # 阶段 2：`load()` 判定走 SQLite 时，`self._index` 是空的（那是设计，
        # 不是漏加载），所以这个分流必须排在下面的 `not self._index` 检查之前。
        if self._sqlite_read_collection is not None:
            from src.ingestion.storage.bm25_sqlite_store import BM25SQLiteStore

            with BM25SQLiteStore(
                self._sqlite_path(self._sqlite_read_collection)
            ) as store:
                return store.query(
                    query_terms, top_k=top_k, k1=self.k1, b=self.b
                )

        if not self._index:
            raise ValueError("Index not loaded. Call load() or build() first.")

        # Lowercase query terms to match index (SparseEncoder lowercases during build)
        query_terms = [t.lower() for t in query_terms]
        
        # Calculate BM25 scores for all documents
        scores: Dict[str, float] = {}
        
        for term in query_terms:
            if term not in self._index:
                continue  # Term not in corpus, skip
            
            term_data = self._index[term]
            idf = term_data["idf"]
            
            for posting in term_data["postings"]:
                chunk_id = posting["chunk_id"]
                tf = posting["tf"]
                doc_length = posting["doc_length"]
                
                # BM25 score contribution from this term
                term_score = self._calculate_bm25_score(
                    tf=tf,
                    doc_length=doc_length,
                    avg_doc_length=self._metadata["avg_doc_length"],
                    idf=idf
                )
                
                scores[chunk_id] = scores.get(chunk_id, 0.0) + term_score
        
        # Sort by score descending, breaking ties on chunk_id.
        #
        # 2026-08-25：原本是 `key=lambda x: x["score"], reverse=True`。Python 的
        # sorted 是稳定排序，reverse=True 也不会打乱同分组，所以同分候选的先后
        # 完全取决于 `scores` 的插入顺序 —— 也就是 postings 的物理顺序，进而取决
        # 于当初的摄入顺序。结果是「重建一次索引，top-k 就可能变一批」，而分数
        # 一个都没变。原型实测：50K 块时截断线上有 14–19 个同分候选，换存储后端
        # 后全量分数映射逐 bit 相同，top-10 却只对上 4/12。
        #
        # chunk_id 在一个 collection 内唯一，所以 (-score, chunk_id) 是全序，
        # 排序结果与输入顺序无关。这是 BM25 存储层改造能被验收的前提：没有它，
        # 「新旧结果是否一致」这个问题本身就没有答案。
        #
        # ⚠️ 必须排完整个列表再截断。先截断再排序会得到「摄入顺序里靠前的那几个」，
        # 看着也有序，但换个摄入顺序结果就变了 —— 有测试专门抓这一点。
        sorted_results = sorted(
            [{"chunk_id": cid, "score": score} for cid, score in scores.items()],
            key=lambda x: (-x["score"], x["chunk_id"]),
        )

        return sorted_results[:top_k]
    
    def rebuild(
        self,
        term_stats: List[Dict[str, Any]],
        collection: str = "default",
        trace: Optional[Any] = None,
    ) -> None:
        """Rebuild index from scratch (alias for build with clear intent).
        
        This is a convenience method that makes the intent clear when
        replacing an existing index.
        
        Args:
            term_stats: List of statistics from SparseEncoder
            collection: Collection name
            trace: Optional TraceContext for observability
        """
        self.build(term_stats, collection, trace)

    def add_documents(
        self,
        term_stats: List[Dict[str, Any]],
        collection: str = "default",
        doc_id: Optional[str] = None,
        trace: Optional[Any] = None,
    ) -> None:
        """Incrementally add documents to the BM25 index.

        Loads the existing index (if any), optionally removes old postings
        for the given *doc_id* (to support re-ingestion), merges the new
        term stats, recomputes IDF scores, and saves.

        Args:
            term_stats: New term statistics from SparseEncoder.encode().
            collection: Collection name.
            doc_id: If provided, remove existing postings whose chunk_id
                starts with this prefix before adding new ones (idempotent
                re-ingestion).
            trace: Optional TraceContext.
        """
        if not term_stats:
            return

        self._validate_term_stats(term_stats)

        # Load existing index (ignore if missing – will start fresh)
        # ⚠️ 必须走 `_load_json_index`，不能走 `load()` —— 阶段 2 起 `load()`
        # 在有 SQLite 副本时会走读路径分流、**刻意不填充 `self._index`**，
        # 而下面的合并重建完全依赖 `self._index`。用 `load()` 会导致
        # "既有 postings 全部丢失、索引被这一份文档覆盖"。
        if not self._index:
            self._load_json_index(collection)

        # Remove stale postings for this document (re-ingest case)
        if doc_id and self._index:
            self.remove_document(doc_id, collection)

        # Reconstruct existing term_stats from current index postings
        existing_stats: Dict[str, Dict[str, Any]] = {}  # chunk_id -> stat
        for term, term_data in self._index.items():
            for posting in term_data["postings"]:
                cid = posting["chunk_id"]
                if cid not in existing_stats:
                    existing_stats[cid] = {
                        "chunk_id": cid,
                        "term_frequencies": {},
                        "doc_length": posting["doc_length"],
                    }
                existing_stats[cid]["term_frequencies"][term] = posting["tf"]

        # Merge: existing + new，**按 chunk_id 去重，新的覆盖旧的**。
        #
        # 2026-08-25：不去重是一个真实的正确性 bug，由 SQLite 侧的主键约束
        # (term, chunk_id) 顶出来。链路是这样的：上面的 `remove_document` 因为
        # 那条已知 P0（`chunk_id.startswith(doc_id)` 恒为假）根本没删掉旧数据，
        # 于是重摄入同一份文档时，`existing_stats` 里的旧 chunk 和 `term_stats`
        # 里的新 chunk **是同一个 chunk_id**，两条都进了 combined。
        #
        # 后果不是"多占点空间"，是**打分错**：实测同一文档摄入两次后
        # postings 里同一个 chunk_id 出现 2 条、`df` 和 `num_docs` 都变成 2，
        # 而真实只有 1 个 chunk。df == num_docs 时经典 IDF
        # log((N-df+0.5)/(df+0.5)) 为负 —— 该文档自己的分数变成 -4.598，
        # 排到了「完全不含这个词的文档」后面。
        #
        # 用 dict 按 chunk_id 收敛、新值覆盖旧值：新摄入的内容天然更权威。
        # 这不能替代修 `remove_document`（旧文档**其他** chunk 的残留仍在，
        # 那是 CLAUDE.md §4 第 1 条那条 P0），只是让"同一 chunk 重复计数"
        # 这个更直接的错误不再发生。
        merged: Dict[str, Dict[str, Any]] = dict(existing_stats)
        for stat in term_stats:
            merged[stat["chunk_id"]] = stat
        combined = list(merged.values())

        # Rebuild full index from combined stats.
        # `doc_id` 是文件内容 SHA256，透传给 SQLite 侧记进 chunks.doc_hash ——
        # 这样删除就能按文档哈希精确定位，不再依赖 chunk_id 前缀匹配。
        # 只标注**本次新增**的 chunk；老 chunk 的出处由 chunks 表自己保留。
        self.build(
            combined,
            collection,
            trace,
            doc_hash_by_chunk=(
                {s["chunk_id"]: doc_id for s in term_stats} if doc_id else None
            ),
        )

    def remove_document(
        self,
        doc_id: str,
        collection: str = "default",
    ) -> bool:
        """Remove all postings for a document from the BM25 index.

        Loads the index (if not already loaded), removes any postings
        whose ``chunk_id`` starts with *doc_id*, recalculates statistics,
        and re-saves the index.

        Args:
            doc_id: Document identifier (or prefix).  All postings whose
                ``chunk_id`` starts with this value are removed.
            collection: Collection name.

        Returns:
            ``True`` if any postings were removed, ``False`` otherwise.
        """
        # SQLite 侧先删：它按 doc_hash 精确匹配，是这条 P0 的正解。
        # 放在前面是因为下面 JSON 侧的前缀匹配恒不命中，会提前 return False，
        # 跟在后面就永远执行不到。
        sqlite_deleted = self._delete_from_sqlite(collection, doc_id)

        if not self._index:
            # 同 `add_documents`：写路径只认 JSON 索引，见那里的说明。
            if not self._load_json_index(collection):
                return sqlite_deleted > 0

        removed_any = False
        terms_to_delete: list[str] = []

        for term, term_data in self._index.items():
            original_len = len(term_data["postings"])
            term_data["postings"] = [
                p for p in term_data["postings"]
                if not p["chunk_id"].startswith(doc_id)
            ]
            if len(term_data["postings"]) < original_len:
                removed_any = True
            # Mark empty terms for cleanup
            if not term_data["postings"]:
                terms_to_delete.append(term)
            else:
                term_data["df"] = len(term_data["postings"])

        # Remove empty terms
        for term in terms_to_delete:
            del self._index[term]

        if removed_any:
            # Recalculate global metadata
            all_chunk_ids: set[str] = set()
            total_length = 0
            for td in self._index.values():
                for p in td["postings"]:
                    all_chunk_ids.add(p["chunk_id"])
                    total_length += p["doc_length"]

            num_docs = len(all_chunk_ids)
            avg_doc_length = total_length / num_docs if num_docs else 0.0

            # Recalculate IDF values
            for td in self._index.values():
                td["idf"] = self._calculate_idf(num_docs, td["df"])

            self._metadata = {
                "num_docs": num_docs,
                "avg_doc_length": avg_doc_length,
                "total_terms": len(self._index),
                "collection": collection,
            }
            self._save(collection)
            # JSON 侧真删掉了东西时才回镜像，否则会把上面刚做完的 SQLite 删除
            # 用一份"还含着这些 postings"的 JSON 索引覆盖回去。
            self._mirror_to_sqlite(collection)

        return removed_any or sqlite_deleted > 0
    
    # ===== Private Helper Methods =====
    
    def _calculate_idf(self, num_docs: int, df: int) -> float:
        """Calculate IDF using BM25 formula.
        
        Formula: IDF(term) = log((N - df + 0.5) / (df + 0.5))
        
        Args:
            num_docs: Total number of documents in corpus
            df: Document frequency (number of docs containing term)
        
        Returns:
            IDF score (can be negative for very common terms)
        """
        return math.log((num_docs - df + 0.5) / (df + 0.5))
    
    def _calculate_bm25_score(
        self,
        tf: int,
        doc_length: int,
        avg_doc_length: float,
        idf: float
    ) -> float:
        """Calculate BM25 score for a single term in a document.
        
        Formula: score = IDF * (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * (doc_length / avg_doc_length)))
        
        Args:
            tf: Term frequency in document
            doc_length: Length of document (number of terms)
            avg_doc_length: Average document length in corpus
            idf: IDF score for this term
        
        Returns:
            BM25 score contribution
        """
        # Avoid division by zero
        if avg_doc_length == 0:
            avg_doc_length = 1.0
        
        # BM25 formula
        numerator = tf * (self.k1 + 1)
        denominator = tf + self.k1 * (1 - self.b + self.b * (doc_length / avg_doc_length))
        
        return idf * (numerator / denominator)
    
    def _validate_term_stats(self, term_stats: List[Dict[str, Any]]) -> None:
        """Validate term_stats structure.
        
        Raises:
            ValueError: If structure is invalid
        """
        for i, stat in enumerate(term_stats):
            if not isinstance(stat, dict):
                raise ValueError(f"term_stats[{i}] must be a dict, got {type(stat)}")
            
            required_fields = ["chunk_id", "term_frequencies", "doc_length"]
            for field in required_fields:
                if field not in stat:
                    raise ValueError(f"term_stats[{i}] missing required field: {field}")
            
            if not isinstance(stat["term_frequencies"], dict):
                raise ValueError(
                    f"term_stats[{i}]['term_frequencies'] must be dict, "
                    f"got {type(stat['term_frequencies'])}"
                )
            
            if not isinstance(stat["doc_length"], int) or stat["doc_length"] < 0:
                raise ValueError(
                    f"term_stats[{i}]['doc_length'] must be non-negative int, "
                    f"got {stat['doc_length']}"
                )
    
    # ===== 方案 C 阶段 1：SQLite 影子写 =====

    def _sqlite_path(self, collection: str) -> Path:
        return self.index_dir / f"{collection}_bm25.sqlite"

    def _use_sqlite_for_read(self, collection: str) -> bool:
        """这个 collection 的读请求该不该走 SQLite。

        默认 `auto`：**必须同时满足"副本存在"和"副本不比 JSON 旧"**，
        否则回退 JSON。

        新鲜度检查不是多余的谨慎 —— 影子写是 fail-soft 的（见
        `_mirror_to_sqlite`），磁盘满、权限、进程被杀都会让某个 collection 的
        SQLite 副本停在旧版本上，而 JSON 侧照常更新。没有这道检查，
        一次无人注意的影子写失败会**静默地让该库的检索结果退回到过去某个时刻**，
        比直接报错难查得多。用 mtime 比较是因为它零成本；代价是精度只到文件级，
        所以它只是安全网，不能替代迁移脚本里那次逐条打分比对。
        """
        if self.read_backend == "json":
            return False

        sqlite_path = self._sqlite_path(collection)
        if self.read_backend == "sqlite":
            # 强制模式下缺副本要显式失败，不能悄悄回退 —— 它的用途就是验收迁移，
            # 一旦回退，"读的到底是哪个后端"就说不清了。
            if not sqlite_path.exists():
                raise FileNotFoundError(
                    f"RAGENT_BM25_READ_BACKEND=sqlite 但 {sqlite_path} 不存在。"
                    f"先跑 scripts/migrate_bm25_json_to_sqlite.py 迁移该 collection，"
                    f"或改回 auto。"
                )
            return True

        if not sqlite_path.exists():
            return False

        json_path = self._get_index_path(collection)

        # JSON 已经不在（阶段 4 清理过）→ 没得选。
        if not json_path.exists():
            return True

        # ⚠️ **小索引上 SQLite 更慢，别无脑切。** 实测交叉点约 50 块 / 290KB：
        #
        #   块数   JSON大小   JSON读   SQLite读
        #     20     119KB    0.38ms    0.82ms   ← JSON 快 2.2 倍
        #     50     292KB    0.86ms    0.86ms   ← 持平
        #    200    1148KB    3.31ms    1.14ms   ← SQLite 快 2.9 倍
        #   3000   16812KB   51.65ms    7.68ms   ← SQLite 快 6.7 倍
        #
        # 原因是固定开销：开一个 sqlite3 连接约 0.5ms，而 60KB 的 json.load
        # 只要 0.35ms。本项目现网 17 个业务库全是 20 篇文档级别，**无条件切读
        # 会让它们每次查询都慢一倍** —— 收益要等单库上到几百块以后才出现。
        # 连接复用救不了这一点：`query_knowledge_hub._build_hybrid_search_for`
        # 每次查询都新建一个 BM25Indexer，实例级缓存跨不过查询边界。
        if json_path.stat().st_size < self._sqlite_min_json_bytes:
            return False

        if json_path.stat().st_mtime > sqlite_path.stat().st_mtime:
            logger.warning(
                "BM25 SQLite 副本比 JSON 旧，本次查询回退 JSON。"
                "该 collection 的影子写可能失败过，需重新迁移。collection=%s",
                collection,
                extra={
                    "event": "bm25.sqlite.stale_fallback",
                    "collection": collection,
                },
            )
            return False

        return True

    def _mirror_to_sqlite(
        self,
        collection: str,
        doc_hash_by_chunk: Optional[Dict[str, str]] = None,
    ) -> None:
        """把当前内存索引镜像进 SQLite 侧。**失败不影响摄入。**

        阶段 1 里 SQLite 没有任何生产读者，所以一次镜像失败不该让整篇文档
        摄入失败——那是拿"还没人用的新后端"去阻断"正在用的老链路"。
        但**必须以 ERROR 记下来**：两边静默分歧正是让阶段 2 切读变危险的东西，
        切读之前要能从日志里查到"这个 collection 的影子副本什么时候掉过队"。
        """
        if not self.dual_write_sqlite:
            return
        try:
            from src.ingestion.storage.bm25_sqlite_store import BM25SQLiteStore

            with BM25SQLiteStore(self._sqlite_path(collection)) as store:
                store.replace_all(
                    index=self._index,
                    metadata=self._metadata,
                    doc_hash_by_chunk=doc_hash_by_chunk,
                )
        except Exception as exc:  # noqa: BLE001 —— 见 docstring：刻意不向上抛
            logger.error(
                "BM25 SQLite 影子写失败，该 collection 的副本已与 JSON 分歧；"
                "切读前必须重建。collection=%s err=%s",
                collection,
                exc,
                extra={
                    "event": "bm25.sqlite.dual_write_failed",
                    "collection": collection,
                },
            )

    def _delete_from_sqlite(self, collection: str, doc_hash: str) -> int:
        """SQLite 侧按 doc_hash 删除。同样失败不影响主流程。

        这是 JSON 侧 `remove_document` 恒返回 False 那条 P0 的正解 ——
        不再用 `chunk_id.startswith(doc_id)` 这种脆弱约定。
        """
        if not self.dual_write_sqlite:
            return 0
        try:
            from src.ingestion.storage.bm25_sqlite_store import BM25SQLiteStore

            with BM25SQLiteStore(self._sqlite_path(collection)) as store:
                return store.delete_by_doc_hash(doc_hash)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "BM25 SQLite 影子删除失败。collection=%s doc_hash=%s err=%s",
                collection,
                doc_hash[:12],
                exc,
                extra={
                    "event": "bm25.sqlite.dual_delete_failed",
                    "collection": collection,
                },
            )
            return 0

    def _get_index_path(self, collection: str) -> Path:
        """Get file path for index file.
        
        Args:
            collection: Collection name
        
        Returns:
            Path to index file
        """
        return self.index_dir / f"{collection}_bm25.json"
    
    def _save(self, collection: str) -> None:
        """Save index to disk.
        
        Args:
            collection: Collection name
        """
        # Ensure directory exists
        self.index_dir.mkdir(parents=True, exist_ok=True)
        
        index_path = self._get_index_path(collection)
        
        # Prepare data
        data = {
            "metadata": self._metadata,
            "index": self._index
        }
        
        # Write atomically (write to temp file, then rename)
        temp_path = index_path.with_suffix('.tmp')
        try:
            with open(temp_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            
            # Atomic rename
            temp_path.replace(index_path)
            
        except Exception as e:
            # Clean up temp file if write failed
            if temp_path.exists():
                temp_path.unlink()
            raise
