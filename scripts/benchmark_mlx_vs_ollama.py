"""MLX vs Ollama 推理框架小型 benchmark。

目的：验证「换推理框架能带来多少收益」这个假设是否值得投入 2-3 天做完整迁移
（工具调用兼容性、embedding 迁移等），量化对比同一颗 qwen2.5:7b 权重在
Ollama（llama.cpp 后端）和 MLX 之间的首 token 延迟与解码速度。

用法：
    python scripts/benchmark_mlx_vs_ollama.py

前提：
    - `ollama serve` 已启动，且已 `ollama pull qwen2.5:7b`
    - `.venv` 已安装 mlx-lm（本机已确认 0.31.3）
    - HuggingFace 本地缓存已有 mlx-community/Qwen2.5-7B-Instruct-4bit
      （本机已确认存在，未做任何下载）

输出：
    - stdout 打印每个 case 每次重复的原始数据和汇总表
    - scripts/benchmark_results/mlx_vs_ollama_<timestamp>.json 保存原始数据
"""
from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path

OLLAMA_URL = "http://localhost:11434/api/chat"
OLLAMA_MODEL = "qwen2.5:7b"
MLX_MODEL_ID = "mlx-community/Qwen2.5-7B-Instruct-4bit"
REPEATS = 3  # 每个 case 正式计时的重复次数（预热 1 次不计入）

RAG_CONTEXT = (
    "【差旅报销制度 第4条】员工因公出差产生的交通、住宿、餐饮费用，须在出差结束后5个工作日内，"
    "通过财务系统提交电子报销单，并附上原始发票或电子发票凭证。逾期未提交的，需部门负责人签字说明"
    "原因方可受理，超过30天未提交的报销申请系统将自动关闭，不予受理。\n"
    "【差旅报销制度 第5条】报销单需经直属主管审批，金额超过5000元的需额外经财务负责人审批。"
    "住宿标准按城市分级：一线城市每晚不超过600元，二线城市不超过450元，其他城市不超过350元，"
    "超标部分由个人承担，不予报销。\n"
    "【差旅报销制度 第6条】往返机票、高铁票原则上应提前3个工作日预订以获得较优价格，"
    "紧急出差（不足24小时通知）不受此限制，但需在报销单备注中说明紧急事由。\n"
) * 3  # 重复 3 遍模拟约 800-1000 字的真实检索上下文长度

CASES = [
    {
        "id": "short_no_context",
        "desc": "无上下文的简单问答（模拟意图分类/短查询场景）",
        "system": "你是企业内部助手，简洁准确地回答问题。",
        "user": "报销单据丢失了应该怎么办？",
        "max_tokens": 150,
    },
    {
        "id": "rag_with_context",
        "desc": "带检索上下文的问答（模拟真实 generate 节点场景）",
        "system": (
            "你是企业内部知识库问答助手。请仅根据下面提供的【参考资料】回答用户问题，"
            "不要编造资料中没有的内容。\n\n【参考资料】\n" + RAG_CONTEXT
        ),
        "user": "安全合规部的差旅报销需要在出差结束后几天内提交？超过多久系统会自动关闭申请？",
        "max_tokens": 300,
    },
]


def ollama_call(system: str, user: str, max_tokens: int, nonce: str = "") -> dict:
    # nonce 拼在 system 开头：llama.cpp/Ollama 的 prompt cache 按前缀匹配，一旦
    # 开头就不同就会整段重新做 prefill，避免"重复同一个 prompt"造成的虚假 TTFT。
    payload = {
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": nonce + system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "options": {"temperature": 0.0, "num_predict": max_tokens},
    }
    req = urllib.request.Request(
        OLLAMA_URL, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST"
    )
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read())
    wall_s = time.perf_counter() - t0
    prompt_tokens = data.get("prompt_eval_count", 0)
    gen_tokens = data.get("eval_count", 0)
    prompt_eval_s = data.get("prompt_eval_duration", 0) / 1e9
    eval_s = data.get("eval_duration", 0) / 1e9
    return {
        "wall_s": round(wall_s, 3),
        "ttft_s": round(prompt_eval_s, 3),
        "decode_s": round(eval_s, 3),
        "prompt_tokens": prompt_tokens,
        "gen_tokens": gen_tokens,
        "decode_tps": round(gen_tokens / eval_s, 1) if eval_s > 0 else None,
        "answer_preview": data.get("message", {}).get("content", "")[:80],
    }


