"""把 `router_lora_data/train_batch1.jsonl`（结构化任务标签：query/rewritten_query/
sub_queries/intent_type/target_tool/workflow_type/need_clarify/confidence/reasoning）
转换成 `mlx_lm.lora` 能直接训练的 chat 格式（`{"messages": [...]}`，见
https://github.com/ml-explore/mlx-lm/blob/main/mlx_lm/LORA.md#Data 与
`mlx_lm/tuner/datasets.py::ChatDataset`）。

# 为什么不能直接拿 query 当 prompt、label 当 completion

真实推理时（`src/ragent_backend/intent.py::analyze_and_route`），发给
`qwen2.5-1.5b-router` 的不是用户的原始问题，而是 LangChain
`llm.with_structured_output(QueryAnalysisAndIntentResult, method="json_mode")`
包出来的一大段 prompt——里面塞了完整的分类规则（`_INTENT_CLASSIFY_RULES`）、
子查询拆分规则（`_SUB_QUERY_SPLIT_RULES`）、当前企业真实的工具列表和流程模板
列表、最近 4 轮对话历史、以及"请直接输出 JSON 对象..."这句收尾指令。如果训练
数据只喂"query -> 标签 JSON"这种裸配对，模型学到的输入分布和真实推理时完全
不同，训练出来的 LoRA 在生产环境里等于没训练过。

本脚本的核心工作就是**逐字节复现这段 prompt**，具体分两部分：

1. **规则/格式化文本直接复用 `intent.py` 里的实现**（`_INTENT_CLASSIFY_RULES` /
   `_SUB_QUERY_SPLIT_RULES` / `_format_tools_text` / `_format_workflows_text`），
   不是照抄一份新常量——这样 `intent.py` 那边改了分类规则，下次重新跑本脚本
   就自动跟上，不会出现"训练用的规则文案"和"线上用的规则文案"两份互相漂移。
2. **外层 f-string 骨架是从 `intent.py::analyze_and_route` 手工复制的**
   （Python 没有把一段内嵌 f-string 单独抽出来重用的轻量写法，抽函数是应用
   代码改动，这次任务明确说了不做这类改动）。为了确保复制没有打错一个字，
   本文件用 `scripts/verify_router_prompt_replication.py` 做过一次**运行时
   实测比对**：monkeypatch httpx 拦截真实发给 Ollama 的请求体，跟本脚本对
   同一条 query + 同一份真实 tools/workflows 生成的 prompt 做逐字节 diff，
   完全一致才能通过——不是"看起来抄对了"，是真的比对过字节。

# 训练目标 completion 的字段

`QueryAnalysisAndIntentResult`（`intent.py:87`）的全部字段，顺序按 prompt
末尾那句"请直接输出 JSON 对象（rewritten_query/sub_queries/intent_type/
confidence/target_tool/tool_args_preview/workflow_type/need_clarify/
clarify_prompt/reasoning）"里写的顺序排列——训练数据没有的两个字段
（`tool_args_preview`/`clarify_prompt`）留 null，这两个字段在生产 schema
里本来就是 Optional，训练数据里从未标注过它们不代表标错，只是这批样本没有
覆盖需要它们的场景。

# 为什么用 `--mask-prompt`

`mlx_lm.tuner.datasets.ChatDataset` 默认 `mask_prompt=False`，会把 prompt
本身也算进 loss——对一个几百字的固定分类规则文本重复计算 loss 没有意义，
还会稀释真正要学的"给定这个 prompt，输出正确 JSON"这个信号。训练命令里
必须带 `--mask-prompt`，本脚本只负责产出数据、不负责传这个 flag（写在
`scripts/train_router_lora.sh` 里）。

用法：
    set -a; source /Users/david/Documents/enterprise-qa-agent/.env; set +a
    .venv/bin/python scripts/convert_router_lora_to_mlx_chat.py \
        --train-frac 0.85 --seed 42 \
        --out-dir router_lora_data/mlx_chat_v1
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langchain_core.messages import AIMessage, HumanMessage  # noqa: E402

from src.ragent_backend import intent as intent_mod  # noqa: E402
from src.ragent_backend.attendance_store import AttendanceStore  # noqa: E402
from src.ragent_backend.workflow_store import WorkflowStore  # noqa: E402
from src.tool_agent.builtin_tools import register_builtin_tools  # noqa: E402
from src.tool_agent.tool_registry import ToolRegistry  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
TRAIN_SRC = REPO_ROOT / "router_lora_data" / "train_batch1.jsonl"


def context_to_messages(context: List[str]) -> list:
    """跟 `scripts/eval_router_against_holdout.py::_context_to_messages` 逐字节
    相同——训练数据的 "context" 字段（"用户: .../助手: ..."）和评估脚本读同一种
    格式，这里必须用同一套解析逻辑，否则训练和评估看到的历史文本会长得不一样。
    """
    out = []
    for line in context or []:
        if line.startswith("用户:") or line.startswith("用户："):
            out.append(HumanMessage(content=line.split(":", 1)[-1].split("：", 1)[-1].strip()))
        else:
            out.append(AIMessage(content=line.split(":", 1)[-1].split("：", 1)[-1].strip()))
    return out


def build_history_text(messages: list) -> str:
    """逐字节复制 `intent.py::analyze_and_route` 里构造 history_text 的那段
    （intent.py:983-990）。"""
    recent = messages[-4:] if messages else []
    history_lines = []
    for m in recent:
        role = "User" if m.type == "human" else "Assistant"
        content = str(getattr(m, "content", "")).strip()
        if content:
            history_lines.append(f"{role}: {content}")
    return "\n".join(history_lines)


def build_prompt(
    cleaned_query: str,
    history_text: str,
    available_tools: List[Dict[str, Any]],
    available_workflows: List[Dict[str, Any]],
) -> str:
    """逐字节复制 `intent.py::analyze_and_route` 里的 prompt f-string
    （intent.py:992-1028）。规则文本/格式化函数直接从 `intent_mod` 导入，
    只有外层骨架是手工复制的——见模块 docstring "为什么不能直接..." 那段
    对这个取舍的说明，以及 `scripts/verify_router_prompt_replication.py`
    对复制正确性的运行时实测。"""
    return f"""你是一个查询分析与意图分类助手。请依次完成两件事：

