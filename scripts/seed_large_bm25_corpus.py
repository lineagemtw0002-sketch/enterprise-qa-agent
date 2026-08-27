#!/usr/bin/env python3
"""生成**大规模合成 BM25 语料**，用来在真实目标规模上复验存储层的性能结论。

为什么需要这个脚本
------------------
`docs/bm25_storage_design.md` §11 §12 里方案 C 的全部实测数字，都是在现网数据上
跑出来的 —— 而现网 17 个业务库**每个只有 20 篇文档 / 22 块 / 60KB**，目标规模却是
单客户几个 G 文档（约 143K 块）。**差 4 个数量级。**

差这么多的时候，"SQLite 比 JSON 慢 2.3 倍"这种结论只描述了固定开销区，
`RAGENT_BM25_SQLITE_MIN_JSON_BYTES=262144` 这个阈值也只是在一条几乎没有数据点
支撑的曲线上拍的。要判断它们对不对，必须先有能造出 1K–143K 块索引的手段。

⚠️ 这是**合成语料**，用途有明确边界
------------------------------------
生成的是 `term_stats`（chunk_id / term_frequencies / doc_length），**不是真实文本**，
词条也是机器编出来的伪中文词（形如 "的一"、"是了"）。

* ✅ **能**用来评价：索引体积、建索引耗时、`load()`/`query()` 延迟、常驻内存、
  两种后端的打分是否逐 bit 相同、并行行为。这些只取决于**词频统计的形状**。
* ❌ **不能**用来评价：检索质量、召回率、Recall@k、重排效果、答案正确性。
  语料没有语义，任何"检索准不准"的结论在它上面都没有意义。

语料形状的依据（不是拍脑袋）
----------------------------
三个分布参数全部对齐 `scripts/measure_bm25_index_growth.py` 在真实中文语料
（1/3 仓库真实文本 + 2/3 模板扩充）上的实测值：

| 量 | 实测（20260825_195452 那次） | 本脚本默认 |
|---|---|---|
| 词表增长 | 1K→6123、4K→10089、16K→26015、50K→70830 | Heaps `V = 6388·(n/1000)^0.616` |
| 平均块长 | 47.4 / 47.9 / 47.7 / 47.7（四档几乎不变） | `--doc-length 48` |
| 块内去重率 | 由 `bytes_per_chunk≈4948` 反推约 0.88 | `--unique-ratio 0.88` |
| 词频分布 | 未直接测，按经典中文 Zipf | `--zipf-s 1.0` |

⚠️ **Heaps 幂律只是两端点拟合，中间点对不上，这一点必须写明**：
`6388·(n/1000)^0.616` 在 1K 和 50K 两端与实测吻合（6388 / 71086，任务书给定），
但代入 16K 得 ~35.2K，而实测是 26.0K —— **真实词表增长不是单一幂律**，
中段比幂律低约 26%。本脚本按任务书给定的幂律生成，因此**中等规模下词表偏大、
索引偏大、查询扫描量偏大**，测出的绝对值对 SQLite 一侧偏保守（更不利），
对 JSON 一侧也偏不利。做跨规模趋势对比时这个偏差是一致的，不影响结论方向。

Heaps 是**按构造强制**的，不是"希望采样出来正好是这样"：第 i 块必定引入
`V(i) - V(i-1)` 个新词条（数量超过块长时排队顺延到后面几块）。剩余槽位再按
Zipf 权重从"已出生"的词表前缀里采样。词条的 Zipf 秩 = 出生顺序，
所以"早出生 = 高频"，与真实语言里 Heaps/Zipf 的耦合方向一致。

不污染现网数据
--------------
所有生成物落在 `data/db/bm25/synthetic_bm25_<块数>/`，前缀 `synthetic_bm25_`
与 17 个真实业务库不重叠。`--clean` 一键删除全部生成物。
**不会**触碰任何既有 collection。

用法
----
    # 生成 1K / 10K / 50K 三档（默认）
    .venv/bin/python scripts/seed_large_bm25_corpus.py

    # 只生成一档；--force 覆盖已存在的
    .venv/bin/python scripts/seed_large_bm25_corpus.py --chunks 143000 --force

    # 只看词表/体积估算，不真的生成
    .venv/bin/python scripts/seed_large_bm25_corpus.py --chunks 143000 --dry-run

    # 列出已生成的
    .venv/bin/python scripts/seed_large_bm25_corpus.py --list

    # 清理全部合成库
    .venv/bin/python scripts/seed_large_bm25_corpus.py --clean

走的是真实 `BM25Indexer.build()`，不自己拼文件 —— 因此 JSON 索引与 SQLite 副本
都由生产代码路径产出（`build()` 末尾的 `_mirror_to_sqlite` 双写），
两种后端的数据来源与线上完全一致。

体积与耗时（2026-08-26 本机实测，Apple Silicon / APFS）
--------------------------------------------------------

| 块数 | 词表 | postings | JSON | SQLite | 生成 | build() |
|---|---|---|---|---|---|---|
| 1,000 | 6,388 | 42,392 | 5.24 MB | 3.68 MB | 0.02s | 0.17s |
| 10,000 | 26,385 | 422,901 | 48.83 MB | 35.29 MB | 0.19s | 1.64s |
| 50,000 | 71,110 | 2,113,345 | 238.4 MB | 175.0 MB | 0.99s | 9.0s |
| 143,000 | 135,848 | 6,039,991 | 675.1 MB | 498.1 MB | 2.97s | 41.1s |

**与真实语料的吻合度是这份合成语料能不能用的关键判据**：
`measure_bm25_index_growth.py` 在真实中文语料上实测 1K 块 = 5.13 MB / 词表 6123、
50K 块 = 235.94 MB / 词表 70830，并据此外推 143K ≈ 672 MB。
本脚本对应给出 5.24 MB / 6388、238.4 MB / 71110、675.1 MB —— **三档全部落在 ±5% 内**，
143K 那一档更是把当初的外推值实测了出来（672 → 675 MB）。

143K 一档约需 1.2GB 磁盘、峰值内存约 2GB（生成侧），耗时不到 1 分钟。
⚠️ 但**读**它才是吃内存的：`json.load` 一份 675MB 索引实测峰值 RSS 2.2–3.5GB，
见 `scripts/benchmark_bm25_backends.py` 的 rss 一节。

配套脚本
--------
`scripts/benchmark_bm25_backends.py` —— 用本脚本的产物做 JSON/SQLite A/B。
"""

