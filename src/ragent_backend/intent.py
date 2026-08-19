"""
意图识别模块 — 三分支路由（clarify / rag / tool）。

核心改进：
1. detect_intent() 支持 LLM-based 三分类，同时保留规则 fallback
2. 工具意图通过 available_tools 列表让 LLM 自主判断
3. 分类理由（reasoning）写入 trace_events，提升可观测性
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field
from langchain_core.messages import HumanMessage
from src.ragent_backend.schemas import IntentResult


# ============== 结构化 LLM 输出模型 ==============

class QueryAnalysisResult(BaseModel):
    """LLM 结构化输出：查询重写 + 子查询拆分"""
    rewritten_query: str = Field(
        description="消除所有代词和指代后的完整、独立查询"
    )
    sub_queries: List[str] = Field(
        description="如果查询包含多个并列主题，拆分为可独立执行的子查询列表；否则只放一个元素"
    )


class IntentDetectionResult(BaseModel):
    """LLM 结构化输出：意图四分类"""
    intent_type: Literal["clarify", "rag", "tool", "workflow"] = Field(
        description="意图类型: clarify=需要澄清, rag=知识库检索, tool=需要调用外部工具, "
                    "workflow=发起一个业务流程（报修/请假/出差等）"
    )
    confidence: float = Field(
        ge=0.0, le=1.0,
        description="分类置信度 (0-1)"
    )
    target_tool: Optional[str] = Field(
        default=None,
        description="当 intent_type=tool 时，指定最适合的工具名"
    )
    tool_args_preview: Optional[Dict[str, Any]] = Field(
        default=None,
        description="当 intent_type=tool 时，预解析的参数（可选）"
    )
    workflow_type: Optional[str] = Field(
        default=None,
        description="当 intent_type=workflow 时，匹配到的流程类型（必须从可用流程列表中选择）"
    )
    need_clarify: bool = Field(
        default=False,
        description="是否需要澄清"
    )
    clarify_prompt: Optional[str] = Field(
        default=None,
        description="当 need_clarify=True 时，给用户的澄清提示"
    )
    reasoning: str = Field(
        description="分类理由（为什么是这个意图类型）"
    )


# ============== 查询分析 ==============

async def analyze_query(query: str, messages: list, llm=None) -> QueryAnalysisResult:
    """
    单次结构化 LLM 调用：同时完成指代消解和子查询拆分。
    如果 LLM 不可用或调用失败，回退到规则-based 处理。
    """
    cleaned = " ".join(query.split())

    # 没有 LLM，直接做基础 fallback
    if llm is None:
        return QueryAnalysisResult(
            rewritten_query=cleaned,
            sub_queries=_fallback_split(cleaned)
        )

    # 取最近最多 4 轮对话作为上下文
    recent = messages[-4:] if messages else []
    history_lines = []
    for m in recent:
        role = "User" if m.type == "human" else "Assistant"
        content = str(getattr(m, "content", "")).strip()
        if content:
            history_lines.append(f"{role}: {content}")

    history_text = "\n".join(history_lines)

    prompt = f"""你是一个查询分析助手。请根据对话历史，将用户的当前问题处理为独立、完整的查询单元。

处理要求：
1. 消除所有代词和指代（如"它"、"这个"、"that"、"这个文档"、"上面说的"、"前者"等），替换为对话历史中提到的具体实体。
2. 如果当前问题包含多个并列主题（如多个城市、多个产品、多个时间段的比较），即使没有连词也必须拆分成可独立执行的子查询列表。
3. 如果问题只涉及单一主题，sub_queries 列表中只放一个元素即可。
4. 每个子查询必须完整、无歧义、不依赖上下文即可理解。

示例 1：
当前问题：北京上海杭州的天气怎么样
输出：{{"rewritten_query": "北京、上海、杭州的天气怎么样", "sub_queries": ["北京的天气怎么样", "上海的天气怎么样", "杭州的天气怎么样"]}}

