#!/usr/bin/env python3
"""切读前的三项验证（`docs/opensearch_migration_design.md` 阶段 3 前置）。

    python scripts/verify_opensearch_parity.py            # 三项全跑
    python scripts/verify_opensearch_parity.py --only dense

阶段 2 交付时留了三条没答案的问题，切读前必须逐条回答：

1. **dense**：kNN 召回质量与 Chroma 是否一致？阶段 2 只验证了"向量写进去了"。
2. **summary**：`{collection}__summary` 层完全没验证过，而它参与
   `query_knowledge_hub` 的文档级收窄检索。它是**纯向量层**（不建 BM25），
   所以只能用 dense 的方式验。
3. **golden**：黄金测试集里的问题，两边检索出的 chunk 是否一致。

## 判据

**重叠率，不是集合相等。** 理由与 `migrate_to_opensearch.py` 相同：
两边是不同引擎，可配置的差异（k1/b）已对齐，剩下的是引擎固有的
（Lucene 的字段长度有损量化）。要求完全相等只会得出错误结论。

⚠️ **dense 侧的情况和 sparse 不同**：Chroma 与 OpenSearch 用的是**不同的 ANN
实现**（HNSW 参数、量化策略都可能不同），而且两边都是**近似**最近邻，
本来就不保证返回同一批。所以 dense 的阈值定得比 sparse 松，
并且把实际数字打出来让人判断，而不是给一个"通过/失败"了事。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_TOP_K = 10
_MARGIN = 5          # 多取一段再截断，避免同分候选在截断线换位被误判
_SPARSE_MIN = 0.95   # sparse 侧阈值，与迁移脚本一致
_DENSE_MIN = 0.80    # dense 侧更松，见模块 docstring


def _overlap(a: List[str], b: List[str], k: int = _TOP_K) -> float:
    sa, sb = set(a[:k]), set(b[:k])
    return len(sa & sb) / len(sa) if sa else 1.0


def _embed(texts: List[str]) -> List[List[float]]:
    from src.core.settings import load_settings
    from src.libs.embedding.embedding_factory import EmbeddingFactory

    return EmbeddingFactory.create(load_settings()).embed(texts)


def _chroma_knn(collection: str, vector: List[float], k: int) -> Optional[List[str]]:
    """collection 不存在时返回 None（不是空列表）。

    区分这两者很重要：空列表意味着"查了但没命中"，是一个可比对的结果；
    None 意味着"这个库压根不在 Chroma 里"，比对无从谈起。
    混为一谈会把"没数据可比"记成"重叠率 0%"，报出假警报。
    """
    import chromadb

    client = chromadb.PersistentClient(path="data/db/chroma")
    if collection not in {c.name for c in client.list_collections()}:
        return None
    r = client.get_collection(collection).query(query_embeddings=[vector], n_results=k)
    return list(r["ids"][0])


def _os_knn(index: str, vector: List[float], k: int) -> List[str]:
    from src.libs.search.opensearch_store import OpenSearchStore

    store = OpenSearchStore()
    if not store._client.indices.exists(index=index):
        return []
    resp = store._client.search(
        index=index,
        body={"size": k, "query": {"knn": {"embedding": {"vector": vector, "k": k}}}},
    )
    return [h["_source"].get("chunk_id", h["_id"]) for h in resp["hits"]["hits"]]


# ────────────────────────── 1. dense ──────────────────────────


def check_dense(collections: List[str], queries: List[str]) -> Dict[str, Any]:
    from src.libs.search.opensearch_store import kb_index_name

    vectors = _embed(queries)
    rows = []
    skipped = []
    for col in collections:
        idx = kb_index_name(col)
        ratios = []
        missing = False
        for q, v in zip(queries, vectors):
            c = _chroma_knn(col, v, _TOP_K + _MARGIN)
            if c is None:
                missing = True
                break
            o = _os_knn(idx, v, _TOP_K + _MARGIN)
            if not o:
                ratios.append(0.0)
                continue
            ratios.append(_overlap(c, o))
        if missing:
            skipped.append(col)
            continue
        avg = sum(ratios) / len(ratios) if ratios else 0.0
        rows.append({"collection": col, "overlap": avg, "queries": len(ratios)})
    return {"rows": rows, "threshold": _DENSE_MIN, "skipped": skipped}


# ────────────────────────── 2. summary 层 ──────────────────────────


def check_summary(collections: List[str], queries: List[str]) -> Dict[str, Any]:
    """`{collection}__summary` 是纯向量层，验法与 dense 相同。

    它参与的是"先在摘要层定位到哪几篇文档，再进正文检索"这条收窄路径
    （`query_knowledge_hub.py:1131`），所以它错了不会报错，只会让召回变差 ——
    **最难发现的那种回退**。
    """
    return check_dense([f"{c}__summary" for c in collections], queries)


# ────────────────────────── 3. 黄金测试集 ──────────────────────────


def check_golden(fixture: Path) -> Dict[str, Any]:
    """黄金测试集：**直接量两边的召回率**，不是量两边互相有多像。

    这比"新旧重叠率"有价值得多 —— 重叠率只能说明"没变"，
    但如果原来就召回得差，"没变"不是好消息。用例里的
    `expected_chunk_ids` 是人标注的正确答案，直接对着它算 recall@k，
    才回答得了"检索质量有没有回退"。

    ⚠️ 只覆盖有 `expected_chunk_ids` 标注的用例。没有标注的用例
    退回"两边重叠率"，并在报告里区分开 —— 两种判据的含义不同，
    混在一起平均是没有意义的。
    """
    from src.ingestion.storage.bm25_indexer import BM25Indexer
    from src.libs.search.opensearch_store import OpenSearchStore, tokenize_for_query

    data = json.loads(fixture.read_text(encoding="utf-8"))
    cases = data.get("test_cases") or data.get("cases") or (
        data if isinstance(data, list) else []
    )
    default_col = data.get("collection") if isinstance(data, dict) else None
    if not default_col:
        # mmarco 那份没有顶层 collection，按文件名推
        stem = fixture.stem.replace("golden_test_set_", "")
        default_col = stem if stem != "tenant_kb" else "product_req_kb"

    store = OpenSearchStore()
    old_cache: Dict[str, Any] = {}
    rows: List[Dict[str, Any]] = []
    old_hit = new_hit = labeled = 0

    for case in cases:
        q = case.get("query") or case.get("question")
        col = case.get("collection") or default_col
        if not q or not col:
            continue
        if col not in old_cache:
            ix = BM25Indexer(index_dir=f"data/db/bm25/{col}")
            ix.read_backend = "json"
            old_cache[col] = ix if ix._load_json_index(col) else None
        old = old_cache[col]
        if old is None:
            continue
        tokens = tokenize_for_query(q)
        if not tokens:
            continue

        o = [r["chunk_id"] for r in old.query(tokens, top_k=_TOP_K + _MARGIN)]
        n = [
            r["chunk_id"]
            for r in store.search_kb(col, q, top_k=_TOP_K + _MARGIN, query_tokens=tokens)
        ]

        expected = case.get("expected_chunk_ids")
        if isinstance(expected, str):
            try:
                expected = json.loads(expected.replace("'", '"'))
            except Exception:  # noqa: BLE001
                expected = None
        if expected:
            labeled += 1
            exp = set(map(str, expected))
            old_hit += 1 if exp & set(o[:_TOP_K]) else 0
            new_hit += 1 if exp & set(n[:_TOP_K]) else 0
        else:
            rows.append(
                {"query": q[:28], "collection": col, "overlap": _overlap(o, n)}
            )

    return {
        "rows": rows,
        "threshold": _SPARSE_MIN,
        "recall": (
            {"labeled": labeled, "old": old_hit / labeled, "new": new_hit / labeled}
            if labeled
            else None
        ),
    }


# ────────────────────────── 输出 ──────────────────────────


def report(title: str, res: Dict[str, Any]) -> bool:
    rows = res["rows"]
    th = res["threshold"]
    print(f"\n=== {title}（阈值 {th:.0%}）===")
    recall = res.get("recall")
    if recall:
        d = recall["new"] - recall["old"]
        print(f"  📊 recall@{_TOP_K}（{recall['labeled']} 条人工标注用例）：")
        print(f"      旧 BM25    {recall['old']:.1%}")
        print(f"      OpenSearch {recall['new']:.1%}   ({d:+.1%})")
        if d < -0.02:
            print("      ❌ 召回率下降超过 2 个百分点")
            return False
        print("      ✅ 未回退")

    if not rows:
        if recall:
            return True
        # ⚠️ 空样本**不能**算通过。第一版这里 return True，导致黄金测试集
        # 一条用例都没解析出来时仍然打印"三项均达标"——正是最坏的一类假绿。
        print("  ❌ 没有可比对的样本 —— **这不是通过，是没测**")
        return False
    bad = 0
    for r in rows:
        label = r.get("collection", "") or ""
        if "query" in r:
            label = f"{r['query']:<30} [{r['collection']}]"
        ok = r["overlap"] >= th
        bad += 0 if ok else 1
        print(f"  {'✅' if ok else '❌'} {label:<46} 重叠 {r['overlap']:>5.0%}")
    avg = sum(r["overlap"] for r in rows) / len(rows)
    print(f"  ── 平均 {avg:.0%}，{len(rows) - bad}/{len(rows)} 达标")
    for col in res.get("skipped", []):
        print(f"  ⏭  {col}：Chroma 里不存在，无法比对（**不等于通过**）")
    return bad == 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--only", choices=["dense", "summary", "golden"], default=None)
    ap.add_argument(
        "--collections",
        default="acme_hr_admin_kb,globex_finance_kb,tenant_acme_kb,mmarco",
    )
    ap.add_argument("--golden", default="tests/fixtures/golden_test_set_tenant_kb.json")
    args = ap.parse_args()

    from src.libs.search.opensearch_store import OpenSearchStore

    if not OpenSearchStore().ping():
        print("❌ OpenSearch 未运行。起：docker compose up -d opensearch")
        return 2

    cols = [c.strip() for c in args.collections.split(",") if c.strip()]
    queries = [
        "年假可以顺延到次年几月",
        "报销流程需要哪些审批",
        "远程办公的考勤怎么计算",
        "试用期多久转正",
    ]

    passed = True
    if args.only in (None, "dense"):
        passed &= report("1. dense kNN 召回一致性（Chroma vs OpenSearch）",
                         check_dense(cols, queries))
    if args.only in (None, "summary"):
        passed &= report("2. __summary 摘要层（纯向量）",
                         check_summary(cols, queries))
    if args.only in (None, "golden"):
        passed &= report("3. 黄金测试集的检索层一致性",
                         check_golden(Path(args.golden)))

    print("\n" + "=" * 60)
    if passed:
        print("✅ 三项均达标 —— 可以考虑切读（仍建议先小范围灰度）")
    else:
        print("❌ 有未达标项，**不要切读**")
    print("\n⚠️ 本脚本只验检索层。端到端答案质量要跑 "
          "run_tenant_kb_golden_tests.py（需真实 LLM），本脚本不覆盖。")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
