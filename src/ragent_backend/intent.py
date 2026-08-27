"""
意图识别模块 — 三分支路由（clarify / rag / tool）。

核心改进：
1. detect_intent() 支持 LLM-based 三分类，同时保留规则 fallback
2. 工具意图通过 available_tools 列表让 LLM 自主判断
3. 分类理由（reasoning）写入 trace_events，提升可观测性
4. analyze_and_route() 把"查询重写+拆分"和"四分类"合并成一次结构化 LLM 调用
   （原来是 analyze_query() + detect_intent() 两次串行往返），省一次本地小模型
   推理耗时；旧的两个函数原样保留，供合并调用失败时降级、以及其他调用方兼容
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel, Field
from langchain_core.messages import HumanMessage
from src.ragent_backend.schemas import IntentResult
from src.observability.logger import get_logger

logger = get_logger(__name__)


# 子查询并行扇出的硬上限（`docs/orchestration_design.md` §4.3 决策 D2、§8 Q1）。
# 已拍板取 3：当前没有评估体系，无法验证放宽到 5 是变好还是变坏，属于盲改；
# 等 D6a/D6b 评估建起来后再用数据决定是否放宽。
#
# 定义在这里、由 workflow.py 导入，是为了让"提示词里写的上限"和"代码里截断
# 的上限"永远是同一个数字——两处各写一个常量迟早会漂。
# **真正的截断只发生在 workflow.py::_retrieve_multi 一处**（那里才有 trace）。
MAX_SUB_QUERY_FANOUT = 3


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


class QueryAnalysisAndIntentResult(BaseModel):
    """LLM 结构化输出：合并版，一次调用同时完成"查询重写+子查询拆分"
    （QueryAnalysisResult 的字段）和"意图四分类"（IntentDetectionResult 的字段）。
    字段定义跟那两个模型逐一对应，方便 `_reconcile_intent_result` 用同一套
    后处理逻辑同时兼容这三个模型（duck typing，不要求共同基类）。"""
    rewritten_query: str = Field(
        description="消除所有代词和指代后的完整、独立查询"
    )
    sub_queries: List[str] = Field(
        description="如果查询包含多个并列主题，拆分为可独立执行的子查询列表；否则只放一个元素"
    )
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
2. 如果当前问题包含多个**彼此独立**的并列主题（如多个城市、多个产品、多个时间段的比较），即使没有连词也必须拆分成可独立执行的子查询列表。
3. 如果问题只涉及单一主题，sub_queries 列表中只放一个元素即可。
4. 每个子查询必须完整、无歧义、不依赖上下文即可理解。

{_SUB_QUERY_SPLIT_RULES}

示例 1（独立，拆）：
当前问题：北京上海杭州的天气怎么样
输出：{{"rewritten_query": "北京、上海、杭州的天气怎么样", "sub_queries": ["北京的天气怎么样", "上海的天气怎么样", "杭州的天气怎么样"]}}
说明：三个城市的天气互不影响，任何一个的答案都不需要另一个。

示例 2（独立，拆）：
当前问题：华为和苹果的旗舰手机对比
输出：{{"rewritten_query": "华为和苹果的旗舰手机对比", "sub_queries": ["华为旗舰手机", "苹果旗舰手机"]}}

示例 3（单一主题，不拆）：
当前问题：2024年英伟达财报表现如何
输出：{{"rewritten_query": "2024年英伟达财报表现如何", "sub_queries": ["2024年英伟达财报表现如何"]}}

示例 4（**有依赖，不拆**——反例）：
当前问题：销售额最高的部门是哪个，该部门今年的招聘预算是多少
输出：{{"rewritten_query": "销售额最高的部门是哪个，该部门今年的招聘预算是多少", "sub_queries": ["销售额最高的部门是哪个，该部门今年的招聘预算是多少"]}}
说明：不先知道是哪个部门，就没法查"该部门"的预算，第二问依赖第一问的答案，**必须保持单查询**。

示例 5（**有依赖，不拆**——反例）：
当前问题：公司的年假制度是什么，它的审批人是谁
输出：{{"rewritten_query": "公司的年假制度是什么，它的审批人是谁", "sub_queries": ["公司的年假制度是什么，它的审批人是谁"]}}
说明："它"指代前一问里的年假制度，属于回指依赖，**必须保持单查询**。

对话历史：
{history_text}

当前问题：{cleaned}

请直接输出 JSON 对象，不要添加任何解释或 Markdown 格式。"""

    try:
        structured_llm = llm.with_structured_output(QueryAnalysisResult, method="json_mode")
        result: QueryAnalysisResult = await structured_llm.ainvoke([HumanMessage(content=prompt)])

        # 后处理：清洗 + 去重 + D1 依赖判据（有依赖则降级为单查询）
        result.rewritten_query = result.rewritten_query or cleaned
        result.sub_queries = _finalize_sub_queries(
            result.rewritten_query, result.sub_queries
        ) or [result.rewritten_query]

        return result
    except Exception:
        logger.exception("structured query analysis failed")
        # Fallback: 用旧逻辑兜底
        rewritten = await rewrite_query(cleaned, messages, llm)
        return QueryAnalysisResult(
            rewritten_query=rewritten,
            sub_queries=_fallback_split(rewritten)
        )


def _fallback_split(query: str) -> List[str]:
    """LLM 失败时的子查询拆分回退。

    走的也是 `_finalize_sub_queries`，因为规则拆分（按"和/与/以及"切）比 LLM
    更容易切出有依赖的碎片（"年假制度和它的申请流程" -> ["年假制度", "它的申请流程"]），
    D1 的依赖判据在这条路径上更有必要，不是可选项。"""
    return _finalize_sub_queries(query, split_parallel_subqueries(query))


