"""索引侧 / 查询侧分词的**唯一定义处**。

在这个模块出现之前，两侧各写了一份 jieba 调用：
`SparseEncoder._tokenize`（索引侧）与 `QueryProcessor._tokenize`（查询侧）。
前者的注释声称"与查询侧一致，这是 BM25 匹配的前提"，**实测不成立**
（`CLAUDE.md` §4 第 9b 条）。两侧同时改动时没有任何机制强制它们同步，
分歧只能靠人肉比对发现 —— 而这个缺陷从建库起一直在线上跑，从没被测过。

## 契约不是"两侧输出相同"，是"索引侧是查询侧的超集"

这一点是本模块的核心，改动前请先读完。

BM25（以及 OpenSearch 的 `whitespace` 分析器）都是**词条级精确匹配**：
查询词条必须逐字出现在索引里才能命中。所以真正需要成立的性质是

    set(match_terms(query_tokens(t)))  ⊆  set(index_tokens(t))        对任意文本 t

**而不是**两边输出相同的列表。要求相同反而会逼出坏方案：

- 统一到索引侧的老规则（`min_term_length=2`，丢单字）→ 用户搜「年假」时
  jieba 把查询切成 `年`/`假` 两个单字，两个都被丢掉，**整条查询没有词条可查**。
  消融实测（mmarco 100 条人工标注）这条方案 recall@5 比现状还低 1 个点，
  且有 2 条查询词条数归零。
- 统一到查询侧的规则（关键词抽取器，会丢停用词）→ 索引里 `主管`、`丢失`
  这类实词会消失。

正确的方向是**让索引侧更细、更全**，查询侧保持精确：这是 jieba 官方对搜索
引擎场景的建议（`cut_for_search`），也是 Lucene 系分析器的通行做法
（index-time 扩展、query-time 精确）。

## 三个函数各自的位置

    index_tokens(t)    摄入时写进倒排索引 / OpenSearch `content` 字段的词条
    query_tokens(t)    查询时从用户问题里切出来的词（大小写原样保留，
                       因为 `ProcessedQuery.keywords` 还要用于展示与 trace）
    match_terms(ts)    **匹配层归一化** —— 真正拿去查索引之前的最后一步

`match_terms` 单独拎出来，是因为大小写归一化属于"匹配"而不是"分词"：
`BM25Indexer.query` 早就自己 `lower()` 了，而 OpenSearch 侧的 `whitespace`
分析器**不做小写化**，于是查询 `HR` 匹配不上索引里的 `hr` —— 同一个契约在
两个后端一个成立一个不成立，正是把它散落在各处的代价。

## 改这个模块 = 存量索引全部失效

任何改动都会让已建好的 BM25 JSON / SQLite 副本 / OpenSearch index 与新分词
对不上（旧索引里没有新规则才会产出的词条）。**必须重建**：

    python scripts/rebuild_bm25_from_chroma.py --skip-transient
    python scripts/migrate_to_opensearch.py --skip-transient

判据见 `scripts/ablate_tokenizer_alignment.py`（稀疏侧 recall@k 消融）与
`scripts/run_tenant_kb_golden_tests.py`（端到端答案质量）。
"""

from __future__ import annotations

import re
from typing import Iterable, List

import jieba

# 纯标点 / 纯空白的 token 直接丢。两侧共用同一条正则 —— 之前是两份各自写的
# `re.fullmatch(r'[\s\W]+', ...)`，字面相同纯属巧合，没有任何东西拦着它们分叉。
_PUNCT_ONLY = re.compile(r"[\s\W]+", re.UNICODE)

# 索引侧的最小词长。**1 而不是 2。**
#
# 旧值是 2，理由写的是"单字不是有用的 BM25 词条"。这个判断对英文成立
# （单字母确实没信息量），对中文则是错的：jieba 的词典里没有「年假」，
# 「年假可以顺延到次年三月」会被切成 `年 / 假 / 可以 / 顺延到 / 次年 / 三月`，
# 两个单字被 min_len=2 丢掉，而查询侧（min_keyword_length=1）原样保留它们
# —— 于是**一个专门讲年假的库，搜「年假」在稀疏侧一个词条都命中不了**。
# 实测 `product_req_kb`：查询词条 `年`/`假` 都不在索引词表里。
_INDEX_MIN_LEN = 1


