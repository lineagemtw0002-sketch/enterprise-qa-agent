"""联邦查询的短 TTL 内存缓存（`docs/aiops_module_design.md` §3.5「缓存边界」）。

设计文档把边界**写死**了，这里逐条落实，改之前先去读那一段：

> 允许极短 TTL（如 30–60 秒）的内存级缓存，用于避免同一个大屏面板重复渲染时
> 频繁重新拉取；**不允许持久化存储、不允许跨会话保留、不允许写入数据库**。

之所以在设计阶段就写死，是因为这个项目在 BM25 索引上吃过亏——"无缓存导致每次
全量加载"和"缓存策略"的两难留到实施后期才发现要返工
（`docs/scale_slo_and_priorities.md` §1.4/§6.1）。

**本文件不 import 任何存储/数据库模块，这是刻意的**：想加持久化就必须先加依赖，
review 时一眼能看见。

两条不那么显然、但很重要的决定：

1. **只缓存完整成功的结果**（`errors` 非空一律不缓存）。否则"某个连接器离线"
   会被缓存 60 秒——用户修好连接器后刷新页面，还是看到"数据不可用"，
   而且 §3.5 第 4 条要求的降级提示会变成一条过期的谎言。
   宁可多查一次，也不要缓存故障。
2. **缓存键必须含 `org_id`**。少了它，A 企业的运维数据会因为查询指纹相同
   被 B 企业读到——这是跨租户数据泄露，不是性能问题。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Sequence, Tuple

from src.ops.types import FederatedResult, QueryRequest

DEFAULT_TTL_SECONDS = 45.0   # 设计文档给的区间是 30–60 秒，取中间值
MAX_TTL_SECONDS = 60.0       # 上界来自设计文档，不允许调大——要改先改设计
DEFAULT_MAX_ENTRIES = 512    # 内存上界：这是缓存不是数据仓库，满了按最旧淘汰


@dataclass
class _Entry:
    result: FederatedResult
    expires_at: float


class FederatedQueryCache:
    """进程内、有 TTL 上界、有条数上界的查询缓存。

    不是线程安全的强一致缓存，也不需要是——最坏情况是并发时多查一次上游，
    代价远小于为此引入锁。**但绝不能出现"读到别的 org 的结果"**，
    那由缓存键里的 org_id 保证，不是靠锁。
    """

    def __init__(
        self,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl_seconds > MAX_TTL_SECONDS:
            raise ValueError(
                f"TTL 上限是 {MAX_TTL_SECONDS}s（设计文档 §3.5 写死的），收到 {ttl_seconds}s。"
                "要放宽必须先改设计文档，不要在这里改。"
            )
        self.ttl_seconds = max(ttl_seconds, 0.0)
        self.max_entries = max_entries
        self._clock = clock
        self._entries: Dict[Tuple[str, str, str], _Entry] = {}

    @staticmethod
    def _key(org_id: str, request: QueryRequest, connection_ids: Sequence[str]) -> Tuple[str, str, str]:
        # connection_ids 排序后参与键：查同样两个连接器、顺序不同，是同一次查询。
        return (org_id, request.fingerprint(), ",".join(sorted(connection_ids)))

    def get(self, org_id: str, request: QueryRequest, connection_ids: Sequence[str]) -> Optional[FederatedResult]:
        if self.ttl_seconds <= 0:
            return None
        key = self._key(org_id, request, connection_ids)
        entry = self._entries.get(key)
        if entry is None:
            return None
        if entry.expires_at <= self._clock():
            del self._entries[key]
            return None
        # from_cache 让调用方能在 UI 上标"这是 N 秒内的缓存"，也让测试能断言命中。
        return FederatedResult(
            request=entry.result.request,
            results=entry.result.results,
            errors=entry.result.errors,
            from_cache=True,
        )

    def put(
        self, org_id: str, request: QueryRequest, connection_ids: Sequence[str], result: FederatedResult
    ) -> bool:
        """写入缓存；返回是否真的写了。

        **有任何连接器失败就不缓存**——见模块顶部第 1 条。
        """
        if self.ttl_seconds <= 0 or result.errors:
            return False
        if len(self._entries) >= self.max_entries:
            self._evict_oldest()
        self._entries[self._key(org_id, request, connection_ids)] = _Entry(
            result=result, expires_at=self._clock() + self.ttl_seconds
        )
        return True

    def _evict_oldest(self) -> None:
        if not self._entries:
            return
        oldest = min(self._entries.items(), key=lambda kv: kv[1].expires_at)[0]
        del self._entries[oldest]

    def invalidate_org(self, org_id: str) -> int:
        """清掉某个 org 的全部缓存。连接器被吊销/重新注册时调用——
        否则用户刚断开一个系统，还能从缓存里继续看到它的数据。"""
        keys = [k for k in self._entries if k[0] == org_id]
        for k in keys:
            del self._entries[k]
        return len(keys)

    def clear(self) -> None:
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)