# ============== D1：子查询拆分的依赖性判据 ==============
#
# 对应 `docs/orchestration_design.md` §4.3 决策 D1（防 F3"假并行"）。
#
# 问题：拆分提示词原来只有"多个并列主题就拆"这一条判据，三个示例（北京/上海/
# 杭州天气、华为/苹果对比、单一主题）**全都是天然独立的**，没有任何一条规则
# 要求判断子问题之间有没有依赖。于是"销售额最高的部门是哪个，该部门的年假
# 有多少天"这种**第二问要用第一问的答案**的问题也会被拆成两个并行子查询，
# 两路各自检索、各自拿到不相干的材料，再拼给生成模型——F3 假并行。
#
# 已拍板（2026-08-25）：**存在依赖时降级为单查询**，交给已有的 ReAct 子图
# （max_iterations=5）自己决定要不要再查一轮，**不做显式多跳分解**。依据是
# 业界基准把"agentic RAG / 复杂多步编排"列在"大厂特有、不必跟"
# （`docs/review_2026-08-24/review_industry_baseline.md`），本项目不新增编排复杂度。
#
# 为什么除了改提示词还要加这层确定性兜底：线上意图分类跑的是
# qwen2.5:1.5b-router（见 workflow.py `_intent_llm` 的说明），**纯 prompt 约束
# 在 1.5b 上没有保证**。这层判据是纯字符串规则、零 LLM 调用、可单测。
#
# ⚠️ 与闲聊白名单短路的关系：本判据**只在 `len(sub_queries) > 1` 时才运行**。
# 闲聊路径（`_match_chitchat_intent`）返回的永远是 `[cleaned]` 单元素，
# 根本走不到这里，因此不可能误伤闲聊——
# `tests/unit/test_intent_chitchat_routing.py` 的 132 条不受影响。

# 一、回指/指代词：子查询里出现这些，说明它在引用另一个子查询的答案。
# 每条都刻意加了否定环视，避开高频误伤：
#   其他/其中/尤其  -> 不算"其"；应该/该怎么办 -> 不算"该"；
#   因此/如此       -> 不算"此"；这个月/那个星期 -> 不算"这个/那个"
#   （最后一条跟 `_has_vague_pronoun` 里那条实测教训是同一个坑）
_ANAPHORA_PATTERNS = [
    r"(?<!其)[他她]们?",
    r"它们?",
    r"(?<![应活])该(?![不当怎如何])",
    r"(?<![尤极与])其(?![他它中实余次间所后])",
    r"(?<![因如彼从由为])此(?![外前后时类])",
    r"上述|前述|上面(?:提到|说的|那|的)|前者|后者|对方",
    r"这[些位家者项]|那[些位家者项]",
    r"这个(?!月|年|星期|周|季度|礼拜|次|时候|时间|问题)",
    r"那个(?!月|年|星期|周|季度|礼拜|次|时候|时间)",
    r"\b(?:it|its|they|them|their)\b",
    r"\b(?:the\s+)?(?:former|latter)\b",
]

# 二、依赖链连接词：出现在整句里，说明用户自己就写明了"先算出A再拿A去问B"。
_DEPENDENCY_CHAIN_PATTERNS = [
    r"先[^，。；？?！!]{0,15}再",
    r"根据(?:上述|上面|前面|第一步|第一问|查到的|检索到的|上一步|结果)",
    r"基于(?:上述|上面|前面|结果|第一步)",
    r"(?:然后|接着|之后)再?",
    r"对应的|相应的|与之(?:对应|相关|匹配)",
    r"由此|据此",
]

# 三、"先确定实体再查属性"型：整句里带一个**实体识别问**（谁/哪个部门/最……的），
# 而模型又把它拆成了多问——这种组合几乎一定是多跳的第一跳 + 第二跳。
# 刻意**不含**"哪个产品/哪个公司/哪家手机"这类比较型措辞，那些是真并列
# （"华为和苹果哪个产品更好"），收进来会误伤 D1 本来要保护的正常拆分。
_ENTITY_LOOKUP_PATTERNS = [
    r"是谁",
    r"^谁",
    r"哪位",
    r"哪个(?:部门|团队|岗位|员工|人员|负责人|项目|供应商|流程)",
    r"(?:最高|最低|最多|最少|最大|最小|最长|最短|排名第一|第一名)的",
]


def _detect_sub_query_dependency(
    rewritten_query: str, sub_queries: List[str]
) -> Optional[str]:
    """判断这组子查询之间是否**存在依赖**，存在则返回可读的原因串，否则 None。

    只有"任何一个子问题的答案都不需要用到另一个子问题的结果"才算独立。
    判不准时**偏向判成有依赖**——降级成单查询只是少一次并行检索（ReAct 仍可
    再查一轮），而错误地并行拆分会直接制造 F3 假并行 + F4 跨材料编造。
    """
    if len(sub_queries) <= 1:
        return None

    for sq in sub_queries:
        for pattern in _ANAPHORA_PATTERNS:
            if re.search(pattern, sq, flags=re.IGNORECASE):
                return f"子查询含回指（{pattern}）: {sq!r}"

    whole = rewritten_query or ""
    for pattern in _DEPENDENCY_CHAIN_PATTERNS:
        if re.search(pattern, whole):
            return f"整句含依赖链连接词（{pattern}）"

    for text in [whole, *sub_queries]:
        for pattern in _ENTITY_LOOKUP_PATTERNS:
            if re.search(pattern, text):
                return f"含实体识别问，属先定实体再查属性（{pattern}）: {text!r}"

    return None


