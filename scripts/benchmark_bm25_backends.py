#!/usr/bin/env python3
"""在**目标规模**上复验 BM25 两种存储后端（JSON / SQLite）的全部性能结论。

为什么要重跑一遍
----------------
`docs/bm25_storage_design.md` §11 §12 的实测数字全部来自现网数据 ——
17 个业务库每个只有 20 篇文档 / 22 块 / 60KB，而目标规模是单客户几个 G 文档
（约 143K 块）。**差 4 个数量级。** "SQLite 比 JSON 慢 2.3 倍"、
`RAGENT_BM25_SQLITE_MIN_JSON_BYTES=262144` 这个阈值，都只在那个不真实的规模上
被验证过。本脚本用 `scripts/seed_large_bm25_corpus.py` 造出的大规模语料重测。

⚠️ 语料是**合成**的（见 seed 脚本的说明）。因此本脚本的结论只覆盖
**体积 / 延迟 / 内存 / 打分一致性 / 并行行为**，**不涉及检索质量与召回率**。

测什么（六件事，逐条对应现有结论）
----------------------------------
1. `crossover` —— 交叉点到底在哪？现有结论是 "~50 块 / ~290KB"。
2. `scale`     —— 1K/10K/50K 三档，**走真实 `load()` + `query()`**、
                  用 `RAGENT_BM25_READ_BACKEND=json|sqlite` 强制 A/B。
3. `parity`    —— 大规模上打分是否仍然逐 bit 相同（判据是完整分数映射 `==`）。
4. `rss`       —— 常驻内存。**在全新子进程里量**，不在主进程取差值。
5. `parallel`  —— 6 库并行：`json.load` 持 GIL 会不会让并行退化成串行？
6. `threshold` —— 现行 `auto` 阈值在各档上的判定，与 1 的实测交叉点对照。

⚠️ **不在主进程取 RSS 差值** —— 之前有人这么做量出过 -608MB 的荒谬值
（Python 分配器把释放的 arena 还给 OS 是滞后且不完整的，跨大分配的差值毫无意义）。
本脚本每个 (后端, 规模) 组合都 fork 一个全新的 `sys.executable` 子进程，
在子进程里用 `resource.getrusage(RUSAGE_SELF).ru_maxrss` 取**峰值** RSS，
并另跑一个"只 import 什么都不做"的基线子进程作为解释器底噪。

用法
----
    # 先造语料（1K/10K/50K 约需 1 分钟、约 350MB 磁盘）
    .venv/bin/python scripts/seed_large_bm25_corpus.py

    # 全量测量
    .venv/bin/python scripts/benchmark_bm25_backends.py

    # 只跑其中几项
    .venv/bin/python scripts/benchmark_bm25_backends.py --only crossover,threshold

    # 换规模档位 / 重复次数
    .venv/bin/python scripts/benchmark_bm25_backends.py \
        --sizes 1000,10000,50000 --repeat 5 --parallel-size 10000

结果 JSON 落 `scripts/benchmark_results/bm25_backends_<时间戳>.json`，带 code_state。

方法学上刻意做的几件事
----------------------
* **每次测量都新建一个 `BM25Indexer`**，因为线上就是这样：
  `query_knowledge_hub._build_hybrid_search_for` 每次查询都现建一个，
  实例级缓存跨不过查询边界。复用实例会测出一个线上根本拿不到的数字。
* **查询词取该库 df 最高的 5 个**（`--query-kind hot`，默认）。这是**最坏情况**：
  postings 最长、扫描量最大。另有 `mixed` 档取 df 分位数上的词，接近日常提问。
  引用数字时必须说明是哪一种 —— 设计文档 §11 里 "2044x / 139–187x / 11x"
  三组差异巨大的倍数，差别全在这里。
* **计时取中位数**，并单独保留第一次（含文件系统冷缓存）。
* **强制后端而不是靠 `auto`**：`auto` 会因为大小阈值自己回退，
  那样测出来的"SQLite"可能其实是 JSON。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import resource
import shutil
import statistics
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.seed_large_bm25_corpus import (  # noqa: E402
    COLLECTION_PREFIX,
    capture_code_state,
    collection_name,
    index_dir_for,
    seed_collection,
)

DEFAULT_OUT_DIR = REPO_ROOT / "scripts" / "benchmark_results"
N_QUERY_TERMS = 5


# ────────────────────────────── 工具 ──────────────────────────────


def maxrss_bytes() -> int:
    """本进程的峰值 RSS，单位统一成字节。

    `ru_maxrss` 的单位随平台不同：macOS/BSD 是字节，Linux 是 KB。
    不做这个换算会得到 1024 倍的差异 —— 而这正是那种"数字看着很确定、
    其实错了三个数量级"的坑。
    """
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return raw if sys.platform == "darwin" else raw * 1024


def pick_query_terms(collection: str, kind: str = "hot",
                     k: int = N_QUERY_TERMS) -> List[str]:
    """从 SQLite 副本的 `terms` 表里挑查询词。

    **刻意从 SQLite 而不是 JSON 里挑** —— 大规模下光是为了选词就 `json.load`
    一份 250MB 索引，会把脚本自己变慢，也没必要：两边 terms 是同一份数据
    （`replace_all` 逐条镜像），而这一步只是选词，不参与计时。

    kind:
      hot   —— df 最高的 k 个词。**最坏情况**，postings 最长。
      mixed —— 在 df 降序表的 5 个分位点上各取一个，接近日常提问的命中分布。
    """
    import sqlite3

    db = index_dir_for(collection) / f"{collection}_bm25.sqlite"
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        if kind == "hot":
            rows = conn.execute(
                "SELECT term FROM terms ORDER BY df DESC, term ASC LIMIT ?", (k,)
            ).fetchall()
            return [r[0] for r in rows]
        total = conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0]
        out: List[str] = []
        for i in range(k):
            off = int(total * (i + 1) / (k + 1))
            r = conn.execute(
                "SELECT term FROM terms ORDER BY df DESC, term ASC LIMIT 1 OFFSET ?",
                (off,),
            ).fetchone()
            if r:
                out.append(r[0])
        return out
    finally:
        conn.close()


def make_indexer(collection: str, backend: str):
    """新建一个 `BM25Indexer` 并强制读后端。

    后端是在 `__init__` 里读环境变量的，所以**必须先设 env 再构造**。
    每次都新建实例是刻意的，理由见模块 docstring。
    """
    from src.ingestion.storage.bm25_indexer import BM25Indexer

    os.environ["RAGENT_BM25_READ_BACKEND"] = backend
    return BM25Indexer(index_dir=str(index_dir_for(collection)))


def timed_load_query(collection: str, backend: str, terms: List[str],
                     top_k: int = 10) -> Tuple[float, float, int]:
    """一次完整的生产读路径：新建 indexer → `load()` → `query()`。

    返回 (load 秒, query 秒, 命中条数)。
    """
    idxr = make_indexer(collection, backend)
    t0 = time.perf_counter()
    ok = idxr.load(collection)
    t1 = time.perf_counter()
    if not ok:
        raise RuntimeError(f"load 失败: {collection} / {backend}")
    res = idxr.query(terms, top_k=top_k)
    t2 = time.perf_counter()
    return t1 - t0, t2 - t1, len(res)


def repeat_measure(collection: str, backend: str, terms: List[str],
                   repeat: int) -> Dict[str, Any]:
    loads: List[float] = []
    queries: List[float] = []
    hits = 0
    # 预热一次：把文件读进 OS page cache，否则第一档测的是磁盘、后面测的是缓存
    timed_load_query(collection, backend, terms)
    for _ in range(repeat):
        l, q, h = timed_load_query(collection, backend, terms)
        loads.append(l)
        queries.append(q)
        hits = h
    return {
        "load_ms": round(statistics.median(loads) * 1000, 3),
        "query_ms": round(statistics.median(queries) * 1000, 3),
        "total_ms": round((statistics.median(loads)
                           + statistics.median(queries)) * 1000, 3),
        "load_ms_all": [round(x * 1000, 3) for x in loads],
        "query_ms_all": [round(x * 1000, 3) for x in queries],
        "hits": hits,
    }


def ensure_seeded(n: int, prefix: str = COLLECTION_PREFIX) -> str:
    col = collection_name(n, prefix)
    j = index_dir_for(col) / f"{col}_bm25.json"
    s = index_dir_for(col) / f"{col}_bm25.sqlite"
    if not (j.exists() and s.exists()):
        print(f"    [seed] {col} 不存在，现造 …", flush=True)
        seed_collection(n, prefix=prefix, force=True, progress=False)
    return col


# ────────────────────────────── 1) 交叉点 ──────────────────────────────


def bench_crossover(sizes: List[int], repeat: int, query_kind: str) -> Dict[str, Any]:
    """小规模区间上把 JSON / SQLite 的读路径耗时逐档量出来，找真实交叉点。

    现有结论（设计文档 §11、`bm25_indexer._use_sqlite_for_read` 注释）是
    "~50 块 / ~290KB"，且据此把 `RAGENT_BM25_SQLITE_MIN_JSON_BYTES` 定为 256KB。
    那批数字跑在现网 20 块的小库和随机小语料上，本函数用同一套分布参数、
    从 10 块一路量到几千块，看它站不站得住。
    """
    rows: List[Dict[str, Any]] = []
    for n in sizes:
        col = ensure_seeded(n)
        terms = pick_query_terms(col, query_kind)
        jbytes = (index_dir_for(col) / f"{col}_bm25.json").stat().st_size
        sbytes = (index_dir_for(col) / f"{col}_bm25.sqlite").stat().st_size
        j = repeat_measure(col, "json", terms, repeat)
        s = repeat_measure(col, "sqlite", terms, repeat)
        rows.append({
            "chunks": n, "collection": col,
            "json_bytes": jbytes, "sqlite_bytes": sbytes,
            "json": j, "sqlite": s,
            "sqlite_speedup_x": round(j["total_ms"] / s["total_ms"], 3)
            if s["total_ms"] else None,
            "winner": "json" if j["total_ms"] < s["total_ms"] else "sqlite",
        })
        print(f"    {n:>7} 块 | JSON {jbytes/1024:>9.0f}KB {j['total_ms']:>9.3f}ms"
              f" | SQLite {sbytes/1024:>9.0f}KB {s['total_ms']:>9.3f}ms"
              f" | {rows[-1]['winner']} 胜 ({rows[-1]['sqlite_speedup_x']}x)",
              flush=True)

    # 交叉点：第一个 SQLite 不再落后的档位，与前一档一起报（区间，不是点）
    cross: Optional[Dict[str, Any]] = None
    for prev, cur in zip(rows, rows[1:]):
        if prev["winner"] == "json" and cur["winner"] == "sqlite":
            cross = {
                "between_chunks": [prev["chunks"], cur["chunks"]],
                "between_json_bytes": [prev["json_bytes"], cur["json_bytes"]],
            }
            break
    if cross is None and rows and rows[0]["winner"] == "sqlite":
        cross = {"between_chunks": [0, rows[0]["chunks"]],
                 "between_json_bytes": [0, rows[0]["json_bytes"]]}
    return {"rows": rows, "crossover": cross, "query_kind": query_kind}


# ────────────────────────────── 2) 规模 A/B ──────────────────────────────


def bench_scale(sizes: List[int], repeat: int, query_kind: str) -> Dict[str, Any]:
    rows: List[Dict[str, Any]] = []
    for n in sizes:
        col = ensure_seeded(n)
        terms = pick_query_terms(col, query_kind)
        jbytes = (index_dir_for(col) / f"{col}_bm25.json").stat().st_size
        sbytes = (index_dir_for(col) / f"{col}_bm25.sqlite").stat().st_size
        j = repeat_measure(col, "json", terms, repeat)
        s = repeat_measure(col, "sqlite", terms, repeat)
        rows.append({
            "chunks": n, "collection": col, "terms": terms,
            "json_bytes": jbytes, "sqlite_bytes": sbytes,
            "size_ratio_sqlite_over_json": round(sbytes / jbytes, 3),
            "json": j, "sqlite": s,
            "sqlite_speedup_x": round(j["total_ms"] / s["total_ms"], 2)
            if s["total_ms"] else None,
        })
        print(f"    {n:>7} 块 | JSON load {j['load_ms']:>9.3f} + query "
              f"{j['query_ms']:>8.3f} = {j['total_ms']:>9.3f}ms"
              f" | SQLite {s['load_ms']:>7.3f} + {s['query_ms']:>8.3f} = "
              f"{s['total_ms']:>8.3f}ms | {rows[-1]['sqlite_speedup_x']}x",
              flush=True)
    return {"rows": rows, "query_kind": query_kind}


# ────────────────────────────── 3) 打分一致性 ──────────────────────────────


def bench_parity(sizes: List[int], query_kind: str) -> Dict[str, Any]:
    """判据是**完整分数映射逐 bit 相同**（`==`），不是 approx、也不是只比 top-k。

    只比 top-k 会漏掉两类问题：同分候选顺序不同（tie-break 退化），
    以及截断线以下的分数错误。这里用 `top_k=10**9` 拿全量。
    """
    rows: List[Dict[str, Any]] = []
    for n in sizes:
        col = ensure_seeded(n)
        checked: List[Dict[str, Any]] = []
        for kind in ("hot", "mixed"):
            terms = pick_query_terms(col, kind)
            ij = make_indexer(col, "json")
            ij.load(col)
            rj = ij.query(terms, top_k=10 ** 9)
            isq = make_indexer(col, "sqlite")
            isq.load(col)
            rs = isq.query(terms, top_k=10 ** 9)

            same_len = len(rj) == len(rs)
            # 完整列表逐条 `==`：dict 相等要求 chunk_id 与 score 都完全一致，
            # 且顺序一致（tie-break 也一起被验了）。
            identical = rj == rs
            first_diff = None
            if not identical:
                for i, (a, b) in enumerate(zip(rj, rs)):
                    if a != b:
                        first_diff = {"i": i, "json": a, "sqlite": b}
                        break
            # 分数映射（忽略顺序）单独再比一次，用来区分"分数错"和"仅顺序错"
            map_same = ({r["chunk_id"]: r["score"] for r in rj}
                        == {r["chunk_id"]: r["score"] for r in rs})
            checked.append({
                "query_kind": kind, "terms": terms,
                "n_results_json": len(rj), "n_results_sqlite": len(rs),
                "same_length": same_len,
                "identical_ordered": identical,
                "identical_score_map": map_same,
                "first_diff": first_diff,
            })
            print(f"    {n:>7} 块 [{kind:<5}] 命中 {len(rj):>7} | "
                  f"分数映射 {'一致' if map_same else '★不一致'} | "
                  f"含顺序 {'一致' if identical else '★不一致'}", flush=True)
        rows.append({"chunks": n, "collection": col, "checks": checked})
    all_ok = all(c["identical_ordered"] and c["identical_score_map"]
                 for r in rows for c in r["checks"])
    return {"rows": rows, "all_bit_identical": all_ok}


# ────────────────────────────── 4) 常驻内存（子进程） ──────────────────────────────

_RSS_CHILD_FLAG = "--_rss-child"


def _rss_child_main(collection: str, backend: str, terms: List[str],
                    baseline: bool) -> int:
    """在**全新子进程**里跑一次真实读路径，然后打印峰值 RSS。

    `baseline=True` 时只 import、不加载索引 —— 用来量解释器 + 依赖的底噪，
    这样报告里既有绝对峰值也有"索引本身占了多少"。
    """
    from src.ingestion.storage.bm25_indexer import BM25Indexer  # noqa: F401

    rss_after_import = maxrss_bytes()
    hits = 0
    if not baseline:
        idxr = make_indexer(collection, backend)
        idxr.load(collection)
        hits = len(idxr.query(terms, top_k=10))
    print(json.dumps({
        "peak_rss_bytes": maxrss_bytes(),
        "rss_after_import_bytes": rss_after_import,
        "hits": hits,
    }))
    return 0


def _run_rss_child(collection: str, backend: str, terms: List[str],
                   baseline: bool = False) -> Dict[str, Any]:
    cmd = [sys.executable, str(Path(__file__).resolve()), _RSS_CHILD_FLAG,
           collection, backend, json.dumps(terms, ensure_ascii=False),
           "1" if baseline else "0"]
    env = dict(os.environ)
    env["RAGENT_BM25_READ_BACKEND"] = backend
    out = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True,
                         env=env, timeout=1800)
    if out.returncode != 0:
        raise RuntimeError(f"RSS 子进程失败: {out.stderr[-2000:]}")
    return json.loads(out.stdout.strip().splitlines()[-1])


def bench_rss(sizes: List[int], query_kind: str) -> Dict[str, Any]:
    base = _run_rss_child("-", "json", [], baseline=True)
    print(f"    基线（只 import，不加载索引）峰值 RSS "
          f"{base['peak_rss_bytes']/1024**2:.1f} MB", flush=True)
    rows: List[Dict[str, Any]] = []
    for n in sizes:
        col = ensure_seeded(n)
        terms = pick_query_terms(col, query_kind)
        j = _run_rss_child(col, "json", terms)
        s = _run_rss_child(col, "sqlite", terms)
        rows.append({
            "chunks": n, "collection": col,
            "json_peak_mb": round(j["peak_rss_bytes"] / 1024 ** 2, 1),
            "sqlite_peak_mb": round(s["peak_rss_bytes"] / 1024 ** 2, 1),
            "json_over_baseline_mb": round(
                (j["peak_rss_bytes"] - base["peak_rss_bytes"]) / 1024 ** 2, 1),
            "sqlite_over_baseline_mb": round(
                (s["peak_rss_bytes"] - base["peak_rss_bytes"]) / 1024 ** 2, 1),
            "raw_json": j, "raw_sqlite": s,
        })
        print(f"    {n:>7} 块 | JSON 峰值 {rows[-1]['json_peak_mb']:>8.1f}MB"
              f" | SQLite 峰值 {rows[-1]['sqlite_peak_mb']:>8.1f}MB", flush=True)
    return {"baseline": base, "rows": rows,
            "note": "每个数字来自一个全新子进程的 ru_maxrss（峰值），"
                    "不是主进程前后取差值"}


# ────────────────────────────── 5) 6 库并行 ──────────────────────────────
#
# 这一节的结论**推翻了立项时的推测**，所以方法学写细一点。
#
# 原推测：「`json.load` 持 GIL，6 库并行会退化成串行；SQLite 查询释放 GIL，
#         所以是真并行 —— 规模上来后这是两种后端最重要的差异。」
#
# 实测：前半句成立，后半句反了。当前 `BM25SQLiteStore.query` 的实现下，
#       6 线程并发比串行还慢一个数量级。原因不在 SQLite，在**每行都要跨一次
#       sqlite3 → Python 的边界**：CPython 的 sqlite3 在每次 `sqlite3_step()`
#       前后释放/重取 GIL，热词查询一次要过 39,237 行 × 6 线程 ≈ 23 万次
#       GIL 交接，形成典型的 convoy。
#
# 所以这一节除了量"串行 vs 并发"，还多做三件事把因果钉死：
#   ① 线程数扫描 1/2/3/N —— convoy 的特征是**超线性**恶化，纯 GIL 排队只会线性；
#   ② `--_proc-child` 用**进程**并行同一批查询 —— 进程没有共享 GIL，
#      若进程并行恢复线性，就说明瓶颈在 GIL 交接而不是磁盘或 SQLite 本身；
#   ③ 诊断变体：把 BM25 打分下推进 SQL（`GROUP BY chunk_id ... LIMIT top_k`），
#      使跨边界的行数从 39,237 降到 top_k。若 convoy 随之消失，
#      "行数 = 交接次数"这个因果就成立。
#      ⚠️ 变体只用于**定位原因**，本次不改 `src/` 下任何生产代码。

_PROC_CHILD_FLAG = "--_proc-child"


def _proc_child_main(collection: str, backend: str, terms: List[str]) -> int:
    """进程并行的 worker：跑一次真实读路径，打印耗时。

    刻意用独立子进程而不是 `multiprocessing` fork —— fork 一个已经起过
    asyncio 线程池的进程在 macOS 上行为不稳定，而这里要量的是"没有共享 GIL
    时能跑多快"，进程怎么起并不重要，起得干净才重要。
    """
    l, q, h = timed_load_query(collection, backend, terms)
    print(json.dumps({"load_s": l, "query_s": q, "hits": h}))
    return 0


def _run_proc_parallel(cols: List[str], backend: str,
                       terms: List[str]) -> float:
    """同时起 len(cols) 个子进程各查一个库，返回挂钟耗时（含进程启动开销）。

    ⚠️ 进程启动 + import 在本机约 0.4s/个，**已经包含在这个数字里**，
    所以它不能直接和线程版比绝对值；它回答的是另一个问题：
    "去掉共享 GIL 之后，多库还会不会互相拖累。"
    """
    procs = []
    env = dict(os.environ)
    env["RAGENT_BM25_READ_BACKEND"] = backend
    t0 = time.perf_counter()
    for c in cols:
        procs.append(subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), _PROC_CHILD_FLAG,
             c, backend, json.dumps(terms, ensure_ascii=False)],
            cwd=REPO_ROOT, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    for p in procs:
        p.wait()
    return time.perf_counter() - t0


def _clone_collection(src_collection: str, dst_collection: str) -> None:
    src = index_dir_for(src_collection)
    dst = index_dir_for(dst_collection)
    shutil.rmtree(dst, ignore_errors=True)
    dst.mkdir(parents=True, exist_ok=True)
    for ext in ("json", "sqlite"):
        s = src / f"{src_collection}_bm25.{ext}"
        if s.exists():
            shutil.copy2(s, dst / f"{dst_collection}_bm25.{ext}")


def _sqlite_boundary_rows(collection: str, terms: List[str]) -> int:
    """一次查询要跨 sqlite3 → Python 边界的行数（= GIL 交接次数的量级）。"""
    import sqlite3

    db = index_dir_for(collection) / f"{collection}_bm25.sqlite"
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        ph = ",".join("?" * len(terms))
        return conn.execute(
            f"SELECT COUNT(*) FROM postings WHERE term IN ({ph})", terms
        ).fetchone()[0]
    finally:
        conn.close()


_SQL_SIDE_AGG = """
SELECT p.chunk_id,
       SUM(t.idf * ((p.tf * (1.5 + 1.0))
           / (p.tf + 1.5 * (1.0 - 0.75 + 0.75 * (p.doc_length / {avgdl}))))) AS s