def mlx_call(model, tokenizer, system: str, user: str, max_tokens: int) -> dict:
    from mlx_lm.generate import stream_generate

    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    prompt = tokenizer.apply_chat_template(messages, add_generation_prompt=True)

    t0 = time.perf_counter()
    first_token_s = None
    last_chunk = None
    for chunk in stream_generate(model, tokenizer, prompt, max_tokens=max_tokens):
        if first_token_s is None:
            first_token_s = time.perf_counter() - t0
        last_chunk = chunk
    wall_s = time.perf_counter() - t0

    return {
        "wall_s": round(wall_s, 3),
        "ttft_s": round(first_token_s, 3) if first_token_s else None,
        "decode_s": round(wall_s - (first_token_s or 0), 3),
        "prompt_tokens": last_chunk.prompt_tokens if last_chunk else None,
        "gen_tokens": last_chunk.generation_tokens if last_chunk else None,
        "decode_tps": round(last_chunk.generation_tps, 1) if last_chunk else None,
        "answer_preview": (last_chunk.text if last_chunk else "")[:0],  # stream_generate 增量文本不含全文，预览略
    }


def summarize(runs: list[dict]) -> dict:
    def avg(key):
        vals = [r[key] for r in runs if r.get(key) is not None]
        return round(sum(vals) / len(vals), 3) if vals else None

    return {
        "n": len(runs),
        "avg_wall_s": avg("wall_s"),
        "avg_ttft_s": avg("ttft_s"),
        "avg_decode_tps": avg("decode_tps"),
        "first_call_wall_s": runs[0]["wall_s"] if runs else None,
    }


def main() -> None:
    print(f"加载 MLX 模型（首次加载含权重反量化，耗时不计入 benchmark）: {MLX_MODEL_ID}")
    from mlx_lm import load as mlx_load

    mlx_model, mlx_tokenizer = mlx_load(MLX_MODEL_ID)

    results = {}
    for case in CASES:
        print(f"\n=== case: {case['id']} — {case['desc']} ===")
        results[case["id"]] = {"ollama": [], "mlx": []}

        print("[Ollama] 预热 1 次（不计时）...")
        ollama_call(case["system"], case["user"], case["max_tokens"], nonce="[warmup] ")
        for i in range(REPEATS):
            # 每次重复都换一个 nonce，强制 miss prompt cache，模拟真实场景下
            # 每次请求的检索上下文/问题都不同，不会命中上一次的 KV cache。
            r = ollama_call(case["system"], case["user"], case["max_tokens"], nonce=f"[run {i} {time.time()}] ")
            print(f"  [Ollama] run {i+1}: {r}")
            results[case["id"]]["ollama"].append(r)

        print("[MLX] 预热 1 次（不计时，含首次推理图编译）...")
        mlx_call(mlx_model, mlx_tokenizer, case["system"], case["user"], case["max_tokens"])
        for i in range(REPEATS):
            r = mlx_call(mlx_model, mlx_tokenizer, case["system"], case["user"], case["max_tokens"])
            print(f"  [MLX] run {i+1}: {r}")
            results[case["id"]]["mlx"].append(r)

        print(f"\n  --- {case['id']} 汇总（{REPEATS} 次均值） ---")
        for engine in ("ollama", "mlx"):
            s = summarize(results[case["id"]][engine])
            print(f"  {engine:8s}: avg_wall={s['avg_wall_s']}s  avg_ttft={s['avg_ttft_s']}s  "
                  f"avg_decode_tps={s['avg_decode_tps']}  first_call={s['first_call_wall_s']}s")

    out_dir = Path(__file__).parent / "benchmark_results"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / f"mlx_vs_ollama_{int(time.time())}.json"
    out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n原始数据已保存到 {out_path}")


if __name__ == "__main__":
    main()
