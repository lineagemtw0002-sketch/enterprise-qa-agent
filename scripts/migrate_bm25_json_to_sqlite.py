#!/usr/bin/env python3
"""把存量 BM25 JSON 索引迁移成 SQLite 副本（方案 C 阶段 3）。

    # 先看会做什么，不写任何东西
    python scripts/migrate_bm25_json_to_sqlite.py --dry-run

    # 只迁真实业务库（跳过 conv_* 会话残留与 test_* 测试库）
    python scripts/migrate_bm25_json_to_sqlite.py --skip-transient

    # 迁指定的几个
    python scripts/migrate_bm25_json_to_sqlite.py --collection tenant_acme_hr

**为什么这一步必须排在"切读"之前**：设计文档 §6 原本把切读列为阶段 2、
存量迁移列为阶段 3。**那个顺序是错的** —— 存量 collection 只有 JSON、没有
SQLite 副本，先切读会让所有既有数据的稀疏检索直接返回空。
（`BM25Indexer._use_sqlite_for_read` 的 `auto` 模式有回退兜底，所以不至于
真出事，但那样等于"迁移完成前一直在走老路径"，切读也就没有意义。）

## 幂等

对同一个 collection 重复跑得到同一个结果：`replace_all` 每次都在单事务里
`DELETE` 再 `INSERT` 整份 terms/postings。中途失败不会留下半份索引。

## 每迁完一个都做打分比对，不是迁完拉倒

迁移的验收标准是**两种后端对同一批查询的完整分数映射逐 bit 相同**，
不是"文件生成了"。查询词从该库自己的高频词里取，因为高频词的 postings 最长、
最容易暴露问题；低频词就算全错也可能碰巧对上。

⚠️ **`doc_hash` 迁不过来。** JSON 索引里根本没存这个字段（那正是
`remove_document` 那条 P0 的根因：存储结构里就没有能关联文档的东西）。
所以迁移出来的 chunks 表 `doc_hash` 全是 NULL，**这些库的按文档删除依然失效**，
要等它们各自的文档下次重新摄入时才会补上。脚本会把这一点算进报告。
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

from src.ingestion.storage.bm25_indexer import BM25Indexer  # noqa: E402
from src.ingestion.storage.bm25_sqlite_store import BM25SQLiteStore  # noqa: E402

# 会话集合与测试库：设计文档 §8 第 4 条拍板"只迁真实业务库"。
# 99 个 collection 里大量是这两类残留，迁了纯属浪费磁盘。
_TRANSIENT_PATTERNS = (
    re.compile(r"^conv_"),
    re.compile(r"^test_"),
    re.compile(r"^e2e_"),
    re.compile(r"_test$"),
)

_VERIFY_QUERIES = 8   # 每个库比对几组查询
_VERIFY_TERMS = 5     # 每组查询取几个词条


def is_transient(collection: str) -> bool:
    return any(p.search(collection) for p in _TRANSIENT_PATTERNS)


def discover(root: Path) -> List[Tuple[str, Path, Path]]:
    """找出所有有 JSON 索引的 collection，返回 (名字, 该库的 index_dir, json 路径)。

    真实布局是**每个 collection 一个子目录**：
        data/db/bm25/{collection}/{collection}_bm25.json
    这来自 `pipeline.py`：`BM25Indexer(index_dir="data/db/bm25/{collection}")`，
    也就是说 `BM25Indexer` 的 `index_dir` 参数指的是**单个库的目录**，
    不是所有库的根目录。两种理解差一层，扁平地扫根目录会一个都找不到。

    也顺带支持扁平布局（`data/db/bm25/{collection}_bm25.json`），
    因为测试和早期数据里两种都出现过。
    """
    found: List[Tuple[str, Path, Path]] = []

    for json_path in sorted(root.glob("*/*_bm25.json")):
        collection = json_path.name[: -len("_bm25.json")]
        found.append((collection, json_path.parent, json_path))

    for json_path in sorted(root.glob("*_bm25.json")):
        collection = json_path.name[: -len("_bm25.json")]
        found.append((collection, root, json_path))

    return found


def pick_verify_queries(index: Dict[str, Any]) -> List[List[str]]:
    """从索引里挑几组查询词做比对。

    按 postings 长度降序取词 —— 高频词扫描量最大、最能暴露实现差异。
    只用低频词做比对是自欺：它们的 postings 可能只有一两条，
    随便怎么实现都对得上。
    """
    by_len = sorted(index.items(), key=lambda kv: -len(kv[1]["postings"]))
    hot = [term for term, _ in by_len[: _VERIFY_QUERIES * _VERIFY_TERMS]]
    if not hot:
        return []
    queries = []
    for i in range(0, min(len(hot), _VERIFY_QUERIES * _VERIFY_TERMS), _VERIFY_TERMS):
        chunk = hot[i : i + _VERIFY_TERMS]
        if chunk:
            queries.append(chunk)
    return queries[:_VERIFY_QUERIES]


def verify(
    indexer: BM25Indexer,
    store: BM25SQLiteStore,
    queries: List[List[str]],
) -> Tuple[int, int, Optional[str]]:
    """比对两种后端的完整分数映射。返回 (比对数, 不一致数, 首个不一致的描述)。"""
    compared = 0
    mismatched = 0
    first_detail = None

    for q in queries:
        json_side = {r["chunk_id"]: r["score"] for r in indexer.query(q, top_k=10**7)}
        sql_side = {r["chunk_id"]: r["score"] for r in store.query(
            q, top_k=10**7, k1=indexer.k1, b=indexer.b
        )}

        if json_side.keys() != sql_side.keys():
            mismatched += 1
            if first_detail is None:
                only_json = set(json_side) - set(sql_side)
                only_sql = set(sql_side) - set(json_side)
                first_detail = (
                    f"候选集不同 查询={q[:3]} "
                    f"仅JSON有={len(only_json)} 仅SQLite有={len(only_sql)}"
                )
            continue

        for cid, score in json_side.items():
            compared += 1
            if score != sql_side[cid]:      # 逐 bit，不是 approx
                mismatched += 1
                if first_detail is None:
                    first_detail = (
                        f"分数不同 chunk={cid} 查询={q[:3]} "
                        f"json={score!r} sqlite={sql_side[cid]!r}"
                    )

    return compared, mismatched, first_detail


def migrate_one(
    index_dir: Path,
    collection: str,
    json_path: Path,
    dry_run: bool,
) -> Dict[str, Any]:
    """`index_dir` 是**该 collection 自己的目录**，不是根目录 —— 见 `discover`。"""
    result: Dict[str, Any] = {
        "collection": collection,
        "json_mb": round(json_path.stat().st_size / 1e6, 2),
    }

    indexer = BM25Indexer(index_dir=str(index_dir))
    # 强制读 JSON：迁移期两边都在，走 auto 会读到刚写的 SQLite，
    # 那样比对就变成"自己跟自己比"，永远通过。
    indexer.read_backend = "json"
    t0 = time.perf_counter()
    if not indexer._load_json_index(collection):
        result["status"] = "skipped"
        result["reason"] = "JSON 索引读不出来"
        return result
    result["load_s"] = round(time.perf_counter() - t0, 2)
    result["num_docs"] = indexer._metadata.get("num_docs", 0)
    result["terms"] = len(indexer._index)

    if dry_run:
        result["status"] = "would-migrate"
        return result

    sqlite_path = index_dir / f"{collection}_bm25.sqlite"
    t0 = time.perf_counter()
    try:
        with BM25SQLiteStore(sqlite_path) as store:
            store.replace_all(index=indexer._index, metadata=indexer._metadata)
            result["write_s"] = round(time.perf_counter() - t0, 2)

            queries = pick_verify_queries(indexer._index)
            compared, mismatched, detail = verify(indexer, store, queries)
    except Exception as exc:  # noqa: BLE001
        result["status"] = "failed"
        result["reason"] = f"{type(exc).__name__}: {exc}"
        return result

    result["sqlite_mb"] = round(sqlite_path.stat().st_size / 1e6, 2)
    result["verified_scores"] = compared
    result["mismatched"] = mismatched
    result["status"] = "ok" if mismatched == 0 else "MISMATCH"
    if detail:
        result["detail"] = detail
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--index-dir", default="data/db/bm25")
    ap.add_argument("--dry-run", action="store_true", help="只报告，不写任何文件")
    ap.add_argument(
        "--skip-transient",
        action="store_true",
        help="跳过 conv_* / test_* / e2e_* 这类会话残留与测试库",
    )
    ap.add_argument(
        "--collection", action="append", default=None,
        help="只迁指定的 collection，可重复",
    )
    args = ap.parse_args()

    index_dir = Path(args.index_dir)
    if not index_dir.is_dir():
        print(f"❌ 索引目录不存在：{index_dir}")
        return 2

    candidates = discover(index_dir)
    if args.collection:
        wanted = set(args.collection)
        candidates = [c for c in candidates if c[0] in wanted]
    skipped_transient = [c[0] for c in candidates if is_transient(c[0])]
    if args.skip_transient:
        candidates = [c for c in candidates if not is_transient(c[0])]

    print(f"索引目录：{index_dir}")
    print(f"发现 {len(candidates)} 个待处理 collection"
          f"{f'（跳过 {len(skipped_transient)} 个会话/测试库）' if skipped_transient else ''}")
    if args.dry_run:
        print("⚠️  --dry-run：只报告，不写任何文件\n")
    print()

    results = []
    for i, (collection, coll_dir, json_path) in enumerate(candidates, 1):
        print(f"[{i}/{len(candidates)}] {collection} ... ", end="", flush=True)
        r = migrate_one(coll_dir, collection, json_path, args.dry_run)
        results.append(r)
        status = r["status"]
        if status == "ok":
            print(f"✅ {r['num_docs']} 文档 / {r['terms']} 词条 · "
                  f"{r['json_mb']}MB → {r['sqlite_mb']}MB · "
                  f"打分比对 {r['verified_scores']} 条全部逐 bit 相同")
        elif status == "would-migrate":
            print(f"（将迁移）{r['num_docs']} 文档 / {r['terms']} 词条 / {r['json_mb']}MB")
        elif status == "MISMATCH":
            print(f"❌ 打分不一致 {r['mismatched']} 处：{r.get('detail')}")
        else:
            print(f"⚠️  {status}：{r.get('reason')}")

    counts = Counter(r["status"] for r in results)
    print("\n" + "=" * 64)
    for status, n in counts.most_common():
        print(f"  {status:>14}: {n}")

    if not args.dry_run and counts.get("ok"):
        tj = sum(r.get("json_mb", 0) for r in results if r["status"] == "ok")
        ts = sum(r.get("sqlite_mb", 0) for r in results if r["status"] == "ok")
        print(f"\n  磁盘：JSON {tj:.1f}MB → SQLite {ts:.1f}MB"
              f"（{ts / tj * 100:.0f}%）" if tj else "")
        print(
            "\n⚠️  迁移出来的 chunks.doc_hash 全为 NULL —— JSON 索引里就没存这个\n"
            "    字段。这些库的「按文档删除」依然失效，要等各自文档下次重新摄入\n"
            "    才会补上。详见 CLAUDE.md §4 第 7 条。"
        )

    bad = counts.get("MISMATCH", 0) + counts.get("failed", 0)
    if bad:
        print(f"\n❌ {bad} 个 collection 未通过，**不要切读**（保持 "
              f"RAGENT_BM25_READ_BACKEND=auto 或 json）。")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
