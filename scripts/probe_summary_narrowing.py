"""层次化检索粗筛层（`_narrow_by_document_summary`）的取样探针。

用途：回答"粗筛预算该怎么分配"这个设计问题时，需要的不是感觉而是数字——
本脚本对 `scripts/demo_kb_content/questions.py` 里的 60 条正向问题，逐条
在候选库的 `{collection}__summary` 摘要层做一次全量排序（top_k = 该库文档
总数），然后统计：

  * 金标文档（问题 `source` 字段标的那篇）在**它自己那个库**里排第几；
  * 同一篇文档在**全部候选库合并后的全局榜**里排第几；
  * 金标文档的摘要分数 与 全局第一名 的差距（判断"硬截断"是否有依据）。

这两个排名的差就是当前实现的损失来源：`_narrow_by_document_summary` 取的是
全局前 N，候选库越多、每库文档越多，全局排名被稀释得越厉害，而库内排名
不受候选库数量影响。

只读：不写任何 collection、不改配置、不碰 BM25 / rerank / LLM。

用法：
    .venv/bin/python scripts/probe_summary_narrowing.py                # 全部 12 库
    .venv/bin/python scripts/probe_summary_narrowing.py --tenant acme  # 只跑 Acme 6 库
    .venv/bin/python scripts/probe_summary_narrowing.py --json out.json
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.demo_kb_content.questions import ACME_KBS, GLOBEX_KBS, POSITIVE  # noqa: E402
from src.core.settings import load_settings  # noqa: E402
from src.ingestion.hierarchy.doc_summary import summary_collection_name  # noqa: E402
from src.libs.embedding.embedding_factory import EmbeddingFactory  # noqa: E402
from src.libs.vector_store.vector_store_factory import VectorStoreFactory  # noqa: E402


def _doc_no(hit: Dict[str, Any]) -> str:
    """摘要记录 -> 文档编号（ACME-IT-001 这种）。摘要层存的是 source_path，
    语料文件名前缀就是编号，见 scripts/generate_demo_kb_dataset.py。"""
    meta = hit.get("metadata") or {}
    path = meta.get("source_path") or ""
    name = Path(path).name
    return name.split("_", 1)[0] if name else ""


def probe(collections: List[str], json_path: Optional[str]) -> int:
    settings = load_settings()
    embed = EmbeddingFactory.create(settings)

    stores = {}
    sizes = {}
    for c in collections:
        store = VectorStoreFactory.create(settings, collection_name=summary_collection_name(c))
        stores[c] = store
        sizes[c] = store.get_collection_stats().get("count", 0)
    print("摘要层文档数：" + ", ".join(f"{c}={sizes[c]}" for c in collections))
    total_docs = sum(sizes.values())
    print(f"候选池合计 {total_docs} 篇 / {len(collections)} 个库\n")

    rows: List[Dict[str, Any]] = []
    for c in collections:
        for q in POSITIVE.get(c, []):
            vec = embed.embed([q.query])[0]
            per_coll: Dict[str, List[Dict[str, Any]]] = {}
            for cand in collections:
                hits = stores[cand].query(vector=vec, top_k=max(sizes[cand], 1))
                for h in hits:
                    h["_collection"] = cand
                per_coll[cand] = hits

            own = per_coll[c]
            own_rank = next(
                (i + 1 for i, h in enumerate(own) if _doc_no(h) == q.source), None
            )
            gold_score = next(
                (h.get("score", 0.0) for h in own if _doc_no(h) == q.source), None
            )

            merged = [h for hits in per_coll.values() for h in hits]
            merged.sort(key=lambda h: h.get("score", 0.0), reverse=True)
            global_rank = next(
                (i + 1 for i, h in enumerate(merged)
                 if _doc_no(h) == q.source and h["_collection"] == c),
                None,
            )
            top1 = merged[0] if merged else {}
            rows.append({
                "collection": c,
                "query": q.query,
                "gold": q.source,
                "own_rank": own_rank,
                "global_rank": global_rank,
                "gold_score": gold_score,
                "top1_score": top1.get("score"),
                "top1_doc": _doc_no(top1),
                "top1_collection": top1.get("_collection"),
            })
            print(
                f"[{c}] {q.query[:26]:<28} gold={q.source} "
                f"库内第 {own_rank}, 全局第 {global_rank}, "
                f"分数 {gold_score:.4f} vs 全局第一 {top1.get('score', 0):.4f} "
                f"({_doc_no(top1)}@{top1.get('_collection')})"
            )

    print("\n" + "=" * 78)
    ok = [r for r in rows if r["own_rank"] and r["global_rank"]]
    if not ok:
        print("没有任何金标文档在摘要层被找到——摘要层可能没有数据。")
        return 1

    def cover(key: str, n: int) -> int:
        return sum(1 for r in ok if r[key] <= n)

    print(f"样本 {len(ok)} 条（共 {len(rows)} 条问题）")
    for key, label in (("own_rank", "库内排名"), ("global_rank", "全局排名")):
        vals = sorted(r[key] for r in ok)
        print(
            f"{label}: 中位 {statistics.median(vals):.0f}, "
            f"p90 {vals[int(len(vals) * 0.9) - 1]}, 最差 {vals[-1]}"
        )
    print("\n覆盖率（金标文档能进入粗筛结果的比例）：")
    print(f"{'N':>4} | {'全局前 N（当前实现）':>22} | {'每库各取前 N（候选方案）':>26}")
    for n in (1, 2, 3, 5, 8, 10, 15, 20, 30, 50):
        g = cover("global_rank", n)
        o = cover("own_rank", n)
        print(f"{n:>4} | {g:>10}/{len(ok)} ({g / len(ok):>5.0%}) | {o:>12}/{len(ok)} ({o / len(ok):>5.0%})")

    margins = [r["top1_score"] - r["gold_score"] for r in ok]
    print(
        f"\n金标 vs 全局第一 的摘要分差：中位 {statistics.median(margins):.4f}, "
        f"最大 {max(margins):.4f}（分数区间 0~1，越接近 0 说明摘要层越分不出来）"
    )

    if json_path:
        Path(json_path).parent.mkdir(parents=True, exist_ok=True)
        Path(json_path).write_text(
            json.dumps({"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                        "collections": collections, "sizes": sizes, "rows": rows},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"\n明细已写入 {json_path}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tenant", choices=["acme", "globex", "both"], default="both")
    ap.add_argument("--json", dest="json_path", default=None)
    args = ap.parse_args()
    cols = {"acme": ACME_KBS, "globex": GLOBEX_KBS, "both": ACME_KBS + GLOBEX_KBS}[args.tenant]
    return probe(cols, args.json_path)


if __name__ == "__main__":
    raise SystemExit(main())
