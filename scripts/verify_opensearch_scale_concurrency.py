#!/usr/bin/env python3
"""目标规模下，完整检索链路的并发行为：OpenSearch 是否复现了旧链路的 GIL convoy？

为什么要跑这个
--------------
`docs/opensearch_migration_design.md` §14 第 4 项一直是"❌ 只在 604 条数据上测过，
数据量不足以触发 convoy"——`scripts/simulate_search_traffic.py` 那次模拟流量测的是
现网最大的库（604 条），§15 自己写得很清楚："完整检索链路在目标规模下的并发行为
仍然是未知数"。这个脚本就是去把这个"未知数"变成一个有数字支撑的结论。

已知背景（不重新论证，直接引用）：
- `CLAUDE.md` §4 第 2e 条：BM25 SQLite 后端在 10K 块 + 高频词查询下，6 线程比
  1 线程慢 103.4 倍（GIL convoy：`sqlite3` 每返回一行都要释放/重取 GIL）。
- `docs/opensearch_migration_design.md` §2：同规模同查询下 OpenSearch 6 线程只有
  4.8 倍（109ms），但那次测的是**裸存储层查询**（直接调 `OpenSearchStore`），
  不是走 `HybridSearch` 的完整链路。
- §15 模拟流量测过完整链路的并发，但语料只有 604 条——不够大，测不出 convoy。

本脚本测什么、不测什么
----------------------
测的是**完整检索链路**在目标规模合成语料上的并发行为：
`QueryKnowledgeHubTool._build_hybrid_search_for(collection).search(...)`
——dense（Chroma / OpenSearch kNN）+ sparse（BM25 SQLite / OpenSearch BM25）
+ RRF 融合全部走真实代码路径。**不含 cross-encoder 重排**——这与
`scripts/simulate_search_traffic.py` 的既有做法一致（它也只测到 `HybridSearch.search`
这一层），重排是模型推理开销，不是这次要回答的"存储层并发会不会 convoy"这个问题
的一部分，混进来会让信号变模糊。

⚠️ **合成语料，边界与 `seed_large_bm25_corpus.py` 相同**：词频统计是机器生成的
伪中文词，dense 向量是随机向量（不调用 Ollama embedding，纯 numpy）。
**只能用来评价并发/延迟行为，不能评价检索质量、召回率或答案正确性**——
这一点已经在 §16 用真实黄金测试集验过了，不是本次要重复的范围。

查询词的选择
------------
用语料里 Zipf 秩第 1 位的词（`make_term(0)`，构造上保证是全语料 df 最高的词，
在原 GIL convoy 实测里对应"9999/10000 命中"那种最坏情况）。
⚠️ **完整链路会经过 `QueryProcessor`（jieba 分词)**，而 jieba 对着看不懂的伪中文
词经常会把它拆开（实测 30 个高频候选词里只有 17 个能在 jieba 分词后原样幸存）。
`make_term(0)` = "一一" 是经验证幸存的（jieba 分词后仍是自己），所以固定用它，
不用每次跑都探测——这是本脚本能复现"命中绝大多数 chunk"这个最坏情况的前提，
换了词表参数（seed/heaps_k 等）要重新验证这一点，脚本启动时会自动校验一次并在
不满足时报错退出，不会静默拿一个错误的词去跑。

dense 侧刻意跳过 Ollama：查询向量与索引向量都是同一个固定种子生成的随机向量
（`--seed`），不产生任何真实网络调用。这样 GIL 竞争只可能来自 BM25/OpenSearch
存储层本身，不会被 Ollama 单线程串行（`CLAUDE.md` §4 第 3 条）污染判断。

"6 库并发"指的是什么
--------------------
跟 `scripts/benchmark_bm25_backends.py::bench_parallel` 的口径一致：不是"6 个线程
查同一个库"，是"6 个不同 collection 各自被查一次，同时发生"——对应真实场景里
`_execute_local_multi` 对用户有权限的全部业务库并行召回。语料内容在 N 个副本间
完全相同（cheap clone），只有 collection 名字不同。

不污染共享环境
--------------
本机 OpenSearch（`docker compose`）与 Chroma（`data/db/chroma`）是跨会话共用的，
其它会话可能同时在用它们跑别的东西。所有产物用前缀 `scaleverify_` 标注：
- BM25：`data/db/bm25/scaleverify_<chunks>_lib<i>/`
- Chroma：collection `scaleverify_<chunks>_lib<i>`
- OpenSearch：index `kb_scaleverify_<chunks>_lib<i>`
`--cleanup-only` 一键清理全部产物，不影响其它 collection/index。

用法
----
    # 先起 OpenSearch（如果还没起）
    docker compose up -d opensearch

    # 完整跑一遍（种数据 + 并发扫描 + 自动清理）
    .venv/bin/python scripts/verify_opensearch_scale_concurrency.py --chunks 20000 --libs 6

    # 只清理，不跑
    .venv/bin/python scripts/verify_opensearch_scale_concurrency.py --chunks 20000 --libs 6 --cleanup-only

    # 跑完不清理（用于人工复核，之后自己再跑一次 --cleanup-only）
    .venv/bin/python scripts/verify_opensearch_scale_concurrency.py --chunks 20000 --libs 6 --keep

结果 JSON 落 `scripts/benchmark_results/opensearch_scale_concurrency_<时间戳>.json`。
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.seed_large_bm25_corpus import (  # noqa: E402
    capture_code_state,
    generate_term_stats,
    index_dir_for,
    make_term,
)

PREFIX = "scaleverify_"
DEFAULT_OUT_DIR = REPO_ROOT / "scripts" / "benchmark_results"
HOT_TERM = make_term(0)  # "一一"，构造上保证是全语料 Zipf 秩第一的词


def collection_for(n: int, i: int, prefix: str = PREFIX) -> str:
    return f"{prefix}{n}_lib{i}"


# ─────────────────────────── 校验查询词能不能扛过 jieba ───────────────────────────


def verify_hot_term_survives_tokenization(term: str) -> None:
    """`HybridSearch` 走 `QueryProcessor`（jieba），伪中文词常被拆开
    （实测 30 个高频候选词里只有 17 个原样幸存，见模块 docstring）。
    `make_term(0)` 经验证是幸存的，但**不能假设这个结论会一直成立**——
    脚本启动时用真实 `QueryProcessor` 复核一次，不满足就直接报错退出，
    不会静默拿一个已经被拆碎的词去跑（那样 BM25/OpenSearch 侧会查询失败
    或命中寥寥，convoy 现象无法被触发，会得出"OpenSearch 没有 convoy"的假结论）。
    """
    from src.core.query_engine.query_processor import QueryProcessor

    keywords = QueryProcessor().process(term).keywords
    if keywords != [term]:
        raise RuntimeError(
            f"查询词 {term!r} 经 QueryProcessor 分词后变成 {keywords!r}，"
            "不再是原样的高频词——本脚本的前提假设不成立，需要换一个查询词"
            "（改 HOT_TERM 常量或调整 make_term 序号），不能直接跑。"
        )


# ─────────────────────────── 种数据：旧链路（BM25 + Chroma） ───────────────────────────


def seed_old_backend(
    n: int, n_libs: int, embeddings: Dict[str, List[float]], *, seed: int, force: bool
) -> Dict[str, Any]:
    """生成一份 term_stats，构建一次 BM25 索引（含 SQLite 双写），
    再克隆成 n_libs 份——对应"n_libs 个业务库内容不同但规模/分布相同"。
    Chroma 侧每个 collection 各写一份相同的向量（向量本身不参与打分对比，
    只需要能被 dense_retriever 查到、不崩即可）。
    """
    import shutil

    from src.core.settings import load_settings
    from src.ingestion.storage.bm25_indexer import BM25Indexer
    from src.libs.vector_store.chroma_store import ChromaStore

    settings = load_settings()
    col0 = collection_for(n, 0)
    idx_dir0 = index_dir_for(col0)

    stats, doc_hash_by_chunk = generate_term_stats(n, seed=seed, progress=True)
    print(f"    [old] term_stats 生成完毕：{len(stats)} 块", flush=True)

    if force:
        shutil.rmtree(idx_dir0, ignore_errors=True)
    indexer = BM25Indexer(index_dir=str(idx_dir0))
    indexer.dual_write_sqlite = True
    t0 = time.perf_counter()
    indexer.build(stats, collection=col0, doc_hash_by_chunk=doc_hash_by_chunk)
    build_s = time.perf_counter() - t0
    json_bytes = (idx_dir0 / f"{col0}_bm25.json").stat().st_size
    sqlite_bytes = (idx_dir0 / f"{col0}_bm25.sqlite").stat().st_size
    print(f"    [old] lib0 build() {build_s:.1f}s，JSON {json_bytes/1024**2:.1f}MB，"
          f"SQLite {sqlite_bytes/1024**2:.1f}MB", flush=True)

    for i in range(1, n_libs):
        col = collection_for(n, i)
        dst = index_dir_for(col)
        shutil.rmtree(dst, ignore_errors=True)
        dst.mkdir(parents=True, exist_ok=True)
        shutil.copy2(idx_dir0 / f"{col0}_bm25.json", dst / f"{col}_bm25.json")
        shutil.copy2(idx_dir0 / f"{col0}_bm25.sqlite", dst / f"{col}_bm25.sqlite")
    print(f"    [old] 已克隆到 {n_libs} 个 collection（BM25 JSON+SQLite）", flush=True)

    chroma_t0 = time.perf_counter()
    batch = 500
    ids = list(embeddings.keys())
    for i in range(n_libs):
        col = collection_for(n, i)
        store = ChromaStore(settings, collection_name=col)
        for b0 in range(0, len(ids), batch):
            chunk_ids = ids[b0:b0 + batch]
            records = [
                {
                    "id": cid,
                    "vector": embeddings[cid],
                    "metadata": {"text": f"synthetic chunk {cid}",
                                "source_path": f"synthetic/{cid.split('_')[0]}"},
                }
                for cid in chunk_ids
            ]
            store.upsert(records)
    chroma_s = time.perf_counter() - chroma_t0
    print(f"    [old] Chroma 写入完毕（{n_libs} 个 collection × {len(ids)} 条），"
          f"{chroma_s:.1f}s", flush=True)

    return {
        "chunks": n, "n_libs": n_libs,
        "bm25_build_s": round(build_s, 2),
        "bm25_json_bytes": json_bytes, "bm25_sqlite_bytes": sqlite_bytes,
        "chroma_write_s": round(chroma_s, 2),
    }


# ─────────────────────────── 种数据：新链路（OpenSearch） ───────────────────────────


def seed_new_backend(
    n: int, n_libs: int, embeddings: Dict[str, List[float]], *, seed: int
) -> Dict[str, Any]:
    """把同一份 term_stats 组装成 OpenSearch 文档，写进 n_libs 个 index。

    tokens 按 term_frequencies 逐词重复 tf 次 —— 与 BM25Indexer 的语义等价，
    content 字段用 `whitespace` 分析器直接吃这份预分词文本（对齐现有约定，
    见 `opensearch_store._mapping` 的说明）。
    """
    from src.libs.search.opensearch_store import (
        OpenSearchStore,
        build_chunk_doc,
        kb_index_name,
    )

    stats, doc_hash_by_chunk = generate_term_stats(n, seed=seed, progress=False)

    store = OpenSearchStore(dense_dims=768)
    batch = 500
    t0 = time.perf_counter()
    for i in range(n_libs):
        col = collection_for(n, i)
        idx = kb_index_name(col)
        store.drop_index(idx)  # 幂等重跑：先清后建，避免多次跑残留脏映射
        docs: List[Dict[str, Any]] = []
        for k, s in enumerate(stats):
            cid = s["chunk_id"]
            doc_no = k // 8
            tf = s["term_frequencies"]
            tokens: List[str] = []
            for term in sorted(tf):
                tokens.extend([term] * tf[term])
            docs.append(build_chunk_doc(
                text="synthetic",
                tokens=tokens,
                source_path=f"synthetic/doc_{doc_no}",
                chunk_index=k % 8,
                chunk_id=cid,
                doc_hash=doc_hash_by_chunk.get(cid),
                embedding=embeddings[cid],
            ))
        for b0 in range(0, len(docs), batch):
            store.index_chunks(idx, docs[b0:b0 + batch], refresh=False)
        store._client.indices.refresh(index=idx)  # noqa: SLF001 —— 脚本内部用，非生产代码路径
        cnt = store.count(idx)
        print(f"    [new] {col} 写入 {cnt} 条", flush=True)
        if cnt != n:
            raise RuntimeError(f"OpenSearch 写入条数对不上：{col} 期望 {n}，实际 {cnt}")
    write_s = time.perf_counter() - t0
    print(f"    [new] OpenSearch 写入完毕（{n_libs} 个 index × {n} 条），{write_s:.1f}s",
          flush=True)
    return {"chunks": n, "n_libs": n_libs, "write_s": round(write_s, 2)}


# ─────────────────────────── 并发扫描：完整链路 ───────────────────────────


def make_warmed_hub() -> Any:
    """建一个 `QueryKnowledgeHubTool` 并**预热一次**（`_ensure_shared_clients()`），
    在计时开始前把 reranker（本地 cross-encoder，首次构造会检查/加载权重，
    实测会发出到 HuggingFace Hub CDN 的 HTTPS 请求）和 embedding client
    的构造成本一次性付掉。

    ⚠️ **踩过的坑**：第一版在每次查询里都新建一个 `QueryKnowledgeHubTool()`
    （照抄了 `simulate_search_traffic.py` "每个线程自己的 hub" 这句话的字面
    意思，但那份脚本是"每个线程建一次、线程内复用"，不是"每次查询都建一次"）。
    `_build_hybrid_search_for` 内部的 `_ensure_shared_clients()` 只在
    `self._reranker is None` 时才构造 —— 每次查询都用全新实例，等于每次查询
    都重新构造一次 reranker，1500 块的 smoke test 卡了 5 分钟都跑不完，
    lsof 显示进程在等 HuggingFace Hub 的 CDN（cloudfront）HTTPS 响应。
    正确做法是"整个测量过程共用同一个（或每线程一个）hub 实例，预热一次"。

    这个 hub 在多线程间共享是安全的：`_build_hybrid_search_for` 的文档明确
    "跟 collection 绑定的部分每次都建新的局部变量返回，不留在实例状态里"，
    唯一写 `self` 的地方（`_ensure_shared_clients`）在预热后已经是
    "条件为假、什么也不做"的空操作，多线程并发读不会互相踩。
    """
    from src.mcp_server.tools.query_knowledge_hub import QueryKnowledgeHubTool

    hub = QueryKnowledgeHubTool()
    hub._ensure_shared_clients()  # noqa: SLF001 —— 脚本内部预热用，非生产代码路径
    return hub


def _one_query(hub: Any, collection: str, query_vector: List[float], top_k: int) -> int:
    hits = hub._build_hybrid_search_for(collection).search(  # noqa: SLF001
        HOT_TERM, top_k=top_k, query_vector=query_vector
    )
    return len(hits)


def run_concurrency_scan(
    hub: Any,
    cols: List[str],
    query_vector: List[float],
    top_k: int,
    thread_counts: List[int],
    repeat: int,
    *,
    label: str,
    env: Dict[str, str],
) -> Dict[str, Any]:
    """1/2/3/6 线程扫描，判据与 `benchmark_bm25_backends.py::bench_parallel` 一致：
    `wall_ratio_vs_1_thread` 远超线程数本身 = convoy，不只是排队
    （`CLAUDE.md` §4 第 2e 条）。

    `hub` 是调用方预热好、贯穿整个脚本复用的**同一个** `QueryKnowledgeHubTool`
    实例（见 `make_warmed_hub` 的说明——每次查询各建一个的第一版直接把
    reranker 构造成本算进了计时区间，5 分钟都跑不完）。
    """
    old_env = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        n_libs = len(cols)

        def one(c: str) -> int:
            return _one_query(hub, c, query_vector, top_k)

        for c in cols:  # 预热：把索引读进 OS page cache / OpenSearch 段缓存
            one(c)

        serial: List[float] = []
        for _ in range(repeat):
            t0 = time.perf_counter()
            for c in cols:
                one(c)
            serial.append(time.perf_counter() - t0)
        serial_ms = statistics.median(serial) * 1000

        sweep: Dict[str, float] = {}
        sweep_hits: Dict[str, int] = {}
        for t in thread_counts:
            runs: List[float] = []
            for _ in range(repeat):
                sub = cols[:t]
                results: List[int] = [0] * len(sub)
                threads = []

                def worker(idx: int, col: str) -> None:
                    results[idx] = one(col)

                t0 = time.perf_counter()
                threads = [threading.Thread(target=worker, args=(i, c))
                          for i, c in enumerate(sub)]
                for th in threads:
                    th.start()
                for th in threads:
                    th.join()
                runs.append(time.perf_counter() - t0)
                sweep_hits[str(t)] = results[0] if results else 0
            sweep[str(t)] = round(statistics.median(runs) * 1000, 2)

        par_ms = sweep[str(n_libs)]
        out = {
            "label": label,
            "n_libs": n_libs,
            "serial_ms": round(serial_ms, 2),
            "parallel_ms": par_ms,
            "speedup_x": round(serial_ms / par_ms, 3) if par_ms else None,
            "ideal_speedup_x": n_libs,
            "parallel_efficiency": round((serial_ms / par_ms) / n_libs, 3) if par_ms else None,
            "thread_sweep_ms": sweep,
            "hits_per_query": sweep_hits,
            "wall_ratio_vs_1_thread": round(par_ms / sweep["1"], 2) if sweep.get("1") else None,
            "serial_ms_all": [round(x * 1000, 2) for x in serial],
        }
        print(f"    [{label}] 串行 {out['serial_ms']:>9.2f}ms | "
              f"{n_libs}线程 {out['parallel_ms']:>9.2f}ms | 提速 {out['speedup_x']}x / "
              f"理想 {n_libs}x | 线程扫描 {sweep} | "
              f"N线程/1线程 = {out['wall_ratio_vs_1_thread']}（理想 ~{n_libs}，"
              f"远超即 convoy）", flush=True)
        return out
    finally:
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ─────────────────────────── 清理 ───────────────────────────


def cleanup(n: int, n_libs: int) -> Dict[str, Any]:
    import shutil

    from src.core.settings import load_settings
    from src.libs.search.opensearch_store import OpenSearchStore, kb_index_name
    from src.libs.vector_store.chroma_store import ChromaStore

    removed_bm25 = []
    removed_chroma = []
    removed_opensearch = []

    settings = load_settings()
    store = OpenSearchStore()
    for i in range(n_libs):
        col = collection_for(n, i)

        d = index_dir_for(col)
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
            removed_bm25.append(col)

        try:
            cs = ChromaStore(settings, collection_name=col)
            if cs.collection.count() >= 0:
                cs.client.delete_collection(col)
                removed_chroma.append(col)
        except Exception:  # noqa: BLE001 —— 不存在就跳过
            pass

        idx = kb_index_name(col)
        if store.drop_index(idx):
            removed_opensearch.append(idx)

    print(f"清理完毕：BM25 {len(removed_bm25)} / Chroma {len(removed_chroma)} / "
          f"OpenSearch {len(removed_opensearch)}")
    return {
        "removed_bm25": removed_bm25,
        "removed_chroma": removed_chroma,
        "removed_opensearch": removed_opensearch,
    }


# ─────────────────────────── main ───────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--chunks", type=int, default=20000,
                    help="每个 collection 的块数（默认 20000；目标规模基准是 143000，"
                        "见脚本 docstring 关于时间预算的说明）")
    ap.add_argument("--libs", type=int, default=6, help="并行 collection 数")
    ap.add_argument("--top-k", type=int, default=10)
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--seed", type=int, default=20260827)
    ap.add_argument("--thread-counts", default="1,2,3,6")
    ap.add_argument("--force-reseed", action="store_true",
                    help="即使数据已存在也重新生成（默认存在即跳过种数据步骤）")
    ap.add_argument("--skip-seed", action="store_true",
                    help="跳过种数据，假定数据已经在（配合分步调试用）")
    ap.add_argument("--keep", action="store_true", help="跑完不清理（默认跑完即清理）")
    ap.add_argument("--cleanup-only", action="store_true", help="只清理，不跑测量")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    n, n_libs = args.chunks, args.libs

    if args.cleanup_only:
        cleanup(n, n_libs)
        return 0

    verify_hot_term_survives_tokenization(HOT_TERM)

    from src.libs.search.opensearch_store import OpenSearchStore

    if not OpenSearchStore().ping():
        print("❌ OpenSearch 未运行。起：docker compose up -d opensearch")
        return 2

    code_state = capture_code_state()
    print("=" * 78)
    print(f"代码状态: {code_state['branch']} @ {code_state['commit']}")
    print(f"          {code_state['warning']}")
    for p in code_state["dirty_paths"]:
        print(f"          脏文件: {p}")
    print(f"规模: {n} 块 × {n_libs} 库 | 查询词: {HOT_TERM!r}（Zipf 秩 1，jieba 分词后幸存已验证）")
    print("⚠️ 合成语料：仅覆盖并发/延迟行为，不涉及检索质量与召回率")
    print("=" * 78)

    import numpy as np
    rng = np.random.default_rng(args.seed)
    # 向量与 BM25 词频统计各自独立生成（seed 不同没关系——dense 侧只需要
    # "能被查到、不崩"，不需要跟 sparse 侧内容对应，这次测的是并发行为不是召回）。
    stats, _ = generate_term_stats(n, seed=args.seed, progress=False)
    embeddings = {
        s["chunk_id"]: rng.normal(size=768).astype("float32").tolist()
        for s in stats
    }
    query_vector = rng.normal(size=768).astype("float32").tolist()

    seed_report: Dict[str, Any] = {}
    old_col0_exists = (index_dir_for(collection_for(n, 0))
                       / f"{collection_for(n, 0)}_bm25.sqlite").exists()
    if args.skip_seed:
        print("\n⏭  跳过种数据（--skip-seed）")
    elif old_col0_exists and not args.force_reseed:
        print(f"\n⏭  数据已存在（{collection_for(n, 0)}），跳过种数据（加 --force-reseed 强制重来）")
    else:
        print(f"\n── 种旧链路数据（BM25 SQLite/JSON + Chroma），{n} 块 × {n_libs} 库 ──")
        seed_report["old"] = seed_old_backend(n, n_libs, embeddings, seed=args.seed,
                                              force=args.force_reseed)
        print(f"\n── 种新链路数据（OpenSearch），{n} 块 × {n_libs} 库 ──")
        seed_report["new"] = seed_new_backend(n, n_libs, embeddings, seed=args.seed)

    cols = [collection_for(n, i) for i in range(n_libs)]
    thread_counts = sorted({int(x) for x in args.thread_counts.split(",") if x.strip()})

    print("\n── 预热 hub（reranker/embedding client 构造一次，不计入下面的计时）──")
    t0 = time.perf_counter()
    hub = make_warmed_hub()
    print(f"    预热完成 {time.perf_counter() - t0:.1f}s", flush=True)

    print(f"\n── 并发扫描：{n} 块 × {n_libs} 库，查询词 {HOT_TERM!r}，"
          f"线程数 {thread_counts} ──")

    results: Dict[str, Any] = {}
    print("\n  旧链路 · BM25 JSON + Chroma")
    results["old_json"] = run_concurrency_scan(
        hub, cols, query_vector, args.top_k, thread_counts, args.repeat,
        label="旧链路(JSON)", env={"RAGENT_OPENSEARCH_READ": "off",
                                  "RAGENT_BM25_READ_BACKEND": "json"},
    )
    print("\n  旧链路 · BM25 SQLite + Chroma（GIL convoy 已知发生在这条路径上）")
    results["old_sqlite"] = run_concurrency_scan(
        hub, cols, query_vector, args.top_k, thread_counts, args.repeat,
        label="旧链路(SQLite)", env={"RAGENT_OPENSEARCH_READ": "off",
                                    "RAGENT_BM25_READ_BACKEND": "sqlite"},
    )
    print("\n  新链路 · OpenSearch")
    results["new_opensearch"] = run_concurrency_scan(
        hub, cols, query_vector, args.top_k, thread_counts, args.repeat,
        label="新链路(OpenSearch)", env={"RAGENT_OPENSEARCH_READ": ",".join(cols)},
    )

    # convoy 判据：N 线程挂钟 / 1 线程挂钟，远超 N 本身才算 convoy（不只是排队）。
    def _judge(row: Dict[str, Any]) -> str:
        ratio = row.get("wall_ratio_vs_1_thread")
        n_libs_ = row["n_libs"]
        if ratio is None:
            return "unknown"
        if ratio > n_libs_ * 1.5:
            return "convoy（超线性恶化）"
        if ratio > n_libs_ * 0.6:
            return "线性排队（符合预期，不是 convoy）"
        return "接近真并行"

    verdict = {k: _judge(v) for k, v in results.items()}
    print("\n=== 判定 ===")
    for k, v in verdict.items():
        print(f"  {k}: {v}")

    cleanup_report = None
    if not args.keep:
        print("\n── 清理临时数据 ──")
        cleanup_report = cleanup(n, n_libs)
    else:
        print(f"\n⚠️ --keep 已指定，未清理。数据仍在：collection 前缀 "
              f"{PREFIX}{n}_lib*（BM25/Chroma/OpenSearch 三处都有），"
              "跑完记得手动 --cleanup-only。")

    out_dir = Path(args.out) if args.out else DEFAULT_OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"opensearch_scale_concurrency_{datetime.now():%Y%m%d_%H%M%S}.json"
    out.write_text(json.dumps({
        "generated_at": datetime.now().isoformat(),
        "script": str(Path(__file__).resolve()),
        "code_state": code_state,
        "args": vars(args),
        "hot_term": HOT_TERM,
        "corpus_kind": "synthetic",
        "corpus_caveat": (
            "合成 term_stats + 随机向量，非真实文本/真实语义。"
            "只可用于评价并发/延迟/GIL 竞争行为；"
            "不可用于评价检索质量、召回率或答案正确性（已在其他脚本用真实黄金测试集验过）。"
        ),
        "seed_report": seed_report,
        "results": results,
        "verdict": verdict,
        "cleanup": cleanup_report,
        "not_covered": [
            "合成语料，不涉及检索质量/召回率/答案正确性",
            "不含 cross-encoder 重排阶段（对齐 simulate_search_traffic.py 的既有范围）",
            "dense 侧用随机向量、跳过真实 Ollama embedding 调用，"
            "隔离掉了 Ollama 单线程串行这个已知的独立瓶颈（CLAUDE.md §4 第 3 条），"
            "不代表端到端 TTFT 里 embedding 那一段也没有并发问题",
            "只在本机 Apple Silicon / Docker Desktop 单节点 OpenSearch 上跑过，"
            "未覆盖生产硬件、多节点集群、security plugin 开启后的开销",
            "只测了一个高频查询词（Zipf 秩 1，最坏情况）；未扫描不同命中率的查询分布",
            "conv_* 对话私有库的并发行为未测（只测了 kb_ 业务库这条路径）",
        ],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果 → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
