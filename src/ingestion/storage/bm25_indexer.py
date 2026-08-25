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
import math
import os
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple


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
        
    def build(
        self,
        term_stats: List[Dict[str, Any]],
        collection: str = "default",
        trace: Optional[Any] = None,
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
        if not self._index:
            raise ValueError("Index not loaded. Call load() or build() first.")
        
        if not query_terms:
            raise ValueError("query_terms cannot be empty")
        
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
        if not self._index:
            self.load(collection)

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

        # Merge: existing + new
        combined = list(existing_stats.values()) + list(term_stats)

        # Rebuild full index from combined stats
        self.build(combined, collection, trace)

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
        if not self._index:
            if not self.load(collection):
                return False

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

        return removed_any
    
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
