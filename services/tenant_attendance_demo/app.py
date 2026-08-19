"""企业考勤系统 —— 参考实现（用于模拟"企业自己的考勤系统"）。

`attendance-tenant-federation.md` 第 2/3 节设计的 HTTP webhook 委托路径的参考
实现：一个完全独立的进程，字段命名故意跟我方规范不一样（模拟"每家企业字段名都
不一样"这个现状，见该文档 1.1 节），用来验证 `query_attendance` 工具委托后能不能
通过 `tenant_connectors.field_mapping` 正确把对方字段名归一化回我方规范。

数据不落库——每次请求按 `employee_id` 确定性生成（同一个员工同一天生成的数据
每次都一样，但两个进程之间、两个员工之间互不相同），只覆盖 2026 年内的日期，
模拟"我们完全不持有企业的考勤数据，实时查、查完就忘"。

启动：
    TENANT_NAME=acme   TENANT_ORG_ID=<...> TENANT_TOKEN=acme-attendance-token-2026 \\
        TENANT_FIELD_STYLE=native  uvicorn services.tenant_attendance_demo.app:app --port 9201

    TENANT_NAME=globex TENANT_ORG_ID=<...> TENANT_TOKEN=globex-attendance-token-2026 \\
        TENANT_FIELD_STYLE=renamed uvicorn services.tenant_attendance_demo.app:app --port 9202
"""

from __future__ import annotations

import os
import random
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from scripts.seed_attendance_data import _generate_day

TENANT_NAME = os.getenv("TENANT_NAME", "demo")
TENANT_ORG_ID = os.getenv("TENANT_ORG_ID", "")
TENANT_TOKEN = os.getenv("TENANT_TOKEN", "")
# native：字段名跟我方规范一致（field_mapping 可以留空）。
# renamed：故意用一套不同的字段名，验证 field_mapping 归一化真的生效。
TENANT_FIELD_STYLE = os.getenv("TENANT_FIELD_STYLE", "native")

# 2026 年内——跟 scripts/seed_tenant_kb_demo.py 的日期范围保持一致。
DATA_START = date(2026, 1, 1)
DATA_END = date(2026, 12, 31)

# 我方规范字段名 -> 这家"企业自己系统"里的字段名（生成响应时用）；
# 对应的反向映射就是接入文档里企业要配的 `tenant_connectors.field_mapping`。
RENAMED_FIELDS = {
    "check_in_at": "clock_in_ts",
    "check_out_at": "clock_out_ts",
    "status": "att_status",
    "late_minutes": "late_min",
    "early_leave_minutes": "early_min",
}


class AttendanceRequest(BaseModel):
    employee_id: str
    start_date: str
    end_date: str


app = FastAPI(title=f"Tenant Attendance Demo — {TENANT_NAME}")


@app.get("/healthz")
def healthz() -> Dict[str, str]:
    return {"status": "ok", "tenant": TENANT_NAME, "field_style": TENANT_FIELD_STYLE}


def _rename(day: Dict[str, Any], work_date: date) -> Dict[str, Any]:
    day = {**day, "work_date": work_date.isoformat()}
    if TENANT_FIELD_STYLE != "renamed":
        return day
    return {RENAMED_FIELDS.get(k, k): v for k, v in day.items()}


@app.post("/webhook/attendance")
def query_attendance(
    request: AttendanceRequest,
    authorization: str = Header(default=""),
    x_organization_id: str = Header(default="", alias="X-Organization-Id"),
) -> Dict[str, List[Dict[str, Any]]]:
    token = authorization.removeprefix("Bearer ").strip()
    if not token or token != TENANT_TOKEN:
        raise HTTPException(status_code=401, detail="invalid or missing bearer token")
    if x_organization_id != TENANT_ORG_ID:
        raise HTTPException(status_code=403, detail="X-Organization-Id does not match this connector's organization")

    try:
        start = datetime.strptime(request.start_date, "%Y-%m-%d").date()
        end = datetime.strptime(request.end_date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="start_date/end_date must be YYYY-MM-DD")

    start = max(start, DATA_START)
    end = min(end, DATA_END)

    records: List[Dict[str, Any]] = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            # 每天单独播种（而不是共用一个跨天推进的 rng）：同一个 employee_id +
            # 同一天，不管这次查询的时间范围是哪天到哪天，生成的数据都一样。
            rng = random.Random(f"{TENANT_NAME}:{request.employee_id}:{d.isoformat()}")
            day = _generate_day(rng, d)
            records.append(_rename(day, d))
        d += timedelta(days=1)

    return {"records": records}