def _finalize_sub_queries(rewritten_query: str, sub_queries: List[str]) -> List[str]:
    """子查询列表的统一后处理：清洗 -> 去重 -> D1 依赖判据。

    **不做扇出截断**——上限（D2）由 `workflow.py::_retrieve_multi` 这唯一一处
    截断点负责，因为只有那里才能把"被丢弃了哪几条"记进 trace。
    """
    cleaned = [sq.strip(" ,，。？！?！") for sq in sub_queries]
    cleaned = [sq for sq in cleaned if sq]

    deduped: List[str] = []
    seen = set()
    for sq in cleaned:
        if sq in seen:
            continue
        seen.add(sq)
        deduped.append(sq)

    fallback = rewritten_query.strip() if rewritten_query else ""
    if not deduped:
        return [fallback] if fallback else []

    reason = _detect_sub_query_dependency(rewritten_query, deduped)
    if reason is not None:
        logger.info("D1 sub-query dependency detected, downgraded to single query", extra={"reason": reason})
        return [fallback or deduped[0]]

    return deduped


# 拆分提示词里共用的 D1/D2 约束段。两个入口（analyze_query 的独立分析、
# analyze_and_route 的合并调用）必须用同一份文本，否则降级路径的行为会和
# 主路径不一致——这正是本项目踩过的"两条路径各写各的 prompt"那类坑。
_SUB_QUERY_SPLIT_RULES = f"""拆分成子查询之前，必须先判断子问题之间**有没有依赖**：
- **只有当每个子问题都能独立回答**——任何一个的答案都用不到另一个的结果——才允许拆分。
- **只要存在依赖，就不要拆分**，sub_queries 只放一个元素（保留完整问题）。
- 依赖的典型信号：后一问里出现"它/他/该/其/这个/上述/前者/后者"等指代前一问答案的词；
  或者必须先确定某个实体（"最高的是哪个""谁是……"）才能问它的属性；
  或者出现"先……再……""根据上面的结果"这类先后顺序表述。
- 最多拆成 {MAX_SUB_QUERY_FANOUT} 个子查询；超过 {MAX_SUB_QUERY_FANOUT} 个主题时只保留最重要的 {MAX_SUB_QUERY_FANOUT} 个。"""


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

# _needs_clarify_rule 用：真正需要澄清的模糊代词。
_VAGUE_PRONOUNS = ["它", "这个", "那个", "that", "it", "this", "上述", "上面"]
# "这个"/"那个" 紧跟着这些时间量词时是"这个月""那个星期"这类具体时间指代，
# 不是指代不明的代词——之前直接用子串匹配（"这个" in "这个月迟到了几次"）
# 会把这类句子也误判成需要澄清，实测"我这个月迟到了几次"这种完整、明确的
# 问题被 100% 命中误判（跟 LLM 模型大小无关，规则层面这一步就短路掉了，
# 根本没轮到 LLM 判断）。
_TEMPORAL_SUFFIXES_AFTER_ZHE_NA = ["月", "年", "星期", "周", "季度", "礼拜", "阵子", "次"]


def _has_vague_pronoun(text: str) -> bool:
    """检测文本里是否包含真正指代不明的模糊代词，排除"这个月""那个星期"
    这类"这个/那个 + 时间量词"的具体时间指代——两者字面上都含"这个"/"那个"
    这个子串，但语义上前者需要追问"你说的是哪个"，后者本身已经是完整信息。"""
    for token in _VAGUE_PRONOUNS:
        if token not in text:
            continue
        if token in ("这个", "那个"):
            idx = text.index(token)
            suffix_start = idx + len(token)
            following = text[suffix_start:suffix_start + 2]
            if any(following.startswith(suf) for suf in _TEMPORAL_SUFFIXES_AFTER_ZHE_NA):
                continue
        return True
    return False


# ============== 闲聊短路（_match_chitchat_intent 用） ==============
#
# 背景与实测依据：docs/review_2026-08-25/smalltalk_routing_regression.md。
# 21 条闲聊 × 2 次实测，误判率 81%——57% 被 `_needs_clarify_rule` 的
# `len < 4` 拦成固定澄清话术、24% 被 1.5b router 判成 tool+query_knowledge_hub
# 后撞上 `workflow.py` 的知识库空命中短路（"您的知识库里没有『你是谁』"）。
#
# 修法为什么是"白名单短路"而不是"放宽长度阈值"：做过 A/B 对照实验
# （把长度阈值从 <4 关到 <0，其余不动，同一批用例同一模型跑两遍）：
#   总误判率 81.0% -> 66.7%，但 kb_refusal 从 23.8% **涨到 28.6%**
#   （"你是谁" 从"澄清话术"变成"知识库里没有你是谁"，正是用户报告的那种
#    更有害的错法）；且放宽后 "早上好" 被判成 target_tool=query_attendance
#   （真的会去查考勤），只是脚本的静态判据把它算成了 answered。
# 结论：**单独放宽阈值只是把失败从一个桶挪进更坏的桶**，不采纳。改成在 LLM
# 之前用高精度白名单把闲聊摘出来，长度阈值**原样不动**——"他呢""多少"这类
# 真正模糊的短查询仍然被拦成 clarify，那个能力一点没丢。
#
# 2026-08-27 更新（Phase 1a，`docs/chitchat_intent_design.md`）：终判改成了
# 真正的第五类 "chitchat"，不再借用 "rag"。历史背景（为什么曾经借用 "rag"）：
# 旧版四分类里没有"直接对话回答"这一桶（根因三），补第五类要同时改
# `IntentDetectionResult` 的 Literal、`workflow.py` 的 `_route_after_intent`，
# 当时判定属于结构性改动、搁置不做，"rag" 是那时四个桶里唯一能走到"正常调
# LLM 生成回答"的临时借位。搁置的理由与代价见该设计文档 §1.2；本次已按
# 用户批准的方案 B+ 正式启动，"rag" 的语义收回到"只回答本次对话上传附件
# 本身的内容"。`_route_after_intent`（workflow.py）在 Phase 1a 阶段还没有
# `chitchat` 对应的路由分支，会落到默认分支 `return "retrieve"`——这与旧版
# "rag" 的走法逐字相同，Phase 1a 上线不改变任何用户可见行为
# （`docs/chitchat_intent_design.md` §2.5"关键设计"）。Phase 2 会给
# `chitchat` 接上真正的路由（直连 generate + 模板/受约束 prompt 两条 lane，
# 见 `src/ragent_backend/chitchat.py`）。