第一步 —— 查询重写与拆分：
1. 消除所有代词和指代（如"它"、"这个"、"that"、"这个文档"、"上面说的"、"前者"等），替换为对话历史中提到的具体实体。
2. 如果当前问题包含多个**彼此独立**的并列主题（如多个城市、多个产品、多个时间段的比较），即使没有连词也必须拆分成可独立执行的子查询列表；只有单一主题时 sub_queries 只放一个元素。
3. 每个子查询必须完整、无歧义、不依赖上下文即可理解。

{intent_mod._SUB_QUERY_SPLIT_RULES}

拆分示例：
- "北京上海杭州的天气怎么样" -> 拆成 3 个（三个城市互不影响，独立）
- "销售额最高的部门是哪个，该部门今年的招聘预算是多少" -> **不拆**（不先知道是哪个部门就查不了"该部门"的预算，第二问依赖第一问）
- "公司的年假制度是什么，它的审批人是谁" -> **不拆**（"它"回指前一问的答案）

第二步 —— 基于第一步重写后的查询，判断用户的真实意图：
{intent_mod._INTENT_CLASSIFY_RULES}

"clarify" 判断要从严：只有当问题本身残缺到没法执行检索或工具调用时才选
"clarify"（比如只有代词没有主语、或者短到不知道在问什么）。像"XX怎么办""XX
流程是什么"这类问题，即使没写清楚是哪个部门/哪家公司，也是可以直接拿去知识库
检索、期待检索结果里包含相关制度说明的完整问题，应该判成 "rag" 或 "tool"，
不要因为细节不够具体就选 "clarify"——检索没查到相关内容会有专门的空结果提示，
不需要在分类这一步就替用户猜"你是不是想问不清楚"。

可用工具列表：
{intent_mod._format_tools_text(available_tools)}

可用流程模板列表：
{intent_mod._format_workflows_text(available_workflows)}

对话历史：
{history_text}

当前问题：{cleaned_query}

请直接输出 JSON 对象（rewritten_query/sub_queries/intent_type/confidence/target_tool/tool_args_preview/workflow_type/need_clarify/clarify_prompt/reasoning），不要添加任何解释或 Markdown 格式。
注意：target_tool 必须从可用工具列表中选择，workflow_type 必须从可用流程模板列表中选择，intent_type 的判断要基于你自己重写后的查询，都不能编造不存在的名字。"""


def build_completion(row: Dict[str, Any]) -> str:
    """按 prompt 里写的字段顺序拼 JSON completion。训练数据没有的两个字段
    （tool_args_preview/clarify_prompt）留 null——两者在 schema 里本来就是
    Optional，见模块 docstring。"""
    payload = {
        "rewritten_query": row["rewritten_query"],
        "sub_queries": row["sub_queries"],
        "intent_type": row["intent_type"],
        "confidence": row["confidence"],
        "target_tool": row.get("target_tool"),
        "tool_args_preview": None,
        "workflow_type": row.get("workflow_type"),
        "need_clarify": row["need_clarify"],
        "clarify_prompt": None,
        "reasoning": row["reasoning"],
    }
    return json.dumps(payload, ensure_ascii=False)


async def build_tools_and_workflows():
    """跟 `scripts/eval_router_against_holdout.py::build_context` 完全相同
    ——训练和评估必须看到同一份真实工具/流程列表，否则"可用工具列表"这段
    文本在训练和评估时不一致，等于又在制造一次分布偏移。"""
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


def load_rows(path: Path) -> List[Dict[str, Any]]:
    rows = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--src", default=str(TRAIN_SRC))
    parser.add_argument("--out-dir", default=str(REPO_ROOT / "router_lora_data" / "mlx_chat_v1"))
    parser.add_argument("--train-frac", type=float, default=0.85,
                         help="按此比例切 train/valid（不是 test，mlx_lm.lora 未传 --test 就不用 test.jsonl）。"
                              "320 条 * 0.85 ≈ 272 train / 48 valid。")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    rows = load_rows(Path(args.src))
    tools, workflows = await build_tools_and_workflows()

    examples = []
    for row in rows:
        messages_ctx = context_to_messages(row.get("context") or [])
        history_text = build_history_text(messages_ctx)
        cleaned_query = " ".join(row["query"].split())
        prompt = build_prompt(cleaned_query, history_text, tools, workflows)
        completion = build_completion(row)
        examples.append({
            "messages": [
                {"role": "user", "content": prompt},
                {"role": "assistant", "content": completion},
            ]
        })

    rng = random.Random(args.seed)
    indices = list(range(len(examples)))
    rng.shuffle(indices)
    n_train = round(len(examples) * args.train_frac)
    train_idx = set(indices[:n_train])

    train_examples = [examples[i] for i in range(len(examples)) if i in train_idx]
    valid_examples = [examples[i] for i in range(len(examples)) if i not in train_idx]

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "train.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in train_examples) + "\n", encoding="utf-8"
    )
    (out_dir / "valid.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in valid_examples) + "\n", encoding="utf-8"
    )
    print(f"[OK] {len(train_examples)} train / {len(valid_examples)} valid -> {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
