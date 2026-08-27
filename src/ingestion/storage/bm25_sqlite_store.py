"""BM25 索引的 SQLite 后端 —— `docs/bm25_storage_design.md` 方案 C 的阶段 1。

⚠️ **模块级这句"读仍然全部走 JSON"已过时**（本段以下、写于阶段 1 落地时，
之后没有回来同步）：阶段 2/3 已把读路径接上（`CLAUDE.md` §4 第 2c 条），
`BM25Indexer.load()` 在 `auto`/`sqlite` 后端下会真的调 `query()` 走这里。
以下三段描述的仍是阶段 1 落地时的动机与选型依据，本身没有错，只是"当前
阶段"这个措辞已经滞后，勿以此判断本模块是否在生产读路径上。

**当前阶段：影子写（双写）。读仍然全部走 JSON。**
本模块被写入，但还没有任何生产查询路径读它。这是刻意的：阶段 1 的目的是
让两种后端的数据先并存，好在切读之前逐条比对打分结果（设计文档 §6）。

## 为什么要换掉 JSON

现状是每次查询 `json.load` 整个索引文件。实测（`scripts/measure_bm25_index_growth.py`）
143K 块 → 672 MB / 5.0s，6 库企业每次提问要加载 6 次、内存峰值约 4 GB，
而 TTFT SLO 是 3s。真正的浪费是「为了用 20 个查询词条，把 70,000 个词条
全部反序列化」。

⚠️ **但换成词条级查询并不会让延迟与规模无关** —— 这条是设计文档最初的核心
论据，已被原型实测推翻（`scripts/prototype_bm25_sqlite.py`）：SQLite 查询
1K/16K/50K 块 = 0.204 / 2.719 / 8.522 ms，α≈0.95 近似线性。原因是每查询
扫描的 postings 条数稳定在 0.327 × 块数 —— 省掉的是「读那 7 万个用不到的
词条」，省不掉的是「高频词自己的 postings 越来越长」。

**真实收益是三条，都不是复杂度降级**：
1. 常数因子降两个数量级（对同一规模提速 139–187 倍）；
2. 常驻内存从随索引线性增长变成有上界（1417 MB → 13.2 MB）；
3. `remove_document` 那条 P0 第一次有解（见下）。

## schema 选型有实测依据

`postings` 用 `PRIMARY KEY(term, chunk_id) WITHOUT ROWID`，即按 term 聚簇存储，
一个词条的 postings 物理连续。对照组（普通 rowid 表 + term 上建二级索引）
在 50K 档实测更慢：查询 8.809 ms vs 8.522 ms、建库 5.63s vs 4.15s。
差距不大但方向一致，且聚簇变体省掉一整个二级索引的空间。

## chunks 表是 `remove_document` 能修好的关键

现有 JSON 实现用 `chunk_id.startswith(doc_id)` 匹配要删的 postings，但 chunk_id
形如 `65046ad1_0000_2a3ac7ab`（源路径哈希前 8 位 + 序号 + 内容哈希），
传进来的 doc_id 却是**文件内容的 64 字符 SHA256** —— 22 字符的串永远不可能
以 64 字符的串开头，**恒返回 False**（原型三轮独立复现）。
这里改为显式记录 `chunks.doc_hash`，删除变成一条按 doc_hash 的 DELETE，
不再依赖字符串前缀这种脆弱约定。
"""

from __future__ import annotations

