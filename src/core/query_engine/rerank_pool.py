"""进 cross-encoder 重排的候选池上限——纯逻辑，无 IO。

**为什么需要这个**：全库并行召回里，"多查几个知识库"变慢的真实机制不是查得慢
（6 个库的 dense+sparse 是并发跑的，墙钟只有 ~32ms），而是**合并后的候选池变大**：
每个库贡献 `top_k × 2` 条，6 个库就是 60 条，而 cross-encoder 要对每个
(query, doc) 对逐一打分，占掉整个检索段 87% 的时间（实测 931.7ms / 1072ms）。

实测（30 条正向问题、6 库候选，见 `docs/hierarchical_narrowing_redesign.md`）：

| 池大小 | 金标召回 | 关键事实 | p50 |
|---|---|---|---|
| 60（不截） | 25/30 | 29/30 | 1669ms |
| 40 | 25/30 | **30/30** | 724ms |
| 30 | 25/30 | **30/30** | ~700ms |
| 20 | 19/30 | 24/30 | 398ms |

截到 30~40 条**召回一条不掉，关键事实反而多一条**（截掉低分噪音后，不再有弱候选
被 cross-encoder 抬上来挤掉正确答案），延迟省掉约 55%。

⚠️ **这里用全局上限，而不是像粗筛层那样按库分配——两处结论相反，理由不同**：
同样 30 条池子，全局截取 25/30，按库各取 5 条只有 22/30。
区别在信号质量：粗筛层的摘要相似度几乎分不出对错（金标与榜首分差中位 0.0495），
全局分配等于随机，所以必须按库保底；而这里的融合分来自各库内部真实的
dense+sparse 命中，**排名是可信的**，真正匹配的库理应多占名额。
**信号可信就全局分配，信号不可信就按库保底。**

⚠️ 但全局上限有一个必须防住的规模陷阱：实测截到 30 条时 6 个库都还有代表
（最少的剩 1~2 条），截到 20 条就只剩 4~5 个库、召回掉到 19/30——
那正是粗筛层那个"全局预算饿死库"的老毛病。所以**上限必须随候选库数增长**
（`per_collection × 库数`），绝不能写成常数。
`tests/integration/test_hierarchy_narrowing_recall.py` 有一条用真实数据守着
"截断后每个候选库仍有代表"。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Sequence


@dataclass(frozen=True)
class RerankPoolConfig:
    """`config/settings.yaml` 的 `rerank.pool_*`。

    `per_collection <= 0` 表示关闭截断（保持"全部候选都进重排"的老行为）。
    """

    per_collection: int = 5
    min_size: int = 20

    @classmethod
    def from_settings(cls, settings: Any) -> "RerankPoolConfig":
        rerank = getattr(settings, "rerank", None)
        default = cls()
        return cls(
            per_collection=int(getattr(rerank, "pool_per_collection", default.per_collection) or 0),
            min_size=int(getattr(rerank, "pool_min", default.min_size) or 0),
        )


def pool_cap(top_k: int, collection_count: int, cfg: RerankPoolConfig) -> int:
    """算出这次查询允许进重排的候选条数；返回 0 表示不截断。

    三条下限，缺一不可：

    1. `per_collection × 候选库数` —— 主公式，**随库数增长**，防止库一多就饿死谁；
    2. `min_size` —— 防止候选库很少时截得过狠。单库用户召回池本来就只有
       `top_k × 2 = 10` 条，不该被这个机制碰到（默认 20 的下限保证了这点）；
    3. `top_k × 2` —— 无论如何要给重排留够"比最终要返回的条数多一倍"的候选，
       否则 top_k 提大时会被这里卡住。
    """
    if cfg.per_collection <= 0:
        return 0
    return max(cfg.per_collection * max(collection_count, 1), cfg.min_size, top_k * 2)


def trim_pool(results: Sequence[Any], cap: int) -> List[Any]:
    """按融合分降序保留前 `cap` 条；`cap <= 0` 或池子本来就不超，原样返回。

    ⚠️ 必须自己排一次序，不能假设调用方合并时是有序的——merged 是多个库的结果
    按 collection 顺序拼起来的，跨库之间没有任何顺序保证。
    """
    if cap <= 0 or len(results) <= cap:
        return list(results)
    return sorted(results, key=lambda r: getattr(r, "score", 0.0), reverse=True)[:cap]
