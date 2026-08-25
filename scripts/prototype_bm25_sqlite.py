"""方案 C（词条级 SQLite 存储）的最小原型验证。

背景
----
`docs/bm25_storage_design.md` 已拍板方案 C：把 BM25 倒排索引从「单个大 JSON」
换成「per-collection 的 SQLite 单文件」，按词条查而不是全量加载。

但 §3 表格里方案 C 那行的「~0 延迟」是**基于 O(查询词条数) 的推断，未实测**，
§8 明确要求「实施前先做最小原型验证」。本脚本就是这一步。

本脚本回答三个问题
------------------
Q1. 查询延迟真的与索引规模无关吗？   → 1K / 16K / 50K 三档跑同一批查询
Q2. 打分结果与现有 JSON 实现一致吗？ → top-k 逐条比对 chunk_id 与分数（位级）
Q3. 写入性能可接受吗？               → 建索引耗时与 JSON 方式对比

原型边界（重要）
----------------
- **不修改 `src/` 任何代码**。JSON 侧直接调用现有 `BM25Indexer`，
  SQLite 侧在本文件内实现一个最小后端。
- 只验证 BM25 打分这一层，不涉及向量库、embedding、端到端检索。

用法
----
    .venv/bin/python scripts/prototype_bm25_sqlite.py
    .venv/bin/python scripts/prototype_bm25_sqlite.py --sizes 1000,16000
    .venv/bin/python scripts/prototype_bm25_sqlite.py --keep --workdir /tmp/proto

结果 JSON 落 scripts/benchmark_results/。
"""
from __future__ import annotations

import argparse
import gc
import hashlib
import json
import math
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 语料生成直接复用增长曲线脚本：它已经解决了「真实中文语料 + 模板扩充」
# 和「不需要 embedding」这两件事，重写一份只会让两次测量的语料不可比。
from scripts.measure_bm25_index_growth import build_corpus, load_real_chunks  # noqa: E402

# BM25 参数与 BM25Indexer 默认值保持一致，否则分数对比没有意义。
K1 = 1.5
B = 0.75

# 每个「源文档」切多少块——决定 doc_hash / source_path 的分布，
# 也决定 chunk_id 是否单调（见 §「平局顺序」）。
CHUNKS_PER_DOC = 8

# 查询集：模拟真实企业问答的关键词，覆盖高频词（大 postings）和低频词（小 postings），
# 因为 Q1 的答案完全取决于命中词条的 postings 有多长。
_QUERY_TEXTS = [
    "员工年假怎么申请",
    "报销单需要哪个部门审批",
    "远程办公的考勤怎么算",
    "差旅费用结算流程和时限",
    "绩效考核结果什么时候公布",
    "合同变更需要法务部复核吗",
    "社保和公积金的缴纳基数",
    "采购预算超支怎么备案",
    "离职手续归档由谁负责",
    "培训申请被驳回可以延期吗",
    "分公司负责人的审批权限",
    "记录编号在系统里怎么查",
]


# ---------------------------------------------------------------- 语料 / 查询

def make_chunk_ids(n: int) -> Tuple[List[str], List[str], List[str]]:
    """生成贴近真实格式的 chunk_id，以及配套的 source_path / doc_hash。

    真实格式是 ``65046ad1_0000_2a3ac7ab``：
    ``sha256(源路径)[:8]`` + 块序号 + ``sha256(内容)[:8]``。

    这里刻意**不用**单调递增的 id（增长曲线脚本用的是 ``g0050000_0000123_growth``）。
    原因：`BM25Indexer` 的 postings 按摄入顺序排列，而 SQLite 聚簇表按
    ``(term, chunk_id)`` 排列。只有 chunk_id 非单调时，两者的 postings 顺序才会
    真正分叉，Q2 的「平局顺序」问题才暴露得出来。用单调 id 会假性通过。
    """
    chunk_ids: List[str] = []
    source_paths: List[str] = []
    doc_hashes: List[str] = []
    for i in range(n):
        doc_no = i // CHUNKS_PER_DOC
        seq = i % CHUNKS_PER_DOC
        src = f"data/uploads/policy_{doc_no:06d}.md"
        src_h = hashlib.sha256(src.encode()).hexdigest()[:8]
        doc_h = hashlib.sha256(f"content-{doc_no}".encode()).hexdigest()
        content_h = hashlib.sha256(f"{src}#{seq}".encode()).hexdigest()[:8]
        chunk_ids.append(f"{src_h}_{seq:04d}_{content_h}")
        source_paths.append(src)
        doc_hashes.append(doc_h)
    return chunk_ids, source_paths, doc_hashes


def tokenize_queries(encoder) -> List[Dict[str, Any]]:
    """用与摄入侧完全相同的分词器切查询，否则词条对不上，测出来的是假的。"""
    out = []
    for q in _QUERY_TEXTS:
        terms = encoder._tokenize(q)  # noqa: SLF001 —— 原型里直接复用摄入侧分词
        out.append({"text": q, "terms": terms})
    return out


# ---------------------------------------------------------------- SQLite 后端