示例 2：
当前问题：华为和苹果的旗舰手机对比
输出：{{"rewritten_query": "华为和苹果的旗舰手机对比", "sub_queries": ["华为旗舰手机", "苹果旗舰手机"]}}

示例 3：
当前问题：2024年英伟达财报表现如何
输出：{{"rewritten_query": "2024年英伟达财报表现如何", "sub_queries": ["2024年英伟达财报表现如何"]}}

对话历史：
{history_text}

当前问题：{cleaned}

请直接输出 JSON 对象，不要添加任何解释或 Markdown 格式。"""

    try:
        structured_llm = llm.with_structured_output(QueryAnalysisResult, method="json_mode")
        result: QueryAnalysisResult = await structured_llm.ainvoke([HumanMessage(content=prompt)])

        # 后处理：确保 sub_queries 非空
        if not result.sub_queries:
            result.sub_queries = [result.rewritten_query or cleaned]

        # 清洗：去掉子查询前后的空白和标点
        result.sub_queries = [
            sq.strip(" ,，。？！?！")
            for sq in result.sub_queries
            if sq.strip(" ,，。？！?！")
        ] or [result.rewritten_query or cleaned]

        return result
    except Exception as e:
        print(f"[Intent] Structured query analysis failed: {e}")
        # Fallback: 用旧逻辑兜底
        rewritten = await rewrite_query(cleaned, messages, llm)
        return QueryAnalysisResult(
            rewritten_query=rewritten,
            sub_queries=_fallback_split(rewritten)
        )


def _fallback_split(query: str) -> List[str]:
    """LLM 失败时的子查询拆分回退"""
    return split_parallel_subqueries(query)


async def rewrite_query(query: str, messages: list, llm=None) -> str:
    """
    （保留用于兼容性 fallback）
    真正的查询重写：拼接最近对话历史，消除指代消解。
    """
    cleaned = " ".join(query.split())

    if not messages or llm is None:
        return cleaned

    recent = messages[-4:]
    history_lines = []
    for m in recent:
        role = "User" if m.type == "human" else "Assistant"
        content = str(getattr(m, "content", "")).strip()
        if content:
            history_lines.append(f"{role}: {content}")

    history_text = "\n".join(history_lines)
    if not history_text:
        return cleaned

    prompt = f"""你是一个查询重写助手。请根据对话历史，将用户的当前问题改写为一个独立、完整、没有歧义的新查询。
改写要求：
1. 必须消除所有代词和指代（如"它"、"这个"、"that"、"这个文档"、"上面说的"等），替换为对话历史中提到的具体实体。
2. 如果当前问题涉及多个实体，保持它们的关系和比较意图。
3. 只输出改写后的查询，不要加任何解释、引号或前缀。

对话历史：
{history_text}

当前问题：{cleaned}

