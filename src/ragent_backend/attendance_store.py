"""
考勤打卡存储 (Attendance Store) — PostgreSQL 版

职责：按用户 + 自然日记录一条打卡记录（上班时间/下班时间/状态），供考勤查询、
统计使用。粒度是"天"，不是"次"——一天只有一条记录，迟到/早退/缺勤都是这条记录
上的状态字段，不单独建事件表（用量级不需要）。

不负责：
- 请假/加班审批流程（那是 workflow_store.py 的 leave_request 模板管的事，两者
  概念不同：这里是"实际打卡结果"，那边是"申请与审批"）
"""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass
from datetime import date
from typing import List, Optional

import asyncpg

STATUS_NORMAL = "normal"
STATUS_LATE = "late"
STATUS_EARLY_LEAVE = "early_leave"
STATUS_LATE_AND_EARLY_LEAVE = "late_and_early_leave"
STATUS_ABSENT = "absent"
STATUS_LEAVE = "leave"
VALID_STATUSES = {
    STATUS_NORMAL,
    STATUS_LATE,
    STATUS_EARLY_LEAVE,
    STATUS_LATE_AND_EARLY_LEAVE,
    STATUS_ABSENT,
    STATUS_LEAVE,
}


@dataclass(frozen=True)
class AttendanceRecord:
    record_id: str
    user_id: str
    work_date: date
    check_in_at: Optional[float]
    check_out_at: Optional[float]
    status: str
    late_minutes: int
    early_leave_minutes: int
    created_at: float


class AttendanceStore:
    """考勤打卡存储 (PostgreSQL)。"""

    def __init__(self) -> None:
        self._pool: Optional[asyncpg.Pool] = None
        self._dsn = os.getenv("RAGENT_POSTGRES_URL", "postgresql://postgres:postgres@localhost:5432/ragent")

    async def _get_pool(self) -> asyncpg.Pool:
        if self._pool is not None:
            return self._pool
        self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=5)
        await self._ensure_schema()
        return self._pool

    async def _ensure_schema(self) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS attendance_records (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    work_date DATE NOT NULL,
                    check_in_at DOUBLE PRECISION,
                    check_out_at DOUBLE PRECISION,
                    status VARCHAR(24) NOT NULL DEFAULT 'normal',
                    late_minutes INT NOT NULL DEFAULT 0,
                    early_leave_minutes INT NOT NULL DEFAULT 0,
                    created_at DOUBLE PRECISION NOT NULL,
                    UNIQUE (user_id, work_date)
                )
                """
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_attendance_user_date ON attendance_records (user_id, work_date)"
            )

    @staticmethod
    def _row_to_record(row: asyncpg.Record) -> AttendanceRecord:
        return AttendanceRecord(
            record_id=row["id"],
            user_id=row["user_id"],
            work_date=row["work_date"],
            check_in_at=row["check_in_at"],
            check_out_at=row["check_out_at"],
            status=row["status"],
            late_minutes=row["late_minutes"],
            early_leave_minutes=row["early_leave_minutes"],
            created_at=row["created_at"],
        )

    async def upsert_record(
        self,
        user_id: str,
        work_date: date,
        check_in_at: Optional[float],
        check_out_at: Optional[float],
        status: str,
        late_minutes: int = 0,
        early_leave_minutes: int = 0,
    ) -> AttendanceRecord:
        if status not in VALID_STATUSES:
            raise ValueError(f"invalid status: {status}")
        pool = await self._get_pool()
        record_id = str(uuid.uuid4())
        created_at = time.time()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO attendance_records
                    (id, user_id, work_date, check_in_at, check_out_at, status,
                     late_minutes, early_leave_minutes, created_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                ON CONFLICT (user_id, work_date) DO UPDATE SET
                    check_in_at = EXCLUDED.check_in_at,
                    check_out_at = EXCLUDED.check_out_at,
                    status = EXCLUDED.status,
                    late_minutes = EXCLUDED.late_minutes,
                    early_leave_minutes = EXCLUDED.early_leave_minutes
                RETURNING *
                """,
                record_id, user_id, work_date, check_in_at, check_out_at, status,
                late_minutes, early_leave_minutes, created_at,
            )
        return self._row_to_record(row)

    async def list_records(
        self,
        user_id: str,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
    ) -> List[AttendanceRecord]:
        pool = await self._get_pool()
        conditions = ["user_id = $1"]
        params: List[object] = [user_id]
        if start_date is not None:
            params.append(start_date)
            conditions.append(f"work_date >= ${len(params)}")
        if end_date is not None:
            params.append(end_date)
            conditions.append(f"work_date <= ${len(params)}")
        query = (
            f"SELECT * FROM attendance_records WHERE {' AND '.join(conditions)} "
            "ORDER BY work_date"
        )
        async with pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
        return [self._row_to_record(r) for r in rows]

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