# 归一化后**整句精确相等**才命中（不是子串匹配）——子串匹配会把
# "报销流程你好像提过" 这类正常业务问句也吞掉。
_CHITCHAT_EXACT: set = {
    # 打招呼
    "你好", "您好", "你好呀", "你好啊", "您好呀", "哈喽", "哈啰", "嗨", "嘿",
    "hi", "hello", "hey", "halo", "helo",
    "早", "早啊", "早上好", "早安", "中午好", "下午好", "晚上好", "晚安",
    "好久不见", "在吗", "在不在", "在么", "在嘛", "在忙吗", "在忙么", "忙吗",
    # 礼貌用语 / 收到确认
    "谢谢", "谢谢你", "谢谢您", "多谢", "感谢", "感谢你", "感谢您", "太感谢了",
    "非常感谢", "非常感谢你的帮助", "谢谢你的帮助", "3q", "thx", "thanks",
    "thankyou", "thank you", "辛苦了", "辛苦", "辛苦你了", "麻烦你了",
    "厉害", "不错", "你真棒", "你太棒了", "好的", "好", "好滴", "行", "行的",
    "ok", "okay", "收到", "知道了", "明白了", "懂了", "没问题", "嗯", "嗯嗯",
    "哦", "好吧", "没事了", "没事",
    # 告别
    "再见", "拜拜", "拜", "bye", "byebye", "goodbye", "下次聊", "先这样",
    # 开场白（后面还没说正事，本身没有可检索的主题）
    "我想问个问题", "我想问个事", "有个问题想问你", "有个问题想请教你",
    "想请教你个问题", "有点事想请教你", "有点事想问你", "请教一下",
    "请教你一下", "打扰一下", "问你个问题", "问个问题",
}

# 问助手自身身份/能力/工作方式的元问题——这类穷举不完，用少量高精度正则，
# 全部锚定 `^...$`（整段匹配）且主语必须是"你/您"，不做子串匹配。
_CHITCHAT_PATTERNS: List[str] = [
    r"^(请问)?(你|您|你们)(是谁|叫什么名字|叫什么|是什么|是什么身份|什么来头)$",
    r"^(请问)?(你|您)是(ai|人工智能|机器人|真人|什么模型)(吗|么)?$",
    r"^(请问)?(你|您)(能|会|可以)(做|干)(什么|啥|嘛)$",
    r"^(请问)?(你|您)(会|能)(什么|啥)$",
    r"^(请问)?(你|您)(有|能提供)(什么|哪些)(功能|能力|服务|帮助)$",
    r"^(介绍|说明|讲讲|说说)一?下?(你自己|你|自己)$",
    r"^(请问)?(你|您)(用的|使用的|背后用的)?(是)?(什么|哪个)(模型|大模型|ai|技术|引擎)$",
    r"^(请问)?(你|您)是(怎么|如何)(工作|运作|运行|实现|训练)的$",
    r"^(请问)?(你|您)(的)?(回答|答案|结果)(准确|靠谱|可靠|正确)(吗|么|不)$",
    r"^(请问)?(你|您)(能|可以|会)(帮我)?(查|找|搜)(东西|点东西|资料|什么)?(吗|么)?$",
]

_CHITCHAT_COMPILED = [re.compile(p, re.IGNORECASE) for p in _CHITCHAT_PATTERNS]

# 保险栓：只要句子里出现任何一个业务主题词，无论其它部分多像寒暄，都**不**当
# 闲聊短路，一律交回给原来的判断路径。这是"闲聊白名单不能伤业务问答"这条底线
# 的兜底——比如"你好，年假多少天""你能帮我查一下报销流程吗"必须照常走检索。
_CHITCHAT_BUSINESS_VETO: List[str] = [
    "年假", "假期", "请假", "事假", "病假", "调休", "加班", "考勤", "打卡",
    "迟到", "早退", "报销", "发票", "出差", "差旅", "报修", "故障", "维修",
    "政策", "制度", "规定", "流程", "规章", "手册", "指南", "条例", "标准",
    "文档", "文件", "资料", "知识库", "合同", "协议", "工资", "薪资", "薪酬",
    "绩效", "社保", "公积金", "入职", "离职", "转正", "培训", "审批", "申请",
    "权限", "账号", "密码", "系统", "远程办公", "考核", "预算", "客户", "项目",
    "订单", "库存", "发货", "部门", "员工", "福利", "补贴", "签到", "排班",
]

# 归一化时剥掉的标点/语气符号（只做整句归一化，不改变句子内部的语义单元）
_CHITCHAT_STRIP_CHARS = " \t\r\n，,。.！!？?～~、；;：:…—-_\"'“”‘’（）()【】[]"