from __future__ import annotations

import argparse
import bisect
import json
import random
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.core.settings import resolve_path  # noqa: E402

# ---------------------------------------------------------------- 默认参数

COLLECTION_PREFIX = "synthetic_bm25_"

DEFAULT_SEED = 20260826
DEFAULT_HEAPS_K = 6388.0       # V(1000) —— 实测 1K 块时的词表规模
DEFAULT_HEAPS_ALPHA = 0.616    # ln(71086/6388)/ln(50)，1K→50K 两端点拟合
DEFAULT_ZIPF_S = 1.0
DEFAULT_DOC_LENGTH = 48.0      # 实测四档均为 47.4–47.9
DEFAULT_UNIQUE_RATIO = 0.88    # 块内去重率：unique_terms / doc_length
CHUNKS_PER_DOC = 8             # 决定 chunk_id 的分布形态，见 make_chunk_id

# 构造伪中文词条用的字符池。取常用汉字区的一段，保证：
#   1. 每个词条 2–3 个汉字，UTF-8 下 6–9 字节，与真实分词结果的字节数同量级
#      （JSON 索引体积对这个很敏感，用 ASCII 假词会系统性低估索引大小）；
#   2. 池子够大（1024^2 > 100 万），143K 词表也不会撞车。
_CJK_BASE = 0x4E00
_CJK_POOL = 1024


# ---------------------------------------------------------------- 词表 / 分布


def heaps_vocab(n: int, k: float = DEFAULT_HEAPS_K,
                alpha: float = DEFAULT_HEAPS_ALPHA) -> int:
    """Heaps 定律：n 块语料对应多大的词表。"""
    if n <= 0:
        return 0
    return max(1, int(round(k * (n / 1000.0) ** alpha)))


def make_term(j: int) -> str:
    """第 j 个词条的字面量。确定性、无碰撞、2–3 个汉字。"""
    if j < _CJK_POOL * _CJK_POOL:
        a, b = divmod(j, _CJK_POOL)
        return chr(_CJK_BASE + a) + chr(_CJK_BASE + b)
    j -= _CJK_POOL * _CJK_POOL
    a, rest = divmod(j, _CJK_POOL * _CJK_POOL)
    b, c = divmod(rest, _CJK_POOL)
    return chr(_CJK_BASE + a) + chr(_CJK_BASE + b) + chr(_CJK_BASE + c)