class SqliteBM25:
    """方案 C 的最小实现：per-collection 单文件，按词条查。

    表结构相对设计文档 §3 有两处**基于实测的调整**（理由见模块末尾的报告文本）：

    1. postings 用 ``WITHOUT ROWID`` + ``PRIMARY KEY(term, chunk_id)``。
       设计文档写的是普通表 + ``INDEX(term)``。普通表的二级索引不覆盖查询，
       每条 posting 都要回表按 rowid 随机读一次；而聚簇表把同一词条的 postings
       物理相邻存放，一次 B-tree 定位后顺序扫完，随机 I/O 降到 O(1) 次/词条。
    2. ``doc_hash`` / ``source_path`` 不直接内联在 postings 里，而是放独立的
       ``chunks`` 表。同一个 chunk 会出现在几十个词条的 postings 里，内联等于把
       路径字符串重复几十遍；分出去后 postings 每行只剩四个短字段。
       删除仍然是一条语句（见 :meth:`remove_by_source_path`）。

    variant="rowid" 保留设计文档原样的写法，用于对照证明上面第 1 条的收益。
    """

    def __init__(self, db_path: Path, variant: str = "clustered"):
        self.db_path = Path(db_path)
        self.variant = variant
        self._conn: sqlite3.Connection | None = None

    # ---- 建库 ----

    def _schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE meta(
                key TEXT PRIMARY KEY,
                value TEXT
            );
            CREATE TABLE terms(
                term TEXT PRIMARY KEY,
                idf  REAL NOT NULL,
                df   INTEGER NOT NULL
            ) WITHOUT ROWID;
            CREATE TABLE chunks(
                chunk_id    TEXT PRIMARY KEY,
                doc_hash    TEXT,
                source_path TEXT
            ) WITHOUT ROWID;
            CREATE INDEX ix_chunks_source ON chunks(source_path);
            CREATE INDEX ix_chunks_dochash ON chunks(doc_hash);
            """
        )
        if self.variant == "clustered":
            conn.executescript(
                """
                CREATE TABLE postings(
                    term       TEXT    NOT NULL,
                    chunk_id   TEXT    NOT NULL,
                    tf         INTEGER NOT NULL,
                    doc_length INTEGER NOT NULL,
                    PRIMARY KEY(term, chunk_id)
                ) WITHOUT ROWID;
                CREATE INDEX ix_postings_chunk ON postings(chunk_id);
                """
            )
        else:  # "rowid"：设计文档 §3 的原始写法，作对照组
            conn.executescript(
                """
                CREATE TABLE postings(
                    term       TEXT    NOT NULL,
                    chunk_id   TEXT    NOT NULL,
                    tf         INTEGER NOT NULL,
                    doc_length INTEGER NOT NULL
                );
                CREATE INDEX ix_postings_term ON postings(term);
                CREATE INDEX ix_postings_chunk ON postings(chunk_id);
                """
            )

    def build(
        self,
        term_stats: List[Dict[str, Any]],
        collection: str,
        chunk_meta: Dict[str, Tuple[str, str]],
    ) -> Dict[str, float]:
        """从 term_stats 建库。返回各阶段耗时，便于定位写入慢在哪。

        注意：这里用**一次遍历**构建倒排（O(总 postings)），而现有
        ``BM25Indexer.build`` 是 ``for term in vocab: for stat in term_stats``，
        即 O(词表 × 文档数)。这个差异是 Q3 结论的关键，报告里单列。
        """
        timings: Dict[str, float] = {}
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        if self.db_path.exists():
            self.db_path.unlink()

        num_docs = len(term_stats)
        total_length = sum(s["doc_length"] for s in term_stats)
        avg_doc_length = total_length / num_docs if num_docs else 0.0

        # --- 阶段 1：单遍构建倒排表 ---
        t0 = time.perf_counter()
        inverted: Dict[str, List[Tuple[str, int, int]]] = {}
        for stat in term_stats:
            cid = stat["chunk_id"]
            dlen = stat["doc_length"]
            for term, tf in stat["term_frequencies"].items():
                inverted.setdefault(term, []).append((cid, tf, dlen))
        timings["invert_s"] = time.perf_counter() - t0

        # --- 阶段 2：写库 ---
        t0 = time.perf_counter()
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA journal_mode=OFF")     # 建库是一次性写，崩了重建即可
        conn.execute("PRAGMA synchronous=OFF")
        conn.execute("PRAGMA cache_size=-64000")    # 64MB page cache
        self._schema(conn)

        conn.execute("BEGIN")
        conn.executemany(
            "INSERT INTO meta(key, value) VALUES(?, ?)",
            [
                ("collection", collection),
                ("num_docs", str(num_docs)),
                ("avg_doc_length", repr(avg_doc_length)),  # repr 保证浮点往返无损
                ("total_terms", str(len(inverted))),
                ("k1", repr(K1)),
                ("b", repr(B)),
            ],
        )
        conn.executemany(
            "INSERT INTO chunks(chunk_id, doc_hash, source_path) VALUES(?,?,?)",
            [(cid, m[0], m[1]) for cid, m in chunk_meta.items()],
        )

        # 按词条字典序插入：聚簇表按 (term, chunk_id) 排序，顺序插入避免页分裂
        def _rows() -> Iterable[Tuple[str, str, int, int]]:
            for term in sorted(inverted):
                for cid, tf, dlen in sorted(inverted[term]):
                    yield (term, cid, tf, dlen)

        conn.executemany(
            "INSERT INTO postings(term, chunk_id, tf, doc_length) VALUES(?,?,?,?)",
            _rows(),
        )
        conn.executemany(
            "INSERT INTO terms(term, idf, df) VALUES(?,?,?)",
            [
                (term, calc_idf(num_docs, len(plist)), len(plist))
                for term, plist in inverted.items()
            ],
        )
        conn.execute("COMMIT")
        conn.execute("ANALYZE")
        conn.close()
        timings["write_s"] = time.perf_counter() - t0
        timings["total_s"] = timings["invert_s"] + timings["write_s"]
        timings["postings_rows"] = sum(len(v) for v in inverted.values())
        timings["vocab"] = len(inverted)
        return timings

    # ---- 查询 ----

    def connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
            self._conn.execute("PRAGMA query_only=ON")
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def load_meta(self, conn: sqlite3.Connection) -> Dict[str, Any]:
        rows = dict(conn.execute("SELECT key, value FROM meta").fetchall())
        return {
            "num_docs": int(rows["num_docs"]),
            "avg_doc_length": float(rows["avg_doc_length"]),
        }

    def score_map(
        self,
        query_terms: Sequence[str],
        conn: sqlite3.Connection,
        meta: Dict[str, Any] | None = None,
    ) -> Dict[str, float]:
        """返回 chunk_id -> BM25 分数的完整映射（不截断、不排序）。

        **刻意逐词条 SELECT，而不是一条 ``WHERE term IN (...)``。**
        原因：BM25 分数是逐词条累加的浮点加法，不满足结合律。一条 IN 查询返回的
        行按 term 字典序排列，累加顺序就与 JSON 实现（按查询词顺序）不同，
        分数会在最后几位 bit 上分叉，Q2 就无法做位级比对了。
        批量 IN 的性能对照见 :meth:`query_batched`。
        """
        if meta is None:
            meta = self.load_meta(conn)
        avg_dl = meta["avg_doc_length"]

        scores: Dict[str, float] = {}
        for term in (t.lower() for t in query_terms):
            row = conn.execute(
                "SELECT idf FROM terms WHERE term = ?", (term,)
            ).fetchone()
            if row is None:
                continue
            idf = row[0]
            for cid, tf, dlen in conn.execute(
                "SELECT chunk_id, tf, doc_length FROM postings WHERE term = ?",
                (term,),
            ):
                scores[cid] = scores.get(cid, 0.0) + bm25_term_score(
                    tf, dlen, avg_dl, idf
                )
        return scores

    def query(
        self,
        query_terms: Sequence[str],
        top_k: int,
        conn: sqlite3.Connection,
        meta: Dict[str, Any] | None = None,
        tiebreak: bool = False,
    ) -> List[Dict[str, Any]]:
        """按词条查并打分，返回 top-k。

        ``tiebreak=False`` 复刻 ``BM25Indexer.query`` 的现状：只按分数降序排，
        平局顺序取决于 scores 字典的插入顺序。
        ``tiebreak=True`` 加上 chunk_id 升序作次序键，**截断之前**就定死顺序。
        """
        scores = self.score_map(query_terms, conn, meta)
        return rank(scores, top_k, tiebreak)

    def query_batched(
        self,
        query_terms: Sequence[str],
        top_k: int,
        conn: sqlite3.Connection,
        meta: Dict[str, Any] | None = None,
    ) -> List[Dict[str, Any]]:
        """一条 IN 查询版本——只用于性能对照，**不保证与 JSON 位级一致**。"""
        if meta is None:
            meta = self.load_meta(conn)
        avg_dl = meta["avg_doc_length"]
        terms = sorted({t.lower() for t in query_terms})
        if not terms:
            return []
        ph = ",".join("?" * len(terms))
        scores: Dict[str, float] = {}
        for cid, tf, dlen, idf in conn.execute(
            f"SELECT p.chunk_id, p.tf, p.doc_length, t.idf "
            f"FROM postings p JOIN terms t ON t.term = p.term "
            f"WHERE p.term IN ({ph})",
            terms,
        ):
            scores[cid] = scores.get(cid, 0.0) + bm25_term_score(tf, dlen, avg_dl, idf)
        return rank(scores, top_k, tiebreak=False)

    # ---- 删除（设计 §5：解 remove_document 那条 P0）----

    def remove_by_source_path(self, source_path: str) -> int:
        """按源路径删除该文档的全部 postings。返回删除的 posting 行数。

        这正是设计 §5 的主张：JSON 结构里 postings 只有
        ``{chunk_id, tf, doc_length}``，**没有任何能关联回源文档的字段**，
        所以 ``remove_document`` 用 ``chunk_id.startswith(doc_id)`` 恒为 False。
        有了 chunks 表，删除就是两条确定性的语句。
        """
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute("BEGIN")
            n = conn.execute(
                "DELETE FROM postings WHERE chunk_id IN "
                "(SELECT chunk_id FROM chunks WHERE source_path = ?)",
                (source_path,),
            ).rowcount
            conn.execute("DELETE FROM chunks WHERE source_path = ?", (source_path,))
            conn.execute("COMMIT")
            return n
        finally:
            conn.close()

    def count_postings_for_source(self, source_path: str) -> int:
        conn = sqlite3.connect(str(self.db_path))
        try:
            return conn.execute(
                "SELECT COUNT(*) FROM postings WHERE chunk_id IN "
                "(SELECT chunk_id FROM chunks WHERE source_path = ?)",
                (source_path,),
            ).fetchone()[0]
        finally:
            conn.close()


# ---------------------------------------------------------------- BM25 打分

def calc_idf(num_docs: int, df: int) -> float:
    """与 BM25Indexer._calculate_idf 逐字相同。"""
    return math.log((num_docs - df + 0.5) / (df + 0.5))


def bm25_term_score(tf: int, doc_length: int, avg_doc_length: float, idf: float) -> float:
    """与 BM25Indexer._calculate_bm25_score 逐字相同（含 avg=0 的兜底）。"""
    if avg_doc_length == 0:
        avg_doc_length = 1.0
    numerator = tf * (K1 + 1)
    denominator = tf + K1 * (1 - B + B * (doc_length / avg_doc_length))
    return idf * (numerator / denominator)


def rank(scores: Dict[str, float], top_k: int, tiebreak: bool) -> List[Dict[str, Any]]:
    """把分数映射排成 top-k。

    ``tiebreak=False`` 就是 ``BM25Indexer.query`` 现在的做法：只按分数降序。
    Python 的 sorted 是稳定排序，所以同分者的相对顺序 = scores 字典的插入顺序，
    而插入顺序取决于 postings 的物理排列——**这在任何两种存储后端之间都不可能一致**。
    """
    items = [{"chunk_id": c, "score": s} for c, s in scores.items()]
    if tiebreak:
        items.sort(key=lambda x: (-x["score"], x["chunk_id"]))
    else:
        items.sort(key=lambda x: x["score"], reverse=True)
    return items[:top_k]


def json_score_map(
    index: Dict[str, Dict[str, Any]],
    metadata: Dict[str, Any],
    query_terms: Sequence[str],
) -> Dict[str, float]:
    """复刻 ``BM25Indexer.query`` 的打分部分，但返回完整分数映射而非 top-k。

    逐字对齐现有实现的遍历顺序（先按 query_terms 顺序、再按 postings 物理顺序），
    这样浮点累加顺序相同，Q2 才能做位级比对而不是「差值小于 eps」这种糊弄。
    """
    scores: Dict[str, float] = {}
    avg_dl = metadata["avg_doc_length"]
    for term in (t.lower() for t in query_terms):
        td = index.get(term)
        if td is None:
            continue
        idf = td["idf"]
        for p in td["postings"]:
            cid = p["chunk_id"]
            scores[cid] = scores.get(cid, 0.0) + bm25_term_score(
                p["tf"], p["doc_length"], avg_dl, idf
            )
    return scores


# ---------------------------------------------------------------- 参照实现

def build_json_fast(term_stats: List[Dict[str, Any]], collection: str) -> Dict[str, Any]:
    """O(总 postings) 的 JSON 构建，用于把「算法慢」和「JSON 格式慢」拆开。

    现有 ``BM25Indexer.build`` 的两层循环是 O(词表 × 文档数)。如果不拆开，
    Q3 会得出「SQLite 写入比 JSON 快十几倍」这种误导性结论——真实原因是
    对照组里有个二次复杂度的 bug，而不是 SQLite 有多快。
    """
    num_docs = len(term_stats)
    total_length = sum(s["doc_length"] for s in term_stats)
    avg_doc_length = total_length / num_docs if num_docs else 0.0
    index: Dict[str, Dict[str, Any]] = {}
    for stat in term_stats:
        cid, dlen = stat["chunk_id"], stat["doc_length"]
        for term, tf in stat["term_frequencies"].items():
            index.setdefault(term, {"idf": 0.0, "df": 0, "postings": []})
            index[term]["postings"].append(
                {"chunk_id": cid, "tf": tf, "doc_length": dlen}
            )
    for term, data in index.items():
        data["df"] = len(data["postings"])
        data["idf"] = calc_idf(num_docs, data["df"])
    return {
        "metadata": {
            "num_docs": num_docs,
            "avg_doc_length": avg_doc_length,
            "total_terms": len(index),
            "collection": collection,
        },
        "index": index,
    }


# ---------------------------------------------------------------- 对比逻辑

def compare_backends(
    json_scores: Dict[str, float],
    sq_scores: Dict[str, float],
    top_k: int,
) -> Dict[str, Any]:
    """三级比对，别把「平局排列」误判成「打分错了」。

    L1 完整分数映射位级一致 —— 这是真正的语义等价判据：候选集相同、每个候选的
       分数逐 bit 相同。L1 过了就说明 SQLite 后端的打分**没有任何漂移**。
    L2 现状写法（只按分数降序）下 top-k 逐条一致 —— L1 过而 L2 挂，
       说明问题出在**现有实现没有确定性 tie-break**，不是新后端的锅。
    L3 加确定性 tie-break 后 top-k 逐条一致 —— 这是可交付的切换判据。
    """
    same_keys = json_scores.keys() == sq_scores.keys()
    max_diff = 0.0
    bitwise = same_keys
    if same_keys:
        for k, v in json_scores.items():
            d = abs(v - sq_scores[k])
            if d > max_diff:
                max_diff = d
            if v != sq_scores[k]:
                bitwise = False

    l2 = rank(json_scores, top_k, False) == rank(sq_scores, top_k, False)
    l3 = rank(json_scores, top_k, True) == rank(sq_scores, top_k, True)

    # 统计 top-k 截断边界上有多少个同分候选——L2 失败的直接原因
    ranked = rank(json_scores, len(json_scores), True)
    boundary_ties = 0
    if len(ranked) > top_k:
        cutoff = ranked[top_k - 1]["score"]
        boundary_ties = sum(1 for r in ranked if r["score"] == cutoff)

    return {
        "l1_full_score_map_bitwise_identical": bitwise,
        "l1_candidate_sets_identical": same_keys,
        "l2_topk_identical_as_is": l2,
        "l3_topk_identical_with_tiebreak": l3,
        "max_abs_score_diff": max_diff,
        "candidates": len(json_scores),
        "topk_boundary_tied_candidates": boundary_ties,
    }


def rss_mb() -> float:
    """当前进程峰值常驻内存（MB）。

    方案 C 的卖点之一就是内存，所以原型自己也要报内存，否则「≈0 常驻」
    同样是没实测的推断。macOS 上 ru_maxrss 单位是字节，Linux 上是 KB。
    """
    import resource

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(peak / (1024**2 if sys.platform == "darwin" else 1024), 1)


def current_rss_mb() -> float:
    """当前（非峰值）常驻内存 MB —— 用来量「持有一个索引要占多少」。

    ru_maxrss 是峰值、只增不减，量不出「放掉之后回落多少」，
    所以常驻内存必须用当前值。优先 psutil，没有就退回 macOS 的 `ps`。
    """
    try:
        import psutil  # type: ignore

        return round(psutil.Process().memory_info().rss / 1024**2, 1)
    except Exception:
        try:
            out = subprocess.run(
                ["ps", "-o", "rss=", "-p", str(__import__("os").getpid())],
                capture_output=True, text=True, check=True,
            ).stdout.strip()
            return round(int(out) / 1024, 1)
        except Exception:
            return float("nan")


def _probe_child(mode: str, json_dir: Path, collection: str, db_path: Path,
                 queries: List[Dict[str, Any]], top_k: int) -> int:
    """子进程模式：只做一件事，然后报告 RSS。结果以 JSON 打到 stdout。

    导入放在取 baseline **之前**：否则 BM25Indexer 那条 import 拉进来的模块
    会被算成「索引占的内存」，把 JSON 侧的数字虚高。
    """
    from src.ingestion.storage.bm25_indexer import BM25Indexer  # noqa: F401

    gc.collect()
    base = current_rss_mb()
    if mode == "json":
        idx = BM25Indexer(index_dir=str(json_dir))
        idx.load(collection)
        for q in queries:
            idx.query(list(q["terms"]), top_k=top_k)
    else:
        db = SqliteBM25(db_path)
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        meta = db.load_meta(conn)
        for q in queries:
            db.query(q["terms"], top_k, conn, meta)
    print("__PROBE__" + json.dumps({
        "baseline_mb": base, "after_mb": current_rss_mb(), "peak_mb": rss_mb(),
    }))
    return 0


def measure_resident_cost(
    json_dir: Path,
    collection: str,
    db: "SqliteBM25",
    queries: List[Dict[str, Any]],
    top_k: int,
) -> Dict[str, Any]:
    """量「常驻一个 collection 的索引要多少内存」——设计 §3 最关键的那一列。

    设计文档说方案 A 常驻 ≈12 GB、方案 C ≈0，两个都是推算，这里实测。

    **必须用全新子进程量，不能在主进程里量。** 第一版就是在主进程里取 RSS 差值，
    结果 16K 档量出 -608 MB：主进程此前已经反复分配/释放过上百 MB 的索引，
    分配器把空闲页留在进程里不还给 OS，RSS 差值反映的是碎片而不是索引占用。
    子进程从干净状态起步，只做一件事，差值才是真的。
    """
    out: Dict[str, Any] = {}
    payload = [{"terms": q["terms"]} for q in queries]
    for mode in ("json", "sqlite"):
        spec = json.dumps({
            "mode": mode, "json_dir": str(json_dir), "collection": collection,
            "db_path": str(db.db_path), "top_k": top_k, "queries": payload,
        })
        r = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--probe", spec],
            capture_output=True, text=True,
        )
        line = next((l for l in r.stdout.splitlines()
                     if l.startswith("__PROBE__")), None)
        if line is None:
            out[f"{mode}_resident_mb"] = None
            out[f"{mode}_probe_error"] = (r.stderr or r.stdout)[-400:]
            continue
        d = json.loads(line[len("__PROBE__"):])
        out[f"{mode}_baseline_mb"] = d["baseline_mb"]
        out[f"{mode}_resident_mb"] = round(d["after_mb"] - d["baseline_mb"], 1)
        out[f"{mode}_peak_mb"] = d["peak_mb"]
    # 与磁盘体积的比值：JSON 反序列化成 Python 对象会显著膨胀，这个倍数是关键
    return out


def timed(fn: Callable[[], Any], repeat: int) -> Tuple[List[float], Any]:
    out = None
    ts: List[float] = []
    for _ in range(repeat):
        t0 = time.perf_counter()
        out = fn()
        ts.append(time.perf_counter() - t0)
    return ts, out


def stats_ms(ts: List[float]) -> Dict[str, float]:
    s = sorted(ts)
    return {
        "median_ms": round(statistics.median(s) * 1000, 3),
        "min_ms": round(s[0] * 1000, 3),
        "max_ms": round(s[-1] * 1000, 3),
    }


# ---------------------------------------------------------------- 单个规模点

def run_size(
    n: int,
    workdir: Path,
    queries: List[Dict[str, Any]],
    top_k: int,
    repeat_fast: int,
    repeat_slow: int,
    real_chunks: List[str],
    skip_json_build: bool,
) -> Dict[str, Any]:
    import random

    from src.ingestion.embedding.sparse_encoder import SparseEncoder
    from src.ingestion.storage.bm25_indexer import BM25Indexer

    print(f"\n{'='*74}\n── {n:,} 块 ──", flush=True)
    rng = random.Random(20260825)
    texts, used_real = build_corpus(n, rng, real_chunks)
    chunk_ids, source_paths, doc_hashes = make_chunk_ids(n)
    chunk_meta = {
        cid: (dh, sp) for cid, sp, dh in zip(chunk_ids, source_paths, doc_hashes)
    }

    class _C:
        __slots__ = ("id", "text")

        def __init__(self, i, t):
            self.id, self.text = i, t

    encoder = SparseEncoder()
    t0 = time.perf_counter()
    term_stats = encoder.encode([_C(cid, t) for cid, t in zip(chunk_ids, texts)])
    encode_s = time.perf_counter() - t0
    print(f"   encode {encode_s:.2f}s", flush=True)

    collection = f"_bm25proto_{n}"
    json_dir = workdir / "json" / collection
    json_dir.mkdir(parents=True, exist_ok=True)
    row: Dict[str, Any] = {
        "chunks": n,
        "real_chunk_count": used_real,
        "encode_s": round(encode_s, 2),
    }

    # ---------- Q3-a：现有 JSON 实现的建索引耗时 ----------
    indexer = BM25Indexer(index_dir=str(json_dir))
    if skip_json_build:
        print("   [跳过] 现有 BM25Indexer.build（--skip-json-build）", flush=True)
        data = build_json_fast(term_stats, collection)
        (json_dir / f"{collection}_bm25.json").write_text(
            json.dumps(data, indent=2, ensure_ascii=False)
        )
        row["json_build_s"] = None
        row["json_build_skipped"] = True
    else:
        t0 = time.perf_counter()
        indexer.build(term_stats, collection=collection)
        row["json_build_s"] = round(time.perf_counter() - t0, 2)
        row["json_build_skipped"] = False
        print(f"   JSON build（现有实现，O(词表×文档)） {row['json_build_s']:.2f}s",
              flush=True)

    json_path = json_dir / f"{collection}_bm25.json"
    row["json_bytes"] = json_path.stat().st_size
    row["json_mb"] = round(row["json_bytes"] / 1024**2, 2)
    # build() 之后 indexer 手里还攥着一份完整索引；下面 warm 还要从磁盘再读一份。
    # 两份同时在内存里是首轮跑挂掉的主因之一，这里主动放掉。
    indexer._index, indexer._metadata = {}, {}  # noqa: SLF001
    gc.collect()

    # ---------- Q3-b：把「算法慢」和「格式慢」拆开 ----------
    # 注意内存：这一段会同时持有 term_stats、indexer._index、fast_data 三份索引，
    # 16K 块以上就会把 16G 机器压到交换区（首轮跑就是死在这里）。
    # 所以每份用完立刻释放，并在下面报 RSS。
    t0 = time.perf_counter()
    fast_data = build_json_fast(term_stats, collection)
    row["json_build_fast_invert_s"] = round(time.perf_counter() - t0, 2)
    row["vocab_terms"] = fast_data["metadata"]["total_terms"]
    t0 = time.perf_counter()
    tmp_json = workdir / "json" / f"{collection}_fast.json"
    tmp_json.write_text(json.dumps(fast_data, indent=2, ensure_ascii=False))
    row["json_dump_s"] = round(time.perf_counter() - t0, 2)
    t0 = time.perf_counter()
    tmp_compact = workdir / "json" / f"{collection}_compact.json"
    tmp_compact.write_text(json.dumps(fast_data, ensure_ascii=False,
                                      separators=(",", ":")))
    row["json_dump_compact_s"] = round(time.perf_counter() - t0, 2)
    row["json_compact_mb"] = round(tmp_compact.stat().st_size / 1024**2, 2)
    del fast_data
    tmp_json.unlink(missing_ok=True)
    tmp_compact.unlink(missing_ok=True)
    gc.collect()
    print(f"   JSON build（单遍算法）        {row['json_build_fast_invert_s']:.2f}s "
          f"+ dump {row['json_dump_s']:.2f}s", flush=True)

    # ---------- Q3-c：SQLite 建库（两种表结构） ----------
    sq_variants: Dict[str, SqliteBM25] = {}
    for variant in ("clustered", "rowid"):
        db = SqliteBM25(workdir / "sqlite" / f"{collection}_{variant}.db", variant)
        t = db.build(term_stats, collection, chunk_meta)
        sq_variants[variant] = db
        row[f"sqlite_{variant}_build_s"] = round(t["total_s"], 2)
        row[f"sqlite_{variant}_invert_s"] = round(t["invert_s"], 2)
        row[f"sqlite_{variant}_write_s"] = round(t["write_s"], 2)
        row[f"sqlite_{variant}_mb"] = round(db.db_path.stat().st_size / 1024**2, 2)
        row["postings_rows"] = t["postings_rows"]
        print(f"   SQLite build [{variant:>9}]      {t['total_s']:.2f}s "
              f"→ {row[f'sqlite_{variant}_mb']:.2f} MB", flush=True)
        gc.collect()

    main_db = sq_variants["clustered"]
    # term_stats / chunk_meta 到这里已经没人要了，查询阶段不需要
    del term_stats, chunk_meta, texts
    gc.collect()
    row["peak_rss_after_build_mb"] = rss_mb()
    print(f"   建索引阶段峰值 RSS {row['peak_rss_after_build_mb']:.1f} MB", flush=True)

    # ---------- Q1：查询延迟 ----------
    # 路径 A（生产现状）：每次查询都 load() 整个 JSON，再打分
    def _json_full(qterms):
        idx = BM25Indexer(index_dir=str(json_dir))
        idx.load(collection)
        return idx.query(list(qterms), top_k=top_k)

    # 路径 B：JSON 已在内存（方案 A「缓存」的上限），只算打分本身
    warm = BM25Indexer(index_dir=str(json_dir))
    warm.load(collection)

    def _json_warm(qterms):
        return warm.query(list(qterms), top_k=top_k)

    # 路径 C：SQLite + 复用连接（方案 C 的目标形态）
    conn = main_db.connect()
    meta = main_db.load_meta(conn)

    def _sq_warm(qterms):
        return main_db.query(qterms, top_k, conn, meta)

    # 路径 D：SQLite + 每次新建连接（对应现在「无任何缓存」的写法）
    def _sq_cold(qterms):
        c = sqlite3.connect(f"file:{main_db.db_path}?mode=ro", uri=True)
        try:
            return main_db.query(qterms, top_k, c)
        finally:
            c.close()

    # 路径 E：SQLite rowid 表结构（对照，证明聚簇的必要性）
    rowid_db = sq_variants["rowid"]
    rconn = rowid_db.connect()
    rmeta = rowid_db.load_meta(rconn)

    def _sq_rowid(qterms):
        return rowid_db.query(qterms, top_k, rconn, rmeta)

    # 路径 F：批量 IN（性能对照，不保证位级一致）
    def _sq_batched(qterms):
        return main_db.query_batched(qterms, top_k, conn, meta)

    paths = [
        ("json_load_per_query", _json_full, repeat_slow),
        ("json_warm_in_memory", _json_warm, repeat_fast),
        ("sqlite_clustered_warm_conn", _sq_warm, repeat_fast),
        ("sqlite_clustered_new_conn", _sq_cold, repeat_fast),
        ("sqlite_rowid_warm_conn", _sq_rowid, repeat_fast),
        ("sqlite_batched_in", _sq_batched, repeat_fast),
    ]

    per_path: Dict[str, Any] = {}
    results_cache: Dict[str, List[List[Dict[str, Any]]]] = {}
    for name, fn, rep in paths:
        all_ts: List[float] = []
        outs: List[List[Dict[str, Any]]] = []
        for q in queries:
            ts, out = timed(lambda: fn(q["terms"]), rep)
            all_ts.extend(ts)
            outs.append(out)
        per_path[name] = stats_ms(all_ts)
        per_path[name]["total_samples"] = len(all_ts)
        results_cache[name] = outs
        print(f"   查询 [{name:<27}] 中位 {per_path[name]['median_ms']:>9.3f} ms",
              flush=True)
    row["query_latency"] = per_path

    # ---------- Q2：结果一致性 ----------
    # 用完整分数映射比对，而不是只比 top-k：top-k 在平局处的截断本身就不确定，
    # 只比 top-k 会把「现有实现缺 tie-break」误记成「新后端打分不一致」。
    cmp_rows = []
    for q in queries:
        js = json_score_map(warm._index, warm._metadata, q["terms"])  # noqa: SLF001
        ss = main_db.score_map(q["terms"], conn, meta)
        c = compare_backends(js, ss, top_k)
        c["query"] = q["text"]
        c["n_terms"] = len(q["terms"])
        cmp_rows.append(c)
    nq = len(cmp_rows)
    row["consistency"] = {
        "queries": nq,
        "l1_full_score_map_bitwise_identical":
            sum(r["l1_full_score_map_bitwise_identical"] for r in cmp_rows),
        "l1_candidate_sets_identical":
            sum(r["l1_candidate_sets_identical"] for r in cmp_rows),
        "l2_topk_identical_as_is": sum(r["l2_topk_identical_as_is"] for r in cmp_rows),
        "l3_topk_identical_with_tiebreak":
            sum(r["l3_topk_identical_with_tiebreak"] for r in cmp_rows),
        "max_abs_score_diff": max(r["max_abs_score_diff"] for r in cmp_rows),
        "per_query": cmp_rows,
    }
    cc = row["consistency"]
    print(f"   一致性 L1 完整分数位级一致 {cc['l1_full_score_map_bitwise_identical']}/{nq}"
          f" | L2 现状 top-k 一致 {cc['l2_topk_identical_as_is']}/{nq}"
          f" | L3 加 tie-break 后 top-k 一致 {cc['l3_topk_identical_with_tiebreak']}/{nq}"
          f" | 最大分数差 {cc['max_abs_score_diff']:.3e}", flush=True)

    # ---------- 命中 postings 规模（Q1 的解释变量） ----------
    hit = []
    for q in queries:
        tot = 0
        for t in {x.lower() for x in q["terms"]}:
            r = conn.execute("SELECT df FROM terms WHERE term=?", (t,)).fetchone()
            if r:
                tot += r[0]
        hit.append(tot)
    row["query_postings_scanned"] = {
        "median": int(statistics.median(hit)),
        "max": max(hit),
        "min": min(hit),
    }
    print(f"   每次查询扫描 postings 中位 {row['query_postings_scanned']['median']:,} 条"
          f" / 词表 {row['vocab_terms']:,}", flush=True)

    # ---------- T-3：删除（设计 §5 的 P0） ----------
    victim = source_paths[0]
    before = main_db.count_postings_for_source(victim)
    # 现有 JSON 实现：传 doc_hash。这正是 document_manager.py:201 的真实调用方式
    # （它传的是 source_hash，即文档内容的 SHA256）。
    # 复用 warm（已在内存里），避免再 load 一份索引。
    json_removed = warm.remove_document(doc_hashes[0], collection=collection)
    deleted = main_db.remove_by_source_path(victim)
    after = main_db.count_postings_for_source(victim)
    row["deletion"] = {
        "source_path": victim,
        "postings_before": before,
        "sqlite_deleted_rows": deleted,
        "postings_after": after,
        "sqlite_ok": before > 0 and after == 0,
        "json_remove_document_returned": json_removed,
    }
    print(f"   删除: SQLite {before} → {after} 条 (删除 {deleted}); "
          f"现有 JSON remove_document 返回 {json_removed}", flush=True)

    # ---------- 常驻内存：方案 A vs 方案 C ----------
    main_db.close()
    rowid_db.close()
    warm._index, warm._metadata = {}, {}  # noqa: SLF001
    del results_cache
    gc.collect()
    row["resident"] = measure_resident_cost(json_dir, collection, main_db,
                                            queries, top_k)
    _res = row["resident"]
    print(f"   常驻内存(子进程实测): JSON 索引 +{_res.get('json_resident_mb')} MB"
          f" vs SQLite +{_res.get('sqlite_resident_mb')} MB"
          f"（索引文件 {row['json_mb']:.1f} MB / {row['sqlite_clustered_mb']:.1f} MB）",
          flush=True)

    row["peak_rss_mb"] = rss_mb()
    print(f"   本档峰值 RSS {row['peak_rss_mb']:.1f} MB（含 JSON 对照组，"
          f"不代表方案 C 的常驻）", flush=True)
    gc.collect()
    return row


# ---------------------------------------------------------------- 主流程

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizes", default="1000,16000,50000")
    ap.add_argument("--top-k", type=int, default=10)
    ap.add_argument("--repeat-fast", type=int, default=5)
    ap.add_argument("--repeat-slow", type=int, default=2)
    ap.add_argument("--workdir", default="")
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--skip-json-build", action="store_true",
                    help="跳过现有 BM25Indexer.build（50K 块要跑 ~8 分钟）")
    ap.add_argument("--out", default="")
    ap.add_argument("--probe", default="",
                    help="内部使用：子进程内存探针，见 measure_resident_cost")
    args = ap.parse_args()

    if args.probe:
        spec = json.loads(args.probe)
        return _probe_child(spec["mode"], Path(spec["json_dir"]),
                            spec["collection"], Path(spec["db_path"]),
                            spec["queries"], spec["top_k"])

    sizes = [int(s) for s in args.sizes.split(",") if s.strip()]
    workdir = Path(args.workdir) if args.workdir else Path(
        tempfile.mkdtemp(prefix="bm25proto_"))
    workdir.mkdir(parents=True, exist_ok=True)
    print(f"工作目录: {workdir}")

    from src.ingestion.embedding.sparse_encoder import SparseEncoder

    real = load_real_chunks(limit=4000)
    queries = tokenize_queries(SparseEncoder())
    print(f"真实语料块: {len(real)} 条；查询 {len(queries)} 条，"
          f"词条数 {[len(q['terms']) for q in queries]}")

    rows = [
        run_size(n, workdir, queries, args.top_k, args.repeat_fast,
                 args.repeat_slow, real, args.skip_json_build)
        for n in sizes
    ]

    # ---------- 汇总 ----------
    q1_path = "sqlite_clustered_warm_conn"   # 方案 C 的目标形态，判定以它为准
    print(f"\n{'='*100}\n汇总：查询延迟中位数（ms）\n")
    paths = list(rows[0]["query_latency"].keys())
    hdr = f"{'路径':<30}" + "".join(f"{r['chunks']:>13,}" for r in rows)
    print(hdr + f"{'  最大/最小倍数':>14}")
    print("-" * (len(hdr) + 14))
    scaling: Dict[str, float] = {}
    for p in paths:
        vals = [r["query_latency"][p]["median_ms"] for r in rows]
        ratio = vals[-1] / vals[0] if vals[0] > 0 else float("nan")
        scaling[p] = round(ratio, 2)
        print(f"{p:<30}" + "".join(f"{v:>13.3f}" for v in vals) + f"{ratio:>13.1f}x")

    # Q1 的解释变量：延迟到底跟什么成正比——词表？还是命中的 postings 条数？
    print(f"\n{'='*100}\n汇总：延迟的解释变量\n")
    print(f"{'块数':>8} {'词表':>9} {'扫描postings中位':>18} "
          f"{'SQLite中位ms':>13} {'每千条postings ms':>18}")
    for r in rows:
        scanned = r["query_postings_scanned"]["median"]
        ms = r["query_latency"][q1_path]["median_ms"]
        print(f"{r['chunks']:>8,} {r['vocab_terms']:>9,} {scanned:>18,} "
              f"{ms:>13.3f} {ms / max(scanned, 1) * 1000:>18.3f}")

    print(f"\n{'='*100}\n汇总：索引体积（MB）与建索引耗时（s）\n")
    print(f"{'块数':>8} {'JSON(indent2)':>14} {'JSON(compact)':>14} "
          f"{'SQLite聚簇':>12} {'SQLite rowid':>13} | "
          f"{'JSON build现有':>15} {'JSON build单遍':>15} {'SQLite build':>13}")
    for r in rows:
        jb = "跳过" if r["json_build_s"] is None else f"{r['json_build_s']:.2f}"
        print(f"{r['chunks']:>8,} {r['json_mb']:>14.2f} {r['json_compact_mb']:>14.2f} "
              f"{r['sqlite_clustered_mb']:>12.2f} {r['sqlite_rowid_mb']:>13.2f} | "
              f"{jb:>15} "
              f"{r['json_build_fast_invert_s'] + r['json_dump_s']:>15.2f} "
              f"{r['sqlite_clustered_build_s']:>13.2f}")

    # ---------- 判定 ----------
    q1_ratio = scaling[q1_path]
    q1_abs = rows[-1]["query_latency"][q1_path]["median_ms"]
    # 判据取设计文档 T-2 的 <20%（即倍数 < 1.2）
    q1_verdict = "通过" if q1_ratio < 1.2 else "未通过（延迟随规模增长）"
    _allq = lambda k: all(  # noqa: E731
        r["consistency"][k] == r["consistency"]["queries"] for r in rows)
    q2_l1 = _allq("l1_full_score_map_bitwise_identical")
    q2_l2 = _allq("l2_topk_identical_as_is")
    q2_l3 = _allq("l3_topk_identical_with_tiebreak")
    max_diff = max(r["consistency"]["max_abs_score_diff"] for r in rows)
    del_ok = all(r["deletion"]["sqlite_ok"] for r in rows)
    json_del_ok = all(r["deletion"]["json_remove_document_returned"] for r in rows)

    # 相对 JSON 现状路径的加速比
    speedup = [
        round(r["query_latency"]["json_load_per_query"]["median_ms"]
              / max(r["query_latency"][q1_path]["median_ms"], 1e-9), 1)
        for r in rows
    ]

    print(f"\n{'='*100}\n判定\n")
    print(f"Q1 查询延迟与规模无关？ {q1_verdict}")
    print(f"   最小→最大规模倍数 {q1_ratio}x；最大规模绝对值 {q1_abs:.3f} ms；"
          f"相对现状 json_load_per_query 提速 {speedup}")
    print(f"Q2 打分一致？ L1 完整分数映射位级一致={q2_l1}（最大绝对差 {max_diff:.3e}）")
    print(f"   L2 现状 top-k 逐条一致={q2_l2}   L3 加确定性 tie-break 后={q2_l3}")
    print(f"Q3 写入性能：见上表")
    print(f"T-3 删除：SQLite={'通过' if del_ok else '未通过'}；"
          f"现有 JSON remove_document 返回 True？{json_del_ok}（False = P0 复现）")

    out = Path(args.out or
               f"scripts/benchmark_results/bm25_sqlite_proto_"
               f"{datetime.now():%Y%m%d_%H%M%S}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "generated_at": datetime.now().isoformat(),
        "script": str(Path(__file__).resolve()),
        "code_state": subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                                     capture_output=True, text=True).stdout.strip(),
        "params": {
            "sizes": sizes, "top_k": args.top_k,
            "repeat_fast": args.repeat_fast, "repeat_slow": args.repeat_slow,
            "k1": K1, "b": B, "chunks_per_doc": CHUNKS_PER_DOC,
            "skip_json_build": args.skip_json_build,
        },
        "queries": [{"text": q["text"], "terms": q["terms"]} for q in queries],
        "real_corpus_chunks": len(real),
        "measurements": rows,
        "latency_scaling_min_to_max_size": scaling,
        "speedup_vs_json_load_per_query": speedup,
        "verdict": {
            "q1_latency_independent_of_size": q1_ratio < 1.2,
            "q1_ratio_min_to_max_size": q1_ratio,
            "q1_max_size_median_ms": q1_abs,
            "q2_l1_full_score_map_bitwise_identical": q2_l1,
            "q2_l2_topk_identical_as_is": q2_l2,
            "q2_l3_topk_identical_with_tiebreak": q2_l3,
            "q2_max_abs_score_diff": max_diff,
            "t3_sqlite_delete_works": del_ok,
            "t3_json_remove_document_works": json_del_ok,
        },
        "not_covered": [
            "只单进程单线程测，未测多库并行查询 / 多进程并发读同一 db（设计 §9 第 2 条仍未覆盖）",
            "未测 OS page cache 冷启动：macOS 下无法在不 sudo 的前提下清 page cache，"
            "所有 SQLite 数字都建立在文件已被 OS 缓存的前提上；首查冷盘会更慢",
            "未测增量摄入（add_documents）路径，只测了全量 build",
            "语料沿用 measure_bm25_index_growth 的口径：约 1/3 仓库真实中文文本、"
            "2/3 模板扩充；模板文本重复度高，会低估真实词表、也会制造偏多的分数平局",
            "未接 SparseRetriever / query_knowledge_hub，端到端 TTFT 未测",
            "未测 SQLite 文件损坏、并发写、WAL 模式下的表现（建库用的是 journal_mode=OFF）",
            "未测 50K 以上规模；143K/716K 仍是外推",
            "建库时倒排表整个驻留内存（50K 块约几百 MB），大规模摄入需要改成分批落盘，本原型未验证",
        ],
    }, ensure_ascii=False, indent=2))
    print(f"\n结果写入 {out}")

    if not args.keep:
        shutil.rmtree(workdir, ignore_errors=True)
        print(f"已清理 {workdir}")
    else:
        print(f"保留工作目录 {workdir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
