"""问答耗时基准测试（TTFT / 总耗时 / 分阶段拆解 / 冷启动对比）。

这个脚本是 `docs/latency_report.md`（2026-08-23）里那个**已丢失的** `latency_probe.py`
的替代品。当时那批数字之所以至今无人能复现，就是因为探测脚本写在临时目录里跑完就丢了。
这份脚本必须留在仓库里 —— 任何人 `git pull` 之后按下面的方法跑一遍，就能重新得到
`docs/architecture.md` §3.2 里的数字。

--------------------------------------------------------------------------------
测什么
--------------------------------------------------------------------------------
覆盖 `docs/latency_report.md` 第二节的同一批 6 个场景（场景 id 见 `SCENARIOS`），
每个场景重复 N 次（默认 3 次）取中位数，每次记录：

* `ttft_s`        —— 首字延迟：从发出请求到收到 **第一个 token** 的墙钟时间。
                     这是 SLO 里最重要的一条（目标 ≤ 3s），流式场景下用户感知的就是它。
                     短路分支（工作流表单、权限拒绝等）不走 token 流，此时 TTFT
                     记为 None，`total_s` 仍然有效 —— 不要把这种情况当成 0s TTFT。
* `total_s`       —— 端到端总耗时：请求发出 → SSE `done` 事件。
* `stages`        —— 分阶段拆解，来自 workflow 自己的 trace 时间戳
                     （`_emit_trace` 的 node_start / node_end 配对），
                     节点为 session / intent / retrieve / tool_subgraph /
                     workflow / clarify / generate / memory_manage / archive。
* `answer_chars`  —— 回答字数，用来解释"生成阶段耗时 ∝ 输出长度"。

**冷启动 vs 热启动**：每个场景的第 1 次（run_index=0）单独标为 `cold`，
第 2 次起标为 `warm`。报告里两者分开列 —— 后端启动阶段已经预加载了
reranker/embedding/LLM 权重（`app.py::_preload_retrieval_models` /
`_warm_llms_at_startup`），但"这个场景第一次跑"仍可能有各自的一次性开销
（首次命中某个 collection 的 BM25 索引、首次编译某个输入形状的推理图等），
只报热启动的漂亮数字是不诚实的。

--------------------------------------------------------------------------------
怎么跑
--------------------------------------------------------------------------------
    cd /Users/david/Documents/enterprise-qa-agent
    set -a; source .env; set +a
    RAGENT_DEBUG=true .venv/bin/python scripts/benchmark_latency.py

    # 只复跑其中几个场景
    RAGENT_DEBUG=true .venv/bin/python scripts/benchmark_latency.py \
        --scenarios smalltalk,kb_hit

    # 改重复次数 / 输出目录
    RAGENT_DEBUG=true .venv/bin/python scripts/benchmark_latency.py --repeat 5
    RAGENT_DEBUG=true .venv/bin/python scripts/benchmark_latency.py --out /tmp/x

    # 只看有哪些场景
    .venv/bin/python scripts/benchmark_latency.py --list

结果写到 `scripts/benchmark_results/latency_<时间戳>.json`
（含每次单测的原始值 + 聚合中位数 + 当次的 git commit / 脏文件清单）。

--------------------------------------------------------------------------------
依赖什么
--------------------------------------------------------------------------------
* `.env`（至少要有 `RAGENT_POSTGRES_URL`、`RAGENT_JWT_SECRET`）
* Postgres 在跑，且已经有测试数据（下面这几个账号来自
  `scripts/seed_tenant_kb_demo.py` / `tests/fixtures/golden_test_set_tenant_kb.json`）：
    - `bob_acme`   —— Acme 有限公司普通员工，角色 `acme_hr_wf_test`，
                      只有 1 个知识库 `acme_hr_admin_kb`（22 块，含年假/远程办公政策）
    - `alice_acme` —— Acme 有限公司 org_admin，隐式通配本企业全部 **6** 个知识库，
                      用于"多知识库并行检索"场景
* Ollama 在跑，且已有 `qwen2.5:7b`、`qwen2.5-1.5b-router`、`nomic-embed-text`
* Chroma 本地库 `./data/db/chroma` 里已摄入 acme_* 语料

**不需要另外开一个终端起后端** —— 脚本自己用 `create_app()` + uvicorn 在本进程内
起一个临时端口的真实 HTTP 服务（跑完自动关掉），再用 httpx 打 `/api/v1/chat/stream`。
启动走的是完整 lifespan，`_preload_retrieval_models()` / `_warm_llms_at_startup()`
两个启动预热跟真实后端一模一样，测的就是真实的 SSE 链路。

> ⚠️ **不要改成 `httpx.ASGITransport`**：那个 transport 会 `await` 整个 app 跑完、
> 把 response body 全部收集完才返回（见 httpx `_transports/asgi.py`，`body_parts`），
> SSE 根本不会逐块到达，测出来的 **TTFT 会恒等于总耗时**。必须走真实 socket。

不改 `src/` 下任何代码：trace 事件通过往 `app.active_trace_ws` 里注册一个
只有 `send_json` 的假 WebSocket 来捕获（`broadcast_trace()` 本来就往这里推），
JWT 直接用 `auth.create_access_token()` 现签，不改任何账号的密码。

--------------------------------------------------------------------------------
已知不覆盖
--------------------------------------------------------------------------------
串行单用户测量。**没有**并发/多用户/排队行为，**没有**真实数据量（几个 G 文档）
下的表现 —— 当前每个知识库只有约 20 块测试数据，检索相关的结论只在这个数据量
下成立，见 `CLAUDE.md` §4 P0 第 2 条。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DEFAULT_OUT_DIR = REPO_ROOT / "scripts" / "benchmark_results"


# --------------------------------------------------------------------------
# 场景定义
# --------------------------------------------------------------------------
# 对齐 docs/latency_report.md 第二节的 6 个场景。原报告只记了括号里的简写
# （比如"远程办公几天"），没留完整问法，这里按同一个意图补全成可执行的问句，
# 并在 `legacy_*` 字段里带上原报告的数字，方便直接对照改善幅度。


@dataclass
class Scenario:
    id: str
    label: str
    username: str
    query: str
    note: str
    legacy_total_s: Optional[float] = None
    legacy_ttft_s: Optional[float] = None


SCENARIOS: List[Scenario] = [
    Scenario(
        id="smalltalk",
        label="闲聊",
        username="bob_acme",
        query="你好，你是谁",
        note=(
            "原报告里这是「不检索、纯生成」的一档（73 字 LLM 回答，意图分类最慢）。"
            "2026-08-25 实测已变成：被判成要查知识库 → tool_subgraph → 未命中短路，"
            "返回 56 字固定话术且不调 LLM。**耗时下降主要来自这个行为回归，不是变快了**，"
            "见 CLAUDE.md §4 P1。"
        ),
        legacy_total_s=22.1,
        legacy_ttft_s=20.1,
    ),
    Scenario(
        id="kb_hit",
        label="检索命中",
        username="bob_acme",
        query="远程办公每个月最多几天",
        note="单知识库（acme_hr_admin_kb，22 块）命中",
        legacy_total_s=20.4,
        legacy_ttft_s=16.0,
    ),
    Scenario(
        id="kb_miss",
        label="检索未命中",
        username="bob_acme",
        query="公司的股票期权比例是多少",
        note="知识库里没有这个内容，走未命中路径",
        legacy_total_s=12.9,
        legacy_ttft_s=12.9,
    ),
    Scenario(
        id="workflow_start",
        label="发起工作流",
        username="bob_acme",
        query="我要申请年假",
        note="走 workflow 节点，不产生生成式回答；只发第一条消息（收集表单），不提交实例",
        legacy_total_s=5.1,
        legacy_ttft_s=5.1,
    ),
    Scenario(
        id="long_answer",
        label="长回答",
        username="bob_acme",
        query="请详细介绍一下公司的年假政策和远程办公政策",
        note="故意要长输出，用来看生成阶段随输出长度的变化",
        legacy_total_s=39.0,
        legacy_ttft_s=17.8,
    ),
    Scenario(
        id="multi_kb",
        label="多知识库并行检索",
        username="alice_acme",
        query="远程办公每个月最多几天",
        note="org_admin 隐式通配 Acme 全部 6 个知识库，与 kb_hit 同一个问题，用于单库 vs 6 库对比",
        legacy_total_s=17.9,
        legacy_ttft_s=13.5,
    ),
]

SCENARIOS_BY_ID = {s.id: s for s in SCENARIOS}


# --------------------------------------------------------------------------
# 代码状态记录（这批数字对应哪个代码状态）
# --------------------------------------------------------------------------

def capture_code_state() -> Dict[str, Any]:
    """记录当前 commit + 工作区脏文件。

    被测代码可能带着未提交改动，那样测出的数字将来 **无法只靠 commit hash 复现**，
    必须把脏文件清单一起写进结果里。
    """
    def _git(*args: str, strip: bool = True) -> str:
        try:
            out = subprocess.run(
                ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, timeout=15
            ).stdout
            return out.strip() if strip else out
        except Exception as e:  # pragma: no cover
            return f"<git failed: {e}>"

    # 注意别 strip 整段输出 —— porcelain 每行前两列是状态码，" M path" 这种
    # 未暂存改动的行首就是空格，整段 strip() 会把第一行的路径咬掉一个字符。
    porcelain = _git("status", "--porcelain", strip=False)
    dirty = [
        f"{line[:2].strip() or '??'} {line[3:]}"
        for line in porcelain.splitlines()
        if line.strip()
    ]
    return {
        "commit": _git("rev-parse", "--short", "HEAD"),
        "commit_full": _git("rev-parse", "HEAD"),
        "branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
        "dirty_paths": dirty,
        "is_clean": not dirty,
        "warning": (
            "工作区有未提交改动，这批数字对应的是「含未提交改动的工作区状态」，"
            "无法只靠 commit hash 复现"
        ) if dirty else "工作区干净，可由 commit hash 复现",
    }


def capture_runtime_env() -> Dict[str, Any]:
    return {
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "llm_model": os.getenv("RAGENT_LLM_MODEL") or "<settings.yaml>",
        "intent_model": os.getenv("RAGENT_INTENT_MODEL", "qwen2.5-1.5b-router"),
        "generate_max_tokens": os.getenv("GENERATE_MAX_TOKENS") or "<default>",
        "ragent_debug": os.getenv("RAGENT_DEBUG"),
    }


# --------------------------------------------------------------------------
# 进程内真实 HTTP 服务
# --------------------------------------------------------------------------

def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@contextlib.asynccontextmanager
async def _serve(fastapi_app, port: int):
    """在本进程内起一个真实 uvicorn 服务，yield 出「启动耗时（秒）」。

    必须走真实 socket 才能测 TTFT —— 见模块 docstring 里关于 `httpx.ASGITransport`
    会整体缓冲 SSE 的说明。lifespan 由 uvicorn 正常驱动，所以启动预热
    （`_preload_retrieval_models` / `_warm_llms_at_startup`）跟真实部署一致。
    """
    import uvicorn

    config = uvicorn.Config(
        fastapi_app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"
    )
    server = uvicorn.Server(config)
    t0 = time.perf_counter()
    task = asyncio.create_task(server.serve())
    while not server.started:
        if task.done():  # 启动失败：把真实异常抛出来，别静默卡死
            await task
            raise RuntimeError("uvicorn 未能启动")
        await asyncio.sleep(0.05)
    try:
        yield round(time.perf_counter() - t0, 3)
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=30)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            task.cancel()


# --------------------------------------------------------------------------
# 单次测量
# --------------------------------------------------------------------------


@dataclass
class RunResult:
    scenario_id: str
    run_index: int
    phase: str  # "cold" | "warm"
    ttft_s: Optional[float]
    total_s: float
    answer_chars: int
    token_count: int
    answer_preview: str
    model_id: Optional[str]
    kb_sources: List[str]
    stages: Dict[str, float] = field(default_factory=dict)
    error: Optional[str] = None


class TraceCollector:
    """假 WebSocket：塞进 app.active_trace_ws 就能收到 broadcast_trace 推来的事件。

    `chat_stream` 的 SSE 流里只有 token / done，trace 事件是往 WebSocket 广播的
    （`app.py::broadcast_trace`），所以这里按 duck-typing 冒充一个 ws 客户端。
    """

    def __init__(self) -> None:
        self.events: List[Dict[str, Any]] = []

    async def send_json(self, data: Dict[str, Any]) -> None:
        self.events.append(data)

    def stage_durations(self) -> Dict[str, float]:
        """按 node 把 node_start / node_end 配对成耗时（秒）。"""
        starts: Dict[str, float] = {}
        out: Dict[str, float] = {}
        for evt in self.events:
            if evt.get("type") != "trace":
                continue
            node, step, ts = evt.get("node"), evt.get("step"), evt.get("ts")
            if not node or ts is None:
                continue
            if step == "node_start":
                starts[node] = ts
            elif step == "node_end" and node in starts:
                # 同一节点多次进入（工具子图重试等）就累加
                out[node] = round(out.get(node, 0.0) + (ts - starts.pop(node)), 3)
        return out


async def run_once(
    client,
    app_module,
    scenario: Scenario,
    token: str,
    run_index: int,
) -> RunResult:
    phase = "cold" if run_index == 0 else "warm"
    headers = {"Authorization": f"Bearer {token}"}

    # 先建对话拿到 conversation_id —— trace 是按 conversation_id 广播的，
    # 必须在发起提问之前就把收集器挂上，否则会漏掉最前面的 session/intent 事件。
    resp = await client.post("/api/v1/conversations", json={"title": f"bench-{scenario.id}"}, headers=headers)
    resp.raise_for_status()
    conversation_id = resp.json()["conversation_id"]

    collector = TraceCollector()
    app_module.active_trace_ws.setdefault(conversation_id, []).append(collector)

    ttft: Optional[float] = None
    token_count = 0
    answer_parts: List[str] = []
    model_id: Optional[str] = None
    kb_sources: List[str] = []
    error: Optional[str] = None

    payload = {"query": scenario.query, "conversation_id": conversation_id}
    t0 = time.perf_counter()
    try:
        async with client.stream(
            "POST", "/api/v1/chat/stream", json=payload, headers=headers, timeout=300.0
        ) as r:
            r.raise_for_status()
            async for line in r.aiter_lines():
                if not line.startswith("data: "):
                    continue
                evt = json.loads(line[6:])
                etype = evt.get("type")
                if etype == "token":
                    if ttft is None:
                        ttft = time.perf_counter() - t0
                    token_count += 1
                    answer_parts.append(evt.get("content", ""))
                elif etype == "done":
                    model_id = evt.get("model_id")
                    kb_sources = evt.get("kb_sources") or []
                    break
                elif etype == "error":
                    error = evt.get("error")
                    break
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
    total = time.perf_counter() - t0

    # 让 broadcast_trace 里那些 create_task 出来的收尾事件落地
    await asyncio.sleep(0.05)
    app_module.active_trace_ws.pop(conversation_id, None)

    answer = "".join(answer_parts)
    return RunResult(
        scenario_id=scenario.id,
        run_index=run_index,
        phase=phase,
        ttft_s=round(ttft, 3) if ttft is not None else None,
        total_s=round(total, 3),
        answer_chars=len(answer),
        token_count=token_count,
        answer_preview=answer[:160],
        model_id=model_id,
        kb_sources=[s if isinstance(s, str) else str(s) for s in kb_sources],
        stages=collector.stage_durations(),
        error=error,
    )


# --------------------------------------------------------------------------
# 聚合
# --------------------------------------------------------------------------

def _median(values: List[float]) -> Optional[float]:
    vals = [v for v in values if v is not None]
    return round(statistics.median(vals), 3) if vals else None


def _pct(values: List[float], q: float) -> Optional[float]:
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    if len(vals) == 1:
        return round(vals[0], 3)
    pos = q * (len(vals) - 1)
    lo, hi = int(pos), min(int(pos) + 1, len(vals) - 1)
    return round(vals[lo] + (vals[hi] - vals[lo]) * (pos - lo), 3)


def overall_percentiles(all_runs: List[RunResult]) -> Dict[str, Any]:
    """跨全部场景的 P50/P95。

    ⚠️ 这**不是**真实流量分布 —— 是 6 个人为挑选的场景等权重混合出来的合成分布，
    只能跟同样口径的数字比，不能直接顶替 `docs/latency_report.md` 第一节那份
    303 条真实对话的统计。报告里必须写明这一点。
    """
    ok = [r for r in all_runs if r.error is None]
    warm = [r for r in ok if r.phase == "warm"]
    totals = [r.total_s for r in ok]
    warm_totals = [r.total_s for r in warm]
    ttfts = [r.ttft_s for r in ok if r.ttft_s is not None]
    warm_ttfts = [r.ttft_s for r in warm if r.ttft_s is not None]
    return {
        "caveat": "6 场景等权重合成分布，非真实流量分布；串行单用户，无并发",
        "sample_size": len(ok),
        "total_s": {"p50": _pct(totals, 0.5), "p95": _pct(totals, 0.95), "max": max(totals) if totals else None},
        "total_s_warm_only": {"p50": _pct(warm_totals, 0.5), "p95": _pct(warm_totals, 0.95)},
        "ttft_s": {"p50": _pct(ttfts, 0.5), "p95": _pct(ttfts, 0.95), "max": max(ttfts) if ttfts else None},
        "ttft_s_warm_only": {"p50": _pct(warm_ttfts, 0.5), "p95": _pct(warm_ttfts, 0.95)},
    }


def aggregate(scenario: Scenario, runs: List[RunResult]) -> Dict[str, Any]:
    ok = [r for r in runs if r.error is None]
    cold = [r for r in ok if r.phase == "cold"]
    warm = [r for r in ok if r.phase == "warm"]

    def _stage_median(rs: List[RunResult]) -> Dict[str, float]:
        keys = sorted({k for r in rs for k in r.stages})
        return {k: _median([r.stages.get(k) for r in rs]) for k in keys}

    return {
        "scenario_id": scenario.id,
        "label": scenario.label,
        "username": scenario.username,
        "query": scenario.query,
        "note": scenario.note,
        "runs_total": len(runs),
        "runs_ok": len(ok),
        "errors": [r.error for r in runs if r.error],
        "legacy_2026_08_23": {
            "total_s": scenario.legacy_total_s,
            "ttft_s": scenario.legacy_ttft_s,
        },
        "cold": {
            "ttft_s": _median([r.ttft_s for r in cold]),
            "total_s": _median([r.total_s for r in cold]),
            "stages": _stage_median(cold),
        } if cold else None,
        "warm_median": {
            "ttft_s": _median([r.ttft_s for r in warm]),
            "total_s": _median([r.total_s for r in warm]),
            "stages": _stage_median(warm),
            "answer_chars": _median([float(r.answer_chars) for r in warm]),
        } if warm else None,
        "all_median": {
            "ttft_s": _median([r.ttft_s for r in ok]),
            "total_s": _median([r.total_s for r in ok]),
            "answer_chars": _median([float(r.answer_chars) for r in ok]),
        } if ok else None,
        "raw_runs": [
            {
                "run_index": r.run_index,
                "phase": r.phase,
                "ttft_s": r.ttft_s,
                "total_s": r.total_s,
                "answer_chars": r.answer_chars,
                "token_count": r.token_count,
                "answer_preview": r.answer_preview,
                "model_id": r.model_id,
                "kb_sources": r.kb_sources,
                "stages": r.stages,
                "error": r.error,
            }
            for r in runs
        ],
    }


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

async def amain(args: argparse.Namespace) -> int:
    os.environ.setdefault("RAGENT_DEBUG", "true")

    import httpx
    from src.ragent_backend import app as app_module
    from src.ragent_backend.auth import create_access_token
    from src.ragent_backend.user_store import UserStore

    selected = (
        [SCENARIOS_BY_ID[s] for s in args.scenarios.split(",") if s.strip()]
        if args.scenarios
        else list(SCENARIOS)
    )

    # 1) 拿 token —— 直接签发，不碰任何账号的密码
    store = UserStore()
    users = {u.username: u for u in await store.list_users()}
    tokens: Dict[str, str] = {}
    for sc in selected:
        u = users.get(sc.username)
        if u is None:
            print(f"[FATAL] 数据库里没有账号 {sc.username}，先跑 scripts/seed_tenant_kb_demo.py")
            return 2
        tokens[sc.username] = create_access_token(user_id=u.user_id, username=u.username)
    await store.close()

    code_state = capture_code_state()
    print("=" * 78)
    print(f"代码状态: {code_state['branch']} @ {code_state['commit']}")
    print(f"          {code_state['warning']}")
    for p in code_state["dirty_paths"]:
        print(f"          脏文件: {p}")
    print("=" * 78)

    # 2) 构造 app 并起真实 HTTP 服务（lifespan 里含启动预热）
    print("[Setup] create_app() ...")
    fastapi_app = app_module.create_app()

    port = _free_port()
    started = time.perf_counter()
    async with _serve(fastapi_app, port) as startup_s:
        print(f"[Setup] 后端就绪（含 reranker/embedding/LLM 预热）: {startup_s}s @ port {port}")

        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=300.0) as client:
            results: List[Dict[str, Any]] = []
            all_runs: List[RunResult] = []
            for sc in selected:
                print(f"\n--- [{sc.id}] {sc.label} ({sc.username}) ---")
                print(f"    Q: {sc.query}")
                runs: List[RunResult] = []
                for i in range(args.repeat):
                    r = await run_once(client, app_module, sc, tokens[sc.username], i)
                    runs.append(r)
                    ttft_txt = f"{r.ttft_s}s" if r.ttft_s is not None else "n/a(短路无token流)"
                    stage_txt = " ".join(f"{k}={v}" for k, v in sorted(r.stages.items()))
                    print(
                        f"    #{i} [{r.phase:4}] TTFT={ttft_txt:<22} 总={r.total_s}s "
                        f"字数={r.answer_chars} model={r.model_id} | {stage_txt}"
                        + (f"  ERROR={r.error}" if r.error else "")
                    )
                results.append(aggregate(sc, runs))
                all_runs.extend(runs)
    del started
    percentiles = overall_percentiles(all_runs)

    # 3) 落盘
    out_dir = Path(args.out) if args.out else DEFAULT_OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"latency_{stamp}.json"
    doc = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "script": "scripts/benchmark_latency.py",
        "repeat_per_scenario": args.repeat,
        "backend_startup_s": startup_s,
        "code_state": code_state,
        "runtime_env": capture_runtime_env(),
        "baseline_source": "docs/latency_report.md (2026-08-23)",
        "not_covered": [
            "并发 / 多用户 / 排队行为——全部为串行单用户测量",
            "真实数据量（几个 G 文档）下的表现——每库仅约 20 块测试数据",
            "非本机硬件（Apple M4 16GB + 本地 Ollama）",
            "前端渲染耗时——只测到 SSE 事件，不含浏览器侧",
            "网络往返——ASGI 进程内调用，没有真实 HTTP/TLS 开销",
        ],
        "overall_percentiles": percentiles,
        "scenarios": results,
    }
    out_path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")

    # 4) 汇总表
    print("\n" + "=" * 100)
    print("汇总（每场景中位数；legacy = docs/latency_report.md 2026-08-23）")
    print("-" * 100)
    print(f"{'场景':<12}{'冷TTFT':>10}{'热TTFT':>10}{'冷总耗时':>11}{'热总耗时':>11}"
          f"{'旧TTFT':>9}{'旧总耗时':>10}{'字数':>7}")
    print("-" * 100)
    for r in results:
        cold = r.get("cold") or {}
        warm = r.get("warm_median") or {}
        lg = r["legacy_2026_08_23"]

        def f(v):
            return "-" if v is None else f"{v:.2f}"
        print(
            f"{r['label']:<12}{f(cold.get('ttft_s')):>10}{f(warm.get('ttft_s')):>10}"
            f"{f(cold.get('total_s')):>11}{f(warm.get('total_s')):>11}"
            f"{f(lg['ttft_s']):>9}{f(lg['total_s']):>10}"
            f"{'-' if warm.get('answer_chars') is None else int(warm['answer_chars']):>7}"
        )
    print("-" * 100)
    p = percentiles
    print(f"全场景合成分布（n={p['sample_size']}，{p['caveat']}）：")
    print(f"  总耗时  P50={p['total_s']['p50']}s  P95={p['total_s']['p95']}s  max={p['total_s']['max']}s")
    print(f"  TTFT    P50={p['ttft_s']['p50']}s  P95={p['ttft_s']['p95']}s  max={p['ttft_s']['max']}s"
          f"  （SLO ≤ 3s）")
    print("-" * 100)
    print(f"结果已写入: {out_path}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description="问答耗时基准测试（TTFT / 总耗时 / 分阶段 / 冷热对比）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--scenarios",
        help="只跑这几个场景，逗号分隔；不给则全跑。可选: " + ", ".join(SCENARIOS_BY_ID),
    )
    p.add_argument("--repeat", type=int, default=3, help="每个场景重复次数（默认 3，取中位数）")
    p.add_argument("--out", help=f"结果输出目录（默认 {DEFAULT_OUT_DIR}）")
    p.add_argument("--list", action="store_true", help="列出所有场景后退出")
    args = p.parse_args()

    if args.list:
        for s in SCENARIOS:
            print(f"{s.id:<16} {s.label:<12} user={s.username:<12} q={s.query}")
            print(f"{'':<16} {s.note}")
        return 0

    if args.scenarios:
        unknown = [s for s in args.scenarios.split(",") if s.strip() and s not in SCENARIOS_BY_ID]
        if unknown:
            print(f"未知场景: {unknown}；可选: {list(SCENARIOS_BY_ID)}")
            return 2

    return asyncio.run(amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
