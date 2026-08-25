"""实测 BM25 索引随语料规模的增长曲线，验证或推翻「真实数据量下索引达 GB 级」这条 P0。

背景
----
`CLAUDE.md` §4 有一条 P0：BM25 索引以 JSON 存储，且 `query_knowledge_hub.py`
每次查询都从磁盘全量 `json.load`。按真实数据量（几个 G 文档 → 143K–716K 块）
推算索引会达 GB 级、单次加载秒级到分钟级。

**但那条推算是从一个 20 块的样本线性外推出来的，误差可能很大**——
词表增长服从 Heaps 定律（次线性），JSON 结构开销也会随规模摊薄。
本脚本用多个规模点实测，拟合真实曲线，取代单点外推。

为什么不跑完整摄入
------------------
完整摄入的 1.4–3.2 小时几乎全花在 embedding 上。而本脚本要量的是
「索引多大、加载多久」，只依赖 SparseEncoder(jieba 分词) → BM25Indexer，
**完全不需要 embedding**，因此几分钟就能跑完多个规模点。

代价：不测向量库大小、不测端到端检索延迟。那两项要另外做。

用法
----
    .venv/bin/python scripts/measure_bm25_index_growth.py
    .venv/bin/python scripts/measure_bm25_index_growth.py --sizes 1000,5000,20000
    .venv/bin/python scripts/measure_bm25_index_growth.py --keep   # 保留生成的索引

结果 JSON 落 scripts/benchmark_results/。
"""
from __future__ import annotations

import argparse
import json
import random
import shutil
import statistics
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.settings import resolve_path

# 真实语料的词汇多样性直接决定词表规模，进而决定索引大小。
# 用合成文本会低估索引（词表偏小），所以这里优先取仓库里的真实中文语料，
# 不够时才用模板扩充，并在结果里标注实际来源占比。
_REAL_CORPUS_DIRS = ["tests/fixtures/sample_documents", "data/uploads", "docs"]

# 模板扩充用的业务词汇池——刻意覆盖多个领域，避免词表过窄
_TOPICS = ["年假", "远程办公", "报销", "考勤", "绩效", "招聘", "培训", "差旅",
           "采购", "合同", "审批", "预算", "薪酬", "社保", "公积金", "离职"]
_ACTIONS = ["申请", "审批", "核算", "备案", "登记", "复核", "结算", "归档",
            "变更", "撤销", "延期", "补交", "驳回", "受理"]
_ROLES = ["员工", "主管", "部门总监", "人力资源部", "财务部", "行政部",
          "法务部", "信息技术部", "分公司负责人"]


def load_real_chunks(limit: int) -> List[str]:
    """从仓库现有语料里抽取真实中文文本块。"""
    out: List[str] = []
    for d in _REAL_CORPUS_DIRS:
        p = Path(resolve_path(d))
        if not p.exists():
            continue
        for f in sorted(p.rglob("*")):
            if len(out) >= limit:
                return out
            if not f.is_file() or f.suffix.lower() not in {".md", ".txt"}:
                continue
            try:
                text = f.read_text(errors="ignore")
            except Exception:
                continue
            # 按段落切，只保留有中文且足够长的段
            for para in text.split("\n\n"):
                s = " ".join(para.split())
                if len(s) >= 120 and sum('一' <= c <= '鿿' for c in s) > 40:
                    out.append(s[:600])
                    if len(out) >= limit:
                        return out
    return out


def synth_chunk(rng: random.Random, seq: int) -> str:
    """模板扩充：结构像真实制度文档，词汇有随机组合以产生合理的词表增长。"""
    t = rng.sample(_TOPICS, k=2)
    a = rng.sample(_ACTIONS, k=3)
    r = rng.sample(_ROLES, k=2)
    return (
        f"第{seq}条 关于{t[0]}管理的补充规定。"
        f"{r[0]}提出{t[0]}{a[0]}后，需由{r[1]}在{rng.randint(1,15)}个工作日内完成{a[1]}。"
        f"若涉及{t[1]}相关事项，还需同步{a[2]}，并在系统中留存记录编号"
        f"{rng.randint(100000,999999)}。"
        f"未按期{a[1]}的，视同{rng.choice(['自动通过','退回重报','转人工复核'])}。"
        f"本条自发布之日起施行，解释权归{r[1]}所有。"
    )


def build_corpus(n: int, rng: random.Random, real: List[str]) -> tuple[List[str], int]:
    """返回 (语料, 其中真实文本的条数)。真实语料不足时循环取用 + 模板补齐。"""
    chunks: List[str] = []
    used_real = 0
    for i in range(n):
        if real and i % 3 == 0:                      # 约 1/3 用真实文本
            chunks.append(real[i % len(real)])
            used_real += 1
        else:
            chunks.append(synth_chunk(rng, i))
    return chunks, used_real