FROM postings p JOIN terms t ON t.term = p.term
WHERE p.term IN ({ph})
GROUP BY p.chunk_id
ORDER BY s DESC, p.chunk_id ASC
LIMIT {topk}
"""


def _sql_side_query(collection: str, terms: List[str], top_k: int = 10) -> int:
    """诊断变体：打分下推进 SQL，只把 top_k 行交回 Python。

    ⚠️ **不是生产实现，也不打算在本次改成生产实现**：它去重了查询词
    （`IN` 语义），因此与 `BM25Indexer.query`「重复词条重复累加」的既有语义
    不等价；SUM 的累加顺序也由 SQLite 决定，逐 bit 等价需要单独论证。
    这里只用来验证"跨边界行数"这一个因果。
    """
    import sqlite3

    db = index_dir_for(collection) / f"{collection}_bm25.sqlite"
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key='avg_doc_length'").fetchone()
        avgdl = float(row[0]) if row else 1.0
        sql = _SQL_SIDE_AGG.format(avgdl=avgdl or 1.0,
                                   ph=",".join("?" * len(terms)), topk=top_k)
        return len(conn.execute(sql, terms).fetchall())
    finally:
        conn.close()


def bench_parallel(n: int, n_libs: int, repeat: int,
                   query_kind: str) -> Dict[str, Any]:
    """N 库并行：把"并行会不会退化"这件事测清楚，并定位原因。

    * `串行/并发 ≈ 1`      → 并行零收益，该后端在 GIL 上排队。
    * `串行/并发 ≈ n_libs` → 真并行。
    * `串行/并发 < 1`      → **比串行还慢**，说明有 convoy，不只是排队。

    ⚠️ 这里量的是 BM25 这一段的上限，不是端到端提速：真实的
    `_execute_local_multi` 每库还要跑 dense 检索与 rerank。
    """
    src = ensure_seeded(n)
    cols = [f"{src}_par{i}" for i in range(n_libs)]
    for c in cols:
        _clone_collection(src, c)

    thread_counts = sorted({1, 2, min(3, n_libs), n_libs})
    out: Dict[str, Any] = {"chunks": n, "n_libs": n_libs,
                           "thread_counts": thread_counts, "by_query_kind": {}}

    # 两种查询词都测：convoy 只在"热词命中大量 postings"时出现，
    # 只报其中一种会得到相反的结论。
    for kind in ("hot", "mixed"):
        terms = pick_query_terms(src, kind)
        section: Dict[str, Any] = {
            "terms": terms,
            "sqlite_boundary_rows_per_query": _sqlite_boundary_rows(src, terms),
            "backends": {},
        }
        print(f"    ── 查询词 [{kind}] terms={terms} | SQLite 每次查询跨边界 "
              f"{section['sqlite_boundary_rows_per_query']} 行 ──", flush=True)

        for backend in ("json", "sqlite"):
            os.environ["RAGENT_BM25_READ_BACKEND"] = backend

            def one(col: str, _b: str = backend) -> int:
                return timed_load_query(col, _b, terms)[2]

            for c in cols:                      # 预热 page cache
                one(c)

            async def par(sub: List[str]) -> float:
                t0 = time.perf_counter()
                await asyncio.gather(*(asyncio.to_thread(one, c) for c in sub))
                return time.perf_counter() - t0

            serial: List[float] = []
            for _ in range(repeat):
                t0 = time.perf_counter()
                for c in cols:
                    one(c)
                serial.append(time.perf_counter() - t0)
            serial_ms = statistics.median(serial) * 1000

            sweep: Dict[str, float] = {}
            for t in thread_counts:
                runs = [asyncio.run(par(cols[:t])) for _ in range(repeat)]
                sweep[str(t)] = round(statistics.median(runs) * 1000, 2)

            par_ms = sweep[str(n_libs)]
            b = {
                "serial_ms": round(serial_ms, 2),
                "parallel_ms": par_ms,
                "speedup_x": round(serial_ms / par_ms, 3) if par_ms else None,
                "ideal_speedup_x": n_libs,
                "parallel_efficiency": round((serial_ms / par_ms) / n_libs, 3)
                if par_ms else None,
                "thread_sweep_ms": sweep,
                # 超线性判据：N 线程的挂钟 / 1 线程的挂钟。纯 GIL 排队 ≈ N，
                # convoy 会显著 > N。
                "wall_ratio_vs_1_thread": round(par_ms / sweep["1"], 2)
                if sweep.get("1") else None,
                "serial_ms_all": [round(x * 1000, 2) for x in serial],
            }
            section["backends"][backend] = b
            print(f"      [{backend:<6}] 串行 {b['serial_ms']:>9.2f}ms | "
                  f"并发 {b['parallel_ms']:>9.2f}ms | 提速 {b['speedup_x']}x / "
                  f"理想 {n_libs}x | 线程扫描 {sweep} | "
                  f"N线程/1线程 = {b['wall_ratio_vs_1_thread']}（理想 {n_libs}）",
                  flush=True)

        # ① 进程并行对照：没有共享 GIL 时还会不会互相拖累
        proc = {}
        for backend in ("json", "sqlite"):
            proc[backend] = round(
                _run_proc_parallel(cols, backend, terms) * 1000, 2)
        section["process_parallel_ms"] = proc
        print(f"      [进程并行] json {proc['json']}ms | "
              f"sqlite {proc['sqlite']}ms （含每进程约 400ms 启动+import 开销）",
              flush=True)

        # ② 诊断变体：打分下推进 SQL，跨边界行数降到 top_k
        os.environ["RAGENT_BM25_READ_BACKEND"] = "sqlite"

        def one_sql(col: str) -> int:
            return _sql_side_query(col, terms)

        for c in cols:
            one_sql(c)

        async def par_sql(sub: List[str]) -> float:
            t0 = time.perf_counter()
            await asyncio.gather(*(asyncio.to_thread(one_sql, c) for c in sub))
            return time.perf_counter() - t0

        sql_sweep = {
            str(t): round(statistics.median(
                [asyncio.run(par_sql(cols[:t])) for _ in range(repeat)]) * 1000, 2)
            for t in thread_counts
        }
        section["sqlite_sql_side_agg_sweep_ms"] = sql_sweep
        section["sqlite_sql_side_agg_note"] = (
            "诊断变体，非生产实现：打分下推进 SQL 后跨边界行数降到 top_k。"
            "它与 BM25Indexer.query 的语义不完全等价（IN 去重了重复查询词），"
            "只用于验证「跨边界行数 → GIL convoy」这一因果。"
        )
        print(f"      [诊断·SQL下推] 线程扫描 {sql_sweep} "
              f"（跨边界行数 {section['sqlite_boundary_rows_per_query']} → 10）",
              flush=True)

        out["by_query_kind"][kind] = section

    for c in cols:
        shutil.rmtree(index_dir_for(c), ignore_errors=True)
    return out


# ────────────────────────────── 6) auto 阈值判定 ──────────────────────────────


def bench_threshold(sizes: List[int]) -> Dict[str, Any]:
    """现行 `auto` 模式在各档上到底选了哪个后端。

    直接调生产代码的 `_use_sqlite_for_read`，不复刻它的判断逻辑 ——
    复刻一遍就变成"测我自己写的 if"，阈值改了也测不出来。
    """
    from src.ingestion.storage.bm25_indexer import BM25Indexer

    thr = int(os.getenv("RAGENT_BM25_SQLITE_MIN_JSON_BYTES", str(256 * 1024)))
    rows = []
    for n in sizes:
        col = ensure_seeded(n)
        os.environ["RAGENT_BM25_READ_BACKEND"] = "auto"
        idxr = BM25Indexer(index_dir=str(index_dir_for(col)))
        jbytes = (index_dir_for(col) / f"{col}_bm25.json").stat().st_size
        rows.append({
            "chunks": n, "collection": col, "json_bytes": jbytes,
            "auto_picks_sqlite": bool(idxr._use_sqlite_for_read(col)),  # noqa: SLF001
        })
        print(f"    {n:>7} 块 | JSON {jbytes/1024:>9.0f}KB | auto → "
              f"{'sqlite' if rows[-1]['auto_picks_sqlite'] else 'json'}", flush=True)
    return {"threshold_bytes": thr, "rows": rows}


# ────────────────────────────── main ──────────────────────────────


ALL_SECTIONS = ["crossover", "scale", "parity", "rss", "parallel", "threshold"]


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == _RSS_CHILD_FLAG:
        return _rss_child_main(sys.argv[2], sys.argv[3],
                               json.loads(sys.argv[4]), sys.argv[5] == "1")
    if len(sys.argv) > 1 and sys.argv[1] == _PROC_CHILD_FLAG:
        return _proc_child_main(sys.argv[2], sys.argv[3],
                                json.loads(sys.argv[4]))

    ap = argparse.ArgumentParser(description="BM25 JSON vs SQLite 后端基准（大规模）")
    ap.add_argument("--sizes", default="1000,10000,50000")
    ap.add_argument("--crossover-sizes", default="10,20,50,100,200,500,1000,2000")
    ap.add_argument("--parallel-size", type=int, default=10000)
    ap.add_argument("--parallel-libs", type=int, default=6)
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--query-kind", default="hot", choices=["hot", "mixed"])
    ap.add_argument("--only", default="", help=f"逗号分隔，可选：{','.join(ALL_SECTIONS)}")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    sizes = [int(x) for x in args.sizes.split(",") if x.strip()]
    cross_sizes = [int(x) for x in args.crossover_sizes.split(",") if x.strip()]
    sections = ([s.strip() for s in args.only.split(",") if s.strip()]
                or ALL_SECTIONS)

    code_state = capture_code_state()
    print("=" * 78)
    print(f"代码状态: {code_state['branch']} @ {code_state['commit']}")
    print(f"          {code_state['warning']}")
    for p in code_state["dirty_paths"]:
        print(f"          脏文件: {p}")
    print("⚠️ 合成语料：仅覆盖体积/延迟/内存/一致性/并行，不涉及检索质量与召回率")
    print("=" * 78)

    results: Dict[str, Any] = {}

    if "crossover" in sections:
        print("\n── 1) 交叉点（小规模区间，JSON vs SQLite 读路径） ──")
        results["crossover"] = bench_crossover(cross_sizes, args.repeat,
                                               args.query_kind)
    if "scale" in sections:
        print("\n── 2) 规模 A/B（真实 load() + query()，强制后端） ──")
        results["scale"] = bench_scale(sizes, args.repeat, args.query_kind)
    if "parity" in sections:
        print("\n── 3) 打分一致性（完整分数映射 ==） ──")
        results["parity"] = bench_parity(sizes, args.query_kind)
    if "rss" in sections:
        print("\n── 4) 常驻内存（全新子进程的 ru_maxrss 峰值） ──")
        results["rss"] = bench_rss(sizes, args.query_kind)
    if "parallel" in sections:
        print(f"\n── 5) {args.parallel_libs} 库并行（GIL 推测的证伪/证实） ──")
        results["parallel"] = bench_parallel(args.parallel_size,
                                             args.parallel_libs,
                                             args.repeat, args.query_kind)
    if "threshold" in sections:
        print("\n── 6) auto 阈值判定 ──")
        results["threshold"] = bench_threshold(cross_sizes + sizes)

    out_dir = Path(args.out) if args.out else DEFAULT_OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"bm25_backends_{datetime.now():%Y%m%d_%H%M%S}.json"
    out.write_text(json.dumps({
        "generated_at": datetime.now().isoformat(),
        "script": str(Path(__file__).resolve()),
        "code_state": code_state,
        "platform": {"sys_platform": sys.platform,
                     "python": sys.version.split()[0]},
        "args": vars(args),
        "corpus_kind": "synthetic",
        "corpus_caveat": (
            "语料由 scripts/seed_large_bm25_corpus.py 合成（Zipf 词频 + Heaps 词表增长）。"
            "结论覆盖体积/延迟/内存/打分一致性/并行行为；"
            "不可用于评价检索质量、召回率或答案正确性。"
        ),
        "results": results,
        "not_covered": [
            "合成语料，非真实文档；不涉及检索质量/召回率",
            "只测 BM25 这一层，不含 dense 检索、rerank、端到端 TTFT",
            "只测单进程内的并发；未测多进程/多 worker 部署形态",
            "未测写路径（add_documents / remove_document）在大规模下的耗时",
            "未测索引更新与查询并发时的读写竞争",
            "只在本机 macOS + APFS 上跑过；生产硬件与文件系统不同",
        ],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果 → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
