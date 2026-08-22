"""本地开发/QA 用：定期探活 tenant_kb_demo / tenant_attendance_demo 这几个模拟企业微服务，
挂了就照 seed_tenant_kb_demo.py docstring 里给的同一套环境变量把它重新拉起来。

只管本地/dev 环境自己起的这几个桩服务——跟"网关"页（app.py::_check_connector_health）
探测真实客户外部连接器是两回事，故意不共用逻辑、也不合并成一个模块：真实客户的外部
服务不归平台管理，平台没有、也不应该有能力把它拉起来（见 knowledge-base-tenant-federation.md
"平台不接管客户自己的外部系统"这条边界）。这里守护的四个进程本质是我们自己在本机
起的桩服务，跟客户的外部系统只是"长得像"，不是一回事。

以后要管理其他本地依赖的桩服务/进程，在 _build_services() 里加一个 ManagedService 就够，
探活/拉起/日志这套逻辑不用碰。

用法：
    python scripts/tenant_service_supervisor.py             # 常驻，默认每 15s 探一轮
    python scripts/tenant_service_supervisor.py --interval 30
    python scripts/tenant_service_supervisor.py --once      # 只探测+拉起一轮就退出，不常驻

前提：数据库里已经跑过 scripts/seed_tenant_kb_demo.py（本脚本用组织名现查 org_id，
没查到会直接报错提示先跑种子脚本）。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx
from dotenv import load_dotenv

load_dotenv()

from src.ragent_backend.org_store import OrgStore

REPO_ROOT = Path(__file__).parent.parent
LOG_DIR = REPO_ROOT / "logs" / "tenant_services"

ACME_ORG_NAME = "Acme 有限公司"
GLOBEX_ORG_NAME = "Globex 环球集团"


def _log(message: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


@dataclass
class ManagedService:
    name: str
    port: int
    module: str  # uvicorn 目标，如 "services.tenant_kb_demo.app:app"
    env: dict[str, str]
    healthz_path: str = "/healthz"
    # 本次运行里自己拉起的进程句柄——用来判断"上一轮刚起的还在启动中"，
    # 避免探活还没转正就在下一轮里被重复拉起第二个抢同一个端口。
    proc: asyncio.subprocess.Process | None = field(default=None, repr=False)


def _build_services(acme_org_id: str, globex_org_id: str) -> list[ManagedService]:
    # 四条命令照抄 scripts/seed_tenant_kb_demo.py docstring 里打印出来的那份，
    # port/token 是种子数据里定死的常量，org_id 现查（见 _resolve_org_ids）。
    return [
        ManagedService(
            name="acme-kb", port=9101, module="services.tenant_kb_demo.app:app",
            env={
                "TENANT_NAME": "acme", "TENANT_ORG_ID": acme_org_id,
                "TENANT_TOKEN": "acme-demo-token-2026", "TENANT_DATA_DIR": "data/tenant_demo/acme",
            },
        ),
        ManagedService(
            name="globex-kb", port=9102, module="services.tenant_kb_demo.app:app",
            env={
                "TENANT_NAME": "globex", "TENANT_ORG_ID": globex_org_id,
                "TENANT_TOKEN": "globex-demo-token-2026", "TENANT_DATA_DIR": "data/tenant_demo/globex",
            },
        ),
        ManagedService(
            name="acme-attendance", port=9201, module="services.tenant_attendance_demo.app:app",
            env={
                "TENANT_NAME": "acme", "TENANT_ORG_ID": acme_org_id,
                "TENANT_TOKEN": "acme-attendance-token-2026", "TENANT_FIELD_STYLE": "native",
            },
        ),
        ManagedService(
            name="globex-attendance", port=9202, module="services.tenant_attendance_demo.app:app",
            env={
                "TENANT_NAME": "globex", "TENANT_ORG_ID": globex_org_id,
                "TENANT_TOKEN": "globex-attendance-token-2026", "TENANT_FIELD_STYLE": "renamed",
            },
        ),
    ]


async def _resolve_org_ids() -> tuple[str, str]:
    orgs = {o.name: o.org_id for o in await OrgStore().list_organizations()}
    missing = [n for n in (ACME_ORG_NAME, GLOBEX_ORG_NAME) if n not in orgs]
    if missing:
        raise RuntimeError(
            f"数据库里找不到组织 {missing}，先跑一遍 python scripts/seed_tenant_kb_demo.py 把种子数据建出来"
        )
    return orgs[ACME_ORG_NAME], orgs[GLOBEX_ORG_NAME]


async def _is_healthy(svc: ManagedService, client: httpx.AsyncClient) -> bool:
    try:
        resp = await client.get(f"http://localhost:{svc.port}{svc.healthz_path}", timeout=2.0)
        return resp.status_code < 400
    except httpx.HTTPError:
        return False


async def _start(svc: ManagedService) -> None:
    if svc.proc is not None and svc.proc.returncode is None:
        _log(f"[{svc.name}] 上一次拉起的进程（pid={svc.proc.pid}）还在启动中，本轮跳过，不重复拉起")
        return

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"{svc.name}.log"
    log_file = open(log_path, "a")
    env = {**os.environ, **svc.env}
    svc.proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "uvicorn", svc.module, "--port", str(svc.port),
        cwd=str(REPO_ROOT), env=env, stdout=log_file, stderr=log_file,
        start_new_session=True,
    )
    _log(f"[{svc.name}] 探活失败，已拉起 pid={svc.proc.pid}（日志见 {log_path}）")


async def _check_and_recover(svc: ManagedService, client: httpx.AsyncClient) -> None:
    if await _is_healthy(svc, client):
        return
    await _start(svc)
    # 给刚拉起的进程几秒钟把 uvicorn 起来，起完立刻复查一次，日志里能看出
    # 这轮到底救没救活，而不是要等到下一轮探活才知道结果。
    await asyncio.sleep(3)
    ok = await _is_healthy(svc, client)
    _log(f"[{svc.name}] {'已恢复' if ok else '仍连不上，下一轮再试'}")


async def run_once(services: list[ManagedService]) -> None:
    async with httpx.AsyncClient() as client:
        await asyncio.gather(*(_check_and_recover(s, client) for s in services))


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--interval", type=float, default=15.0, help="探活间隔秒数（默认 15s，跟运营仪表盘网关页前端自动刷新周期一致）")
    parser.add_argument("--once", action="store_true", help="只探测+拉起一轮就退出，不常驻")
    args = parser.parse_args()

    acme_org_id, globex_org_id = await _resolve_org_ids()
    services = _build_services(acme_org_id, globex_org_id)

    if args.once:
        await run_once(services)
        return

    _log(f"tenant_service_supervisor 启动，管理 {len(services)} 个本地服务，每 {args.interval:.0f}s 探活一轮（Ctrl+C 退出）")
    while True:
        await run_once(services)
        await asyncio.sleep(args.interval)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