# 句尾纯语气词，剥掉不改变语义（"在不在呀" -> "在不在"）。**刻意不含**
# "吗/么/呢"——它们参与疑问句式判断，白名单正则里就写着 `(吗|么)`，
# 在这里剥掉会让"你的回答准确吗"反而匹配不上。
_CHITCHAT_TAIL_PARTICLES = ("呀", "啊", "哈", "嘞", "咯", "啦", "喔", "噢", "嘛", "唷", "耶")


def _normalize_chitchat_token(text: str) -> str:
    """归一化一个语义片段：去空白、去首尾标点、剥句尾语气词、转小写。"""
    token = text.strip(_CHITCHAT_STRIP_CHARS).strip().lower()
    while len(token) > 1 and token.endswith(_CHITCHAT_TAIL_PARTICLES):
        token = token[:-1].strip(_CHITCHAT_STRIP_CHARS)
    return token


def _is_chitchat_segment(segment: str) -> bool:
    """判断**单个**语义片段是不是闲聊。业务主题词一票否决。"""
    token = _normalize_chitchat_token(segment)
    if not token:
        return True  # 空片段（连续标点切出来的）不影响整句判定
    if any(w in token for w in _CHITCHAT_BUSINESS_VETO):
        return False
    if token in _CHITCHAT_EXACT:
        return True
    return any(p.match(token) for p in _CHITCHAT_COMPILED)


def _match_chitchat_intent(query: str) -> Optional[IntentResult]:
    """闲聊白名单短路：整句（按标点切成的**每一个**片段）都是寒暄/致谢/告别/
    问助手自身身份能力时，直接判成 chitchat —— 不调 LLM、不进知识库检索工具。

    与 `_needs_clarify_rule` 的分工（这是本函数存在的关键）：本函数只认**白名单
    里那些确定无疑的寒暄**，`_needs_clarify_rule` 继续负责拦真正模糊的短查询。
    两者都作用于短句，靠"是否在白名单里"区分，不靠字数：
      "你好"(2 字) -> 闲聊短路 -> 正常回答
      "他呢"(2 字) -> 不在白名单 -> 照旧被长度/模糊代词规则拦成 clarify
    所以调用顺序必须是**闲聊检查在澄清检查之前**，且长度阈值保持不变。

    复合句按标点拆开逐段判定，要求**每一段都是闲聊**才短路：
      "你好，你是谁"      -> ["你好", "你是谁"]     两段都是 -> 短路
      "你好，年假多少天"   -> ["你好", "年假多少天"] 第二段带业务词 -> 不短路
    """
    cleaned = " ".join((query or "").split())
    if not cleaned:
        return None
    # 太长的句子即使每段都像寒暄，也更可能是夹带了正事，交给 LLM 更稳妥
    if len(cleaned) > 30:
        return None

    segments = [s for s in re.split(r"[，,。.！!？?；;～~\n]+", cleaned) if s.strip()]
    if not segments:
        return None
    if not all(_is_chitchat_segment(seg) for seg in segments):
        return None

    return IntentResult(
        intent_type="chitchat",
        rewritten_query=cleaned,
        confidence=0.9,
        need_clarify=False,
        reasoning=(
            "规则短路：整句命中闲聊白名单（寒暄/致谢/告别/问助手自身身份能力），"
            "不查企业知识库、不走澄清话术，直接交给生成节点正常对话回答"
        ),
    )


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
    # === Step 0: 闲聊白名单短路（硬规则，不经过 LLM）===
    # 必须排在 Step 1 澄清检查**之前**："你好""谢谢"这类 2~3 字的寒暄会被
    # `_needs_clarify_rule` 的长度阈值 100% 拦成澄清话术（实测占全部闲聊误判
    # 约四成），而那道阈值本身要留着拦"他呢""多少"这种真正模糊的短查询，
    # 不能放宽。两者的分工见 `_match_chitchat_intent` 的 docstring。
    chitchat_intent = _match_chitchat_intent(rewritten_query)
    if chitchat_intent is not None:
        return chitchat_intent

    # === Step 1: 澄清检查（硬规则，不经过 LLM）===
    clarify_override = _needs_clarify_rule(rewritten_query)
    if clarify_override is not None:
        return clarify_override

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
        except Exception:
            logger.exception("LLM-based intent detection failed, falling back to rule-based")

    # === Step 3: 规则 fallback ===
    return _detect_intent_rule_based(rewritten_query, available_tools or [], available_workflows or [])


def _format_tools_text(available_tools: List[Dict[str, Any]]) -> str:
    """把 OpenAI function-calling schema 列表格式化成 prompt 里的工具清单文本。
    `_detect_intent_with_llm` 和合并版 `analyze_and_route` 共用同一份格式，
    两边 prompt 措辞不一致会让"可用工具列表"这段的措辞漂移，不好维护。"""
    if not available_tools:
        return "（当前无可用的外部工具）"
    lines = []
    for t in available_tools:
        func = t.get("function") or {}
        name = func.get("name") or t.get("name", "unknown")
        desc = func.get("description") or t.get("description", "无描述")
        lines.append(f"- {name}: {desc[:80]}")
    return "\n".join(lines)


def _format_workflows_text(available_workflows: List[Dict[str, Any]]) -> str:
    if not available_workflows:
        return "（当前无可用的流程模板）"
    lines = [
        f"- {w.get('workflow_type')}: {w.get('display_name')} — {(w.get('description') or '')[:80]}"
        for w in available_workflows
    ]
    return "\n".join(lines)


