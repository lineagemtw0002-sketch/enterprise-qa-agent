"""演示用知识库语料的内容定义（Acme / Globex 各 6 个部门库）。

**为什么不复用 `scripts/generate_tenant_kb_corpus.py`**：那份脚本用
`rng.choice([5, 10, 15, 20])` 随机挑数字，于是同一个库里会同时存在
"年假 5 天""年假 10 天""年假 20 天"三种说法——做压力/相似度语料没问题，
但做**人工测试数据集**就是灾难：问"年假多少天"没有唯一正确答案，
人没法判断模型答得对不对。

这里的语料是**确定性**的：
- 没有 RNG，同样的代码永远生成同样的文件（幂等摄入的前提）；
- 每条会变的数字都**显式绑定一个主语**（哪个部门 / 哪个枢纽 / 哪个套餐），
  所以不会出现"同一个问题两个答案"；
- 两家企业的同名部门刻意给**不同的锚点事实**（Acme 年假 15 天 /
  Globex 年假 20 天），跨企业隔离一旦失效，演示时一眼就能看出来。

⚠️ 语料输出目录是 `data/demo_kb_corpus/`，**刻意不写进
`data/tenant_demo/{tenant}/kb_corpus/`** —— 后者被
`tests/integration/test_tenant_kb_similarity.py` 直接 rglob 扫描并断言
跨企业词法相似度 < 10%，往里加文件会改变那个测试的输入。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Sequence


@dataclass(frozen=True)
class DocSpec:
    """一篇知识库文档。

    - `no`：文档编号（如 `ACME-HR-001`），人工测试文档里用它定位"数据在哪篇文档里"。
    - `title`：标题，同时进正文和文件名。
    - `body`：正文，几行纯文本。
    """

    no: str
    title: str
    body: str

    def filename(self) -> str:
        return f"{self.no}_{_slug(self.title)}.txt"

    def render(self, org_name: str, kb_label: str) -> str:
        """抬头 + 正文。

        ⚠️ 2026-08-26 排查记录，别再重复走一遍：一度怀疑「开头四行统一抬头
        让所有文档的 `_rule_based_summary` 都以同一段样板话开头，摘要向量挤成
        一团」是检索不到的原因，于是把抬头搬到文末重新摄入了一遍
        `acme_it_support_kb`——**同一批问题仍然 0/5，抬头位置不是原因**。
        真正的原因是 `_narrow_by_document_summary` 取的是**跨全部候选库的
        全局前 `top_docs`（默认 5）篇文档**，候选池从 6×20 涨到 6×121 之后，
        正确文档挤不进这全局 5 篇，于是整条链路返回「未找到相关结果」。
        实测同一份数据、同一批问题：`top_docs=5` 命中 2/10，`top_docs=30` 命中 7/10。
        """
        return (
            f"文档编号：{self.no}\n"
            f"标题：{self.title}\n"
            f"所属企业：{org_name}\n"
            f"所属知识库：{kb_label}\n"
            f"\n"
            f"{self.body.strip()}\n"
        )


def _slug(title: str) -> str:
    bad = ' \t/\\:*?"<>|（）()，,。.、'
    out = "".join("_" if ch in bad else ch for ch in title)
    while "__" in out:
        out = out.replace("__", "_")
    return out.strip("_")[:40]


def D(no: str, title: str, body: str) -> DocSpec:
    return DocSpec(no=no, title=title, body=body)


def series(
    prefix: str,
    start: int,
    title_tpl: str,
    body_tpl: str,
    subjects: Sequence[str],
    **fields: Sequence,
) -> List[DocSpec]:
    """按"一个主题 × 多个主语"展开成多篇文档。

    每篇的浮动数字来自 `fields` 里各列表按下标取值（`i % len`），**不是随机**——
    同一个下标永远对应同一个主语，重跑结果逐字节一致。因为标题和正文里都写死了
    主语（`{s}`），不同文档之间不会出现"同一个问题两个答案"。
    """
    docs: List[DocSpec] = []
    for i, s in enumerate(subjects):
        vals = {k: v[i % len(v)] for k, v in fields.items()}
        docs.append(
            D(
                f"{prefix}-{start + i:03d}",
                title_tpl.format(s=s, **vals),
                body_tpl.format(s=s, **vals),
            )
        )
    return docs


@dataclass(frozen=True)
class KbSpec:
    """一个知识库：collection 名 + 展示名 + 文档列表构造函数。"""

    collection: str
    tenant: str
    category: str
    org_name: str
    kb_label: str
    build: Callable[[], List[DocSpec]]

    def docs(self) -> List[DocSpec]:
        from scripts.demo_kb_content.quick_facts import build_quick_facts_doc

        docs = self.build()
        # 速查表排在最前面：它负责消解「原有 20 篇随机语料」跟本批统一口径的
        # 数值冲突，见 quick_facts.py 顶部说明。前缀从本库第一篇的编号里取。
        prefix = docs[0].no.rsplit("-", 1)[0]
        docs = [build_quick_facts_doc(self.collection, prefix)] + docs
        nos = [d.no for d in docs]
        assert len(set(nos)) == len(nos), f"{self.collection} 文档编号重复"
        return docs


def all_kb_specs() -> List[KbSpec]:
    from scripts.demo_kb_content import acme, globex

    return acme.KBS + globex.KBS


ORG_NAMES = {
    "acme": "Acme 有限公司",
    "globex": "Globex 环球集团",
}
