"""
造半年的考勤打卡测试数据，写入 attendance_records 表（PostgreSQL）。

规则参照 data/kb_seed/attendance_kb.txt 里的考勤制度：
- 标准工时 9:30-18:30，弹性上班 9:00-10:00 之间打卡都算正常。
- 只对 users 表里现有的用户造数据；周末不打卡。
- 每个工作日按概率随机落到：正常 / 迟到 / 早退 / 迟到+早退 / 请假 / 缺勤。

用法：
    python scripts/seed_attendance_data.py [--months 6] [--seed 42]
"""

import argparse
import asyncio
import random
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

import asyncpg

from src.ragent_backend.attendance_store import (
    AttendanceStore,
    STATUS_ABSENT,
    STATUS_EARLY_LEAVE,
    STATUS_LATE,
    STATUS_LATE_AND_EARLY_LEAVE,
    STATUS_LEAVE,
    STATUS_NORMAL,
)

DSN = "postgresql://ragent:ragent@localhost:5432/ragent"

# (状态, 概率) —— 概率之和为 1
STATUS_WEIGHTS = [
    (STATUS_NORMAL, 0.82),
    (STATUS_LATE, 0.06),
    (STATUS_EARLY_LEAVE, 0.05),
    (STATUS_LATE_AND_EARLY_LEAVE, 0.02),
    (STATUS_LEAVE, 0.03),
    (STATUS_ABSENT, 0.02),
]

STANDARD_CHECK_IN = (9, 30)   # 政策标准上班时间
STANDARD_CHECK_OUT = (18, 30)  # 政策标准下班时间
FLEX_WINDOW_END = (10, 0)     # 弹性打卡窗口截止，超过算迟到


def _epoch(d: date, hour: int, minute: int) -> float:
    return datetime(d.year, d.month, d.day, hour, minute).timestamp()


def _pick_status(rng: random.Random) -> str:
    r = rng.random()
    acc = 0.0
    for status, weight in STATUS_WEIGHTS:
        acc += weight
        if r <= acc:
            return status
    return STATUS_NORMAL


def _generate_day(rng: random.Random, work_date: date) -> dict:
    status = _pick_status(rng)

    if status in (STATUS_LEAVE, STATUS_ABSENT):
        return {
            "check_in_at": None,
            "check_out_at": None,
            "status": status,
            "late_minutes": 0,
            "early_leave_minutes": 0,
        }

    # 正常打卡：9:00-10:00 弹性窗口内随机
    check_in_minute = rng.randint(0, 60)  # 相对 9:00 的偏移
    check_in_hour, check_in_min = 9, check_in_minute
    if check_in_min >= 60:
        check_in_hour, check_in_min = 10, check_in_min - 60

    check_out_hour, check_out_min = 18, 30 + rng.randint(-5, 20)
    if check_out_min >= 60:
        check_out_hour, check_out_min = 19, check_out_min - 60
    elif check_out_min < 0:
        check_out_hour, check_out_min = 18, 60 + check_out_min

    late_minutes = 0
    early_leave_minutes = 0

    if status in (STATUS_LATE, STATUS_LATE_AND_EARLY_LEAVE):
        late_minutes = rng.randint(5, 45)
        check_in_hour, check_in_min = 10, late_minutes
        if check_in_min >= 60:
            check_in_hour, check_in_min = 11, check_in_min - 60

    if status in (STATUS_EARLY_LEAVE, STATUS_LATE_AND_EARLY_LEAVE):
        early_leave_minutes = rng.randint(15, 90)
        total_min = 18 * 60 + 30 - early_leave_minutes
        check_out_hour, check_out_min = divmod(total_min, 60)

    return {
        "check_in_at": _epoch(work_date, check_in_hour, check_in_min),
        "check_out_at": _epoch(work_date, check_out_hour, check_out_min),
        "status": status,
        "late_minutes": late_minutes,
        "early_leave_minutes": early_leave_minutes,
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--months", type=int, default=6)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    rng = random.Random(args.seed)

    conn = await asyncpg.connect(DSN)
    users = await conn.fetch("SELECT id, username FROM users ORDER BY created_at")
    await conn.close()

    if not users:
        print("[SKIP] users 表为空，先建几个用户再造考勤数据")
        return

    end_date = date.today()
    start_date = end_date - timedelta(days=30 * args.months)

    store = AttendanceStore()
    total = 0
    status_counter = {status: 0 for status, _ in STATUS_WEIGHTS}

    for user in users:
        user_id, username = user["id"], user["username"]
        d = start_date
        user_records = 0
        while d <= end_date:
            if d.weekday() < 5:  # 0-4 = 周一至周五
                day = _generate_day(rng, d)
                await store.upsert_record(
                    user_id=user_id,
                    work_date=d,
                    check_in_at=day["check_in_at"],
                    check_out_at=day["check_out_at"],
                    status=day["status"],
                    late_minutes=day["late_minutes"],
                    early_leave_minutes=day["early_leave_minutes"],
                )
                status_counter[day["status"]] += 1
                user_records += 1
                total += 1
            d += timedelta(days=1)
        print(f"[OK] {username} ({user_id}): {user_records} 条工作日记录")

    await store.close()

    print(f"\n共写入 {total} 条记录，时间范围 {start_date} ~ {end_date}")
    print("状态分布：", status_counter)


if __name__ == "__main__":
    asyncio.run(main())
