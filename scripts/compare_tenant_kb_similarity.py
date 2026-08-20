"""对比 Acme / Globex 两家企业「同类型知识库」的内容相似度。

背景：`generate_tenant_kb_corpus.py` 给两家企业各生成了 11 个分类的语料
（`data/tenant_demo/{acme,globex}/kb_corpus/<category>/*.txt`），其中 4 个分类
两家企业都有同名对应——hr（人力资源制度）、meeting（会议纪要摘要）、
troubleshoot（故障/问题排查）、onboarding（新员工入职指南）——这 4 个就是本次
要核对的「同类型知识库」，要求内容相似度低于 10%，防止两家企业的知识库其实
在灌水同一批内容、检索时串号。

两种相似度指标，含义不同，都算，原因见下面 `_compute_category` 的注释：
  - 词法相似度（TF-IDF 余弦，用 jieba 分词）：衡量"用词/内容重叠"，
    两份文档谈完全不同的事，这个值会趋近 0。这是本次 <10% 要求真正能够、
    也应该达到的指标。
  - 语义相似度（生产环境同款 embedding 模型的余弦相似度）：衡量"语义空间
    距离"，同语言同领域的中文商务文本，这个值天然有一个较高的基线（不同
    公司写的毫不相关的 HR 制度，语义余弦相似度也可能到 60%+），不是内容
    重叠的信号，不能拿它硬卡 10%——所以只做参考，跟"公司内部同类文档的
    自相似度"做对比：只要跨企业相似度不高于企业内部自相似度，就说明两家
    内容确实是各写各的，没有互相抄。

用法：
    python scripts/compare_tenant_kb_similarity.py              # 只算词法相似度（快，无外部依赖）
    python scripts/compare_tenant_kb_similarity.py --with-embeddings  # 额外算语义相似度（需要本地 Ollama）
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

import jieba
import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer

sys.path.insert(0, str(Path(__file__).parent.parent))

REPO_ROOT = Path(__file__).parent.parent
TENANT_DEMO_DIR = REPO_ROOT / "data" / "tenant_demo"
TENANTS = ["acme", "globex"]

# 两家企业语料里，分类目录名完全相同的 4 个，即"同类型知识库"。
SHARED_CATEGORIES = {
    "hr": "人力资源制度",
    "meeting": "会议纪要摘要",
    "troubleshoot": "故障/问题排查",
    "onboarding": "新员工入职指南",
}


def _load_category_texts(tenant: str, category: str) -> List[str]:
    cat_dir = TENANT_DEMO_DIR / tenant / "kb_corpus" / category
    files = sorted(cat_dir.glob("*.txt"))
    return [f.read_text(encoding="utf-8") for f in files]


def _tfidf_cross_similarity(acme_texts: List[str], globex_texts: List[str]) -> np.ndarray:
    """两组文本各自分词后统一建一个 TF-IDF 词表，再切回两半算余弦相似度矩阵。
    统一建词表（而不是分别建两个 vectorizer）是必须的——两个独立词表的向量
    维度、维度含义都对不上，没法直接做内积。"""
    tokenized = [" ".join(jieba.cut(t)) for t in acme_texts + globex_texts]
    matrix = TfidfVectorizer().fit_transform(tokenized)
    a = matrix[: len(acme_texts)]
    g = matrix[len(acme_texts):]
    return (a @ g.T).toarray()


def _embed_texts(texts: List[str], embedder) -> np.ndarray:
    vectors = np.array(embedder.embed(texts), dtype=np.float64)
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms[norms == 0] = 1e-12
    return vectors / norms


def _compute_category(category: str, embedder=None) -> Dict:
    acme_texts = _load_category_texts("acme", category)
    globex_texts = _load_category_texts("globex", category)

    lexical_sim = _tfidf_cross_similarity(acme_texts, globex_texts)
    result = {
        "category": category,
        "label": SHARED_CATEGORIES[category],
        "acme_count": len(acme_texts),
        "globex_count": len(globex_texts),
        "lexical_mean": float(lexical_sim.mean()),
        "lexical_max": float(lexical_sim.max()),
        "lexical_pairs_over_10pct": int((lexical_sim >= 0.10).sum()),
        "lexical_total_pairs": int(lexical_sim.size),
    }

    if embedder is not None:
        acme_vecs = _embed_texts(acme_texts, embedder)
        globex_vecs = _embed_texts(globex_texts, embedder)
        cross_sim = acme_vecs @ globex_vecs.T

        acme_self = acme_vecs @ acme_vecs.T
        acme_iu = np.triu_indices_from(acme_self, k=1)
        globex_self = globex_vecs @ globex_vecs.T
        globex_iu = np.triu_indices_from(globex_self, k=1)

        result.update({
            "semantic_cross_mean": float(cross_sim.mean()),
            "semantic_acme_self_mean": float(acme_self[acme_iu].mean()),
            "semantic_globex_self_mean": float(globex_self[globex_iu].mean()),
        })
        result["semantic_cross_lower_than_self"] = bool(
            result["semantic_cross_mean"] <= min(
                result["semantic_acme_self_mean"], result["semantic_globex_self_mean"]
            )
        )

    return result


def run(with_embeddings: bool) -> List[Dict]:
    embedder = None
    if with_embeddings:
        from src.core.settings import load_settings
        from src.libs.embedding.embedding_factory import EmbeddingFactory

        settings = load_settings("config/settings.yaml")
        embedder = EmbeddingFactory.create(settings)

    return [_compute_category(cat, embedder) for cat in SHARED_CATEGORIES]


def _print_table(results: List[Dict]) -> None:
    header = f"{'分类':<14}{'Acme条数':<10}{'Globex条数':<12}{'词法均值':<10}{'词法最大':<10}{'>10%对数':<10}"
    if any("semantic_cross_mean" in r for r in results):
        header += f"{'语义均值':<10}{'企业内基线(A/G)':<20}"
    print(header)
    print("-" * len(header))
    for r in results:
        row = (
            f"{r['label']:<14}{r['acme_count']:<10}{r['globex_count']:<12}"
            f"{r['lexical_mean']*100:>7.2f}%  {r['lexical_max']*100:>7.2f}%  "
            f"{r['lexical_pairs_over_10pct']:>4}/{r['lexical_total_pairs']:<5}"
        )
        if "semantic_cross_mean" in r:
            row += (
                f"{r['semantic_cross_mean']*100:>7.2f}%  "
                f"{r['semantic_acme_self_mean']*100:>6.2f}% / {r['semantic_globex_self_mean']*100:>6.2f}%"
            )
        print(row)


def _write_report(results: List[Dict]) -> Path:
    reports_dir = REPO_ROOT / "reports"
    reports_dir.mkdir(exist_ok=True)
    ts = int(time.time())

    json_path = reports_dir / f"tenant_kb_similarity_{ts}.json"
    json_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# Acme / Globex 同类型知识库相似度对比",
        "",
        f"生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "词法相似度＝TF-IDF 余弦（jieba 分词），衡量内容/用词重叠，<10% 是本次的达标线。"
        "语义相似度＝生产环境同款 embedding 模型的余弦相似度，同语言同领域文本天然有较高基线，"
        "只跟\"企业内部同类文档自相似度\"做相对比较，不拿绝对值卡 10%。",
        "",
        "| 分类 | Acme条数 | Globex条数 | 词法均值 | 词法最大 | 超10%的文档对 | 语义均值 | 企业内基线(Acme/Globex) | 跨企业语义 ≤ 企业内基线 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        semantic_mean = f"{r['semantic_cross_mean']*100:.2f}%" if "semantic_cross_mean" in r else "—"
        baseline = (
            f"{r['semantic_acme_self_mean']*100:.2f}% / {r['semantic_globex_self_mean']*100:.2f}%"
            if "semantic_acme_self_mean" in r else "—"
        )
        lower = ("是" if r.get("semantic_cross_lower_than_self") else "否") if "semantic_cross_mean" in r else "—"
        lines.append(
            f"| {r['label']} | {r['acme_count']} | {r['globex_count']} | "
            f"{r['lexical_mean']*100:.2f}% | {r['lexical_max']*100:.2f}% | "
            f"{r['lexical_pairs_over_10pct']}/{r['lexical_total_pairs']} | "
            f"{semantic_mean} | {baseline} | {lower} |"
        )

    md_path = reports_dir / f"tenant_kb_similarity_{ts}.md"
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return md_path


def main() -> None:
    parser = argparse.ArgumentParser(description="对比 Acme/Globex 同类型知识库的内容相似度")
    parser.add_argument("--with-embeddings", action="store_true", help="额外计算语义相似度（需要本地 Ollama embedding 服务）")
    args = parser.parse_args()

    results = run(args.with_embeddings)
    _print_table(results)

    over_threshold = [r["category"] for r in results if r["lexical_mean"] >= 0.10]
    if over_threshold:
        print(f"\n[FAIL] 以下分类词法相似度均值 >= 10%: {over_threshold}")
    else:
        print("\n[OK] 全部同类型知识库词法相似度均值 < 10%")

    report_path = _write_report(results)
    print(f"报告已写入: {report_path}")


if __name__ == "__main__":
    main()
