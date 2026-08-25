"""验证：闲聊/非知识库问题被意图分类误路由进企业知识库检索（功能回归）。

背景
----
2026-08-25 的性能实测（scripts/benchmark_results/latency_20260825_154548.json，
scenario_id=smalltalk）里，bob_acme 问「你好，你是谁」，系统回了一句：

    抱歉，在您当前可访问的知识库范围内，未检索到相关内容。
    请确认关键词是否正确，或联系管理员确认您的知识库访问权限。

对照 2026-08-23 优化前，同一个问题返回的是 73 字的正常自我介绍。这不是性能
问题，是正确性回归：系统现在会告诉用户「你的知识库里没有『我是谁』这个信息」。

链路（都在主图里，不需要起服务就能推出终态）：
  _intent_node → analyze_and_route()（intent.py:589）判成 intent_type="tool"
  且 target_tool="query_knowledge_hub"
  → _route_after_intent（workflow.py:335-339）路由到 tool_subgraph
  → 知识库当然查不到「你是谁」
  → _generate_node 的空命中短路（workflow.py:1294-1314）：只要
    state["target_tool"] == "query_knowledge_hub" 且这一轮没有任何 collections
    命中，就直接返回上面那句固定话术，完全不调用 LLM
    （benchmark 里 used_model="n/a (empty kb hit, no LLM call)"、generate 阶段
     0.0s，就是这条分支）

本脚本做什么
------------
1. 【任务 A】把一批闲聊问题喂给真正的 analyze_and_route()，同时抓取
   _reconcile_intent_result 之前的**原始 LLM 结构化输出**，用来区分误判到底
   出在哪一层：
     - 规则短路（_needs_clarify_rule / _match_workflow_action_intent）
     - LLM 本身就判成了 tool
     - _reconcile_intent_result 的后处理把 rag 改写成了 tool
   脚本会把三者分别标出来（rule_hit / llm_said / reconciled 列）。
2. 【任务 B】跑一批分层的闲聊 + 对照用例，每条重复 N 次，统计误判率。
3. 【任务 C】用 --models 一次跑多个意图模型（1.5b 微调 router vs 7b 通用模型），
   同一批用例横向对比，判断这个回归是不是「换 1.5b router 模型」引入的。
4. 【任务 C 补充】用 --path two-call 切到**旧的两次调用路径**
   （analyze_query() + detect_intent()，intent.py:116/347）。2026-08-23 那份
   基线跑的就是这条路径 —— 合并版 analyze_and_route() 当时「长期不接线」
   （见 workflow.py:616-625 的说明）。要复现「优化前」的真实行为，必须同时
   切模型**和**切路径，只切模型是不够的。

判据（终态怎么算对）
--------------------
脚本不真的去查知识库，而是**静态推导终态**——闲聊问题在任何租户库里都必然
零命中，所以 intent 结果就唯一决定了用户看到什么：

  outcome=answered  → generate 会真的调 LLM 生成回答（intent=rag；或 intent=tool
                      但 target_tool 不是 query_knowledge_hub —— 空命中短路的
                      闸门只认 target_tool 这一个字段，见 workflow.py:1294）
  outcome=kb_refusal→ intent=tool 且 target_tool=query_knowledge_hub，撞上空命中
                      短路，用户看到那句固定拒绝话术，LLM 从不被调用  ← 本次回归
  outcome=clarify   → need_clarify=True，被 clarify 节点用固定澄清话术短路，
                      同样不调 LLM（对闲聊来说也是错的，只是错法不同）
  outcome=workflow  → 误发起业务流程

  闲聊类用例（expect="chat"）：只有 answered 算通过，kb_refusal / clarify /
    workflow 都算误判（misroute）。
  对照组（expect="kb"）：只有 outcome=kb_refusal 那条路径（intent=tool +
    target_tool=query_knowledge_hub）算通过——对照组本来就该查知识库，真实
    知识库里有对应内容时它命中的是正常回答，不是拒绝话术。
  考勤对照组（expect="tool"）：intent=tool 即算通过，不限定具体工具。
  流程对照组（expect="workflow"）：intent=workflow 即算通过，不限定具体模板
    （模板选得对不对是另一个问题，这里只关心"没被别的分支截胡"）。

怎么跑
------
    set -a; source .env; set +a
    RAGENT_DEBUG=true .venv/bin/python scripts/verify_smalltalk_routing.py

    # 每条跑 3 次（LLM 即使 temperature=0 也可能不稳）
    ... scripts/verify_smalltalk_routing.py --repeat 3

    # 任务 C：1.5b 微调 router vs 7b 通用模型横向对比（都跑当前的合并调用路径）
    ... scripts/verify_smalltalk_routing.py --models qwen2.5-1.5b-router,qwen2.5:7b

    # 任务 C 补充：复现 2026-08-23「优化前」的真实配置（7b + 旧的两次调用路径）
    ... scripts/verify_smalltalk_routing.py --models qwen2.5:7b --path two-call

    # 存原始结果供二次分析
    ... scripts/verify_smalltalk_routing.py --json /tmp/smalltalk.json

依赖：本机 Ollama 起着、对应模型已 `ollama pull`/`ollama create`；Postgres 起着
（只用来读 workflow 模板列表，让传给 LLM 的「可用流程模板」跟线上完全一致）。
本脚本只读不写，不改任何 src/ 代码，不碰知识库数据。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.settings import load_settings  # noqa: E402
from src.ragent_backend import intent as intent_mod  # noqa: E402
from src.ragent_backend.attendance_store import AttendanceStore  # noqa: E402
from src.ragent_backend.workflow_store import WorkflowStore  # noqa: E402
from src.tool_agent.builtin_tools import register_builtin_tools  # noqa: E402
from src.tool_agent.tool_registry import ToolRegistry  # noqa: E402

DEFAULT_MODEL = "qwen2.5-1.5b-router"
KB_TOOL = "query_knowledge_hub"


# ============== 用例集（分层） ==============
# expect: "chat"     = 应该直接对话回答（不该查知识库、不该澄清、不该发起流程）
#         "kb"       = 应该查企业知识库（intent=tool + target_tool=query_knowledge_hub）
#         "tool"     = 应该调某个工具，不限定是哪个（考勤类）
#         "workflow" = 应该发起业务流程，不限定具体模板
@dataclass(frozen=True)
class Case:
    category: str
    query: str
    expect: str


CASES: List[Case] = [
    # 1. 纯打招呼
    Case("1_greeting", "你好", "chat"),
    Case("1_greeting", "早上好", "chat"),
    Case("1_greeting", "在吗", "chat"),
    Case("1_greeting", "hello", "chat"),
    Case("1_greeting", "嗨，在忙吗", "chat"),
    # 2. 问身份/能力
    Case("2_identity", "你好，你是谁", "chat"),          # ← benchmark 里复现的那条
    Case("2_identity", "你是谁", "chat"),
    Case("2_identity", "你能做什么", "chat"),
    Case("2_identity", "你会什么", "chat"),
    Case("2_identity", "介绍一下你自己", "chat"),
    # 3. 礼貌用语
    Case("3_courtesy", "谢谢", "chat"),
    Case("3_courtesy", "好的", "chat"),
    Case("3_courtesy", "辛苦了", "chat"),
    Case("3_courtesy", "再见", "chat"),
    Case("3_courtesy", "非常感谢你的帮助", "chat"),
    # 4. 元问题（问系统自身怎么工作的）
    Case("4_meta", "你用的是什么模型", "chat"),
    Case("4_meta", "你是怎么工作的", "chat"),
    Case("4_meta", "你的回答准确吗", "chat"),
    # 5. 闲聊夹杂业务词
    Case("5_mixed", "你好，我想问个问题", "chat"),
    Case("5_mixed", "你能帮我查东西吗", "chat"),
    Case("5_mixed", "在吗，有点事想请教你", "chat"),
    # 6. 对照组：本来就该查知识库 / 该调工具 / 该发起流程
    #    2026-08-25 扩充：原来只有 4 条（3 kb + 1 考勤），报告 §6.5 明确说
    #    "不足以证明修复不会伤业务问答"。给闲聊路由做任何规则短路都必须先
    #    证明这一点，所以这里补到 18 条，并新增两类：
    #      - "闲聊夹真业务"（"你好，年假多少天"）：专门用来抓"闲聊白名单
    #        把带寒暄前缀的正经业务问题也吞掉"这种误伤，是白名单类修法最
    #        危险的失败模式；
    #      - workflow 对照（"我想请假"）：确认新增的短路没有插到工作流
    #        动作短路前面、把发起流程的句子截胡。
    Case("6_control_kb", "年假多少天", "kb"),
    Case("6_control_kb", "远程办公政策是什么", "kb"),
    Case("6_control_kb", "报销流程是怎样的", "kb"),
    Case("6_control_kb", "员工手册里关于加班是怎么规定的", "kb"),
    Case("6_control_kb", "试用期多久可以转正", "kb"),
    Case("6_control_kb", "差旅住宿标准是多少", "kb"),
    Case("6_control_kb", "公司的绩效考核制度是什么", "kb"),
    Case("6_control_kb", "入职需要准备哪些材料", "kb"),
    Case("6_control_kb", "公积金缴纳比例是多少", "kb"),
    # 带寒暄前缀 / 寒暄措辞的**真**业务问题——闲聊白名单绝不能吞掉这些
    Case("6_control_kb_mixed", "你好，年假多少天", "kb"),
    Case("6_control_kb_mixed", "你能帮我查一下报销流程吗", "kb"),
    Case("6_control_kb_mixed", "谢谢，那远程办公政策呢", "kb"),
    Case("6_control_tool", "我这个月迟到几次", "tool"),
    Case("6_control_tool", "上个月我的考勤记录", "tool"),
    Case("6_control_tool", "我这周的打卡情况怎么样", "tool"),
    Case("6_control_workflow", "我想请假", "workflow"),
    Case("6_control_workflow", "我要报销", "workflow"),
    Case("6_control_workflow", "帮我报修电脑", "workflow"),
]


# ============== 单次运行结果 ==============
@dataclass
class RunResult:
    model: str
    path: str
    category: str
    query: str
    expect: str
    rewritten: str = ""
    intent_type: str = ""
    target_tool: Optional[str] = None
    confidence: float = 0.0
    need_clarify: bool = False
    workflow_type: Optional[str] = None
    reasoning: str = ""
    # 误判归因用
    rule_hit: str = ""        # clarify_rule / workflow_action_rule / ""（没被规则短路）
    llm_intent: str = ""      # 原始 LLM 输出的 intent_type（规则短路时为 "-"）
    llm_target_tool: str = ""
    reconciled: bool = False  # 后处理改写过 intent_type
    outcome: str = ""         # answered / kb_refusal / clarify / workflow
    ok: bool = False
    elapsed_s: float = 0.0
    error: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


def classify_outcome(r: RunResult) -> str:
    """静态推导用户最终看到什么（判据见模块 docstring）。"""
    if r.need_clarify:
        return "clarify"
    if r.intent_type == "workflow":
        return "workflow"
    if r.intent_type == "tool" and r.target_tool == KB_TOOL:
        # 闲聊在任何库里都零命中 -> 必然撞上 workflow.py:1294 的空命中短路
        return "kb_refusal"
    if r.intent_type == "clarify":
        # need_clarify=False 但 intent_type=clarify：自相矛盾分支，
        # _route_after_intent(workflow.py:340-355) 会把它交给 tool_subgraph，
        # 主图 target_tool 为空 -> 不会撞空命中短路 -> generate 正常调 LLM
        return "answered"
    return "answered"


def is_ok(r: RunResult) -> bool:
    if r.expect == "chat":
        return r.outcome == "answered"
    if r.expect == "kb":
        return r.intent_type == "tool" and r.target_tool == KB_TOOL
    if r.expect == "tool":
        return r.intent_type == "tool"
    if r.expect == "workflow":
        return r.intent_type == "workflow"
    return False


# ============== 环境搭建 ==============
async def build_context() -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """构造跟线上 _intent_node 完全一致的 available_tools / available_workflows。

    工具列表按 app.py:364-378 的方式从 ToolRegistry 生成（内置工具部分；
    MCP 外部工具 simple.* 需要外部进程，本脚本不拉起，见报告「未覆盖范围」）。
    流程模板从真实 WorkflowStore 读（app.py 走的也是 list_templates()）。
    """
    workflow_store = WorkflowStore()
    registry = ToolRegistry()
    register_builtin_tools(
        registry,
        workflow_store=workflow_store,
        attendance_store=AttendanceStore(),
    )
    tools = registry.to_openai_tools()
    templates = await workflow_store.list_templates()
    workflows = [
        {
            "workflow_type": t.workflow_type,
            "display_name": t.display_name,
            "description": t.description,
        }
        for t in templates
    ]
    return tools, workflows


def build_llm(model: str):
    """跟 app.py 的 _build_llm(app.py:416-435) 同构：走 Ollama 的 OpenAI 兼容端点。"""
    from langchain_openai import ChatOpenAI

    settings = load_settings()
    kwargs: Dict[str, Any] = {
        "model": model,
        "temperature": settings.llm.temperature,
        "max_tokens": settings.llm.max_tokens,
    }
    base_url = getattr(settings.llm, "base_url", None)
    api_key = getattr(settings.llm, "api_key", None)
    if settings.llm.provider == "ollama":
        kwargs["base_url"] = f"{(base_url or 'http://localhost:11434').rstrip('/')}/v1"
        kwargs["api_key"] = api_key or "ollama"
    else:
        if base_url:
            kwargs["base_url"] = base_url
        if api_key:
            kwargs["api_key"] = api_key
    return ChatOpenAI(**kwargs)


# ============== 单条执行 ==============
async def _call_two_call_path(
    case: Case,
    llm,
    tools: List[Dict[str, Any]],
    workflows: List[Dict[str, Any]],
):
    """旧的两次调用路径：analyze_query() 先重写+拆分，detect_intent() 再分类。
    这是 2026-08-23 基线实际跑的那条链路（合并版 analyze_and_route() 当时未接线，
    见 workflow.py:616-625）。这里复刻 analyze_and_route 异常降级分支
    （intent.py:690-700）里的同一段调用顺序，保证两条路径可比。"""
    analysis = await intent_mod.analyze_query(query=case.query, messages=[], llm=llm)
    intent = await intent_mod.detect_intent(
        rewritten_query=analysis.rewritten_query,
        llm=llm,
        available_tools=tools,
        available_workflows=workflows,
    )
    return analysis.rewritten_query, analysis.sub_queries, intent


async def run_one(
    case: Case,
    model: str,
    path: str,
    llm,
    tools: List[Dict[str, Any]],
    workflows: List[Dict[str, Any]],
) -> RunResult:
    r = RunResult(model=model, path=path, category=case.category, query=case.query, expect=case.expect)

    # 归因：先离线判一遍两个规则短路会不会命中（纯函数，不调 LLM）
    cleaned = " ".join(case.query.split())
    if intent_mod._match_workflow_action_intent(cleaned, workflows) is not None:
        r.rule_hit = "workflow_action_rule"
    elif intent_mod._match_chitchat_intent(cleaned) is not None:
        # 闲聊白名单短路（2026-08-25 修复）也是在 LLM 之前返回、不经过
        # _reconcile_intent_result，不先标出来的话下面那段兜底会把它错记成
        # clarify_rule，明细表会显示成"被澄清规则拦下"，正好是相反的意思。
        r.rule_hit = "chitchat_rule"

    # 抓取 _reconcile_intent_result 之前的原始 LLM 输出，用来区分
    # 「模型判错」和「后处理改写」——这是任务 A 的关键证据
    captured: Dict[str, Any] = {}
    original_reconcile = intent_mod._reconcile_intent_result

    def spy(result, rewritten_query, available_tools, available_workflows):
        captured["intent_type"] = getattr(result, "intent_type", "")
        captured["target_tool"] = getattr(result, "target_tool", None)
        captured["confidence"] = getattr(result, "confidence", 0.0)
        return original_reconcile(result, rewritten_query, available_tools, available_workflows)

    intent_mod._reconcile_intent_result = spy
    t0 = time.perf_counter()
    try:
        if path == "two-call":
            rewritten, _sub_queries, intent = await _call_two_call_path(case, llm, tools, workflows)
        else:
            rewritten, _sub_queries, intent = await intent_mod.analyze_and_route(
                query=case.query,
                messages=[],
                llm=llm,
                available_tools=tools,
                available_workflows=workflows,
            )
    except Exception as e:  # noqa: BLE001
        r.error = f"{type(e).__name__}: {e}"
        r.elapsed_s = round(time.perf_counter() - t0, 3)
        return r
    finally:
        intent_mod._reconcile_intent_result = original_reconcile
    r.elapsed_s = round(time.perf_counter() - t0, 3)

    r.rewritten = rewritten
    r.intent_type = intent.intent_type
    r.target_tool = intent.target_tool
    r.confidence = round(float(intent.confidence or 0.0), 2)
    r.need_clarify = bool(intent.need_clarify)
    r.workflow_type = intent.workflow_type
    r.reasoning = (intent.reasoning or "")[:120]

    if captured:
        r.llm_intent = captured.get("intent_type", "")
        r.llm_target_tool = captured.get("target_tool") or ""
        r.reconciled = r.llm_intent != r.intent_type
    else:
        # 没走到 _reconcile_intent_result = 被规则短路了
        r.llm_intent = "-"
        r.llm_target_tool = "-"
        if not r.rule_hit:
            # 不经 reconcile 的返回只有两条：_needs_clarify_rule 安全网
            # （intent.py:681 / detect_intent 的 Step 1，intent.py:373）和
            # _match_workflow_action_intent（两条路径里都在 LLM 之前）
            r.rule_hit = (
                "workflow_action_rule" if r.intent_type == "workflow" else "clarify_rule"
            )

    r.outcome = classify_outcome(r)
    r.ok = is_ok(r)
    return r


# ============== 统计与输出 ==============
def _pct(n: int, d: int) -> str:
    return "n/a" if d == 0 else f"{100.0 * n / d:5.1f}%"


def print_detail_table(results: List[RunResult], model: str, path: str) -> None:
    rows = [r for r in results if r.model == model and r.path == path]
    print()
    print(f"===== 明细：intent_model = {model} / path = {path} =====")
    header = (
        f"{'分类':<14} {'问题':<18} {'期望':<5} {'规则短路':<20} "
        f"{'LLM原始':<8} {'终判':<9} {'target_tool':<20} {'终态':<11} {'判定':<5} {'耗时':>6}"
    )
    print(header)
    print("-" * len(header))
    for r in rows:
        if r.error:
            print(f"{r.category:<14} {r.query:<18} {r.expect:<5} ERROR: {r.error}")
            continue
        print(
            f"{r.category:<14} {r.query:<18} {r.expect:<5} {(r.rule_hit or '-'):<20} "
            f"{r.llm_intent:<8} {r.intent_type:<9} {(r.target_tool or '-'):<20} "
            f"{r.outcome:<11} {('OK' if r.ok else 'MISS'):<5} {r.elapsed_s:>6.2f}s"
        )


def print_summary(results: List[RunResult], arms: List[tuple[str, str]]) -> None:
    def pick(model: str, path: str, **kw) -> List[RunResult]:
        out = [r for r in results if r.model == model and r.path == path and not r.error]
        for k, v in kw.items():
            out = [r for r in out if getattr(r, k) == v]
        return out

    labels = [f"{m}[{p}]" for m, p in arms]
    categories = sorted({c.category for c in CASES})
    print()
    print("===== 汇总：按分类的误判率（misroute = 未按期望路由的运行次数占比）=====")
    head = f"{'分类':<16}" + "".join(f"{lb:>30}" for lb in labels)
    print(head)
    print("-" * len(head))
    for cat in categories:
        line = f"{cat:<16}"
        for m, p in arms:
            rows = pick(m, p, category=cat)
            miss = sum(1 for r in rows if not r.ok)
            line += f"{f'{miss}/{len(rows)}  {_pct(miss, len(rows))}':>30}"
        print(line)

    print()
    print("===== 汇总：闲聊类（expect=chat）终态分布 =====")
    head = f"{'终态':<14}" + "".join(f"{lb:>30}" for lb in labels)
    print(head)
    print("-" * len(head))
    for oc in ("answered", "kb_refusal", "clarify", "workflow"):
        line = f"{oc:<14}"
        for m, p in arms:
            rows = pick(m, p, expect="chat")
            n = sum(1 for r in rows if r.outcome == oc)
            line += f"{f'{n}/{len(rows)}  {_pct(n, len(rows))}':>30}"
        print(line)

    print()
    print("===== 汇总：总体 =====")
    for m, p in arms:
        chat = pick(m, p, expect="chat")
        ctrl = [r for r in pick(m, p) if r.expect != "chat"]
        chat_miss = sum(1 for r in chat if not r.ok)
        ctrl_miss = sum(1 for r in ctrl if not r.ok)
        kb_ref = sum(1 for r in chat if r.outcome == "kb_refusal")
        errs = sum(1 for r in results if r.model == m and r.path == p and r.error)
        lat = [r.elapsed_s for r in pick(m, p)]
        print(
            f"  {m}[{p}]: 闲聊误判 {chat_miss}/{len(chat)} ({_pct(chat_miss, len(chat))})"
            f" | 其中撞上知识库拒绝话术 {kb_ref}/{len(chat)} ({_pct(kb_ref, len(chat))})"
            f" | 对照组误判 {ctrl_miss}/{len(ctrl)} ({_pct(ctrl_miss, len(ctrl))})"
            f" | 异常 {errs}"
            f" | 单次分类耗时 中位 {statistics.median(lat) if lat else 0:.2f}s"
        )

    # 稳定性：同一条 query 多次运行结果是否一致
    print()
    print("===== 稳定性：同一问法多次运行结果不一致的用例 =====")
    unstable = 0
    for m, p in arms:
        for case in CASES:
            rows = pick(m, p, query=case.query)
            variants = {(r.intent_type, r.target_tool or "-", r.need_clarify) for r in rows}
            if len(variants) > 1:
                unstable += 1
                print(f"  [{m}/{p}] {case.query!r} -> {sorted(str(v) for v in variants)}")
    if unstable == 0:
        print("  （无：所有用例在多次运行下结果稳定）")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--models",
        default=DEFAULT_MODEL,
        help=f"逗号分隔的意图模型名，按顺序依次跑同一批用例（默认 {DEFAULT_MODEL}）。"
             f"任务 C 对比用：--models qwen2.5-1.5b-router,qwen2.5:7b",
    )
    parser.add_argument(
        "--path",
        default="merged",
        choices=["merged", "two-call"],
        help="意图链路：merged = 当前线上的 analyze_and_route() 一次合并调用；"
             "two-call = 旧的 analyze_query() + detect_intent() 两次调用"
             "（2026-08-23 基线跑的就是这条，见 workflow.py:616-625）",
    )
    parser.add_argument("--repeat", type=int, default=2, help="每条用例重复次数（默认 2）")
    parser.add_argument("--filter", default="", help="只跑 category 或 query 包含该子串的用例")
    parser.add_argument("--json", dest="json_out", default="", help="把原始结果写到这个 JSON 文件")
    args = parser.parse_args()

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    cases = [
        c for c in CASES
        if not args.filter or args.filter in c.category or args.filter in c.query
    ]

    print(f"[Setup] 载入工具注册表与流程模板 ...")
    tools, workflows = await build_context()
    print(f"[Setup] available_tools = {[ (t.get('function') or {}).get('name') for t in tools ]}")
    print(f"[Setup] available_workflows = {[w['workflow_type'] for w in workflows]}")
    print(f"[Setup] 链路 path = {args.path}")
    print(f"[Setup] 模型 {models} × 用例 {len(cases)} 条 × 重复 {args.repeat} 次"
          f" = {len(models) * len(cases) * args.repeat} 轮"
          f"（two-call 每轮是 2 次 LLM 调用）")

    results: List[RunResult] = []
    for model in models:
        llm = build_llm(model)
        print(f"\n[Run] intent_model = {model} / path = {args.path}")
        for i, case in enumerate(cases, 1):
            for _ in range(args.repeat):
                r = await run_one(case, model, args.path, llm, tools, workflows)
                results.append(r)
            print(f"  [{i}/{len(cases)}] {case.query}", flush=True)

    arms = [(m, args.path) for m in models]
    for model in models:
        print_detail_table(results, model, args.path)
    print_summary(results, arms)

    if args.json_out:
        payload = {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "script": "scripts/verify_smalltalk_routing.py",
            "models": models,
            "path": args.path,
            "repeat": args.repeat,
            "case_count": len(cases),
            "results": [r.as_dict() for r in results],
        }
        Path(args.json_out).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n[JSON] 原始结果已写入 {args.json_out}")

    # 退出码：闲聊类只要有任何一次撞上知识库拒绝话术，就算回归仍然存在
    regression = any(r.expect == "chat" and r.outcome == "kb_refusal" for r in results)
    print(f"\n[结论] 闲聊被误路由进知识库检索：{'仍然存在' if regression else '未复现'}")
    return 1 if regression else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