_INTENT_CLASSIFY_RULES = """分类规则：
- "clarify": 查询模糊、不完整、连主题都看不出来，必须先问用户才能回答（比如只有代词、
  或者短到不知道在问什么）；如果问题主题清楚，即使细节不全，也不要选这个，选 "rag" 或
  "tool"，检索/工具没查到结果自然会有专门的空结果提示，不需要在分类这一步替用户猜
- "rag": 查询是关于用户在当前这次对话里自己上传的文件/附件本身的内容（比如"总结一下
  我刚上传的这份文档""这份 PDF 第二页说了什么"），只搜这次对话上传的文件，不搜企业
  知识库
- "tool": 查询明确需要调用外部工具/查数据，包括查企业知识库、文档、规章制度、流程
  说明（用 query_knowledge_hub 工具，不要跟上面的 "rag" 搞混——员工手册、报销制度、
  入职指南这类"公司内部资料"问题都归这一类，不是 "rag"）、搜网页、查天气、算数、
  查考勤等
- "workflow": 用户想发起一个业务流程/申请（如报修、请假、出差、报销），不是在问知识、不是在查资料"""


def _reconcile_intent_result(
    result: Any,
    rewritten_query: str,
    available_tools: List[Dict[str, Any]],
    available_workflows: List[Dict[str, Any]],
) -> IntentResult:
    """结构化 LLM 输出 -> IntentResult 的公共后处理：校验 target_tool/workflow_type
    是否真实存在、修正小模型常见的自相矛盾、应用置信度阈值兜底。

    `result` 接受 `IntentDetectionResult` 或 `QueryAnalysisAndIntentResult`
    ——两者在意图相关字段上完全同名（duck typing，不需要共同基类），
    `_detect_intent_with_llm`（旧的两次调用路径）和 `analyze_and_route`
    （合并成一次调用的新路径）共用这同一套校验，两条路径的分类行为
    不会因为各写一份校验逻辑而慢慢跑偏。"""
    # available_tools 实际传入的是 OpenAI function-calling schema（见
    # workflow.py 的 to_openai_tools()）：{"type": "function", "function":
    # {"name": ...}}，name 嵌在 function 里，不是顶层字段——之前直接
    # t.get("name") 永远拿到 None，导致 available_tool_names 恒为空集，
    # target_tool 无论 LLM 判断得多准都会被这里清空。同时兼容顶层 name
    # （规则 fallback 等场景可能直接传扁平字典）。
    available_tool_names = {
        (t.get("function") or {}).get("name") or t.get("name", "") for t in available_tools
    }
    target_tool = result.target_tool
    if target_tool and target_tool not in available_tool_names:
        target_tool = None  # 让子图自己选

    available_workflow_types = {w.get("workflow_type", "") for w in available_workflows}
    workflow_type = result.workflow_type
    if workflow_type and workflow_type not in available_workflow_types:
        workflow_type = None

    # 复现过的真实误判："远程办公的申请流程是什么？"被小模型判成
    # workflow_type=business_trip——四个流程模板的 display_name 里全都带
    # "申请"这个字，用户查询只要沾上这个字就容易被带偏，跟具体是哪个模板
    # 无关。Step 1.5 的规则短路（_match_workflow_action_intent）已经用"动作型
    # 前缀 + 无疑问句提示词"这个组合精确区分过"我想请假"和"请假标准是什么"，
    # 这里复用同一组信号对 LLM 的结构化输出做事后校验：没有动作型前缀、却
    # 带明显疑问句式时，不采信这次 workflow_type，避免把明摆着在问政策的
    # 疑问句发起成一个用户根本没打算触发的业务流程。
    if workflow_type and not any(p in rewritten_query for p in _WORKFLOW_ACTION_PREFIXES) and any(
        w in rewritten_query for w in _WORKFLOW_QUESTION_CUE_WORDS
    ):
        workflow_type = None

    intent_type = result.intent_type
    # 小模型（如 qwen2.5:7b）的结构化输出偶尔会自相矛盾：workflow_type 已经准确
    # 判断出来了（reasoning 里也明确点出是请假/报修等申请），但 intent_type 却
    # 没有同步设成 "workflow"（常见误标成 "rag" 或 "clarify"）。workflow_type
    # 是从受限枚举（可用流程列表）里选出来的，比自由文本的 intent_type 更可信，
    # 出现这种矛盾时以 workflow_type 为准，否则用户明确说"我想请假"也会被当成
    # 知识库问答处理。
    if workflow_type and intent_type != "workflow":
        intent_type = "workflow"

    # 同样的自相矛盾也会出现在 tool 上：target_tool 已经命中了一个真实存在的
    # 工具（reasoning 里往往也点名了具体该调哪个工具），但 intent_type 被标成
    # 了 "rag" 或别的。target_tool 是从受限枚举（可用工具列表）里选出来的，比
    # intent_type 更可信，出现矛盾时以 target_tool 为准（workflow 优先级更高，
    # 已经在上面处理过，这里不覆盖 workflow）。
    if target_tool and intent_type not in ("tool", "workflow"):
        intent_type = "tool"

    # workflow 意图但没能对应到一个合法类型（包括被上面的疑问句守卫剔除），
    # 退回 "tool" 而不是 "rag"——_route_after_intent 里 "rag" 只会走
    # "retrieve" 节点，那条路径只搜这次对话自己上传的附件，多数场景压根没
    # 传过文件，查到 0 条后 generate 会凭训练知识编一段看似合理的回答，用户
    # 完全看不出这不是知识库查出来的（workflow.py `_route_after_intent`
    # 里 "clarify" 自相矛盾分支已经踩过同一个坑、改成交给 tool_subgraph，
    # 这里是同一类"分类器判不准"情形，沿用同一个更安全的默认：交给工具
    # 子图，让它按可用工具列表自己再判断一次要不要查企业知识库）。
    if intent_type == "workflow" and not workflow_type:
        return IntentResult(
            intent_type="tool",
            rewritten_query=rewritten_query,
            confidence=0.6,
            target_tool=target_tool,
            reasoning="LLM 判断为 workflow 意图，但未能匹配到合法的流程类型（或被疑问句守卫剔除），回退到 tool 交给工具子图判断",
        )

    # 置信度阈值
    if result.confidence < 0.5:
        return IntentResult(
            intent_type="rag",
            rewritten_query=rewritten_query,
            confidence=0.6,
            reasoning=f"LLM 分类置信度过低({result.confidence:.2f})，默认回退到 rag",
        )

    return IntentResult(
        intent_type=intent_type,
        rewritten_query=rewritten_query,
        confidence=result.confidence,
        target_tool=target_tool,
        tool_args=result.tool_args_preview,
        workflow_type=workflow_type,
        need_clarify=result.need_clarify,
        clarify_prompt=result.clarify_prompt,
        reasoning=result.reasoning,
    )


