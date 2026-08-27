"""用 `tests/fixtures/router_eval.jsonl` 冻结 holdout 跑当前 1.5b router 的
真实基线（Phase 3 重训前，`docs/chitchat_intent_design.md` §4.5 ③）。

**这不是训练脚本**——本机没有装 LoRA 训练工具链（`mlx-lm`/`peft`/`torch`
均未安装，且原训练用的是 LoRA+MLX 流程，`docs/optimization_tracking.md`
"换推理框架"那节记过），Phase 3 的实际重训**本次没有执行**，交付报告里
如实标注为未完成。

本脚本做的是训练之前必须先有的那一步：**在冻结 holdout 上跑一遍现有模型，
拿到一个真实的"重训前"基线**——没有这个数字，将来谁也说不清楚重训到底
有没有用、用了多少。跑的是线上同一条链路（`analyze_and_route`，merged 路径，
真实 `qwen2.5-1.5b-router`），不是造一个简化的分类器去测。

用法：
    set -a; source .env; set +a
    RAGENT_DEBUG=true .venv/bin/python scripts/eval_router_against_holdout.py
    # 存原始结果：... --json scripts/router_lora_results/router_eval_<日期>_<模型名>.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langchain_core.messages import AIMessage, HumanMessage  # noqa: E402

from src.ragent_backend import intent as intent_mod  # noqa: E402
from src.ragent_backend.attendance_store import AttendanceStore  # noqa: E402
from src.ragent_backend.workflow_store import WorkflowStore  # noqa: E402
from src.tool_agent.builtin_tools import register_builtin_tools  # noqa: E402
from src.tool_agent.tool_registry import ToolRegistry  # noqa: E402
from src.core.settings import load_settings  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
EVAL_PATH = REPO_ROOT / "tests" / "fixtures" / "router_eval.jsonl"
DEFAULT_MODEL = "qwen2.5-1.5b-router"


def load_eval() -> List[Dict[str, Any]]:
    rows = []
    with EVAL_PATH.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def build_llm(model: str):
    from langchain_openai import ChatOpenAI

    settings = load_settings()
    kwargs: Dict[str, Any] = {
        "model": model, "temperature": settings.llm.temperature, "max_tokens": settings.llm.max_tokens,
    }
    base_url = getattr(settings.llm, "base_url", None)
    api_key = getattr(settings.llm, "api_key", None)
    if settings.llm.provider == "ollama":
        kwargs["base_url"] = f"{(base_url or 'http://localhost:11434').rstrip('/')}/v1"
        kwargs["api_key"] = api_key or "ollama"
    return ChatOpenAI(**kwargs)


async def build_context():
    workflow_store = WorkflowStore()
    registry = ToolRegistry()
    register_builtin_tools(registry, workflow_store=workflow_store, attendance_store=AttendanceStore())
    tools = registry.to_openai_tools()
    templates = await workflow_store.list_templates()
    workflows = [
        {"workflow_type": t.workflow_type, "display_name": t.display_name, "description": t.description}
        for t in templates
    ]
    return tools, workflows


def _context_to_messages(context: List[str]) -> list:
    out = []
    for line in context or []:
        if line.startswith("用户:") or line.startswith("用户："):
            out.append(HumanMessage(content=line.split(":", 1)[-1].split("：", 1)[-1].strip()))
        else:
            out.append(AIMessage(content=line.split(":", 1)[-1].split("：", 1)[-1].strip()))
    return out


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--json", dest="json_out", default="")
    args = parser.parse_args()

    rows = load_eval()
    tools, workflows = await build_context()
    llm = build_llm(args.model)

    results = []
    confusion: Dict[str, Counter] = defaultdict(Counter)
    per_subcat_correct: Dict[str, List[bool]] = defaultdict(list)
    t0 = time.time()
    for i, row in enumerate(rows, 1):
        messages = _context_to_messages(row.get("context") or [])
        start = time.time()
        try:
            _rewritten, _subs, intent = await intent_mod.analyze_and_route(
                query=row["query"], messages=messages, llm=llm,
                available_tools=tools, available_workflows=workflows,
            )
            predicted = intent.intent_type
            error = ""
        except Exception as e:  # noqa: BLE001
            predicted = "ERROR"
            error = str(e)
        elapsed = time.time() - start
        expected = row["intent_type"]
        correct = predicted == expected
        confusion[expected][predicted] += 1
        subcat = row.get("_subcategory") or "-"
        per_subcat_correct[f"{expected}/{subcat}" if subcat != "-" else expected].append(correct)
        results.append({
            "query": row["query"], "expected": expected, "predicted": predicted,
            "correct": correct, "elapsed_s": round(elapsed, 2), "error": error,
        })
        print(f"  [{i}/{len(rows)}] {row['query']!r} expect={expected} got={predicted}"
              f" {'OK' if correct else 'MISS'} ({elapsed:.2f}s)")

    total = len(results)
    n_correct = sum(1 for r in results if r["correct"])
    print(f"\n===== 总体准确率: {n_correct}/{total} ({n_correct / total * 100:.1f}%) "
          f"耗时 {time.time() - t0:.1f}s =====")

    print("\n===== 按期望类型的准确率 =====")
    by_expected: Dict[str, List[bool]] = defaultdict(list)
    for r in results:
        by_expected[r["expected"]].append(r["correct"])
    for expected in sorted(by_expected):
        vals = by_expected[expected]
        n_ok = sum(vals)
        print(f"  {expected:<10} {n_ok}/{len(vals)}  ({n_ok / len(vals) * 100:.1f}%)")

    print("\n===== 混淆矩阵（行=期望, 列=实际预测）=====")
    all_types = sorted({t for c in confusion.values() for t in c} | set(confusion.keys()))
    header = f"{'':<10}" + "".join(f"{t:>10}" for t in all_types)
    print(header)
    for expected in sorted(confusion):
        row_str = f"{expected:<10}"
        for t in all_types:
            row_str += f"{confusion[expected].get(t, 0):>10}"
        print(row_str)

    if args.json_out:
        payload = {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "script": "scripts/eval_router_against_holdout.py",
            "model": args.model,
            "eval_path": str(EVAL_PATH.relative_to(REPO_ROOT)),
            "accuracy": n_correct / total,
            "results": results,
        }
        out_path = Path(args.json_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n[JSON] 写入 {out_path}")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