def _clean(tokens: Iterable[str]) -> List[str]:
    out: List[str] = []
    for tok in tokens:
        tok = tok.strip()
        if not tok or _PUNCT_ONLY.fullmatch(tok):
            continue
        out.append(tok)
    return out


def index_tokens(
    text: str,
    *,
    min_len: int = _INDEX_MIN_LEN,
    lowercase: bool = True,
) -> List[str]:
    """**索引侧**分词 —— 摄入时写进索引的词条。

    用 `jieba.lcut_for_search`（搜索引擎模式）而不是 `lcut`（精确模式）。
    两者的差别正是本次要修的第二个缺陷：

        文档 "年假可以顺延到次年三月"  lcut → …, 顺延到, …
        用户查询 "顺延"                lcut → 顺延

    精确模式对同一串字在不同上下文给出不同粒度，于是索引里存的是 `顺延到`、
    用户搜的是 `顺延`，**精确匹配不上**。注意这条与"两侧配置不一致"无关 ——
    两侧都调 `lcut` 也照样中招，所以"把两个分词器统一起来"修不掉它。
    `cut_for_search` 会同时产出 `顺延`/`延到`/`顺延到`，索引侧因此成为查询侧
    的超集，这才是修法。

    代价（mmarco 604 块实测）：词表 7,471 → 9,327（+25%），
    postings 15,919 → 25,696（+61%）。索引体积是 P0 第 2 条盯着的东西，
    这个代价必须和收益一起报，不要只报 recall。

    收益（mmarco 100 条人工标注 recall）：
        top-5   79.0% → 82.0%
        top-10  83.0% → 85.0%
        top-20  83.0% → 86.0%

    ⚠️ **刻意不在索引侧过滤停用词**，哪怕那能省下约 15% 的 postings
    （实测 recall 与过滤后完全相同）。因为一旦过滤，索引的内容就和
    `QueryProcessor` 的停用词表**绑死**：将来有人从停用词表里删掉一个词，
    存量索引里根本没有那个词条，查询会静默查不到，直到全量重建为止。
    上面那条 ⊆ 契约现在是**构造上成立**的（查询侧只做减法），不依赖两张
    表保持同步 —— 这个性质比 15% 磁盘更值钱。
    """
    if min_len < 1:
        raise ValueError(f"min_len must be >= 1, got {min_len}")
    tokens = _clean(jieba.lcut_for_search(text))
    if lowercase:
        tokens = [t.lower() for t in tokens]
    return [t for t in tokens if len(t) >= min_len]


def query_tokens(text: str) -> List[str]:
    """**查询侧**分词 —— 从用户问题里切词，精确模式。

    刻意**不**用 `cut_for_search`：查询侧多切出子词会让每次查询扫更多
    postings（实测词条数 ×1.24），而 P0 第 2e 条正卡在"命中大量 postings
    时并发退化"上。消融显示查询侧也换成 `cut_for_search` 还能再多 1 个点
    （recall@10 85% → 86%），**这个取舍留给人拍板，不在本次改动里**。

    大小写**原样保留** —— `ProcessedQuery.keywords` 会进 trace 和前端展示，
    把 `iTunes` 变成 `itunes` 是无谓的信息损失。真正拿去匹配之前
    调 `match_terms()` 归一化。
    """
    return _clean(jieba.lcut(text))


def match_terms(terms: Iterable[str]) -> List[str]:
    """匹配层归一化：拿词条去查索引之前的最后一步。

    目前只有小写化。**每一个查索引的地方都必须过这一层**，否则就会出现
    `BM25Indexer.query` 自己 `lower()` 了、而 OpenSearch 的 `whitespace`
    分析器没有小写化这种"同一个契约在一个后端成立、另一个不成立"的局面
    —— 那正是修这一版之前的真实状态（查 `HR` 命中不了索引里的 `hr`）。
    """
    return [t.lower() for t in terms]