async def _detect_intent_with_llm(
    rewritten_query: str,
    llm,
    available_tools: List[Dict[str, Any]],
    available_workflows: List[Dict[str, Any]],
) -> IntentResult:
    """使用 LLM 做结构化意图四分类（独立一次调用；合并版见 analyze_and_route）。"""
    prompt = f"""你是意图分类专家。请根据用户查询、可用工具列表和可用流程模板列表，判断用户的真实意图。

{_INTENT_CLASSIFY_RULES}

可用工具列表：
{_format_tools_text(available_tools)}

可用流程模板列表：
{_format_workflows_text(available_workflows)}

用户查询：{rewritten_query}

请输出结构化分类结果，包含 intent_type、confidence、reasoning 等字段。
注意：target_tool 必须从可用工具列表中选择，workflow_type 必须从可用流程模板列表中选择，都不能编造不存在的名字。"""

    structured_llm = llm.with_structured_output(IntentDetectionResult, method="json_mode")
    result: IntentDetectionResult = await structured_llm.ainvoke([HumanMessage(content=prompt)])
    return _reconcile_intent_result(result, rewritten_query, available_tools, available_workflows)


def _needs_clarify_rule(rewritten_query: str) -> Optional[IntentResult]:
    """Step 1 的模糊代词澄清检查（硬规则，不经过 LLM）——独立成函数，供
    `detect_intent`（旧路径，检查的是待分类的 rewritten_query）和
    `analyze_and_route`（合并路径，检查 LLM 自己重写出来的 rewritten_query，
    当"后置安全网"用，防止合并调用里模型自己也没把指代消解干净）两处复用。

    这里的 `len < 4` 阈值是**刻意保留**的（做过 A/B 实测，放宽它会让更有害的
    kb_refusal 从 23.8% 涨到 28.6%，详见 `_match_chitchat_intent` 上方的说明）：
    它的职责是拦"他呢""多少"这类真正模糊的短查询。寒暄类短句由排在本函数
    **之前**的 `_match_chitchat_intent` 先摘走，不会再走到这里。"""
    has_vague = _has_vague_pronoun(rewritten_query)
    if len(rewritten_query.strip()) < 4 or (has_vague and len(rewritten_query) < 10):
        return IntentResult(
            intent_type="clarify",
            rewritten_query=rewritten_query,
            confidence=0.35,
            need_clarify=True,
            clarify_prompt="请补充更具体的信息，例如具体的产品名、文档名或业务指标。",
            reasoning="查询过短或包含模糊代词，需要澄清",
        )
    return None


