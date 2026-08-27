"""层次化检索粗筛策略的 A/B 探针：准确率 + 延迟一起量。

`scripts/probe_summary_narrowing.py` 只量摘要层自己的排名质量；这个脚本量
**整条本地并行召回链路**（粗筛 -> 各库 hybrid search -> 合并 -> cross-encoder
重排 -> MIN_RELEVANCE_SCORE 过滤 -> 组装响应）在不同粗筛策略下的表现，用来
回答"提高预算的延迟代价是多少"和"粗筛到底有没有正收益"。

对比的策略（都不改生产代码，靠替换 `_narrow_by_document_summary` 实现）：

  current    全局前 top_docs 篇（现状，settings.yaml 的 5）
  global-N   全局前 N 篇（只放大预算，不改分配方式）
  percoll-N  每个候选库各取前 N 篇（改分配方式）
  off        完全不做粗筛（层次化检索上线前的平铺行为，作为基线）
  shipped    不打桩，跑 config/settings.yaml 当前配置下的真实链路（改动验收用这一档）
  shipped+capN  在 shipped 基础上，把进 cross-encoder 的候选池按融合分先截到 N 条（**全局**上限）
  shipped+perlibN 在 shipped 基础上，每个库只贡献前 N 条候选（**按库**分配，库多也饿不死谁）

`shipped+capN` 是用来回答"重排候选池设上限值不值"的：检索段 87% 的耗时是
cross-encoder 给合并后的候选池逐条打分（每库 top_k×2 条，6 库=60 条），
而"多查几个库"变慢的真实机制就是池子变大，不是查库本身（6 库并行召回墙钟只有 ~32ms）。
截池子能线性省下重排时间，但它**直接决定谁能进最终排序**，所以必须连召回一起量。

判据两条，都对着 `scripts/demo_kb_content/questions.py` 的正向问题：
  gold_hit  最终结果里有没有出现问题标注的那篇金标文档（检索层的真实召回）
  kw_hit    响应正文里有没有出现预期关键事实（跟人工测试文档同一个判据）

只读：不写 collection、不改 settings.yaml，也不碰 Postgres（直接调
`_execute_local_multi`，绕开 ACL —— 候选库清单由本脚本按租户写死）。

用法：
    .venv/bin/python scripts/probe_narrowing_strategies.py
    .venv/bin/python scripts/probe_narrowing_strategies.py --strategies current,percoll-5,off
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.settings import load_settings  # noqa: E402
from src.core.trace import TraceContext  # noqa: E402
from src.ingestion.hierarchy.doc_summary import summary_collection_name  # noqa: E402
from src.libs.embedding.embedding_factory import EmbeddingFactory  # noqa: E402
from src.libs.vector_store.vector_store_factory import VectorStoreFactory  # noqa: E402
from src.mcp_server.tools.query_knowledge_hub import QueryKnowledgeHubTool  # noqa: E402
from scripts.demo_kb_content.questions import ACME_KBS, GLOBEX_KBS, POSITIVE  # noqa: E402


def _code_state() -> Dict[str, Any]:
    """CLAUDE.md §7.5：数字必须能定位到代码状态。"""
    def _run(*args: str) -> str:
        try:
            return subprocess.check_output(args, text=True).strip()
        except Exception:
            return "unknown"
    return {
        "commit": _run("git", "rev-parse", "HEAD"),
        "dirty_files": _run("git", "status", "--porcelain"),
    }


class _Narrower:
    """按策略产出 {collection: [doc_id, ...]}，签名跟被替换的
    `_narrow_by_document_summary` 一致。"""

    def __init__(self, settings, collections: List[str], mode: str, budget: int):
        self.mode = mode
        self.budget = budget
        self.embed = EmbeddingFactory.create(settings)
        self.stores = {
            c: VectorStoreFactory.create(settings, collection_name=summary_collection_name(c))
            for c in collections
        }

    async def __call__(self, query: str, candidate_collections: List[str]) -> Dict[str, List[str]]:
        if self.mode == "off":
            return {}
        vec = await asyncio.to_thread(lambda: self.embed.embed([query])[0])
        per: Dict[str, List[Dict[str, Any]]] = {}
        for c in candidate_collections:
            hits = self.stores[c].query(vector=vec, top_k=self.budget)
            for h in hits:
                h["_collection"] = c
            per[c] = hits

        narrowed: Dict[str, List[str]] = {}
        if self.mode == "percoll":
            for c, hits in per.items():
                ids = [h.get("metadata", {}).get("doc_id") or h.get("id") for h in hits[: self.budget]]
                ids = [i for i in ids if i]
                if ids:
                    narrowed[c] = ids
            return narrowed

        merged = [h for hits in per.values() for h in hits]
        merged.sort(key=lambda h: h.get("score", 0.0), reverse=True)
        for h in merged[: self.budget]:
            doc_id = h.get("metadata", {}).get("doc_id") or h.get("id")
            if doc_id:
                narrowed.setdefault(h["_collection"], []).append(doc_id)
        return narrowed


def _parse(strategy: str):
    if strategy in ("current", "off"):
        return ("global" if strategy == "current" else "off"), None
    kind, _, n = strategy.partition("-")
    return kind, int(n)


async def run(tenant: str, strategies: List[str], top_k: int, json_path: str) -> int:
    settings = load_settings()
    default_budget = (getattr(getattr(settings, "ingestion", None), "doc_summary", None) or {}).get("top_docs", 5)
    collections = {"acme": ACME_KBS, "globex": GLOBEX_KBS}[tenant]
    questions = [q for c in collections for q in POSITIVE.get(c, [])]
    print(f"租户 {tenant}：{len(collections)} 个候选库 / {len(questions)} 条正向问题 / top_k={top_k}")
    print(f"settings.yaml 当前 top_docs={default_budget}\n")

    tool = QueryKnowledgeHubTool(settings=settings)
    report: Dict[str, Any] = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "code_state": _code_state(),
        "tenant": tenant, "top_k": top_k, "default_top_docs": default_budget,
        "strategies": {},
    }

    for strategy in strategies:
        cap = perlib = None
        if strategy.startswith("shipped+cap"):
            cap = int(strategy.split("cap", 1)[1])
        elif strategy.startswith("shipped+perlib"):
            perlib = int(strategy.split("perlib", 1)[1])
        if strategy == "shipped" or cap is not None or perlib is not None:
            # 不打桩：跑 config/settings.yaml 当前配置下的真实链路（含收窄开关、
            # 置信门控、空结果兜底）。改动落地后的验收就用这一档。
            budget = None
            tool = QueryKnowledgeHubTool(settings=settings)
            if cap is not None:
                # 就地模拟"重排前按融合分截断"：包住 _apply_rerank，进 cross-encoder
                # 之前先排序取前 cap 条。故意包在这一层而不是改生产代码——探针要能
                # 在同一次运行里对比多个 cap 值，改代码做不到。
                _orig_rerank = tool._apply_rerank

                def _capped(query_, results, top_k_, trace_=None, _cap=cap, _orig=_orig_rerank):
                    if len(results) > _cap:
                        results = sorted(results, key=lambda r: r.score, reverse=True)[:_cap]
                    return _orig(query_, results, top_k_, trace_)

                tool._apply_rerank = _capped  # type: ignore[assignment]
            if perlib is not None:
                # 每个库只贡献前 N 条（库内已排好序）。跟全局 cap 的区别是
                # **没有库会被别的库挤掉**——正是粗筛那个 bug 教的教训。
                _orig_search = tool._search_with

                def _perlib(*a, _n=perlib, _orig=_orig_search, **kw):
                    return _orig(*a, **kw)[:_n]

                tool._search_with = _perlib  # type: ignore[assignment]
        else:
            kind, n = _parse(strategy)
            budget = n if n is not None else default_budget
            narrower = _Narrower(settings, collections, kind, budget)
            tool._narrow_by_document_summary = narrower  # type: ignore[assignment]

        rows: List[Dict[str, Any]] = []
        for q in questions:
            trace = TraceContext(trace_type="query")
            t0 = time.monotonic()
            resp = await tool._execute_local_multi(q.query, top_k, list(collections), trace)
            elapsed = (time.monotonic() - t0) * 1000.0
            sources = " ".join(c.source for c in resp.citations)
            rows.append({
                "collection": q.collection, "query": q.query, "gold": q.source,
                "gold_hit": q.source in sources,
                "kw_hit": any(k in resp.content for k in q.keywords),
                "empty": resp.is_empty,
                "latency_ms": round(elapsed, 1),
                "searched_collections": sorted(resp.metadata.get("collections", []) or []),
            })

        gold = sum(r["gold_hit"] for r in rows)
        kw = sum(r["kw_hit"] for r in rows)
        empty = sum(r["empty"] for r in rows)
        lat = sorted(r["latency_ms"] for r in rows)
        summary = {
            "gold_hit": gold, "kw_hit": kw, "empty": empty, "total": len(rows),
            "p50_ms": statistics.median(lat),
            "p95_ms": lat[max(0, int(len(lat) * 0.95) - 1)],
            "max_ms": lat[-1],
        }
        report["strategies"][strategy] = {"budget": budget, "summary": summary, "rows": rows}
        print(
            f"{strategy:<12} 金标召回 {gold:>2}/{len(rows)}  关键事实 {kw:>2}/{len(rows)}  "
            f"空结果 {empty:>2}  延迟 p50 {summary['p50_ms']:>7.0f}ms  p95 {summary['p95_ms']:>7.0f}ms"
        )

    Path(json_path).parent.mkdir(parents=True, exist_ok=True)
    Path(json_path).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n明细已写入 {json_path}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tenant", choices=["acme", "globex"], default="acme")
    ap.add_argument("--strategies", default="current,global-30,percoll-5,percoll-15,off")
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--json", dest="json_path", default="scripts/benchmark_results/narrowing_strategies.json")
    args = ap.parse_args()
    return asyncio.run(run(args.tenant, args.strategies.split(","), args.top_k, args.json_path))


if __name__ == "__main__":
    raise SystemExit(main())