def make_chunk_id(i: int) -> Tuple[str, str]:
    """返回 (chunk_id, doc_hash)，格式贴近真实：``65046ad1_0000_2a3ac7ab``。

    真实 chunk_id = ``sha256(源路径)[:8]`` + 块序号 + ``sha256(内容)[:8]``，
    **不单调递增**。这一点对本次测量是必须的：`BM25Indexer` 的 postings 按摄入
    顺序排，SQLite 聚簇表按 `(term, chunk_id)` 排 —— 只有 chunk_id 非单调时两者
    的物理顺序才真的分叉，"两种后端 top-k 是否一致"才测得出来。
    用单调 id 会假性通过（原型脚本踩过这个）。

    这里用一个便宜的确定性散列代替 sha256：本脚本要造几十万个 id，
    真跑 sha256 会白白多花十几秒，而这里只需要"看起来乱、且确定"。
    """
    import hashlib

    doc_no = i // CHUNKS_PER_DOC
    seq = i % CHUNKS_PER_DOC
    src_h = hashlib.blake2b(f"synthetic/doc_{doc_no}".encode(),
                            digest_size=4).hexdigest()
    content_h = hashlib.blake2b(f"synthetic/doc_{doc_no}#{seq}".encode(),
                                digest_size=4).hexdigest()
    doc_hash = hashlib.blake2b(f"synthetic-content-{doc_no}".encode(),
                               digest_size=32).hexdigest()
    return f"{src_h}_{seq:04d}_{content_h}", doc_hash


def _zipf_cumweights(vocab: int, s: float) -> List[float]:
    """Zipf 权重的前缀和。秩 r（1-based）的权重 = 1/r^s。

    用前缀和 + bisect 采样，是为了支持"只从前 V(i) 个词条里采"这件事 ——
    词表是随 i 增长的，每块都重建一次权重表会把生成耗时抬成 O(n·V)。
    """
    cum = [0.0] * (vocab + 1)
    total = 0.0
    for r in range(1, vocab + 1):
        total += 1.0 / (r ** s)
        cum[r] = total
    return cum


# ---------------------------------------------------------------- 语料生成