async def analyze_and_route(
    query: str,
    messages: list,
    llm,
    available_tools: Optional[List[Dict[str, Any]]] = None,
    available_workflows: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[str, List[str], IntentResult]:
    """合并版入口：一次结构化 LLM 调用同时完成"指代消解+子查询拆分"
    （原 analyze_query）和"四分类"（原 detect_intent），取代原来两次串行往返
    ——本地小模型每次推理都是几秒级的，省一次往返是当前这条链路里最直接的
    延迟优化（`_intent_node` 实测能省小几秒，具体见 workflow.py 调用点的说明）。

    正确性上跟旧的两函数路径保持等价，靠两点保证：
    1. Step 1.5（动作型前缀 + 流程关键词规则短路）挪到本函数最前面、在清洗后
       的原始 query 上做，命中直接返回、完全不碰 LLM——比旧路径（`detect_intent`
       内部才做这个短路，但那时候 analyze_query 这次 LLM 调用已经先付过了）
       更省，代价是极少数"动作型前缀里刚好带指代"的边界句子拿不到指代消解，
       可接受（工作流意图后续会再走一轮结构化字段收集，不依赖这里的重写质量）。
    2. Step 1（模糊代词澄清检查）必须在拿到（LLM 自己产出的）rewritten_query
       之后才能做，所以挪到合并调用返回之后，当一层后置安全网，跟旧路径的
       检查时机（也是拿到 rewritten_query 之后）等价。
    3. Step 0（闲聊白名单短路）在 LLM 之前和之后各做一次：之前那次针对原始
       query（命中就零 LLM 调用），之后那次针对 LLM 自己重写出来的
       rewritten_query（重写可能把"嗨~"整理成"你好"这类白名单形式），
       两次都排在澄清检查前面，理由见 `_match_chitchat_intent` 的 docstring。

    合并调用本身失败（网络错误/JSON 解析失败/schema 不匹配）时，整个函数
    降级回旧的两次调用路径（`analyze_query` + `detect_intent`），不会比合并
    之前更脆弱，只是退回原来的延迟。
    """
    cleaned = " ".join(query.split())
    available_tools = available_tools or []
    available_workflows = available_workflows or []

    # Step 1.5：规则短路，命中则零 LLM 调用（见上面 docstring 第 1 点）
    action_intent = _match_workflow_action_intent(cleaned, available_workflows)
    if action_intent is not None:
        return cleaned, [cleaned], action_intent

    # Step 0：闲聊白名单短路，同样零 LLM 调用（见上面 docstring 第 3 点）。
    # 放在工作流动作短路之后：真要发起流程的句子（"我想请假"）优先走流程，
    # 白名单本身也不会命中它们（带业务词一票否决）。
    chitchat_intent = _match_chitchat_intent(cleaned)
    if chitchat_intent is not None:
        return cleaned, [cleaned], chitchat_intent

    if llm is None:
        rewritten = cleaned
        sub_queries = _fallback_split(cleaned)
        intent = _detect_intent_rule_based(rewritten, available_tools, available_workflows)
        return rewritten, sub_queries, intent

    try:
        recent = messages[-4:] if messages else []
        history_lines = []
        for m in recent:
            role = "User" if m.type == "human" else "Assistant"
            content = str(getattr(m, "content", "")).strip()
            if content:
                history_lines.append(f"{role}: {content}")
        history_text = "\n".join(history_lines)

        prompt = f"""你是一个查询分析与意图分类助手。请依次完成两件事：

第一步 —— 查询重写与拆分：
1. 消除所有代词和指代（如"它"、"这个"、"that"、"这个文档"、"上面说的"、"前者"等），替换为对话历史中提到的具体实体。
2. 如果当前问题包含多个**彼此独立**的并列主题（如多个城市、多个产品、多个时间段的比较），即使没有连词也必须拆分成可独立执行的子查询列表；只有单一主题时 sub_queries 只放一个元素。
3. 每个子查询必须完整、无歧义、不依赖上下文即可理解。

{_SUB_QUERY_SPLIT_RULES}

拆分示例：
- "北京上海杭州的天气怎么样" -> 拆成 3 个（三个城市互不影响，独立）
- "销售额最高的部门是哪个，该部门今年的招聘预算是多少" -> **不拆**（不先知道是哪个部门就查不了"该部门"的预算，第二问依赖第一问）
- "公司的年假制度是什么，它的审批人是谁" -> **不拆**（"它"回指前一问的答案）

第二步 —— 基于第一步重写后的查询，判断用户的真实意图：
{_INTENT_CLASSIFY_RULES}

"clarify" 判断要从严：只有当问题本身残缺到没法执行检索或工具调用时才选
"clarify"（比如只有代词没有主语、或者短到不知道在问什么）。像"XX怎么办""XX
流程是什么"这类问题，即使没写清楚是哪个部门/哪家公司，也是可以直接拿去知识库
检索、期待检索结果里包含相关制度说明的完整问题，应该判成 "rag" 或 "tool"，
不要因为细节不够具体就选 "clarify"——检索没查到相关内容会有专门的空结果提示，
不需要在分类这一步就替用户猜"你是不是想问不清楚"。

可用工具列表：
{_format_tools_text(available_tools)}

可用流程模板列表：
{_format_workflows_text(available_workflows)}

对话历史：
{history_text}

当前问题：{cleaned}

请直接输出 JSON 对象（rewritten_query/sub_queries/intent_type/confidence/target_tool/tool_args_preview/workflow_type/need_clarify/clarify_prompt/reasoning），不要添加任何解释或 Markdown 格式。
注意：target_tool 必须从可用工具列表中选择，workflow_type 必须从可用流程模板列表中选择，intent_type 的判断要基于你自己重写后的查询，都不能编造不存在的名字。"""

        structured_llm = llm.with_structured_output(QueryAnalysisAndIntentResult, method="json_mode")
        result: QueryAnalysisAndIntentResult = await structured_llm.ainvoke([HumanMessage(content=prompt)])

        rewritten_query = result.rewritten_query or cleaned
        # 清洗 + 去重 + D1 依赖判据（有依赖则降级为单查询，见 _finalize_sub_queries）
        sub_queries = _finalize_sub_queries(
            rewritten_query, result.sub_queries or [rewritten_query]
        ) or [rewritten_query]

        # Step 0 后置安全网：必须在下面的澄清检查之前（见 docstring 第 3 点）
        chitchat_override = _match_chitchat_intent(rewritten_query)
        if chitchat_override is not None:
            return rewritten_query, [rewritten_query], chitchat_override

        # Step 1 后置安全网（见上面 docstring 第 2 点）
        clarify_override = _needs_clarify_rule(rewritten_query)
        if clarify_override is not None:
            return rewritten_query, sub_queries, clarify_override

        intent = _reconcile_intent_result(result, rewritten_query, available_tools, available_workflows)
        return rewritten_query, sub_queries, intent

    except Exception:
        logger.exception("merged analyze_and_route failed, falling back to two-call path")
        analysis = await analyze_query(query=query, messages=messages, llm=llm)
        intent = await detect_intent(
            rewritten_query=analysis.rewritten_query,
            llm=llm,
            available_tools=available_tools,
            available_workflows=available_workflows,
        )
        sub_queries = analysis.sub_queries
        if intent.intent_type in ("tool", "workflow") or intent.need_clarify:
            sub_queries = [intent.rewritten_query]
        return analysis.rewritten_query, sub_queries, intent


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
