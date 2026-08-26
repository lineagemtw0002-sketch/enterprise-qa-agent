#!/usr/bin/env python3
"""索引侧 / 查询侧分词器对齐方案的**消融实验**（`CLAUDE.md` §4 第 9b 条）。

    python scripts/ablate_tokenizer_alignment.py
    python scripts/ablate_tokenizer_alignment.py --collection mmarco --top-k 10

## 这个脚本回答什么

`sparse_encoder.py::_tokenize` 的注释声称"与 `QueryProcessor` 一致"，实测不成立。
但**"让两边一致"不是判据，检索质量才是** —— 统一到哪一边会显著改变召回：

    统一到 SparseEncoder（丢单字）  → 用户搜「年假」「3」查不到
    统一到 QueryProcessor（留单字） → 索引变大，停用词进不进索引也要定

所以先把候选方案逐个跑一遍**带人工标注的召回率**，再决定改哪边。

## 结论（2026-08-26，已实施 = C8）

mmarco 604 块 / 100 条人工标注，三档 k 结论一致：

| | top-5 | top-10 | top-20 |
|---|---|---|---|
| C0' 修复前 | 79.0% | 83.0% | 83.0% |
| C3 只对齐（两侧同规则、精确模式） | 80.0% | 84.0% | 85.0% |
| **C8 已实施**（索引侧 cut_for_search） | **82.0%** | **85.0%** | **86.0%** |
| C5/C6 统一到"丢单字"那一边 | 78.0% | 83.0% | 83.0% |（且 2 条查询词条归零）
| C9 两侧都 cut_for_search | 85.0% | 86.0% | 87.0% |

两条要记住的：
1. **只做"对齐"只值 +1 个点**（C3）。大头在索引侧换搜索模式（C8），
   而那是另一个缺陷（jieba 精确模式粒度随上下文变），"统一分词"修不掉。
2. **C9 还能再多 1 个点，本次没做。** 它让查询侧也产出子词，词条数 ×1.24，
   而 P0 第 2e 条正卡在"命中大量 postings 时并发退化"上。这个取舍留给人拍板。

## 为什么用 BM25 而不是 OpenSearch

判据要能**快速迭代**。这里直接复用生产的 `BM25Indexer.build/query`
（写进临时目录，跑完删），一轮 604 块约 2 秒；OpenSearch 侧要重建索引，
一轮几十秒。两者用同一套分词，BM25 上的结论对 OpenSearch 成立
—— 但 **OpenSearch 侧有一条 BM25 没有的额外差异（大小写）**，见 §大小写 一节，
那条必须单独在 OpenSearch 上验，本脚本不覆盖。

## 判据

`expected_chunk_ids` 是人工标注的正确答案，直接算 recall@k。
同时打印索引规模（词表大小 / postings 总数），因为 P0 第 2 条盯的就是索引体积
—— 召回涨 1 个点但索引翻倍，是要拿出来讨论的取舍，不该被单一指标盖掉。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import jieba  # noqa: E402

_PUNCT = re.compile(r"[\s\W]+", re.UNICODE)


# ────────────────────────── 候选分词规则 ──────────────────────────
#
# 全部建立在同一个 jieba 切分之上，只在三个开关上有差别：
#   min_len   最小词长（1 = 保留单字/单数字，2 = 丢掉）
#   lower     是否小写化
#   stop      是否过滤停用词
# 这样"方案之间差在哪"是可枚举的，不会变成两份互相抄不动的实现。


def _cut(text: str, search_mode: bool = False) -> List[str]:
    cutter = jieba.lcut_for_search if search_mode else jieba.lcut
    out: List[str] = []
    for tok in cutter(text):
        tok = tok.strip()
        if not tok or _PUNCT.fullmatch(tok):
            continue
        out.append(tok)
    return out


def make_tokenizer(
    *,
    min_len: int,
    lower: bool,
    stop: bool,
    stopwords: Optional[Set[str]] = None,
    search_mode: bool = False,
) -> Callable[[str], List[str]]:
    sw = stopwords or set()

    def _tok(text: str) -> List[str]:
        toks = _cut(text, search_mode)
        if lower:
            toks = [t.lower() for t in toks]
        if stop:
            toks = [t for t in toks if t not in sw and t.lower() not in sw]
        return [t for t in toks if len(t) >= min_len]

    return _tok


def real_index_tokenizer() -> Callable[[str], List[str]]:
    """当前生产的索引侧分词（原样调用，不复刻）。"""
    from src.ingestion.embedding.sparse_encoder import SparseEncoder

    enc = SparseEncoder()
    return enc._tokenize


def real_query_tokenizer() -> Callable[[str], List[str]]:
    """当前生产的查询侧分词（原样调用，不复刻）。"""
    from src.core.query_engine.query_processor import QueryProcessor

    qp = QueryProcessor()
    return lambda text: list(qp.process(text).keywords)


# ────────────────────────── 语料与用例 ──────────────────────────


def load_corpus(collection: str) -> List[Tuple[str, str]]:
    """从 Chroma 取正文 —— 它是本项目唯一存了 chunk 原文的地方。"""
    import chromadb

    client = chromadb.PersistentClient(path="data/db/chroma")
    names = {c.name for c in client.list_collections()}
    if collection not in names:
        raise SystemExit(f"❌ Chroma 里没有 collection `{collection}`")
    got = client.get_collection(collection).get(include=["documents"])
    return list(zip(got["ids"], got["documents"]))


def load_cases(fixture: Path) -> List[Dict[str, Any]]:
    data = json.loads(fixture.read_text(encoding="utf-8"))
    cases = data.get("test_cases") or data.get("cases") or []
    out = []
    for c in cases:
        q = c.get("query") or c.get("question")
        exp = c.get("expected_chunk_ids")
        if isinstance(exp, str):
            try:
                exp = json.loads(exp.replace("'", '"'))
            except Exception:  # noqa: BLE001
                exp = None
        if q and exp:
            out.append({"query": q, "expected": set(map(str, exp))})
    return out


# ────────────────────────── 一次消融 ──────────────────────────


def run_variant(
    name: str,
    corpus: List[Tuple[str, str]],
    cases: List[Dict[str, Any]],
    index_tok: Callable[[str], List[str]],
    query_tok: Callable[[str], List[str]],
    top_k: int,
) -> Dict[str, Any]:
    """用真实 `BM25Indexer` 建一遍索引再查，避免"消融脚本自己实现一版 BM25"
    这种最经典的假结论来源。"""
    from collections import Counter

    from src.ingestion.storage.bm25_indexer import BM25Indexer

    term_stats = []
    for cid, text in corpus:
        terms = index_tok(text or "")
        term_stats.append(
            {
                "chunk_id": cid,
                "term_frequencies": dict(Counter(terms)),
                "doc_length": len(terms),
            }
        )

    tmp = Path(tempfile.mkdtemp(prefix="ablate_bm25_"))
    try:
        ix = BM25Indexer(index_dir=str(tmp))
        ix.dual_write_sqlite = False       # 消融只关心打分，不测存储后端
        ix.read_backend = "json"
        ix.build(term_stats, collection="ablate")

        vocab = len(ix._index)
        postings = sum(len(v["postings"]) for v in ix._index.values())

        hit = 0
        empty_q = 0
        for case in cases:
            toks = query_tok(case["query"])
            if not toks:
                empty_q += 1
                continue
            res = ix.query(toks, top_k=top_k)
            got = {r["chunk_id"] for r in res}
            if case["expected"] & got:
                hit += 1
        return {
            "name": name,
            "recall": hit / len(cases) if cases else 0.0,
            "hit": hit,
            "total": len(cases),
            "empty_query": empty_q,
            "vocab": vocab,
            "postings": postings,
        }
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ────────────────────────── 主流程 ──────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--collection", default="mmarco")
    ap.add_argument("--golden", default="tests/fixtures/golden_test_set_mmarco.json")
    ap.add_argument("--top-k", type=int, default=10)
    args = ap.parse_args()

    from src.core.query_engine.query_processor import DEFAULT_STOPWORDS

    corpus = load_corpus(args.collection)
    cases = load_cases(Path(args.golden))
    print(f"语料 {len(corpus)} 块（{args.collection}）· 标注用例 {len(cases)} 条 · top_k={args.top_k}\n")

    sw = DEFAULT_STOPWORDS

    # 候选方案。命名规则：`索引侧 / 查询侧`
    variants: List[Tuple[str, Callable[[str], List[str]], Callable[[str], List[str]]]] = [
        # ⚠️ C0 调的是**当前代码里的**索引侧分词，不是某个固定的历史版本。
        #    2026-08-26 修复之前它等价于 min2+lower+精确模式；修复之后它就是
        #    C8。所以修复后跑这个脚本，C0 与 C8 会同分 —— 那是对的，
        #    不是脚本坏了。要拿旧规则做对照请看下面 C5/C6 那两行。
        ("C0 当前生产代码（index_tokens / QueryProcessor）",
         real_index_tokenizer(), real_query_tokenizer()),
        ("C0' 修复前的索引侧规则（min2+lower+精确模式）/ 现状查询",
         make_tokenizer(min_len=2, lower=True, stop=False), real_query_tokenizer()),
        # 统一到"查询侧那一边"：索引也保留单字
        ("C1 索引 min1（留单字，不过停用词）/ 现状查询",
         make_tokenizer(min_len=1, lower=True, stop=False), real_query_tokenizer()),
        ("C2 索引 min1+停用词 / 现状查询",
         make_tokenizer(min_len=1, lower=True, stop=True, stopwords=sw), real_query_tokenizer()),
        # 完全统一：两侧同一个函数
        ("C3 两侧同一规则（min1+lower+停用词）",
         make_tokenizer(min_len=1, lower=True, stop=True, stopwords=sw),
         make_tokenizer(min_len=1, lower=True, stop=True, stopwords=sw)),
        ("C4 两侧同一规则（min1+lower，不过停用词）",
         make_tokenizer(min_len=1, lower=True, stop=False),
         make_tokenizer(min_len=1, lower=True, stop=False)),
        # 统一到"索引侧那一边"：查询也丢单字
        ("C5 两侧同一规则（min2+lower+停用词）——丢单字",
         make_tokenizer(min_len=2, lower=True, stop=True, stopwords=sw),
         make_tokenizer(min_len=2, lower=True, stop=True, stopwords=sw)),
        ("C6 当前生产索引 / 查询也 min2（丢单字）",
         real_index_tokenizer(),
         make_tokenizer(min_len=2, lower=True, stop=True, stopwords=sw)),
        # ── 下面四条动的是**另一个缺陷**：jieba 精确模式对同一串字在不同上下文
        #    切法不同（文档里 `顺延到`、查询里 `顺延`）。那不是两侧配置不一致，
        #    两侧都调 lcut 也一样中招，所以"统一分词"修不掉它。
        #    正解是索引侧改用 `cut_for_search`（细粒度、含子词），
        #    让索引侧的产出成为查询侧的**超集**。
        ("C7 索引 cut_for_search+停用词 / 查询 lcut",
         make_tokenizer(min_len=1, lower=True, stop=True, stopwords=sw, search_mode=True),
         make_tokenizer(min_len=1, lower=True, stop=True, stopwords=sw)),
        ("C8 索引 cut_for_search 不过停用词 / 查询 lcut",
         make_tokenizer(min_len=1, lower=True, stop=False, search_mode=True),
         make_tokenizer(min_len=1, lower=True, stop=True, stopwords=sw)),
        ("C9 两侧都 cut_for_search",
         make_tokenizer(min_len=1, lower=True, stop=True, stopwords=sw, search_mode=True),
         make_tokenizer(min_len=1, lower=True, stop=True, stopwords=sw, search_mode=True)),
        ("C10 索引 cut_for_search min2 / 查询 lcut min1",
         make_tokenizer(min_len=2, lower=True, stop=True, stopwords=sw, search_mode=True),
         make_tokenizer(min_len=1, lower=True, stop=True, stopwords=sw)),
    ]

    rows = [run_variant(n, corpus, cases, it, qt, args.top_k) for n, it, qt in variants]

    # 基准取"修复前的索引侧规则"那一行 —— 拿当前代码当基准的话，改完之后
    # 所有 Δ 都会变成 0，表就没信息了。
    base = rows[1]["recall"]
    print(f"{'方案':<44}{'recall@k':>10}{'Δ':>9}{'空查询':>7}{'词表':>9}{'postings':>11}")
    print("-" * 92)
    for r in rows:
        d = r["recall"] - base
        print(
            f"{r['name']:<44}{r['recall']:>9.1%}{d:>+9.1%}"
            f"{r['empty_query']:>7}{r['vocab']:>9}{r['postings']:>11}"
        )
    print(
        "\n⚠️ 本脚本只量 BM25 稀疏侧的召回。混合检索（dense+RRF+rerank）与端到端答案质量"
        "\n   不在覆盖范围内 —— 那两项分别跑 verify_opensearch_parity.py 和 "
        "run_tenant_kb_golden_tests.py。"
        "\n⚠️ OpenSearch 侧还有一条 BM25 没有的差异：`whitespace` 分析器不做小写化，"
        "\n   而 BM25Indexer.query 自己会 lower()。这条只能在 OpenSearch 上验，本脚本测不到。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