def generate_term_stats(
    n: int,
    *,
    seed: int = DEFAULT_SEED,
    heaps_k: float = DEFAULT_HEAPS_K,
    heaps_alpha: float = DEFAULT_HEAPS_ALPHA,
    zipf_s: float = DEFAULT_ZIPF_S,
    doc_length: float = DEFAULT_DOC_LENGTH,
    unique_ratio: float = DEFAULT_UNIQUE_RATIO,
    progress: bool = False,
) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
    """生成 n 块的 term_stats，返回 (term_stats, chunk_id -> doc_hash)。

    结构与 `SparseEncoder.encode()` 的输出一致（`BM25Indexer.build` 只用
    chunk_id / term_frequencies / doc_length 三个字段，`unique_terms` 一并给上）。

    确定性：同一个 seed + 同一组参数 → 逐字节相同的输出。
    """
    if n <= 0:
        raise ValueError(f"chunks must be > 0, got {n}")

    rng = random.Random(seed)
    vocab_total = heaps_vocab(n, heaps_k, heaps_alpha)
    cum = _zipf_cumweights(vocab_total, zipf_s)
    terms = [make_term(j) for j in range(vocab_total)]

    stats: List[Dict[str, Any]] = []
    born = 0                 # 已出生的词条数（= 词表前缀长度）
    pending: List[int] = []  # 该出生但当前块塞不下的词条，顺延到后面几块

    # 前一块的 Heaps 目标值，用来算"这一块该新引入几个词条"
    prev_target = 0
    report_every = max(1, n // 20)

    for i in range(1, n + 1):
        target = min(vocab_total, heaps_vocab(i, heaps_k, heaps_alpha))
        if target > prev_target:
            pending.extend(range(prev_target, target))
            prev_target = target

        # 本块的长度与去重数。用轻微抖动而不是定值：定长会让 BM25 的
        # 长度归一化项 (1 - b + b*dl/avgdl) 变成常数，掩盖真实打分的分散度。
        dl = max(8, int(round(rng.gauss(doc_length, doc_length * 0.25))))
        want_unique = max(1, min(dl, int(round(dl * unique_ratio))))

        chosen: Dict[int, int] = {}

        # ① 先塞"必须在本块出生"的新词条 —— Heaps 是强制满足的，不是碰运气
        while pending and len(chosen) < want_unique:
            j = pending.pop(0)
            chosen[j] = 1
            born = max(born, j + 1)

        # ② 剩余槽位按 Zipf 从已出生的前缀里采（无放回：撞到已选的就重抽）
        if born > 0:
            hi = cum[born]
            attempts = 0
            max_attempts = want_unique * 12 + 64
            while len(chosen) < want_unique and attempts < max_attempts:
                attempts += 1
                u = rng.random() * hi
                j = bisect.bisect_left(cum, u, 1, born + 1) - 1
                if j < 0:
                    j = 0
                if j not in chosen:
                    chosen[j] = 1

        # ③ 把 dl - unique 个重复次数按 Zipf 权重摊到已选词条上（高频词更容易重复）
        extra = dl - len(chosen)
        if extra > 0 and chosen:
            keys = sorted(chosen)
            w = [1.0 / ((j + 1) ** zipf_s) for j in keys]
            tot = sum(w)
            acc: List[float] = []
            run = 0.0
            for x in w:
                run += x
                acc.append(run)
            for _ in range(extra):
                u = rng.random() * tot
                k = bisect.bisect_left(acc, u)
                if k >= len(keys):
                    k = len(keys) - 1
                chosen[keys[k]] += 1
        dl = sum(chosen.values())

        cid, _doc_hash = make_chunk_id(i - 1)
        stats.append({
            "chunk_id": cid,
            "term_frequencies": {terms[j]: tf for j, tf in chosen.items()},
            "doc_length": dl,
            "unique_terms": len(chosen),
        })

        if progress and i % report_every == 0:
            print(f"    …{i}/{n} 块（已出生词条 {born}/{vocab_total}）", flush=True)

    doc_hash_by_chunk = {
        s["chunk_id"]: make_chunk_id(k)[1] for k, s in enumerate(stats)
    }
    return stats, doc_hash_by_chunk


# ---------------------------------------------------------------- 落盘


def collection_name(n: int, prefix: str = COLLECTION_PREFIX) -> str:
    return f"{prefix}{n}"


def index_dir_for(collection: str) -> Path:
    return Path(resolve_path(f"data/db/bm25/{collection}"))


def seed_collection(
    n: int,
    *,
    prefix: str = COLLECTION_PREFIX,
    force: bool = False,
    progress: bool = True,
    **corpus_kwargs: Any,
) -> Dict[str, Any]:
    """生成一档语料并通过真实 `BM25Indexer.build()` 落成 JSON + SQLite。

    **刻意走 `build()` 而不是自己写文件**：JSON 的序列化参数（`indent=2`、
    `ensure_ascii=False`）直接决定索引体积，SQLite 副本则由 `build()` 末尾的
    `_mirror_to_sqlite` 双写产出。自己拼文件测出来的就不是生产路径的数字了。
    """
    from src.ingestion.storage.bm25_indexer import BM25Indexer

    collection = collection_name(n, prefix)
    idx_dir = index_dir_for(collection)
    json_path = idx_dir / f"{collection}_bm25.json"
    sqlite_path = idx_dir / f"{collection}_bm25.sqlite"

    if json_path.exists() and not force:
        return {
            "chunks": n, "collection": collection, "skipped": True,
            "reason": "已存在（--force 可覆盖）",
            "index_dir": str(idx_dir),
            "json_bytes": json_path.stat().st_size,
            "sqlite_bytes": sqlite_path.stat().st_size if sqlite_path.exists() else 0,
        }

    shutil.rmtree(idx_dir, ignore_errors=True)

    t0 = time.perf_counter()
    stats, doc_hash_by_chunk = generate_term_stats(n, progress=progress,
                                                   **corpus_kwargs)
    gen_s = time.perf_counter() - t0

    vocab_realized = len({t for s in stats for t in s["term_frequencies"]})
    postings_total = sum(len(s["term_frequencies"]) for s in stats)
    tokens_total = sum(s["doc_length"] for s in stats)

    # 双写必须开着，否则拿不到 SQLite 副本。这里显式打开，免得调用者的
    # 环境里恰好设了 RAGENT_BM25_SQLITE_DUAL_WRITE=false 而静默只出 JSON。
    indexer = BM25Indexer(index_dir=str(idx_dir))
    indexer.dual_write_sqlite = True

    t0 = time.perf_counter()
    indexer.build(stats, collection=collection,
                  doc_hash_by_chunk=doc_hash_by_chunk)
    build_s = time.perf_counter() - t0

    return {
        "chunks": n,
        "collection": collection,
        "skipped": False,
        "index_dir": str(idx_dir),
        "json_path": str(json_path),
        "sqlite_path": str(sqlite_path),
        "json_bytes": json_path.stat().st_size if json_path.exists() else 0,
        "sqlite_bytes": sqlite_path.stat().st_size if sqlite_path.exists() else 0,
        "vocab_target": heaps_vocab(
            n,
            corpus_kwargs.get("heaps_k", DEFAULT_HEAPS_K),
            corpus_kwargs.get("heaps_alpha", DEFAULT_HEAPS_ALPHA),
        ),
        "vocab_realized": vocab_realized,
        "postings_total": postings_total,
        "postings_per_chunk": round(postings_total / n, 2),
        "avg_doc_length": round(tokens_total / n, 3),
        "generate_s": round(gen_s, 2),
        "build_s": round(build_s, 2),
    }


def list_synthetic(prefix: str = COLLECTION_PREFIX) -> List[Dict[str, Any]]:
    root = Path(resolve_path("data/db/bm25"))
    out: List[Dict[str, Any]] = []
    if not root.exists():
        return out
    for d in sorted(root.glob(f"{prefix}*")):
        if not d.is_dir():
            continue
        j = next(d.glob("*_bm25.json"), None)
        s = next(d.glob("*_bm25.sqlite"), None)
        out.append({
            "collection": d.name,
            "index_dir": str(d),
            "json_bytes": j.stat().st_size if j else 0,
            "sqlite_bytes": s.stat().st_size if s else 0,
        })
    return out


def clean_synthetic(prefix: str = COLLECTION_PREFIX) -> List[str]:
    """删除全部合成库。只认前缀，绝不碰其他 collection。"""
    removed: List[str] = []
    root = Path(resolve_path("data/db/bm25"))
    if not root.exists():
        return removed
    for d in sorted(root.glob(f"{prefix}*")):
        if d.is_dir() and d.name.startswith(prefix):
            shutil.rmtree(d, ignore_errors=True)
            removed.append(d.name)
    return removed


# ---------------------------------------------------------------- code_state


def capture_code_state() -> Dict[str, Any]:
    """记录当前 commit + 工作区脏文件（写法对齐 `scripts/benchmark_latency.py`）。

    被测代码带未提交改动时，这批数字将来无法只靠 commit hash 复现，
    必须把脏文件清单一起写进结果里（`CLAUDE.md` §7.5）。
    """
    def _git(*args: str, strip: bool = True) -> str:
        try:
            out = subprocess.run(["git", *args], cwd=REPO_ROOT,
                                 capture_output=True, text=True,
                                 timeout=15).stdout
            return out.strip() if strip else out
        except Exception as e:  # pragma: no cover
            return f"<git failed: {e}>"

    porcelain = _git("status", "--porcelain", strip=False)
    dirty = [
        f"{line[:2].strip() or '??'} {line[3:]}"
        for line in porcelain.splitlines() if line.strip()
    ]
    return {
        "commit": _git("rev-parse", "--short", "HEAD"),
        "commit_full": _git("rev-parse", "HEAD"),
        "branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
        "dirty_paths": dirty,
        "is_clean": not dirty,
        "warning": (
            "工作区有未提交改动，这批数字对应「含未提交改动的工作区状态」，"
            "不能只靠 commit hash 复现"
            if dirty else "工作区干净，可只靠 commit hash 复现"
        ),
    }


# ---------------------------------------------------------------- CLI


def main() -> int:
    ap = argparse.ArgumentParser(
        description="生成大规模合成 BM25 语料（仅用于性能测量，不可用于评价检索质量）"
    )
    ap.add_argument("--chunks", default="1000,10000,50000",
                    help="逗号分隔的块数档位，默认 1000,10000,50000")
    ap.add_argument("--prefix", default=COLLECTION_PREFIX)
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--heaps-k", type=float, default=DEFAULT_HEAPS_K)
    ap.add_argument("--heaps-alpha", type=float, default=DEFAULT_HEAPS_ALPHA)
    ap.add_argument("--zipf-s", type=float, default=DEFAULT_ZIPF_S)
    ap.add_argument("--doc-length", type=float, default=DEFAULT_DOC_LENGTH)
    ap.add_argument("--unique-ratio", type=float, default=DEFAULT_UNIQUE_RATIO)
    ap.add_argument("--force", action="store_true", help="覆盖已存在的合成库")
    ap.add_argument("--dry-run", action="store_true",
                    help="只打印词表/体积估算，不生成")
    ap.add_argument("--list", action="store_true", help="列出已生成的合成库")
    ap.add_argument("--clean", action="store_true", help="删除全部合成库")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    if args.clean:
        removed = clean_synthetic(args.prefix)
        print(f"已删除 {len(removed)} 个合成库：" + (", ".join(removed) or "（无）"))
        return 0

    if args.list:
        rows = list_synthetic(args.prefix)
        if not rows:
            print("没有已生成的合成库。")
            return 0
        print(f"{'collection':<28} {'JSON':>12} {'SQLite':>12}")
        for r in rows:
            print(f"{r['collection']:<28} {r['json_bytes']/1024**2:>10.2f}MB "
                  f"{r['sqlite_bytes']/1024**2:>10.2f}MB")
        return 0

    sizes = [int(x) for x in args.chunks.split(",") if x.strip()]

    if args.dry_run:
        print(f"{'块数':>8} {'词表(Heaps)':>12} {'预估postings':>14} {'预估JSON':>12}")
        for n in sizes:
            v = heaps_vocab(n, args.heaps_k, args.heaps_alpha)
            p = int(n * args.doc_length * args.unique_ratio)
            # 每条 posting 在 indent=2 的 JSON 里约 116 字节（实测反推）
            print(f"{n:>8} {v:>12} {p:>14} {p*116/1024**2:>10.1f}MB")
        return 0

    corpus_kwargs = dict(
        seed=args.seed, heaps_k=args.heaps_k, heaps_alpha=args.heaps_alpha,
        zipf_s=args.zipf_s, doc_length=args.doc_length,
        unique_ratio=args.unique_ratio,
    )

    print("=" * 78)
    print("⚠️ 合成语料：可用于体积/延迟/内存/一致性测量，"
          "**不可**用于评价检索质量或召回率")
    print("=" * 78)

    rows: List[Dict[str, Any]] = []
    for n in sizes:
        print(f"\n── {n} 块 ({collection_name(n, args.prefix)}) ──", flush=True)
        r = seed_collection(n, prefix=args.prefix, force=args.force,
                            progress=not args.quiet, **corpus_kwargs)
        rows.append(r)
        if r["skipped"]:
            print(f"   跳过：{r['reason']}  JSON {r['json_bytes']/1024**2:.2f}MB")
            continue
        print(f"   词表 {r['vocab_realized']}/{r['vocab_target']} | "
              f"postings {r['postings_total']} ({r['postings_per_chunk']}/块) | "
              f"avg_doc_length {r['avg_doc_length']}")
        print(f"   JSON {r['json_bytes']/1024**2:.2f}MB | "
              f"SQLite {r['sqlite_bytes']/1024**2:.2f}MB | "
              f"生成 {r['generate_s']}s | build {r['build_s']}s")

    out_dir = REPO_ROOT / "scripts" / "benchmark_results"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"bm25_seed_{datetime.now():%Y%m%d_%H%M%S}.json"
    out.write_text(json.dumps({
        "generated_at": datetime.now().isoformat(),
        "script": str(Path(__file__).resolve()),
        "code_state": capture_code_state(),
        "corpus_params": corpus_kwargs,
        "corpus_kind": "synthetic",
        "corpus_caveat": (
            "合成 term_stats，非真实文本。可用于体积/延迟/内存/一致性测量；"
            "不可用于评价检索质量、召回率或答案正确性。"
        ),
        "collections": rows,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果 → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
