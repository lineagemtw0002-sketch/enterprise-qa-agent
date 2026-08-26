"""
运营仪表盘统计 (Dashboard Stats) — 只读聚合查询

职责：给平台管理员的「运营仪表盘」提供 KPI 概览 + 趋势图数据。纯只读，不建
表——复用 `conversations`（会话元数据，见 conversation_store.py）和
`conversation_archive`（逐条消息，见 store.py）两张已有表按时间戳聚合，没有
自己的 schema，也不需要新的埋点。

范围克制：这次只做四个有真实数据支撑的指标——会话数、消息数、活跃用户数
（三张表现成的时间戳直接聚合）、以及平均响应延迟（`conversation_archive`
的 `latency_ms` 列，只在 assistant 消息上有值，见 store.py 该列旁的说明）。
不做"质量分"——现在系统里没有真实的用户反馈信号（没有点赞/点踩机制），编一个
不可靠的质量指标出来比不做更容易误导运营方管理员，等有真实信号了再加。

延迟这一列踩过一次坑：最早想拿同一轮 user/assistant 消息的 created_at 相减
近似延迟，实测算出来的数字只有真实响应耗时的几百分之一——因为这两条消息是
`_archive_node` 同一个循环里前后脚写的 `time.time()`，都是"归档时刻"，不是
"用户发问时刻"/"回答生成完成时刻"，量级根本不对。真正的耗时改成在
`_session_node`（图执行起点）记一个 `_turn_start_ts`，`_archive_node` 归档时
相减，直接存成 `latency_ms` 列，这里只需要 `AVG(latency_ms)`，不需要自连接。

跟 conversations/conversation_archive 里两种不同的时间戳表示对齐：
- `conversations.created_at`/`updated_at` 是不带时区的 TIMESTAMP，写入时用的
  是 `datetime.now()`（本机时区，见 conversation_store.py），这里统一也用
  不带时区的 `datetime.now()` 做窗口边界，避免 asyncpg 因为一边 aware 一边
  naive 报错或者悄悄比错。
- `conversation_archive.created_at` 是 DOUBLE PRECISION 的 Unix 时间戳（写入
  时用的是 `time.time()`，见 store.py），这里用 naive datetime 的 `.timestamp()`
  转换——Python 对 naive datetime 调用 `.timestamp()` 默认按本机时区解释，
  跟 `time.time()` 锚定的是同一个本机时钟，换算结果一致。
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Literal, Optional, Tuple

import asyncpg

from src.ragent_backend.db_pool import get_shared_pool

Window = Literal["24h", "7d", "30d"]
TrendMetric = Literal["sessions", "messages", "active_users", "latency"]

_WINDOW_DELTA = {
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}


@dataclass(frozen=True)
class Overview:
    session_count: int
    session_count_prev: int
    message_count: int
    message_count_prev: int
    active_users: int
    active_users_prev: int
    avg_latency_ms: Optional[float]
    avg_latency_ms_prev: Optional[float]


@dataclass(frozen=True)
class TrendPoint:
    bucket: str  # ISO 时间戳（24h 窗口精确到小时，7d/30d 精确到天）
    value: float


@dataclass(frozen=True)
class CostOverview:
    """成本与质量概览——token 用量来自 conversation_archive（只在 assistant
    消息上填，见 workflow.py _generate_node），工具调用成功率来自 audit_logs
    （action='tool_call'，见 audit_store.py/subgraph.py tool_node）。两张表
    都可能在全新部署时还不存在，调用方（app.py）按 UndefinedTableError 兜底
    成全零，跟 get_overview 同一个模式。"""
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    total_tokens_prev: int
    tool_call_count: int
    tool_call_count_prev: int
    tool_success_count: int
    tool_success_count_prev: int


CostTrendMetric = Literal["tokens", "tool_success_rate"]


class DashboardStatsService:
    """只读聚合服务 (PostgreSQL)。"""

    # 类级别共享连接池，见 store.py 同名字段的注释——调用方经常每次都 new 一个
    # 新实例，池必须挂在类属性上才不会被重复创建、打满 Postgres 连接数。
    _pool: Optional[asyncpg.Pool] = None
    _pool_lock = asyncio.Lock()

    def __init__(self) -> None:
        self._dsn = os.getenv("RAGENT_POSTGRES_URL", "postgresql://postgres:postgres@localhost:5432/ragent")

    async def _get_pool(self) -> asyncpg.Pool:
        # 只读，不建表——conversations/conversation_archive 分别由
        # ConversationStore/ConversationArchiveStore 的 _ensure_schema 负责建；
        # 全新部署、一次对话都没发生过时这两张表可能还不存在，下面的查询会
        # 直接抛 UndefinedTableError，调用方（app.py）兜底成一个"暂无数据"的
        # 空响应，不在这里特殊处理。
        if self._pool is not None:
            return self._pool
        async with self._pool_lock:
            if self._pool is None:
                type(self)._pool = await get_shared_pool(self._dsn)
        return self._pool

    @staticmethod
    def _window_bounds(window: Window) -> Tuple[datetime, datetime, datetime]:
        """返回 (当前窗口起点, 当前窗口终点=now, 上一个等长窗口起点)，供环比用。"""
        now = datetime.now()
        delta = _WINDOW_DELTA[window]
        return now - delta, now, now - 2 * delta

    async def get_overview(self, window: Window) -> Overview:
        start, end, prev_start = self._window_bounds(window)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            session_count = await conn.fetchval(
                "SELECT COUNT(*) FROM conversations WHERE created_at >= $1 AND created_at < $2", start, end,
            )
            session_count_prev = await conn.fetchval(
                "SELECT COUNT(*) FROM conversations WHERE created_at >= $1 AND created_at < $2", prev_start, start,
            )
            message_count = await conn.fetchval(
                "SELECT COUNT(*) FROM conversation_archive WHERE created_at >= $1 AND created_at < $2",
                start.timestamp(), end.timestamp(),
            )
            message_count_prev = await conn.fetchval(
                "SELECT COUNT(*) FROM conversation_archive WHERE created_at >= $1 AND created_at < $2",
                prev_start.timestamp(), start.timestamp(),
            )
            active_users = await conn.fetchval(
                """SELECT COUNT(DISTINCT user_id) FROM conversations
                   WHERE user_id IS NOT NULL AND updated_at >= $1 AND updated_at < $2""",
                start, end,
            )
            active_users_prev = await conn.fetchval(
                """SELECT COUNT(DISTINCT user_id) FROM conversations
                   WHERE user_id IS NOT NULL AND updated_at >= $1 AND updated_at < $2""",
                prev_start, start,
            )
            avg_latency_ms = await self._avg_latency(conn, start, end)
            avg_latency_ms_prev = await self._avg_latency(conn, prev_start, start)
        return Overview(
            session_count=session_count or 0, session_count_prev=session_count_prev or 0,
            message_count=message_count or 0, message_count_prev=message_count_prev or 0,
            active_users=active_users or 0, active_users_prev=active_users_prev or 0,
            avg_latency_ms=avg_latency_ms, avg_latency_ms_prev=avg_latency_ms_prev,
        )

    @staticmethod
    async def _avg_latency(conn: asyncpg.Connection, start: datetime, end: datetime) -> Optional[float]:
        avg_latency = await conn.fetchval(
            """SELECT AVG(latency_ms) FROM conversation_archive
               WHERE role = 'assistant' AND latency_ms IS NOT NULL
                 AND created_at >= $1 AND created_at < $2""",
            start.timestamp(), end.timestamp(),
        )
        return round(avg_latency, 1) if avg_latency is not None else None

    async def get_trend(self, metric: TrendMetric, window: Window) -> List[TrendPoint]:
        start, end, _ = self._window_bounds(window)
        bucket_unit = "hour" if window == "24h" else "day"  # 只有这两个受控字面量，不是拼用户输入
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            if metric == "sessions":
                rows = await conn.fetch(
                    f"""SELECT date_trunc('{bucket_unit}', created_at) AS bucket, COUNT(*) AS value
                        FROM conversations WHERE created_at >= $1 AND created_at < $2
                        GROUP BY bucket ORDER BY bucket""",
                    start, end,
                )
            elif metric == "active_users":
                rows = await conn.fetch(
                    f"""SELECT date_trunc('{bucket_unit}', updated_at) AS bucket, COUNT(DISTINCT user_id) AS value
                        FROM conversations WHERE user_id IS NOT NULL AND updated_at >= $1 AND updated_at < $2
                        GROUP BY bucket ORDER BY bucket""",
                    start, end,
                )
            elif metric == "messages":
                rows = await conn.fetch(
                    f"""SELECT date_trunc('{bucket_unit}', to_timestamp(created_at)) AS bucket, COUNT(*) AS value
                        FROM conversation_archive WHERE created_at >= $1 AND created_at < $2
                        GROUP BY bucket ORDER BY bucket""",
                    start.timestamp(), end.timestamp(),
                )
            else:  # latency
                rows = await conn.fetch(
                    f"""SELECT date_trunc('{bucket_unit}', to_timestamp(created_at)) AS bucket, AVG(latency_ms) AS value
                        FROM conversation_archive
                        WHERE role = 'assistant' AND latency_ms IS NOT NULL
                          AND created_at >= $1 AND created_at < $2
                        GROUP BY bucket ORDER BY bucket""",
                    start.timestamp(), end.timestamp(),
                )
        return [TrendPoint(bucket=r["bucket"].isoformat(), value=float(r["value"] or 0)) for r in rows]

    async def get_cost_overview(self, window: Window) -> CostOverview:
        start, end, prev_start = self._window_bounds(window)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            prompt_tokens, completion_tokens, total_tokens = await self._token_sums(conn, start, end)
            _, _, total_tokens_prev = await self._token_sums(conn, prev_start, start)
            tool_call_count, tool_success_count = await self._tool_call_counts(conn, start, end)
            tool_call_count_prev, tool_success_count_prev = await self._tool_call_counts(conn, prev_start, start)
        return CostOverview(
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
            total_tokens=total_tokens, total_tokens_prev=total_tokens_prev,
            tool_call_count=tool_call_count, tool_call_count_prev=tool_call_count_prev,
            tool_success_count=tool_success_count, tool_success_count_prev=tool_success_count_prev,
        )

    @staticmethod
    async def _token_sums(conn: asyncpg.Connection, start: datetime, end: datetime) -> Tuple[int, int, int]:
        row = await conn.fetchrow(
            """SELECT COALESCE(SUM(prompt_tokens), 0) AS p, COALESCE(SUM(completion_tokens), 0) AS c,
                      COALESCE(SUM(total_tokens), 0) AS t
               FROM conversation_archive
               WHERE role = 'assistant' AND created_at >= $1 AND created_at < $2""",
            start.timestamp(), end.timestamp(),
        )
        return int(row["p"]), int(row["c"]), int(row["t"])

    @staticmethod
    async def _tool_call_counts(conn: asyncpg.Connection, start: datetime, end: datetime) -> Tuple[int, int]:
        row = await conn.fetchrow(
            """SELECT COUNT(*) AS total, COUNT(*) FILTER (WHERE success) AS ok
               FROM audit_logs
               WHERE action = 'tool_call' AND created_at >= $1 AND created_at < $2""",
            start.timestamp(), end.timestamp(),
        )
        return int(row["total"]), int(row["ok"])

    async def get_cost_trend(self, metric: CostTrendMetric, window: Window) -> List[TrendPoint]:
        start, end, _ = self._window_bounds(window)
        bucket_unit = "hour" if window == "24h" else "day"  # 只有这两个受控字面量，不是拼用户输入
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            if metric == "tokens":
                rows = await conn.fetch(
                    f"""SELECT date_trunc('{bucket_unit}', to_timestamp(created_at)) AS bucket,
                               COALESCE(SUM(total_tokens), 0) AS value
                        FROM conversation_archive
                        WHERE role = 'assistant' AND created_at >= $1 AND created_at < $2
                        GROUP BY bucket ORDER BY bucket""",
                    start.timestamp(), end.timestamp(),
                )
            else:  # tool_success_rate
                rows = await conn.fetch(
                    f"""SELECT date_trunc('{bucket_unit}', to_timestamp(created_at)) AS bucket,
                               (COUNT(*) FILTER (WHERE success))::float / NULLIF(COUNT(*), 0) * 100 AS value
                        FROM audit_logs
                        WHERE action = 'tool_call' AND created_at >= $1 AND created_at < $2
                        GROUP BY bucket ORDER BY bucket""",
                    start.timestamp(), end.timestamp(),
                )
        return [TrendPoint(bucket=r["bucket"].isoformat(), value=float(r["value"] or 0)) for r in rows]

    async def close(self) -> None:
        # 池现在是跨 14 个 Store 共享的（db_pool.py，P1-2），这里只清掉
        # 本 Store 持有的引用，不触发真实关闭——那会把其它 Store 正在用的
        # 连接一起关掉。真正关闭见 db_pool.close_shared_pools()，只在 app
        # 关闭时调一次。
        type(self)._pool = None