def measure_one(n: int, rng: random.Random, real: List[str]) -> Dict[str, Any]:
    from src.ingestion.embedding.sparse_encoder import SparseEncoder
    from src.ingestion.storage.bm25_indexer import BM25Indexer

    collection = f"_bm25growth_{n}"
    index_dir = Path(resolve_path(f"data/db/bm25/{collection}"))
    shutil.rmtree(index_dir, ignore_errors=True)

    chunks, used_real = build_corpus(n, rng, real)

    # SparseEncoder.encode 只用到 chunk.id / chunk.text 两个属性，
    # 用轻量替身避免依赖完整 Chunk 的构造参数（那会把无关字段的变更牵连进来）。
    class _C:
        __slots__ = ("id", "text")
        def __init__(self, i, t): self.id, self.text = i, t

    t0 = time.perf_counter()
    encoder = SparseEncoder()
    term_stats = encoder.encode([
        _C(f"g{n:07d}_{i:07d}_growth", c) for i, c in enumerate(chunks)
    ])
    encode_s = time.perf_counter() - t0

    indexer = BM25Indexer(index_dir=str(index_dir))
    t0 = time.perf_counter()
    indexer.build(term_stats, collection=collection)
    build_s = time.perf_counter() - t0

    f = next(index_dir.glob("*.json"), None)
    size = f.stat().st_size if f else 0

    # 加载耗时取 3 次中位数（首次含文件系统缓存冷启动）
    loads = []
    for _ in range(3):
        t0 = time.perf_counter()
        json.loads(f.read_text())
        loads.append(time.perf_counter() - t0)

    idx = json.loads(f.read_text())
    vocab = len(idx.get("index", {}))
    meta = idx.get("metadata", {})

    return dict(
        chunks=n, real_chunk_count=used_real,
        index_bytes=size, index_mb=round(size / 1024**2, 2),
        vocab_terms=vocab,
        avg_doc_length=meta.get("avg_doc_length"),
        total_terms=meta.get("total_terms"),
        bytes_per_chunk=round(size / n, 1),
        encode_s=round(encode_s, 2), build_s=round(build_s, 2),
        json_load_s_median=round(statistics.median(loads), 4),
        json_load_s_first=round(loads[0], 4),
        _index_dir=str(index_dir),
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizes", default="500,2000,8000,25000",
                    help="逗号分隔的块数规模点")
    ap.add_argument("--keep", action="store_true", help="保留生成的索引目录")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    sizes = [int(s) for s in args.sizes.split(",") if s.strip()]
    rng = random.Random(20260825)
    real = load_real_chunks(limit=4000)
    print(f"真实语料块: {len(real)} 条（不足部分用模板扩充）\n")

    rows: List[Dict[str, Any]] = []
    for n in sizes:
        print(f"── {n} 块 ──", flush=True)
        r = measure_one(n, rng, real)
        rows.append(r)
        print(f"   索引 {r['index_mb']:>8.2f} MB | 词表 {r['vocab_terms']:>7d} | "
              f"{r['bytes_per_chunk']:>7.1f} B/块 | json.load {r['json_load_s_median']:.4f}s")

    # 用最后两个点的对数斜率外推（比线性外推更贴近 Heaps 定律的次线性增长）
    print(f"\n{'='*74}\n外推到真实数据量")
    import math
    (n1, s1), (n2, s2) = (rows[-2]["chunks"], rows[-2]["index_bytes"]), \
                         (rows[-1]["chunks"], rows[-1]["index_bytes"])
    alpha = math.log(s2 / s1) / math.log(n2 / n1)
    load_per_mb = rows[-1]["json_load_s_median"] / max(rows[-1]["index_mb"], 1e-9)
    print(f"  实测增长指数 alpha = {alpha:.3f}   (1.0=线性; <1 表示次线性)")
    print(f"  实测加载速率 = {1/load_per_mb:,.0f} MB/s\n")
    print(f"  {'块数':>9} {'索引估算':>12} {'单次 json.load':>16}")
    proj = []
    for target in (143_000, 358_000, 716_000):
        est = s2 * (target / n2) ** alpha
        est_mb = est / 1024**2
        proj.append(dict(chunks=target, est_mb=round(est_mb, 1),
                         est_load_s=round(est_mb * load_per_mb, 2)))
        print(f"  {target:>9,} {est_mb:>10.1f} MB {est_mb*load_per_mb:>14.2f} s")

    out = Path(args.out or
               f"scripts/benchmark_results/bm25_growth_{datetime.now():%Y%m%d_%H%M%S}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(dict(
        generated_at=datetime.now().isoformat(), script=__file__,
        code_state=subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                                  capture_output=True, text=True).stdout.strip(),
        real_corpus_chunks=len(real),
        growth_exponent_alpha=round(alpha, 4),
        load_rate_mb_per_s=round(1 / load_per_mb, 1),
        measurements=rows, projections=proj,
        not_covered=[
            "只测 BM25 索引，未测向量库体积与端到端检索延迟",
            "语料约 1/3 为仓库真实中文文本、2/3 为模板扩充；真实企业文档的词表更丰富，"
            "实际索引可能更大",
            "未测多库并行加载的叠加效应",
            "未测索引写入/更新耗时随规模的变化",
        ],
    ), ensure_ascii=False, indent=2))
    print(f"\n结果写入 {out}")

    if not args.keep:
        for r in rows:
            shutil.rmtree(r["_index_dir"], ignore_errors=True)
        print("已清理生成的索引目录")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