import logging
import math
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = "1"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS terms(
    term TEXT PRIMARY KEY,
    idf  REAL    NOT NULL,
    df   INTEGER NOT NULL
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS chunks(
    chunk_id    TEXT PRIMARY KEY,
    doc_hash    TEXT,
    source_path TEXT
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_chunks_dochash ON chunks(doc_hash);
CREATE INDEX IF NOT EXISTS ix_chunks_source  ON chunks(source_path);
CREATE TABLE IF NOT EXISTS postings(
    term       TEXT    NOT NULL,
    chunk_id   TEXT    NOT NULL,
    tf         INTEGER NOT NULL,
    doc_length INTEGER NOT NULL,
    PRIMARY KEY(term, chunk_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_postings_chunk ON postings(chunk_id);
"""


class BM25SQLiteStore:
    """一个 collection 一个 `.sqlite` 文件。

    单文件 per collection 是已拍板的决定（设计文档 §8 第 2 条）：多租户隔离
    直接落在文件系统上、删库就是删文件、和现有 `data/db/bm25/{collection}`
    布局一致。**不要合并成一个全局库** —— `CLAUDE.md` §3.3 把「物理隔离」
    列为已验证成立的隔离保证，合并会把它降级成「靠 WHERE 条件隔离」。
    """

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self._conn: Optional[sqlite3.Connection] = None

    # ────────────────────────────── 连接 ──────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self.db_path))
            # 写入是批量重建，崩了重建即可，不值得为它付 fsync 的钱。
            # 读路径另有 query_only 连接，不受这里影响。
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA cache_size=-64000")  # 64MB page cache
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "BM25SQLiteStore":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ────────────────────────────── 写入 ──────────────────────────────

    def replace_all(
        self,
        index: Dict[str, Dict[str, Any]],
        metadata: Dict[str, Any],
        doc_hash_by_chunk: Optional[Dict[str, str]] = None,
    ) -> None:
        """用内存索引整体覆盖 SQLite 侧的 terms/postings。

        阶段 1 的双写语义是**逐条镜像 JSON 侧的结果**，不是"更聪明地增量写"。
        这是刻意的：只有当两边由同一份 `_index` 生成时，"打分是否逐 bit 相同"
        这个比对才有意义。增量写入（真正的性能收益）留到切读之后再做，
        否则一旦两边打分对不上，无法区分是后端算错了还是增量逻辑漏了。

        `doc_hash_by_chunk` 只覆盖**本次新增**的 chunk。老 chunk 的 doc_hash
        必须跨重建保留下来 —— 上层 `build()` 是全量重建，会丢掉出处信息，
        所以这里对 chunks 表做 UPSERT 而不是先清空再写。

        ⚠️ **不需要记录 postings 在 term_stats 里的原始顺序**（本实现一度加过
        一个 `chunks.ord` 列干这个，已删）。理由不显然，写在这里免得有人再加：
        单个 chunk 的分数 = 各查询词贡献之和，而这个和的累加顺序**只由外层
        `for term in query_terms` 决定** —— 词条内 postings 的先后只影响
        "哪个 chunk 先拿到这一项"，不改变任何单个 chunk 自身的累加序列。
        两种后端的外层循环都按 `query_terms` 原序走，所以逐 bit 等价天然成立。
        （浮点加法确实不满足结合律：实测 20 万组真实 BM25 分数、朴素累加下
        62.8% 的组合重排后会差 1 ULP。所以担心是对的，只是落点不在这里。）
        """
        conn = self._connect()
        chunk_lengths: Dict[str, int] = {}
        posting_rows: List[Tuple[str, str, int, int]] = []
        term_rows: List[Tuple[str, float, int]] = []

        for term, term_data in index.items():
            term_rows.append((term, float(term_data["idf"]), int(term_data["df"])))
            for posting in term_data["postings"]:
                cid = posting["chunk_id"]
                dl = int(posting["doc_length"])
                chunk_lengths[cid] = dl
                posting_rows.append((term, cid, int(posting["tf"]), dl))

        with conn:  # 单事务：要么整份索引都换掉，要么一条都不动
            conn.executescript(_SCHEMA)
            conn.execute("DELETE FROM terms")
            conn.execute("DELETE FROM postings")
            conn.executemany(
                "INSERT INTO terms(term, idf, df) VALUES(?, ?, ?)", term_rows
            )
            conn.executemany(
                "INSERT INTO postings(term, chunk_id, tf, doc_length) "
                "VALUES(?, ?, ?, ?)",
                posting_rows,
            )

            # chunks：补齐本次索引里出现的所有 chunk，并清掉已不在索引里的。
            # INSERT OR IGNORE 保证既有 doc_hash 不被这次重建抹掉。
            conn.executemany(
                "INSERT OR IGNORE INTO chunks(chunk_id, doc_hash, source_path) "
                "VALUES(?, NULL, NULL)",
                [(cid,) for cid in chunk_lengths],
            )
            conn.execute(
                "CREATE TEMP TABLE IF NOT EXISTS _live(chunk_id TEXT PRIMARY KEY)"
            )
            conn.execute("DELETE FROM _live")
            conn.executemany(
                "INSERT INTO _live(chunk_id) VALUES(?)",
                [(cid,) for cid in chunk_lengths],
            )
            conn.execute(
                "DELETE FROM chunks WHERE chunk_id NOT IN (SELECT chunk_id FROM _live)"
            )

            if doc_hash_by_chunk:
                conn.executemany(
                    "UPDATE chunks SET doc_hash = ? WHERE chunk_id = ?",
                    [
                        (dh, cid)
                        for cid, dh in doc_hash_by_chunk.items()
                        if cid in chunk_lengths
                    ],
                )

            conn.executemany(
                "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
                [
                    ("schema_version", _SCHEMA_VERSION),
                    ("num_docs", str(metadata.get("num_docs", 0))),
                    ("avg_doc_length", repr(float(metadata.get("avg_doc_length", 0.0)))),
                    ("total_terms", str(metadata.get("total_terms", len(term_rows)))),
                    ("collection", str(metadata.get("collection", ""))),
                ],
            )

    def delete_by_doc_hash(self, doc_hash: str) -> int:
        """按文档哈希删除该文档的全部 postings，返回删掉的条数。

        这是现有 `BM25Indexer.remove_document` 恒返回 False 那条 P0 的正解：
        不再拿 chunk_id 去和 doc_id 做字符串前缀匹配，而是查 chunks 表。

        ⚠️ 只删 postings 与 chunks 行，**不重算 idf/df**。阶段 1 里 SQLite 是
        影子副本，权威的 idf/df 由 JSON 侧全量重建后经 `replace_all` 覆盖过来。
        切读之前必须补上重算，否则删除后打分会偏 —— 已记在设计文档待办里。
        """
        conn = self._connect()
        with conn:
            conn.executescript(_SCHEMA)
            cur = conn.execute(
                "DELETE FROM postings WHERE chunk_id IN "
                "(SELECT chunk_id FROM chunks WHERE doc_hash = ?)",
                (doc_hash,),
            )
            deleted = cur.rowcount or 0
            conn.execute("DELETE FROM chunks WHERE doc_hash = ?", (doc_hash,))
        return deleted

    # ────────────────────────────── 读取 ──────────────────────────────

    def query(
        self,
        query_terms: List[str],
        top_k: int = 10,
        k1: float = 1.5,
        b: float = 0.75,
    ) -> List[Dict[str, Any]]:
        """词条级检索，打分**下推进 SQL**（`CLAUDE.md` §4 第 2e 条 P0 的修法）。

        为什么下推：Python 侧逐行取数再累加时，热词查询一次要跨
        `sqlite3_step()` 边界（= GIL 释放/重取一次）与 postings 条数同阶
        （50K 档实测约 4 万次）；6 线程并发命中同一批热词时，这个边界交接
        次数会引发 GIL convoy——6 线程比单线程还慢（`scripts/
        benchmark_bm25_backends.py` 诊断实测 2633ms vs 25ms）。把 `GROUP BY
        chunk_id ... ORDER BY ... LIMIT top_k` 整段丢给 SQLite，跨边界的行数
        从「postings 总数」降到「top_k」，convoy 随之消失。

        **与诊断脚本 `_sql_side_query`（scripts/benchmark_bm25_backends.py）
        的关键差异**：诊断版本用 `WHERE term IN (...)`，这是去重语义 ——
        同一个查询词出现两次只会贡献一次分数，与
        `BM25Indexer.query`「`for term in query_terms` 不去重，重复词条要
        重复累加」的既有语义不等价。这里改用 `WITH q(term, mult) AS
        (VALUES ...)` 把「每个词条在本次查询里出现几次」当一个乘数交给
        SQL，`SUM(q.mult * 单词条贡献)` 还原重复累加的效果。

        单行打分表达式与 `BM25Indexer._calculate_bm25_score` 逐项对应
        （`numerator = tf*(k1+1)`、`denominator = tf + k1*((1-b) + b*(dl/avgdl))`、
        `idf * (numerator/denominator)`），保证单条 posting 的贡献值本身与
        JSON 侧位级相同；**但跨词条/跨 posting 的求和顺序由 SQLite 的查询
        计划决定，不再是 `for term in terms` 的原序**，浮点加法不满足结合律，
        `SUM()` 的聚合结果因此不再保证与 JSON 侧逐 bit 相同（原「完整分数映射
        逐 bit 相同」判据已刻意放宽为带容差比较，见
        `tests/unit/test_bm25_sqlite_store.py` 模块 docstring 里的实测误差
        数量级说明）。

        tie-break 与 JSON 侧同为 `(-score, chunk_id)`，且**排完整个候选集
        再截断**（`ORDER BY score DESC, chunk_id ASC LIMIT top_k`，不是先
        `LIMIT` 再排）。同分候选在大规模语料下并不罕见（50K 档截断线上有
        14–19 个），少了这条两种后端的 top-k 会退化成各自不可预测的物理顺序。
        """
        if not query_terms:
            return []

        terms = [t.lower() for t in query_terms]
        conn = self._connect()
        conn.executescript(_SCHEMA)

        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'avg_doc_length'"
        ).fetchone()
        if row is None:
            return []
        avg_doc_length = float(row[0])

        # ⚠️ avg_doc_length==0 时代入 1.0 继续算（不是短路成别的式子）——
        # 与 `BM25Indexer._calculate_bm25_score` 的边界处理逐字一致。
        if avg_doc_length == 0:
            avg_doc_length = 1.0

        # 词条重复次数当乘数交给 SQL —— 保留「重复词条重复累加」的语义，
        # 同时避免对同一个词重复 JOIN/扫描一遍 postings（比诊断脚本的
        # 去重 IN 语义更完整，比逐次重复扫描更省）。
        term_counts: Dict[str, int] = {}
        for t in terms:
            term_counts[t] = term_counts.get(t, 0) + 1

        values_sql = ",".join(["(?,?)"] * len(term_counts))
        values_params: List[Any] = []
        for term, count in term_counts.items():
            values_params.append(term)
            values_params.append(count)

        # k1+1 与 1-b 各自只是 k1/b 的纯函数，提前算一次和逐行现算数值上
        # 完全相同（同一个确定性表达式对同一输入永远给同一个浮点结果），
        # 这样写只是少让 SQLite 对每一行重复算一遍常量。
        k1_plus_1 = k1 + 1.0
        one_minus_b = 1.0 - b

        sql = f"""
            WITH q(term, mult) AS (VALUES {values_sql})
            SELECT p.chunk_id AS cid,
                   SUM(
                       q.mult * (
                           t.idf * (
                               (p.tf * ?)
                               / (p.tf + ? * (? + ? * (p.doc_length / ?)))
                           )
                       )
                   ) AS score
            FROM postings p
            JOIN terms t ON t.term = p.term
            JOIN q ON q.term = p.term
            GROUP BY p.chunk_id
            ORDER BY score DESC, cid ASC
            LIMIT ?
        """
        params = values_params + [
            k1_plus_1, k1, one_minus_b, b, avg_doc_length, top_k
        ]
        cursor = conn.execute(sql, params)
        return [{"chunk_id": cid, "score": score} for cid, score in cursor.fetchall()]

    # ────────────────────────────── 自检 ──────────────────────────────

    def count_postings(self) -> int:
        conn = self._connect()
        conn.executescript(_SCHEMA)
        return conn.execute("SELECT COUNT(*) FROM postings").fetchone()[0]

    def count_chunks_for_doc(self, doc_hash: str) -> int:
        conn = self._connect()
        conn.executescript(_SCHEMA)
        return conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE doc_hash = ?", (doc_hash,)
        ).fetchone()[0]

    def load_metadata(self) -> Dict[str, Any]:
        conn = self._connect()
        conn.executescript(_SCHEMA)
        rows = dict(conn.execute("SELECT key, value FROM meta").fetchall())
        if not rows:
            return {}
        return {
            "num_docs": int(rows.get("num_docs", 0)),
            "avg_doc_length": float(rows.get("avg_doc_length", 0.0)),
            "total_terms": int(rows.get("total_terms", 0)),
            "collection": rows.get("collection", ""),
            "schema_version": rows.get("schema_version", ""),
        }
