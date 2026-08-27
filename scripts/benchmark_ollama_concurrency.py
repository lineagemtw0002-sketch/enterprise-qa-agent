#!/usr/bin/env python
"""P0 第 3 条实测：模型服务并发形态 —— CLAUDE.md §4 第 3 条一直标着"约 10x 缺口"，
但那是从旧 P50 24.2s 反推的估算，`OLLAMA_NUM_PARALLEL` 默认 1 之后**从未被真实
测过并发**（既有的 `scripts/benchmark_latency.py` 是串行单用户基准）。

本脚本直接打 Ollama 的 `/api/generate`，绕开整个应用层（intent/retrieve/generate
链路），只测"模型服务本身在给定并发请求数下延迟怎么退化、聚合吞吐怎么变"，避免
把应用层其它耗时（BM25/Chroma/rerank）混进"模型服务并发"这一个变量里。

**不会自己重启 Ollama 服务**——本机 Ollama 是 `brew services` 托管的 launchd
后台服务，脚本只负责打流量和记录结果，重启/改 `OLLAMA_NUM_PARALLEL` 由运行者
手动做（见下面用法），避免脚本悄悄改动一个不属于本仓库、其它进程可能也在用的
共享本地服务。

用法：
    # 1) 保持当前默认（OLLAMA_NUM_PARALLEL=1，未设置时 Ollama 的默认值）跑一遍：
    .venv/bin/python scripts/benchmark_ollama_concurrency.py --tag num_parallel_1

    # 2) 停掉 brew 服务，手动用别的并发度启动，再跑一遍：
    brew services stop ollama
    OLLAMA_NUM_PARALLEL=4 ollama serve &  # 记下这个 shell 的 PID
    .venv/bin/python scripts/benchmark_ollama_concurrency.py --tag num_parallel_4 --num-parallel-label 4
    kill %1  # 杀掉手动起的 serve
    brew services start ollama  # 恢复原状

    # 3) 对比两份 scripts/benchmark_results/ollama_concurrency_*.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = REPO_ROOT / "scripts" / "benchmark_results"

OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL = "qwen2.5:7b"  # 与 config/settings.yaml 的 llm.model 一致（generate 节点用的模型）

# 固定 num_predict，保证不同并发度下每个请求要生成的 token 数一样多——
# 否则"吞吐变化"里会混进"这次模型自己决定答得短了"这个噪声变量。
NUM_PREDICT = 200

PROMPT = (
    "请详细说明企业年假管理制度的核心要点，包括年假天数计算方式、"
    "申请流程、审批权限、以及未休完年假的处理办法。请分点说明，"
    "每点至少两句话。"
)

CONCURRENCY_LEVELS = [1, 2, 4, 6]
REPEATS_PER_LEVEL = 2


def capture_code_state() -> Dict[str, Any]:
    def _git(*args: str, strip: bool = True) -> str:
        try:
            out = subprocess.run(
                ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, timeout=15
            ).stdout
            return out.strip() if strip else out
        except Exception as e:  # pragma: no cover
            return f"<git failed: {e}>"

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
    }


async def _one_request(client: httpx.AsyncClient) -> Dict[str, Any]:
    t0 = time.monotonic()
    resp = await client.post(
        OLLAMA_URL,
        json={
            "model": MODEL,
            "prompt": PROMPT,
            "stream": False,
            "options": {"num_predict": NUM_PREDICT},
        },
        timeout=180.0,
    )
    elapsed = time.monotonic() - t0
    resp.raise_for_status()
    body = resp.json()
    return {
        "elapsed_s": elapsed,
        "eval_count": body.get("eval_count"),  # 实际生成的 token 数
        "eval_duration_ns": body.get("eval_duration"),  # 纯生成阶段耗时（不含排队/prompt eval）
        "prompt_eval_count": body.get("prompt_eval_count"),
        "total_duration_ns": body.get("total_duration"),
    }


async def _run_level(concurrency: int) -> Dict[str, Any]:
    async with httpx.AsyncClient() as client:
        t0 = time.monotonic()
        results = await asyncio.gather(
            *[_one_request(client) for _ in range(concurrency)],
            return_exceptions=True,
        )
        wall_s = time.monotonic() - t0

    errors = [r for r in results if isinstance(r, Exception)]
    oks = [r for r in results if not isinstance(r, Exception)]

    per_request_latencies = [r["elapsed_s"] for r in oks]
    total_eval_tokens = sum(r["eval_count"] or 0 for r in oks)

    return {
        "concurrency": concurrency,
        "wall_s": wall_s,
        "errors": len(errors),
        "error_samples": [str(e) for e in errors[:3]],
        "ok_count": len(oks),
        "per_request_latency_s": {
            "min": min(per_request_latencies) if per_request_latencies else None,
            "median": statistics.median(per_request_latencies) if per_request_latencies else None,
            "max": max(per_request_latencies) if per_request_latencies else None,
        },
        "aggregate_tokens_per_s": (total_eval_tokens / wall_s) if wall_s > 0 else None,
        "total_eval_tokens": total_eval_tokens,
    }


async def _warm_up() -> None:
    """跑一次单请求把模型加载进内存，不计入正式测量（避免把冷启动模型加载
    时间混进"并发退化"的第一个数据点里）。"""
    async with httpx.AsyncClient() as client:
        await _one_request(client)


async def run_benchmark(num_parallel_label: str) -> Dict[str, Any]:
    print(f"预热模型 {MODEL} ...")
    await _warm_up()

    levels_result: List[Dict[str, Any]] = []
    for level in CONCURRENCY_LEVELS:
        runs = []
        for rep in range(REPEATS_PER_LEVEL):
            print(f"  concurrency={level} run {rep + 1}/{REPEATS_PER_LEVEL} ...")
            runs.append(await _run_level(level))
        # 取吞吐中位数那一轮作为代表（跟 benchmark_latency.py 的"每场景取中位数"一致）
        runs_sorted = sorted(runs, key=lambda r: r["aggregate_tokens_per_s"] or 0)
        representative = runs_sorted[len(runs_sorted) // 2]
        levels_result.append({
            "concurrency": level,
            "runs": runs,
            "representative": representative,
        })
        print(
            f"    -> wall={representative['wall_s']:.2f}s "
            f"aggregate_tokens_per_s={representative['aggregate_tokens_per_s']:.1f} "
            f"errors={representative['errors']}"
        )

    baseline_tps = levels_result[0]["representative"]["aggregate_tokens_per_s"]
    scaling = []
    for lvl in levels_result:
        tps = lvl["representative"]["aggregate_tokens_per_s"]
        scaling.append({
            "concurrency": lvl["concurrency"],
            "aggregate_tokens_per_s": tps,
            "speedup_vs_concurrency_1": (tps / baseline_tps) if baseline_tps else None,
            "ideal_speedup": lvl["concurrency"],
        })

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "script": "scripts/benchmark_ollama_concurrency.py",
        "model": MODEL,
        "num_predict": NUM_PREDICT,
        "num_parallel_label": num_parallel_label,
        "code_state": capture_code_state(),
        "concurrency_levels": levels_result,
        "scaling_summary": scaling,
        "not_covered": [
            "只测了单个模型（qwen2.5:7b，generate 节点）的并发形态，"
            "1.5b router 模型（_intent_node 用）未测，它的调用频率其实更高",
            "只在本机（单机 Ollama，Apple Silicon）测过，未覆盖目标部署硬件",
            "prompt 长度固定，未测长 prompt（如命中检索后拼接大段上下文）下的并发形态",
            "num_predict 固定为 200，真实生成长度分布未覆盖（CLAUDE.md 记录的长回答场景是 354 字）",
            "没有跑应用全链路（intent+retrieve+generate）的并发，只测了模型服务这一层",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True, help="结果文件名标签，如 num_parallel_1")
    parser.add_argument(
        "--num-parallel-label", default="unknown",
        help="运行者手动设置的 OLLAMA_NUM_PARALLEL 值（脚本不会自己探测），用于记录在结果里",
    )
    args = parser.parse_args()

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    result = asyncio.run(run_benchmark(args.num_parallel_label))

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = RESULTS_DIR / f"ollama_concurrency_{args.tag}_{ts}.json"
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已写入 {out_path}")


if __name__ == "__main__":
    main()
