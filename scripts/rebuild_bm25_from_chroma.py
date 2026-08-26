#!/usr/bin/env python3
"""用当前分词器重建存量 BM25 索引（JSON + SQLite 副本）。

    python scripts/rebuild_bm25_from_chroma.py --dry-run
    python scripts/rebuild_bm25_from_chroma.py --skip-transient
    python scripts/rebuild_bm25_from_chroma.py --collection mmarco

## 什么时候必须跑

**改了 `src/core/tokenization.py` 就必须跑。** BM25 是词条级精确匹配，
索引里存的是建库当时那套分词器的产物；换了分词器而不重建，等于拿新查询词条
去查旧词表 —— 比对出来的任何数字都没有意义（新词条大多根本不在旧索引里）。

配套的 OpenSearch 侧重建是 `scripts/migrate_to_opensearch.py`。
**两个都要跑**，否则两个后端的检索结果会不一致，而灰度期正是靠"两边重叠率"
做判据的。顺序：先这个（`migrate_to_opensearch.py` 的 verify 会拿 BM25
索引里的高频词当比对查询，BM25 旧的话比对基准就是旧的）。

## 数据从哪来

正文在 **Chroma** —— BM25 索引里只有词频，没有原文，所以重建不能"就地转换"，
必须回到原文重新分词。这与 `migrate_to_opensearch.py` 的取数方式一致。

⚠️ **`doc_hash` 顺带补上**（从 Chroma metadata 取）。存量 JSON 索引里没有这个
字段，`CLAUDE.md` §4 第 2c 条记的"`doc_hash` 迁不过来、按文档删除依然失效"
就是指这个。这里重建时一并写入，不需要等文档下次重新摄入。

## 判据

重建前后**不可能**逐 bit 相同 —— 分词变了，词表和分数当然都变。所以这里
不做"新旧一致"的比对（那是上一轮存储层迁移的判据，此处不适用），只报告
规模变化，并让调用方去跑真正的判据：

    scripts/ablate_tokenizer_alignment.py      稀疏侧 recall@k（人工标注）
    scripts/verify_opensearch_parity.py        两个后端的召回率
    scripts/run_tenant_kb_golden_tests.py      端到端答案质量（需真实 LLM）
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 与 migrate_to_opensearch.py 保持同一套判定，避免两个脚本处理的库集合不同
_TRANSIENT = (
    re.compile(r"^conv_"),
    re.compile(r"^test_"),
    re.compile(r"^e2e_"),
    re.compile(r"_test$"),
    re.compile(r"^verify_"),
)

# `__summary` 层是纯向量层，不建 BM25 —— 给它建一份是纯浪费，而且会让
# `data/db/bm25/` 下凭空多出一批目录，将来分不清哪些是真在用的。
_SUMMARY_SUFFIX = "__summary"


def is_transient(name: str) -> bool:
    return any(p.search(name) for p in _TRANSIENT)


def discover(chroma_path: Path) -> List[str]:
    import chromadb

    client = chromadb.PersistentClient(path=str(chroma_path))
    return sorted(c.name for c in client.list_collections())


def rebuild_one(
    collection: str, chroma_path: Path, bm25_dir: Path, dry_run: bool
) -> Dict[str, Any]:
    import chromadb

    from src.ingestion.embedding.sparse_encoder import SparseEncoder
    from src.ingestion.storage.bm25_indexer import BM25Indexer

    res: Dict[str, Any] = {"collection": collection}
    out_dir = bm25_dir / collection
    json_path = out_dir / f"{collection}_bm25.json"
    res["before_bytes"] = json_path.stat().st_size if json_path.exists() else 0

    client = chromadb.PersistentClient(path=str(chroma_path))
    col = client.get_collection(collection)
    n = col.count()
    res["chunks"] = n
    if n == 0:
        res["status"] = "skipped"
        res["reason"] = "空 collection"
        return res
    if dry_run:
        res["status"] = "would-rebuild"
        return res

    got = col.get(include=["documents", "metadatas"])
    ids, docs = got["ids"], got["documents"]
    metas = got["metadatas"] or [{}] * n

    t0 = time.perf_counter()
    try:
        encoder = SparseEncoder()
        term_stats = []
        doc_hash_by_chunk: Dict[str, str] = {}
        for cid, text, meta in zip(ids, docs, metas):
            terms = encoder._tokenize(text or "")
            term_stats.append(
                {
                    "chunk_id": cid,
                    "term_frequencies": dict(Counter(terms)),
                    "doc_length": len(terms),
                }
            )
            dh = (meta or {}).get("doc_hash")
            if dh:
                doc_hash_by_chunk[cid] = dh

        # 整库重建 = 先清空。不清的话 `build()` 虽然会整体覆盖 JSON，
        # 但同目录下的 SQLite 副本是增量写的，旧词条会以"新旧混在一起"
        # 的形式活下来 —— 那正是这次要消灭的东西。
        if out_dir.exists():
            shutil.rmtree(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        ix = BM25Indexer(index_dir=str(out_dir))
        ix.build(
            term_stats,
            collection=collection,
            doc_hash_by_chunk=doc_hash_by_chunk or None,
        )
        res["build_s"] = round(time.perf_counter() - t0, 2)
        res["terms"] = len(ix._index)
        res["postings"] = sum(len(v["postings"]) for v in ix._index.values())
        res["after_bytes"] = json_path.stat().st_size if json_path.exists() else 0
        res["doc_hash"] = len(doc_hash_by_chunk)
        res["status"] = "ok"
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

    chroma_path = Path(args.chroma_path)
    bm25_dir = Path(args.bm25_dir)

    names = [n for n in discover(chroma_path) if not n.endswith(_SUMMARY_SUFFIX)]
    if args.collection:
        names = [n for n in names if n in set(args.collection)]
    transient = [n for n in names if is_transient(n)]
    if args.skip_transient:
        names = [n for n in names if not is_transient(n)]

    print(f"Chroma: {chroma_path} → BM25: {bm25_dir}")
    print(
        f"待重建 {len(names)} 个 collection"
        f"{f'（跳过 {len(transient)} 个会话/测试库）' if args.skip_transient and transient else ''}"
    )
    if args.dry_run:
        print("⚠️  --dry-run：只报告，不写任何东西")
    print()

    results = []
    for i, name in enumerate(names, 1):
        print(f"[{i}/{len(names)}] {name} ... ", end="", flush=True)
        r = rebuild_one(name, chroma_path, bm25_dir, args.dry_run)
        results.append(r)
        st = r["status"]
        if st == "ok":
            grow = (
                f"{r['before_bytes'] / 1024:.0f}KB → {r['after_bytes'] / 1024:.0f}KB"
                f"（{r['after_bytes'] / r['before_bytes']:.2f}x）"
                if r["before_bytes"]
                else f"{r['after_bytes'] / 1024:.0f}KB（新建）"
            )
            print(
                f"✅ {r['chunks']} 块 · {r['build_s']}s · "
                f"词表 {r['terms']} · postings {r['postings']} · {grow} · "
                f"doc_hash {r['doc_hash']}/{r['chunks']}"
            )
        elif st == "would-rebuild":
            print(f"（将重建）{r['chunks']} 块")
        else:
            print(f"⚠️  {st}：{r.get('reason')}")

    counts = Counter(r["status"] for r in results)
    ok = [r for r in results if r["status"] == "ok"]
    print("\n" + "=" * 60)
    for st, n in counts.most_common():
        print(f"  {st:>16}: {n}")
    if ok:
        b = sum(r["before_bytes"] for r in ok)
        a = sum(r["after_bytes"] for r in ok)
        print(
            f"\n  JSON 总量 {b / 1024:.0f}KB → {a / 1024:.0f}KB"
            f"（{a / b:.2f}x）" if b else ""
        )
        no_hash = [r["collection"] for r in ok if r["doc_hash"] < r["chunks"]]
        if no_hash:
            print(
                f"\n⚠️ 这些库有 chunk 在 Chroma 里没有 `doc_hash` metadata，"
                f"按文档删除对它们仍然失效：{', '.join(no_hash[:8])}"
                f"{' …' if len(no_hash) > 8 else ''}"
            )
    if counts.get("failed"):
        print(f"\n❌ {counts['failed']} 个失败")
        return 1
    print(
        "\n⚠️ 重建只是前提，不是验收。判据要另外跑："
        "\n   scripts/ablate_tokenizer_alignment.py（稀疏侧 recall）"
        "\n   scripts/verify_opensearch_parity.py（两个后端召回率）"
        "\n   scripts/run_tenant_kb_golden_tests.py（端到端，需真实 LLM）"
        "\n⚠️ OpenSearch 侧还要单独重建：scripts/migrate_to_opensearch.py"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
