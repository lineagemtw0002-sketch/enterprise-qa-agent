#!/usr/bin/env python3
"""模拟检索流量，对照新旧后端（`docs/opensearch_migration_design.md` §14 第 2 条）。

    python scripts/simulate_search_traffic.py                    # 默认 200 次请求 / 6 并发
    python scripts/simulate_search_traffic.py --requests 500 --concurrency 12
    python scripts/simulate_search_traffic.py --collection mmarco

开发阶段没有真实流量，但"没灰度过"是切读前最后一个空白。这个脚本用**真实问题**
（黄金测试集的 189 条）打两条路径，回答四个具体问题：

1. **结果一致吗** —— 逐请求比对两边命中的 chunk 集合。
2. **快了还是慢了** —— p50 / p95，分开报 sparse 与整链路。
3. **并发下会不会塌** —— 这是重点。现有 SQLite 后端实测 6 线程比 1 线程
   慢 103 倍（GIL convoy），OpenSearch 在裸 HTTP 上测过 6 线程正常，
   **但没在完整检索链路上测过**（链路里还有 embedding、重排这些持 GIL 的部分）。
4. **有没有静默降级** —— 实施阶段 3 时踩过一次：dense retriever 少个参数，
   `HybridSearch` 捕获异常后退化成 sparse-only，检索照常返回、只少一半召回、
   日志只有一行 warning。这里主动捕获那行 warning 并计数。

## 这不是压测

请求量和并发度都是开发机规模，目的是**发现行为差异**，不是测吞吐上限。
真实容量规划要在目标硬件上另做（`docs/scale_slo_and_priorities.md`）。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import statistics
import sys
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_DEGRADE_MARK = "using sparse only"


class _DegradeCounter(logging.Handler):
    """数"静默降级"发生了多少次。

    `HybridSearch._dense_search` 出错时只打一行 warning 就继续，
    所以这类问题**不会体现在成功率或异常里** —— 必须专门盯日志。
    实施阶段 3 时就是这么漏掉一个 bug 的。
    """

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.count = 0
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            return
        if _DEGRADE_MARK in msg:
            with self._lock:
                self.count += 1


def load_queries(limit: Optional[int] = None) -> List[Tuple[str, str]]:
    """从黄金测试集取真实问题，返回 (query, collection)。"""
    out: List[Tuple[str, str]] = []
    fixtures = Path("tests/fixtures")
    for f in sorted(fixtures.glob("golden_test_set*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        cases = d.get("test_cases") if isinstance(d, dict) else d
        if not isinstance(cases, list):
            continue
        # ⚠️ **不要从文件名推 collection。** 第一版用
        # `golden_test_set_v2.json` -> "v2"，推出一个根本不存在的库，
        # 60 次请求里 22 次直接失败（两条路径对称失败，才看出不是后端的问题）。
        # 没有显式声明 collection 的用例宁可跳过 —— 造出来的流量必须打在
        # 真实存在的库上，否则测的是错误处理不是检索。
        default_col = d.get("collection") if isinstance(d, dict) else None
        if not default_col and f.stem == "golden_test_set_mmarco":
            default_col = "mmarco"
        for c in cases:
            if not isinstance(c, dict):
                continue
            q = c.get("query") or c.get("question")
            col = c.get("collection") or default_col
            if q and col:
                out.append((q, col))
    # 再过一道：只保留 Chroma 与 OpenSearch **两边都有数据**的库。
    # 缺任一边都会让对照失去意义（一边查得到一边查不到，重叠率恒为 0）。
    import chromadb

    from src.libs.search.opensearch_store import OpenSearchStore, kb_index_name

    chroma_cols = {c.name for c in chromadb.PersistentClient(path="data/db/chroma").list_collections()}
    store = OpenSearchStore()
    usable = {
        c
        for c in {c for _, c in out}
        if c in chroma_cols and store._client.indices.exists(index=kb_index_name(c))
    }
    dropped = {c for _, c in out} - usable
    if dropped:
        print(f"⏭  跳过 {len(dropped)} 个两边数据不全的 collection：{', '.join(sorted(dropped))}")
    out = [(q, c) for q, c in out if c in usable]

    random.Random(20260826).shuffle(out)
    return out[:limit] if limit else out


def _search(hub: Any, collection: str, query: str, top_k: int) -> Tuple[List[str], float]:
    t0 = time.perf_counter()
    hits = hub._build_hybrid_search_for(collection).search(query, top_k=top_k)
    return [h.chunk_id for h in hits], (time.perf_counter() - t0) * 1000


def run_backend(
    label: str,
    env_value: str,
    plan: List[Tuple[str, str]],
    concurrency: int,
    top_k: int,
) -> Dict[str, Any]:
    from src.mcp_server.tools.query_knowledge_hub import QueryKnowledgeHubTool

    os.environ["RAGENT_OPENSEARCH_READ"] = env_value

    degrade = _DegradeCounter()
    logging.getLogger().addHandler(degrade)

    results: Dict[int, List[str]] = {}
    lat: List[float] = []
    errors: List[str] = []
    lock = threading.Lock()
    idx = {"i": 0}

    def worker() -> None:
        # 每个线程自己的 hub —— 检索链路上有非线程安全的组件（Chroma client、
        # reranker），共享会把"并发问题"和"共享状态问题"混在一起，测不清楚。
        hub = QueryKnowledgeHubTool()

        # ⚠️ **先预热再计时。** 首次查询要 7–10 秒，全花在加载 reranker /
        # embedding 模型上 —— 两条路径都一样，跟检索后端无关。
        # 第一版没预热，于是每个线程的第一次请求都把冷启动算进了延迟，
        # p95 报出 28.7 秒、"OpenSearch 慢 18.9 倍"的假结论。
        # 实际稳态是 39.7ms vs 74.7ms。
        try:
            wq, wc = plan[0]
            hub._build_hybrid_search_for(wc).search(wq, top_k=top_k)
        except Exception:  # noqa: BLE001
            pass

        while True:
            with lock:
                i = idx["i"]
                if i >= len(plan):
                    return
                idx["i"] = i + 1
            q, col = plan[i]
            try:
                ids, ms = _search(hub, col, q, top_k)
                with lock:
                    results[i] = ids
                    lat.append(ms)
            except Exception as exc:  # noqa: BLE001
                with lock:
                    errors.append(f"{type(exc).__name__}: {exc}")

    t0 = time.perf_counter()
    threads = [threading.Thread(target=worker) for _ in range(concurrency)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.perf_counter() - t0

    logging.getLogger().removeHandler(degrade)
    return {
        "label": label,
        "results": results,
        "latencies": lat,
        "errors": errors,
        "wall_s": wall,
        "degraded": degrade.count,
    }


def _pct(xs: List[float], p: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    k = min(int(len(s) * p), len(s) - 1)
    return s[k]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--requests", type=int, default=200)
    ap.add_argument("--concurrency", type=int, default=6)
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--collection", default=None, help="只打这一个库")
    args = ap.parse_args()

    from src.libs.search.opensearch_store import OpenSearchStore

    if not OpenSearchStore().ping():
        print("❌ OpenSearch 未运行。起：docker compose up -d opensearch")
        return 2

    pool = load_queries()
    if args.collection:
        pool = [(q, c) for q, c in pool if c == args.collection]
    if not pool:
        print("❌ 没有可用的查询素材")
        return 2
    plan = [pool[i % len(pool)] for i in range(args.requests)]
    cols = sorted({c for _, c in plan})

    print(f"素材 {len(pool)} 条真实问题 → 计划 {len(plan)} 次请求 / {args.concurrency} 并发")
    print(f"覆盖 collection：{', '.join(cols)}")
    print("每线程先预热一次（排除模型加载的 7–10s，两条路径都一样）\n")

    old = run_backend("旧链路 (BM25+Chroma)", "off", plan, args.concurrency, args.top_k)
    new = run_backend("OpenSearch", "*", plan, args.concurrency, args.top_k)
    os.environ.pop("RAGENT_OPENSEARCH_READ", None)

    print(f"{'':<24} {'成功':>6} {'失败':>5} {'p50':>9} {'p95':>9} {'挂钟':>8} {'静默降级':>9}")
    for r in (old, new):
        print(
            f"  {r['label']:<22} {len(r['latencies']):>6} {len(r['errors']):>5} "
            f"{_pct(r['latencies'], .5):>7.0f}ms {_pct(r['latencies'], .95):>7.0f}ms "
            f"{r['wall_s']:>6.1f}s {r['degraded']:>9}"
        )

    # 结果一致性：逐请求比对
    common = set(old["results"]) & set(new["results"])
    exact = sum(1 for i in common if old["results"][i] == new["results"][i])
    setsame = sum(
        1 for i in common if set(old["results"][i]) == set(new["results"][i])
    )
    inter = [
        len(set(old["results"][i]) & set(new["results"][i]))
        / max(len(old["results"][i]), 1)
        for i in common
        if old["results"][i]
    ]

    print(f"\n=== 结果一致性（{len(common)} 次可比对请求）===")
    print(f"  逐位完全相同      {exact:>5} / {len(common)}  ({exact / len(common):.0%})")
    print(f"  集合相同（顺序可异）{setsame:>5} / {len(common)}  ({setsame / len(common):.0%})")
    if inter:
        print(f"  平均重叠率        {statistics.mean(inter):.0%}")

    ok = True
    print("\n=== 判定 ===")
    if new["errors"]:
        ok = False
        top = Counter(new["errors"]).most_common(3)
        print(f"  ❌ OpenSearch 侧 {len(new['errors'])} 次失败：")
        for msg, n in top:
            print(f"       ×{n} {msg[:88]}")
    else:
        print("  ✅ 零失败")

    if new["degraded"]:
        ok = False
        print(f"  ❌ 检测到 {new['degraded']} 次静默降级（dense 失败退化成 sparse-only）")
    else:
        print("  ✅ 无静默降级")

    if inter and statistics.mean(inter) < 0.90:
        ok = False
        print(f"  ❌ 平均重叠率 {statistics.mean(inter):.0%} 低于 90%")
    else:
        print("  ✅ 结果一致性达标")

    slow = _pct(new["latencies"], .95) / max(_pct(old["latencies"], .95), 1e-9)
    # ⚠️ 阈值放到 3x 并**不是**为了让它通过。当前数据规模（最大的库 604 条）
    # 下 OpenSearch 本来就该更慢：HTTP 往返 + JSON 序列化的固定开销盖过收益，
    # 与 SQLite/JSON 那次交叉点分析同一个道理。
    # 收益要等单库上到几千条以后才出现（实测 3000 块时 SQLite 侧已快 6.7 倍）。
    # **拿"现在更快"当切读理由是错的**；切读买的是"数据长大后不崩"。
    if slow > 3.0:
        ok = False
        print(f"  ❌ p95 相对旧链路慢 {slow:.1f}x（超出小数据量下的预期）")
    else:
        print(f"  ✅ p95 {slow:.2f}x（小数据量下 OpenSearch 更慢属预期，见脚本注释）")

    print("\n" + "=" * 62)
    print("✅ 模拟流量未发现问题" if ok else "❌ 发现问题，见上")
    print(
        "\n⚠️ 这不是压测：请求量与并发度都是开发机规模，目的是发现**行为差异**，"
        "\n   不是测吞吐上限。容量规划要在目标硬件上另做。"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
