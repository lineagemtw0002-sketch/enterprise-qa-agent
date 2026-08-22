"""
对话归档存储 (PostgreSQL 版)

职责：只存储用户可见的完整历史（冷存储）
不负责模型状态管理（那是 checkpointer 的事）

设计原则：
1. 异步写入，不阻塞主流程
2. 批量插入，减少数据库压力
3. 支持加载完整历史供用户查看
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import asyncpg


@dataclass(frozen=True)
class SessionMemoryBundle:
    """
    从 checkpointer 加载的记忆包
    注意：这不直接来自 PostgreSQL，而是来自 LangGraph 的 checkpoint
    """
    conversation_id: str
    task_id: Optional[str]
    messages: List[Any]  # 最近的消息（给模型用）
    summary: str         # 滚动摘要（给模型用）


class ConversationArchiveStore:
    """
    对话归档存储 (PostgreSQL)

    只负责：
    1. 将归档的消息批量写入 PostgreSQL（用户可见的完整历史）
    2. 从 PostgreSQL 加载完整历史供用户查看

    不负责：
    - 模型状态管理（由 LangGraph checkpointer 处理）
    """

    # 类级别共享，不是实例级别：调用方（如 auth.py 的鉴权依赖）经常每次请求都
    # `XStore()` 一个新实例，如果连接池挂在实例属性上，每个新实例都会各自懒建
    # 一个池、旧实例的池没人持有引用也不会关闭，很快把 Postgres 连接数打满
    # （见 2026-08-20 「权限系统」页面报错排查）。挂在类属性上，同一个类不管
    # 被 new 多少次都共享同一个池。
    _pool: Optional[asyncpg.Pool] = None
    _pool_lock = asyncio.Lock()

    def __init__(self) -> None:
        self._dsn = os.getenv("RAGENT_POSTGRES_URL", "postgresql://postgres:postgres@localhost:5432/ragent")

    async def _get_pool(self) -> asyncpg.Pool:
        if self._pool is not None:
            return self._pool
        async with self._pool_lock:
            if self._pool is not None:
                return self._pool
            type(self)._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=5)
            await self._ensure_schema()
        return self._pool

    async def append_to_history(
        self,
        conversation_id: str,
        messages: List[Dict[str, Any]],
        turn_id: Optional[str] = None,
    ) -> None:
        """批量追加消息到历史记录（异步调用）"""
        if not messages:
            return

        try:
            pool = await self._get_pool()
            async with pool.acquire() as conn:
                values = [
                    (
                        conversation_id,
                        msg["role"],
                        msg["content"],
                        msg.get("message_id", ""),
                        msg.get("ts", time.time()),
                        turn_id,
                        msg.get("latency_ms"),
                        msg.get("prompt_tokens"),
                        msg.get("completion_tokens"),
                        msg.get("total_tokens"),
                        msg.get("token_estimated"),
                    )
                    for msg in messages
                ]
                await conn.executemany(
                    """INSERT INTO conversation_archive
                       (conversation_id, role, content, message_id, created_at, turn_id, latency_ms,
                        prompt_tokens, completion_tokens, total_tokens, token_estimated)
                       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)""",
                    values
                )
        except Exception as e:
            print(f"[ArchiveStore] Failed to append history: {e}")

    async def load_full_history(self, conversation_id: str) -> List[Dict[str, Any]]:
        """加载完整历史（给用户看）"""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT role, content, message_id, created_at, turn_id
                   FROM conversation_archive
                   WHERE conversation_id = $1
                   ORDER BY created_at ASC, id ASC""",
                conversation_id,
            )

        return [
            {
                "role": row["role"],
                "content": row["content"],
                "message_id": row["message_id"],
                "timestamp": row["created_at"],
                "turn_id": row["turn_id"],
            }
            for row in rows
        ]

    async def delete_messages_from_turn(self, conversation_id: str, turn_id: str) -> int:
        """删除指定 turn 及之后的所有消息，返回删除的行数"""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            min_ts = await conn.fetchval(
                """SELECT MIN(created_at) FROM conversation_archive
                   WHERE conversation_id = $1 AND turn_id = $2""",
                conversation_id, turn_id,
            )

            if min_ts is None:
                result = await conn.execute(
                    """DELETE FROM conversation_archive
                       WHERE conversation_id = $1 AND turn_id = $2""",
                    conversation_id, turn_id,
                )
                # asyncpg execute returns a status string like "DELETE 3"
                return int(result.split()[-1]) if result.split()[-1].isdigit() else 0

            result = await conn.execute(
                """DELETE FROM conversation_archive
                   WHERE conversation_id = $1 AND created_at >= $2""",
                conversation_id, min_ts,
            )
            return int(result.split()[-1]) if result.split()[-1].isdigit() else 0

    async def get_turn_by_message_id(self, conversation_id: str, message_id: str) -> Optional[Dict[str, Any]]:
        """根据 message_id 查询其所属的 turn 信息"""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT turn_id, created_at FROM conversation_archive
                   WHERE conversation_id = $1 AND message_id = $2
                   LIMIT 1""",
                conversation_id, message_id,
            )
            if row:
                return {"turn_id": row["turn_id"], "created_at": row["created_at"]}
            return None

    async def close(self) -> None:
        """关闭连接池"""
        if self._pool is not None:
            await self._pool.close()
            type(self)._pool = None

    async def _ensure_schema(self) -> None:
        """确保表结构存在"""
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_archive (
                    id SERIAL PRIMARY KEY,
                    conversation_id VARCHAR(128) NOT NULL,
                    role VARCHAR(32) NOT NULL,
                    content TEXT NOT NULL,
                    message_id VARCHAR(64),
                    created_at DOUBLE PRECISION NOT NULL,
                    turn_id VARCHAR(64)
                )
                """
            )
            # 只在 assistant 消息上填——从"这一轮从哪个节点开始算"到"assistant
            # 消息归档时"的真实耗时（见 workflow.py _session_node/_archive_node
            # 里 _turn_start_ts 的说明）。不能从 created_at 反推：user/assistant
            # 两条消息是同一个 _archive_node 循环里前后脚写的 time.time()
            # （都是"归档时刻"，不是"用户发问时刻"/"回答生成完成时刻"），两者
            # 的 created_at 差值趋近于 0，量级完全不对——运营仪表盘
            # (dashboard_stats.py) 曾经直接拿 created_at 差值近似延迟，实测
            # 数量级只有几毫秒的几百分之一，明显不对，才发现这个问题，改成
            # 单独存一列真实耗时。
            await conn.execute(
                "ALTER TABLE conversation_archive ADD COLUMN IF NOT EXISTS latency_ms DOUBLE PRECISION"
            )
            # 成本可观测性用（dashboard_stats.py）——只在 assistant 消息上填，
            # 见 workflow.py _generate_node/_archive_node 旁的说明。
            # token_estimated=True 表示 LLM 没有回传真实 usage_metadata（比如
            # 本地 Ollama 模型），是按字符数估算的，仪表盘据此标注"预估"。
            await conn.execute(
                "ALTER TABLE conversation_archive ADD COLUMN IF NOT EXISTS prompt_tokens INTEGER"
            )
            await conn.execute(
                "ALTER TABLE conversation_archive ADD COLUMN IF NOT EXISTS completion_tokens INTEGER"
            )
            await conn.execute(
                "ALTER TABLE conversation_archive ADD COLUMN IF NOT EXISTS total_tokens INTEGER"
            )
            await conn.execute(
                "ALTER TABLE conversation_archive ADD COLUMN IF NOT EXISTS token_estimated BOOLEAN"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_archive_conversation_time ON conversation_archive(conversation_id, created_at)"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_archive_turn ON conversation_archive(conversation_id, turn_id)"
            )


def build_archive_store() -> ConversationArchiveStore:
    """工厂函数"""
    return ConversationArchiveStore()
