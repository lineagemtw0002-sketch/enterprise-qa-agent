"""故障注入的命令行入口。**演示件。**

    # 看当前状态（有哪些服务、注了什么故障）
    .venv/bin/python -m services.ops_probe_demo.control state

    # 把 order-service 弄坏，然后去大屏上看它变红
    .venv/bin/python -m services.ops_probe_demo.control inject \
        --service order-service --kind error_spike

    # 手动恢复（执行一次修复动作也会自动恢复对应服务）
    .venv/bin/python -m services.ops_probe_demo.control heal --service order-service
    .venv/bin/python -m services.ops_probe_demo.control heal          # 全部恢复
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def _call(port: int, method: str, path: str, body: dict | None = None):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        print(f"❌ {e.code}: {e.read().decode()}")
        raise SystemExit(1)
    except urllib.error.URLError as e:
        # 最常见的情形就是探针没起，值得直接说清楚而不是抛一个连接错误。
        print(f"❌ 连不上控制口 127.0.0.1:{port}（探针没在跑？）：{e.reason}")
        raise SystemExit(1)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=["state", "inject", "heal"])
    ap.add_argument("--service")
    ap.add_argument("--kind", default="error_spike",
                    help="error_spike / latency_spike / queue_backlog / down")
    ap.add_argument("--port", type=int, default=9330)
    args = ap.parse_args()

    if args.action == "state":
        s = _call(args.port, "GET", "/state")
        print(f"环境：{s['label']}（{s['environment']}）")
        print("服务：" + "、".join(s["services"]))
        print("故障类型：" + "、".join(f"{k}={v}" for k, v in s["fault_kinds"].items()))
        print("当前注入：" + (json.dumps(s["faults"], ensure_ascii=False) if s["faults"] else "无（一切正常）"))
        return 0

    if args.action == "inject":
        if not args.service:
            print("❌ inject 需要 --service")
            return 1
        r = _call(args.port, "POST", "/inject", {"service": args.service, "kind": args.kind})
        print(f"✅ 已注入 {args.service} ← {args.kind}；当前故障：{json.dumps(r['faults'], ensure_ascii=False)}")
        print("   现在去运维塔台看它变红；跑一次分析 → 提议 → 审批 → 执行，执行成功后会自动恢复。")
        return 0

    r = _call(args.port, "POST", "/heal", {"service": args.service} if args.service else {})
    print(f"✅ 已恢复；剩余故障：{json.dumps(r['faults'], ensure_ascii=False) or '无'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