改写后查询："""

    try:
        response = await llm.ainvoke([HumanMessage(content=prompt)])
        rewritten = response.content.strip()
        rewritten = re.sub(r'^(改写后查询[：:]?\s*|[""「])', '', rewritten).strip()
        return rewritten if rewritten else cleaned
    except Exception:
        return cleaned


# ============== 意图检测（三分支） ==============

# 工具意图关键词映射（规则 fallback 用）
# 工具名必须与 ToolRegistry 中注册的实际名称完全一致
_TOOL_KEYWORDS: Dict[str, List[str]] = {
    # 考勤关键词放最前面：query_knowledge_hub 的"查询"太通用，
    # 会抢先匹配到"查询xx日的考勤"这类句子，必须让更具体的关键词先判断
    "query_attendance": ["考勤", "打卡", "迟到", "早退"],
    # 内置工具
    "query_knowledge_hub": ["文档", "文件", "知识库", "资料", "帮我找", "查询"],
    "list_collections": ["集合", "collection", "有哪些文件"],
    "get_document_summary": ["摘要", "总结", "文档详情", "doc_id"],
    # MCP 外部工具（simple.* 前缀必须与注册时一致）
    "simple.web_search": ["搜索", "网上", "网页", "google", "百度", "bing", "查一下", "查查", "搜一下"],
    "simple.calculator": ["计算", "等于", "公式", "算一下", "等于多少"],
    "simple.get_current_time": ["时间", "现在几点", "日期", "当前时间"],
    "simple.list_directory": ["目录", "文件夹", "文件列表", "ls"],
}

# 通用工具意图关键词（不绑定特定工具，只判断 intent_type="tool"）
_TOOL_INTENT_KEYWORDS: List[str] = [
    "搜索", "网上", "网页", "google", "百度", "bing", "查一下", "查查", "搜一下",
    "计算", "等于", "公式", "算一下", "等于多少",
    "时间", "现在几点", "日期",
    "天气", "气温", "降水",
    "考勤", "打卡", "迟到", "早退",
]

# 工作流意图关键词映射（规则 fallback 用）——workflow_type 必须与
# WorkflowStore 里注册的实际类型完全一致（见 workflow_store.py 的种子模板）
_WORKFLOW_KEYWORDS: Dict[str, List[str]] = {
    "laptop_repair": ["报修", "电脑坏了", "键盘坏了", "屏幕坏了", "设备故障"],
    "leave_request": ["请假", "事假", "病假", "年假", "调休"],
    "business_trip": ["出差", "出差申请"],
    "expense_reimbursement": ["报销", "发票", "报销单"],
}

# detect_intent Step 1.5 用：动作型表达的前缀词，跟疑问句式天然可分
# （"我想报销" vs "报销标准是什么"）。
_WORKFLOW_ACTION_PREFIXES = ["我想", "我要", "我需要", "帮我", "帮忙", "麻烦"]
# 出现这些词大概率是在问政策/流程本身，不是要发起——即使带了"我想"前缀
# （比如"我想知道报销标准是什么"），也不该被规则短路，交给 LLM 判断更稳妥。
_WORKFLOW_QUESTION_CUE_WORDS = [
    "吗", "呢", "？", "?", "怎么", "如何", "什么", "多少", "标准", "规定", "政策", "流程是",
]


def _match_workflow_action_intent(
    rewritten_query: str, available_workflows: List[Dict[str, Any]],
) -> Optional[IntentResult]:
    """detect_intent Step 1.5 的规则短路：只有"动作型前缀 + 流程关键词"同时命中、
    且不带疑问句提示词时才短路成 workflow，避免把"我想知道报销标准是什么"这种
    其实是在问政策的句子也误判成发起工作流——这个函数本身只做高精度的正向匹配，
    不确定的情况一律返回 None，交回给 LLM/规则 fallback 走原来的判断路径。"""
    if not any(p in rewritten_query for p in _WORKFLOW_ACTION_PREFIXES):
        return None
    if any(w in rewritten_query for w in _WORKFLOW_QUESTION_CUE_WORDS):
        return None
    available_workflow_types = {w.get("workflow_type", "") for w in available_workflows}
    query_lower = rewritten_query.lower()
    for workflow_type, keywords in _WORKFLOW_KEYWORDS.items():
        if workflow_type not in available_workflow_types:
            continue
        for kw in keywords:
            if kw.lower() in query_lower:
                return IntentResult(
                    intent_type="workflow",
                    rewritten_query=rewritten_query,
                    confidence=0.9,
                    workflow_type=workflow_type,
                    reasoning=(
                        f"规则短路：动作型前缀 + 流程关键词 '{kw}' 命中，跳过 LLM 分类"
                        f"（本地小模型面对这类短句容易被通用检索工具的描述带偏，误判成查文档）"
                    ),
                )
    return None


async def detect_intent(
    rewritten_query: str,
    llm=None,
    available_tools: Optional[List[Dict[str, Any]]] = None,
    available_workflows: Optional[List[Dict[str, Any]]] = None,
) -> IntentResult:
    """
    意图四分类：clarify / rag / tool / workflow。

    策略：
    1. 先检查是否需要澄清（保留现有规则）
    1.5. 明确的"我想/我要 + 流程关键词"动作型表达，规则直接短路成 workflow
    2. 如果有 LLM，用结构化调用做四分类（推荐）
    3. 无 LLM 时，回退到规则-based 分类

    Args:
        rewritten_query: 已重写（指代消解后）的查询
        llm: 可选的 LLM 实例
        available_tools: 可用工具列表（用于 LLM 判断 tool 意图）
        available_workflows: 可用流程模板列表，每项至少含
            {"workflow_type", "display_name", "description"}（用于 LLM 判断 workflow 意图）

    Returns:
        IntentResult
    """
    # === Step 1: 澄清检查（硬规则，不经过 LLM）===
    vague_pronouns = ["它", "这个", "那个", "that", "it", "this", "上述", "上面"]
    has_vague = any(token in rewritten_query for token in vague_pronouns)

    if len(rewritten_query.strip()) < 4 or (has_vague and len(rewritten_query) < 10):
        return IntentResult(
            intent_type="clarify",
            rewritten_query=rewritten_query,
            confidence=0.35,
            need_clarify=True,
            clarify_prompt="请补充更具体的信息，例如具体的产品名、文档名或业务指标。",
            reasoning="查询过短或包含模糊代词，需要澄清",
        )

    # === Step 1.5：动作型表达规则短路（不经过 LLM）===
    # "我想报销"这类短句交给 LLM 判断时，本项目用的本地小模型（qwen2.5:7b）
    # 经常被 query_knowledge_hub 这个通用检索工具的描述带偏，判成"查文档"
    # （intent_type=tool），完全跳过 workflow——概率性误判，不是能靠调整置信度
    # 阈值解决的问题。"我想/我要/帮我"这类前缀 + 流程关键词是高精度信号：
    # 真的在问政策的句子通常是"报销标准是什么""请假流程是怎样的"这种疑问句式，
    # 不会用"我想"这种动作型前缀开头，两者句式上天然可分——命中这个模式时直接
    # 判定 workflow，不必也不该让 LLM 去猜，省一次容易出错的分类调用，顺带省掉
    # 误判后额外绕一圈 tool 子图（ReAct 循环里好几次 LLM 往返）的时间。
    action_intent = _match_workflow_action_intent(rewritten_query, available_workflows or [])
    if action_intent is not None:
        return action_intent

    # === Step 2: LLM-based 四分类 ===
    if llm is not None:
        try:
            return await _detect_intent_with_llm(
                rewritten_query, llm, available_tools or [], available_workflows or [],
            )
        except Exception as e:
            print(f"[Intent] LLM-based detection failed: {e}, falling back to rule-based")

    # === Step 3: 规则 fallback ===
    return _detect_intent_rule_based(rewritten_query, available_tools or [], available_workflows or [])


async def _detect_intent_with_llm(
    rewritten_query: str,
    llm,
    available_tools: List[Dict[str, Any]],
    available_workflows: List[Dict[str, Any]],
) -> IntentResult:
    """使用 LLM 做结构化意图四分类。"""

    # 构建工具描述（从 OpenAI function schema 中提取）
    tools_text = ""
    if available_tools:
        lines = []
        for t in available_tools:
            # OpenAI schema: {"type": "function", "function": {"name": ..., "description": ...}}
            func = t.get("function") or {}
            name = func.get("name") or t.get("name", "unknown")
            desc = func.get("description") or t.get("description", "无描述")
            lines.append(f"- {name}: {desc[:80]}")
        tools_text = "\n".join(lines)
    else:
        tools_text = "（当前无可用的外部工具）"

    workflows_text = ""
    if available_workflows:
        lines = [
            f"- {w.get('workflow_type')}: {w.get('display_name')} — {(w.get('description') or '')[:80]}"
            for w in available_workflows
        ]
        workflows_text = "\n".join(lines)
    else:
        workflows_text = "（当前无可用的流程模板）"

    prompt = f"""你是意图分类专家。请根据用户查询、可用工具列表和可用流程模板列表，判断用户的真实意图。

