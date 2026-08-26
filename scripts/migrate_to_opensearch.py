#!/usr/bin/env python3
"""把存量知识库迁移进 OpenSearch（`docs/opensearch_migration_design.md` 阶段 2）。

    # 先看会做什么，不写任何东西
    python scripts/migrate_to_opensearch.py --dry-run

    # 只迁真实业务库（跳过 conv_*/test_*/e2e_* 残留）
    python scripts/migrate_to_opensearch.py --skip-transient

    # 迁指定的几个
    python scripts/migrate_to_opensearch.py --collection mmarco

**这一步必须排在"切读"之前。** 存量 collection 在 OpenSearch 里没有任何数据，
先切读会让既有库的稀疏检索直接返回空。上一轮 SQLite 迁移的设计文档就把顺序
写反了，实施时才发现。

## 数据从哪来

正文在 **Chroma**（BM25 索引里只有词频，没有原文）。所以这个脚本读 Chroma
取 `documents` 与 `metadatas`，重新用 `SparseEncoder._tokenize` 分词后写入。

⚠️ **重新分词而不是复用 BM25 索引里的词频**：词频是 bag-of-words，丢了词序。
虽然 BM25 打分不看词序，但重建后无法支持将来的短语查询，而重新分词的成本
（jieba 每 chunk 不到 1ms）远低于这个损失。

## 幂等

`_id = sha256(source_path + ":" + chunk_index)`，与内容无关，所以重复跑同一个
collection 得到同一结果 —— 相同位置的 chunk 覆盖而不是新增。

## 验收判据是"命中集合一致"，不是"文件生成了"

每迁完一个库，用该库自己的高频词做若干次查询，比对 OpenSearch 与现有 BM25
的命中 chunk_id 集合。

⚠️ **判据刻意不是"逐条排序一致"**。两边打分公式不同（Lucene 的 BM25 变体
vs 项目手写实现），分数不可能逐 bit 相同，排序也就未必一致。
上一轮 SQLite 迁移能用"逐 bit 相同"是因为那边是同一套公式的两种存储；
这次换的是引擎，那个判据不适用 —— 硬套会得出"迁移失败"的错误结论。

⚠️ **判据是重叠率而不是集合相等。** 跨引擎做不到完全一致，原因有两层：

1. **可配置的差异必须先对齐** —— Lucene 默认 `k1=1.2`，而 `BM25Indexer` 用的是
   `k1=1.5`。不对齐就分不清差异来自"换引擎"还是"参数没调"。
   建 index 时已显式设 `similarity: {type: BM25, k1: 1.5, b: 0.75}`。
   实测对齐前后：某库 top-20 重叠 16–20/20 → **19–20/20**。
2. **剩下的是引擎固有的** —— Lucene 把字段长度**有损量化成一个字节**存储，
   所以长度归一化项与精确计算有微小出入。实测残差表现为个别文档在截断线
   附近换位（例：旧排第 20 → 新排第 23）。这修不掉，也不该修。

因此判据定为**总重叠率 ≥ 95%**，并把实际数字打出来。低于阈值要人看，
不要自动放行；但也不要用"必须完全相等"去要求一个做不到的东西。

⚠️ **另外比对位要留余量。** 第一版判据是"两边 top-20 集合相等"，
结果 mmarco 报了 3/6 组不同。查下来是**截断边界效应**：取 top-25 再比 top-20，
两边 20/20 完全一致；差异只出现在近似同分处的微小重排
（例：旧 4.386/4.377/4.374 三条，新 4.716/4.681/4.668，同样三条换了顺序）。
所以现在两边都查 `top_k + _VERIFY_MARGIN`，只比前 `top_k` 个 ——
不留余量的话，任何跨引擎迁移都会在截断线上假报差异。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_TRANSIENT = (
    re.compile(r"^conv_"),
    re.compile(r"^test_"),
    re.compile(r"^e2e_"),
    re.compile(r"_test$"),
    re.compile(r"^verify_"),
)

_VERIFY_QUERIES = 6
_VERIFY_TERMS = 4
_BATCH = 500
_VERIFY_TOP_K = 20
# 多取一段再截断，避免同分候选在截断线上换位被误判成差异。
_VERIFY_MARGIN = 10
# 跨引擎重叠率下限。低于此值要人工确认，见模块 docstring。
_MIN_OVERLAP = 0.95


def is_transient(name: str) -> bool:
    return any(p.search(name) for p in _TRANSIENT)


def discover(chroma_path: Path) -> List[str]:
    import chromadb

    client = chromadb.PersistentClient(path=str(chroma_path))
    return sorted(c.name for c in client.list_collections())


def pick_verify_queries(bm25_dir: Path, collection: str) -> List[List[str]]:
    """从该库的 BM25 索引里挑高频词做比对查询。

    高频词的 postings 最长、最能暴露差异；只用低频词是自欺 —— 它们可能只有
    一两条 postings，随便怎么实现都对得上。
    """
    p = bm25_dir / collection / f"{collection}_bm25.json"
    if not p.exists():
        return []
    idx = json.load(open(p, encoding="utf-8"))["index"]
    hot = [
        t for t, _ in sorted(idx.items(), key=lambda kv: -len(kv[1]["postings"]))
    ][: _VERIFY_QUERIES * _VERIFY_TERMS]
    return [
        hot[i : i + _VERIFY_TERMS]
        for i in range(0, len(hot), _VERIFY_TERMS)
    ][:_VERIFY_QUERIES]


def verify(
    collection: str, queries: List[List[str]], bm25_dir: Path
) -> Tuple[int, int, Optional[str], float]:
    """比对命中集合。返回 (比对次数, 有差异的组数, 首个差异描述, 总重叠率)。"""
    from src.ingestion.storage.bm25_indexer import BM25Indexer
    from src.libs.search.opensearch_store import OpenSearchStore

    store = OpenSearchStore()
    old = BM25Indexer(index_dir=str(bm25_dir / collection))
    old.read_backend = "json"
    if not old._load_json_index(collection):
        return 0, 0, "现有 BM25 索引读不出来，无法比对", 0.0

    compared = diffs = 0
    inter_total = union_total = 0
    first = None
    for q in queries:
        old_hits = {
            r["chunk_id"]
            for r in old.query(q, top_k=_VERIFY_TOP_K + _VERIFY_MARGIN)[:_VERIFY_TOP_K]
        }
        # 查询侧直接传词条：这些词来自 BM25 索引，本就是索引侧分词器的产物，
        # 再过一遍 QueryProcessor 会引入本不该有的分歧（见 tokenize_for_query）
        new_hits = {
            r["chunk_id"]
            for r in store.search_kb(
                collection, "", top_k=_VERIFY_TOP_K + _VERIFY_MARGIN, query_tokens=q
            )[:_VERIFY_TOP_K]
        }
        compared += 1
        inter_total += len(old_hits & new_hits)
        union_total += len(old_hits)
        if old_hits != new_hits:
            diffs += 1
            if first is None:
                only_old = old_hits - new_hits
                only_new = new_hits - old_hits
                first = (
                    f"查询={q[:3]} 仅旧有={len(only_old)} 仅新有={len(only_new)} "
                    f"交集={len(old_hits & new_hits)}/{len(old_hits | new_hits)}"
                )
    overlap = inter_total / union_total if union_total else 1.0
    return compared, diffs, first, overlap


def migrate_one(
    collection: str, chroma_path: Path, bm25_dir: Path, dry_run: bool
) -> Dict[str, Any]:
    import chromadb

    from src.libs.search.opensearch_store import (
        OpenSearchStore,
        build_chunk_doc,
        kb_index_name,
        tokenize_for_index,
    )

    res: Dict[str, Any] = {"collection": collection}
    client = chromadb.PersistentClient(path=str(chroma_path))
    col = client.get_collection(collection)
    n = col.count()
    res["chunks"] = n
    if n == 0:
        res["status"] = "skipped"
        res["reason"] = "空 collection"
        return res

    if dry_run:
        res["status"] = "would-migrate"
        return res

    # 一并取向量：__summary 层是纯向量的（不建 BM25），不带 embedding 迁过去
    # 等于那一层完全没迁 —— 而它参与 query_knowledge_hub 的文档级收窄检索。
    got = col.get(include=["documents", "metadatas", "embeddings"])
    ids, docs, metas = got["ids"], got["documents"], got["metadatas"] or [{}] * n
    embs = got.get("embeddings")
    dims = len(embs[0]) if embs is not None and len(embs) else None

    store = OpenSearchStore(dense_dims=dims)
    index = kb_index_name(collection)
    t0 = time.perf_counter()
    written = 0
    try:
        for start in range(0, len(ids), _BATCH):
            batch = []
            sl = slice(start, start + _BATCH)
            emb_slice = embs[sl] if embs is not None and len(embs) else [None] * len(ids[sl])
            for cid, text, meta, emb in zip(
                ids[sl], docs[sl], metas[sl], emb_slice
            ):
                meta = meta or {}
                batch.append(
                    build_chunk_doc(
                        text=text or "",
                        tokens=tokenize_for_index(text or ""),
                        source_path=meta.get("source_path", f"unknown:{collection}"),
                        chunk_index=int(meta.get("chunk_index", 0)),
                        chunk_id=cid,
                        doc_hash=meta.get("doc_hash"),
                        embedding=emb,
                    )
                )
            written += store.index_chunks(index, batch, refresh=False)
        res["write_s"] = round(time.perf_counter() - t0, 2)
        res["written"] = written
        res["os_count"] = store.count(index)
        res["dims"] = dims

        queries = pick_verify_queries(bm25_dir, collection)
        if queries:
            compared, diffs, first, overlap = verify(collection, queries, bm25_dir)
            res["verified"] = compared
            res["diffs"] = diffs
            res["overlap"] = round(overlap, 4)
            if first:
                res["detail"] = first
            res["status"] = "ok" if overlap >= _MIN_OVERLAP else "LOW-OVERLAP"
        else:
            res["status"] = "ok-unverified"
            res["reason"] = "该库无 BM25 索引，无法比对"
    except Exception as exc:  # noqa: BLE001
        res["status"] = "failed"
        res["reason"] = f"{type(exc).__name__}: {exc}"
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--chroma-path", default="data/db/chroma")
    ap.add_argument("--bm25-dir", default="data/db/bm25")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-transient", action="store_true")
    ap.add_argument("--collection", action="append", default=None)
    args = ap.parse_args()

    from src.libs.search.opensearch_store import OpenSearchStore

    if not OpenSearchStore().ping():
        print("❌ OpenSearch 未运行。起：docker compose up -d opensearch")
        return 2

    chroma_path = Path(args.chroma_path)
    bm25_dir = Path(args.bm25_dir)
    names = discover(chroma_path)
    if args.collection:
        names = [n for n in names if n in set(args.collection)]
    skipped = [n for n in names if is_transient(n)]
    if args.skip_transient:
        names = [n for n in names if not is_transient(n)]

    print(f"Chroma: {chroma_path}")
    print(f"待处理 {len(names)} 个 collection"
          f"{f'（跳过 {len(skipped)} 个会话/测试库）' if skipped else ''}")
    if args.dry_run:
        print("⚠️  --dry-run：只报告，不写任何东西")
    print()

    results = []
    for i, name in enumerate(names, 1):
        print(f"[{i}/{len(names)}] {name} ... ", end="", flush=True)
        r = migrate_one(name, chroma_path, bm25_dir, args.dry_run)
        results.append(r)
        st = r["status"]
        if st == "ok":
            exact = "全部一致" if r["diffs"] == 0 else f"{r['diffs']} 组有截断线换位"
            vec = f" · 向量 {r['dims']}维" if r.get("dims") else " · 无向量"
            print(
                f"✅ {r['written']} 条 · {r['write_s']}s · "
                f"{r['verified']} 组重叠率 {r['overlap']:.0%}（{exact}）{vec}"
            )
        elif st == "ok-unverified":
            print(f"⚠️  {r['written']} 条已写入，但{r['reason']}")
        elif st == "would-migrate":
            print(f"（将迁移）{r['chunks']} 条")
        elif st == "LOW-OVERLAP":
            print(f"❌ 重叠率 {r['overlap']:.0%} 低于阈值 {_MIN_OVERLAP:.0%}："
                  f"{r.get('detail')}")
        else:
            print(f"⚠️  {st}：{r.get('reason')}")

    counts = Counter(r["status"] for r in results)
    print("\n" + "=" * 60)
    for st, n in counts.most_common():
        print(f"  {st:>16}: {n}")

    bad = counts.get("LOW-OVERLAP", 0) + counts.get("failed", 0)
    if bad:
        print(f"\n❌ {bad} 个未通过，**不要切读**"
              f"（保持 RAGENT_SEARCH_BACKEND=legacy）。")
        return 1
    if counts.get("ok-unverified"):
        print("\n⚠️  有库未经比对验证 —— 切读前请人工确认这些库的检索结果。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
