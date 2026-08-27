"""验证 `convert_router_lora_to_mlx_chat.py::build_prompt` 是否与
`intent.py::analyze_and_route` 真实发给 Ollama 的 prompt **逐字节相同**。

做法：monkeypatch `httpx.AsyncClient.send` 拦截真正发往 `/v1/chat/completions`
的请求体，跑一遍真实 `analyze_and_route()`（对一条不会被 Step 0/1.5 规则短路
拦截的 query，否则根本不会构造 prompt/不会调 LLM），把拦到的 `messages[0].content`
跟 `build_prompt(...)` 本地构造出来的字符串做 `==` 比较。

这不是"看起来抄对了"，是真的把两边的字节拿来 diff 过。

用法（需要本机 Ollama 在跑、Postgres 在跑）：
    set -a; source /Users/david/Documents/enterprise-qa-agent/.env; set +a
    .venv/bin/python scripts/verify_router_prompt_replication.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from src.ragent_backend import intent as intent_mod  # noqa: E402
from src.core.settings import load_settings  # noqa: E402

from convert_router_lora_to_mlx_chat import (  # noqa: E402
    build_prompt,
    build_tools_and_workflows,
)

CAPTURED: dict = {}
_ORIG_SEND = httpx.AsyncClient.send


async def _patched_send(self, request, *args, **kwargs):
    url = str(request.url)
    if "chat/completions" in url:
        try:
            CAPTURED["body"] = json.loads(request.content)
        except Exception as e:  # noqa: BLE001
            CAPTURED["err"] = str(e)
    return await _ORIG_SEND(self, request, *args, **kwargs)


async def main() -> int:
    httpx.AsyncClient.send = _patched_send

    from langchain_openai import ChatOpenAI

    settings = load_settings()
    kwargs = {
        "model": "qwen2.5-1.5b-router",
        "temperature": settings.llm.temperature,
        "max_tokens": settings.llm.max_tokens,
    }
    base_url = getattr(settings.llm, "base_url", None)
    kwargs["base_url"] = f"{(base_url or 'http://localhost:11434').rstrip('/')}/v1"
    kwargs["api_key"] = getattr(settings.llm, "api_key", None) or "ollama"
    llm = ChatOpenAI(**kwargs)

    tools, workflows = await build_tools_and_workflows()

    # 用一条真实会撞进 LLM 的查询（不在闲聊白名单 / 不是动作型工作流前缀），
    # 否则 analyze_and_route 会在 Step 0/1.5 直接短路返回，永远不会真的调用
    # llm.with_structured_output(...)，拦不到任何请求体。
    test_query = "报销流程需要哪些审批人？"
    ok_no_shortcut = intent_mod._match_chitchat_intent(test_query) is None
    print(f"[check] 测试 query 未被白名单短路: {ok_no_shortcut}")
    if not ok_no_shortcut:
        print("[FAIL] 测试 query 被规则短路拦截了，换一条")
        return 1

    result = await intent_mod.analyze_and_route(
        query=test_query, messages=[], llm=llm,
        available_tools=tools, available_workflows=workflows,
    )
    print(f"[info] analyze_and_route 返回: {result}")

    if "body" not in CAPTURED:
        print("[FAIL] 没有拦到任何 /v1/chat/completions 请求——是不是又被规则短路了？")
        return 1

    real_prompt = CAPTURED["body"]["messages"][0]["content"]

    cleaned_query = " ".join(test_query.split())
    local_prompt = build_prompt(cleaned_query, "", tools, workflows)

    if real_prompt == local_prompt:
        print(f"[OK] 本地复现的 prompt 与真实发给 Ollama 的请求体逐字节相同（{len(real_prompt)} 字符）")
        return 0

    print("[FAIL] prompt 不一致，逐行 diff：")
    import difflib
    real_lines = real_prompt.splitlines()
    local_lines = local_prompt.splitlines()
    for line in difflib.unified_diff(real_lines, local_lines, "real(生产实测)", "local(本脚本复现)", lineterm=""):
        print(line)
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