分类规则：
- "clarify": 查询模糊、不完整、需要用户补充信息才能回答
- "rag": 查询涉及知识库、文档、内部资料等，可以通过检索回答
- "tool": 查询明确需要调用外部工具（如搜索网页、查天气、计算等）
- "workflow": 用户想发起一个业务流程/申请（如报修、请假、出差、报销），不是在问知识、不是在查资料

可用工具列表：
{tools_text}

可用流程模板列表：
{workflows_text}

用户查询：{rewritten_query}

请输出结构化分类结果，包含 intent_type、confidence、reasoning 等字段。
注意：target_tool 必须从可用工具列表中选择，workflow_type 必须从可用流程模板列表中选择，都不能编造不存在的名字。"""

    structured_llm = llm.with_structured_output(IntentDetectionResult, method="json_mode")
    result: IntentDetectionResult = await structured_llm.ainvoke([HumanMessage(content=prompt)])

    # 验证 target_tool 是否存在于可用工具列表中
    # available_tools 实际传入的是 OpenAI function-calling schema（见
    # workflow.py 的 to_openai_tools()）：{"type": "function", "function":
    # {"name": ...}}，name 嵌在 function 里，不是顶层字段——之前直接
    # t.get("name") 永远拿到 None，导致 available_tool_names 恒为空集，
    # target_tool 无论 LLM 判断得多准都会被这里清空。同时兼容顶层 name
    # （规则 fallback 等场景可能直接传扁平字典）。
    available_tool_names = {
        (t.get("function") or {}).get("name") or t.get("name", "") for t in available_tools
    }
    if result.target_tool and result.target_tool not in available_tool_names:
        result.target_tool = None  # 让子图自己选

    # 验证 workflow_type 是否存在于可用流程模板列表中
    available_workflow_types = {w.get("workflow_type", "") for w in available_workflows}
    if result.workflow_type and result.workflow_type not in available_workflow_types:
        result.workflow_type = None

    # 小模型（如 qwen2.5:7b）的结构化输出偶尔会自相矛盾：workflow_type 已经准确
    # 判断出来了（reasoning 里也明确点出是请假/报修等申请），但 intent_type 却
    # 没有同步设成 "workflow"（常见误标成 "rag" 或 "clarify"）。workflow_type
    # 是从受限枚举（可用流程列表）里选出来的，比自由文本的 intent_type 更可信，
    # 出现这种矛盾时以 workflow_type 为准，否则用户明确说"我想请假"也会被当成
    # 知识库问答处理。
    if result.workflow_type and result.intent_type != "workflow":
        result.intent_type = "workflow"

    # 同样的自相矛盾也会出现在 tool 上：target_tool 已经命中了一个真实存在的
    # 工具（reasoning 里往往也点名了具体该调哪个工具），但 intent_type 被标成
    # 了 "rag" 或别的。target_tool 是从受限枚举（可用工具列表）里选出来的，比
    # intent_type 更可信，出现矛盾时以 target_tool 为准（workflow 优先级更高，
    # 已经在上面处理过，这里不覆盖 workflow）。
    if result.target_tool and result.intent_type not in ("tool", "workflow"):
        result.intent_type = "tool"

    # workflow 意图但没能对应到一个合法类型，退回 rag，避免进入一个没有类型的死路
    if result.intent_type == "workflow" and not result.workflow_type:
        return IntentResult(
            intent_type="rag",
            rewritten_query=rewritten_query,
            confidence=0.6,
            reasoning="LLM 判断为 workflow 意图，但未能匹配到合法的流程类型，回退到 rag",
        )

    # 置信度阈值
    if result.confidence < 0.5:
        # 低置信度，默认 rag
        return IntentResult(
            intent_type="rag",
            rewritten_query=rewritten_query,
            confidence=0.6,
            reasoning=f"LLM 分类置信度过低({result.confidence:.2f})，默认回退到 rag",
        )

    return IntentResult(
        intent_type=result.intent_type,
        rewritten_query=rewritten_query,
        confidence=result.confidence,
        target_tool=result.target_tool,
        tool_args=result.tool_args_preview,
        workflow_type=result.workflow_type,
        need_clarify=result.need_clarify,
        clarify_prompt=result.clarify_prompt,
        reasoning=result.reasoning,
    )


def _detect_intent_rule_based(
    rewritten_query: str,
    available_tools: List[Dict[str, Any]] = None,
    available_workflows: List[Dict[str, Any]] = None,
) -> IntentResult:
    """规则-based 意图分类（无 LLM 时的 fallback）。

    策略：
    1. 先匹配具体工具关键词，返回对应工具名
    2. 再匹配工作流关键词
    3. 再匹配通用工具意图关键词，返回 tool 但不指定具体工具（让子图自选）
    4. 默认 rag
    """
    query_lower = rewritten_query.lower()
    available_tool_names = {t.get("name", "") for t in (available_tools or [])}
    available_workflow_types = {w.get("workflow_type", "") for w in (available_workflows or [])}

    # Step 1: 匹配具体工具关键词
    for tool_name, keywords in _TOOL_KEYWORDS.items():
        # 只推荐实际存在的工具
        if tool_name not in available_tool_names:
            continue
        for kw in keywords:
            if kw.lower() in query_lower:
                return IntentResult(
                    intent_type="tool",
                    rewritten_query=rewritten_query,
                    confidence=0.75,
                    target_tool=tool_name,
                    reasoning=f"关键词匹配工具 '{tool_name}': {kw}",
                )

    # Step 2: 匹配工作流关键词（只推荐实际存在的模板类型）
    for workflow_type, keywords in _WORKFLOW_KEYWORDS.items():
        if workflow_type not in available_workflow_types:
            continue
        for kw in keywords:
            if kw.lower() in query_lower:
                return IntentResult(
                    intent_type="workflow",
                    rewritten_query=rewritten_query,
                    confidence=0.75,
                    workflow_type=workflow_type,
                    reasoning=f"关键词匹配流程 '{workflow_type}': {kw}",
                )

    # Step 3: 通用工具意图（不指定具体工具，让子图自己选）
    for kw in _TOOL_INTENT_KEYWORDS:
        if kw.lower() in query_lower:
            return IntentResult(
                intent_type="tool",
                rewritten_query=rewritten_query,
                confidence=0.65,
                target_tool=None,  # 子图自己选工具
                reasoning=f"通用工具意图关键词: {kw}",
            )

    # 默认 rag
    return IntentResult(
        intent_type="rag",
        rewritten_query=rewritten_query,
        confidence=0.85,
        reasoning="无工具/流程关键词匹配，默认归类为知识库检索",
    )


# ============== 子查询拆分（保留） ==============

def split_parallel_subqueries(query: str) -> List[str]:
    """将包含多个并列主题的查询拆分为子查询列表（保留作为 fallback）。"""
    normalized = query.strip()
    if not normalized:
        return []

    segments = re.split(r"[；;]|，并|,\s*and\s+", normalized)

    pieces: List[str] = []
    for seg in segments:
        seg = seg.strip(" ,，。？！?！")
        if not seg:
            continue

        sub_parts = re.split(r"\s*(?:和|与|以及|及|and)\s*", seg)
        if len(sub_parts) <= 1:
            pieces.append(seg)
            continue

        tail = sub_parts[-1].strip()
        predicate_match = re.search(r"(是.*|有.*|怎么.*|如何.*|多少.*|哪些.*|是什么.*)$", tail)
        predicate = predicate_match.group(1) if predicate_match else ""

        for idx, part in enumerate(sub_parts):
            part = part.strip(" ,，。？！?！")
            if not part:
                continue
            if idx < len(sub_parts) - 1 and predicate:
                pieces.append(f"{part}{predicate}")
            else:
                pieces.append(part)

    deduped: List[str] = []
    seen = set()
    for piece in pieces:
        item = piece.strip(" ,，。？！?！")
        if not item or item in seen:
            continue
        seen.add(item)
        deduped.append(item)

    return deduped or [normalized]
