"""
Tool Subgraph — 工具子智能体的 LangGraph 实现。

拓扑：
    START → think_node → router_node
                             │
                ┌────────────┼────────────┐
                ▼            ▼            ▼
           tool_node    summarize_node   END (max_iter)
                │            │
                └────→ think_node (循环，最多 N 轮)

关键设计：
- 独立 State Schema，internal_messages 不返回主图
- 只输出 tool_summary + tool_execution_trace
- 主图通过 tool_subgraph 节点调用，像普通节点一样使用
"""

from __future__ import annotations

import time
from datetime import date
from typing import Any, Callable, Dict, List, Literal, Optional

from langchain_core.messages import HumanMessage, AIMessage, ToolMessage, SystemMessage
from langgraph.graph import StateGraph, START, END

from src.tool_agent.state import ToolSubgraphState
from src.tool_agent.tool_registry import ToolRegistry
from src.tool_agent.unified_tool import ToolDecision, ToolResult


# =============================================================================
# Builder
# =============================================================================

def build_tool_subgraph(
    tool_registry: ToolRegistry,
    llm: Any,
    max_iterations: int = 5,
    emit_trace: Optional[Callable[[str, str, str, Optional[Dict[str, Any]]], None]] = None,
) -> Any:
    """构建工具子智能体子图。

    Args:
        tool_registry: 工具注册表
        llm: LangChain LLM 实例（需要支持 with_structured_output）
        max_iterations: ReAct 最大迭代次数
        emit_trace: 可选的埋点回调，签名同 RAGWorkflow._emit_trace(node, step,
            status, payload)——传入的话，think/tool/summarize 每一步的起止都会
            推到 TracePanel，"工具调用"这个节点原来只有整体 start/end（见
            workflow.py `_tool_subgraph_node` 旁的说明），看不出耗时具体花在
            哪一轮的 think 还是 tool 还是 summarize；不传（比如独立跑的 MCP
            server 场景）就是纯 no-op，子图行为不受影响。这个子图本身在
            RAGWorkflow 构造时只编译一次，不是每次请求都重新 build，所以不能
            直接闭包捕获某一次请求的 trace_queue，只能接收调用方（RAGWorkflow
            实例方法）作为回调，由调用方自己在每次请求时决定往哪个队列推——
            具体见 workflow.py 里 `self._emit_trace` 的传参方式。

    Returns:
        编译后的 LangGraph（可作为子图节点加入主图）
    """
    def _trace(node: str, step: str, status: str = "running", payload: Optional[Dict[str, Any]] = None) -> None:
        if emit_trace is not None:
            emit_trace(node, step, status, payload)

    # ------------------------------------------------------------------
    # think_node: LLM 决策下一步动作
    # ------------------------------------------------------------------
    async def think_node(state: ToolSubgraphState) -> Dict[str, Any]:
        """分析当前上下文，决定调用工具还是结束。"""
        iteration = state.get("iteration_count", 0)
        query = state["query"]
        target_tool = state.get("target_tool")
        available_tools = state.get("available_tools", [])
        internal_messages = state.get("internal_messages", [])
        
        # 构建 messages（百炼 API 要求必须有 user 角色消息）
        if iteration == 0:
            system_prompt = _build_system_prompt(query, available_tools, target_tool)
            messages = [
                SystemMessage(content=system_prompt),
                HumanMessage(content=f"用户查询：{query}\n\n请根据上述查询和可用工具列表，做出工具调用决策。直接输出 JSON。"),
            ]
        else:
            # 非第一轮：已有工具执行结果，需要 user 消息触发 LLM 响应
            messages = list(internal_messages)
            messages.append(HumanMessage(content="基于上述工具执行结果，请决定下一步行动（继续调用工具或结束）。直接输出 JSON。"))
        
        # LLM 结构化决策
        _trace("tool_subgraph", f"think[{iteration}]", "running", {"目的": "决定是否调用工具/该调哪个"})
        try:
            structured_llm = llm.with_structured_output(ToolDecision, method="json_mode")
            decision: ToolDecision = await structured_llm.ainvoke(messages)
        except Exception as e:
            # 结构化失败，默认 finish
            decision = ToolDecision(
                thought=f"结构化决策失败: {e}，默认结束",
                action="finish",
                reasoning="fallback due to structured output error",
            )
        _trace("tool_subgraph", f"think[{iteration}]", "success", {"action": decision.action, "thought": decision.thought[:100]})

        # 记录 AI 的思考过程到 internal_messages
        ai_msg = AIMessage(content=f"Thought: {decision.thought}\nAction: {decision.action}")
        
        update: Dict[str, Any] = {
            "internal_messages": messages + [ai_msg],
            "iteration_count": iteration + 1,
        }
        
        if decision.action == "call_tool" and decision.tool_calls:
            update["tool_calls"] = decision.tool_calls
            update["next_node"] = "tool"
        else:
            update["next_node"] = "summarize"
        
        return update
    
    # ------------------------------------------------------------------
    # tool_node: 执行工具调用
    # ------------------------------------------------------------------
    async def tool_node(state: ToolSubgraphState) -> Dict[str, Any]:
        """执行 LLM 决定的工具调用。"""
        tool_calls = state.get("tool_calls", [])
        internal_messages = state.get("internal_messages", [])
        failed_tools = list(state.get("failed_tools", []))
        tool_results = list(state.get("tool_results", []))
        tool_execution_trace = list(state.get("tool_execution_trace", []))
        
        new_messages = []
        
        user_id = state.get("user_id")

        for call in tool_calls:
            name = call.get("name", "")
            args = dict(call.get("arguments") or call.get("args") or {})
            if user_id:
                # 服务端注入，覆盖任何 LLM 可能编造的同名参数——user_id 从不
                # 出现在暴露给 LLM 的 tool schema 里，LLM 没有机会"正常"提供它，
                # 这里的覆盖只是防御 LLM 瞎编的边界情况。
                args["user_id"] = user_id

            t0 = time.monotonic()
            _trace("tool_subgraph", f"tool[{name}]", "running", {"args": {k: v for k, v in args.items() if k != "user_id"}})
            try:
                result: ToolResult = await tool_registry.execute(name, args)
            except Exception as e:
                result = ToolResult.from_error(str(e))

            latency_ms = (time.monotonic() - t0) * 1000
            _trace("tool_subgraph", f"tool[{name}]", "success" if result.success else "error", {
                "latency_ms": round(latency_ms), "success": result.success,
            })
            result.latency_ms = latency_ms
            
            # 记录 ToolMessage（LangGraph 格式）
            tool_msg = ToolMessage(
                content=result.output,
                name=name,
                tool_call_id=f"{name}_{int(t0*1000)}",
            )
            new_messages.append(tool_msg)
            
            # 记录结果
            tool_results.append({
                "name": name,
                "args": args,
                "output": result.output,
                "success": result.success,
                "error": result.error,
                "latency_ms": latency_ms,
            })
            
            # 记录 trace——collection 顺手带上，供 workflow.py 的 _generate_node
            # 提取"这次回答用了哪个知识库"，UI 上展示来源角标用。MCPToolResponse
            # 自带 to_dict()，adapters.py 的 wrap_function_tool 优先用它填
            # result.structured_data（而不是拿 .metadata 摊平），实际形状是
            # {"content":..., "structuredContent": {"metadata": {"collection": ...,
            # "result_count": ...}, "isEmpty": ...}}，不是顶层 "collection"，取的
            # 时候要按这个嵌套结构来。
            #
            # 检索/委托查询"这个 collection 确实查过"和"这个 collection 里确实有
            # 命中内容"是两件事——命中 0 条（isEmpty=True 或 result_count=0）时
            # MCPToolResponse.metadata 里的 collection 字段照样会填（见
            # response_builder.py _build_empty_response/_build_metadata），如果
            # 这时候还把它算进 kb_sources，UI 上的知识库来源角标会挂在一个"其实
            # 什么都没查到、回答是模型自己编的"的回复上，看起来像是有依据实际上
            # 没有——发现的真实案例：企业员工问了一个只有平台自己 IT 部知识库才有
            # 的问题，本企业知识库检索不到任何相关内容，模型转而编了一段通用建议，
            # 角标却照样显示"通用知识库"，误导用户以为这是查出来的。只有命中过
            # 真实结果的 collection 才计入 kb_sources。
            # "全库混合召回"（query_knowledge_hub._execute_local_multi）合并了
            # 多个 collection 的候选结果再统一重排，最终结果可能来自不止一个
            # 部门知识库，response_builder._build_metadata 相应地会在 metadata
            # 里多写一个 "collections"（复数，list）——优先取这个；没有就说明
            # 是老的单 collection 路径（委托模式/显式指定 collection），退回单
            # 数 "collection" 包一层 list，两条路径最终都归一成 list，下面统一
            # 处理。
            structured = result.structured_data or {}
            structured_content = structured.get("structuredContent") or {}
            result_metadata = structured_content.get("metadata") or {}
            attempted_collections = result_metadata.get("collections")
            if not attempted_collections:
                single = result_metadata.get("collection")
                attempted_collections = [single] if single else []
            # "查过哪些库"（attempted_collections，不管有没有查到东西，_generate_node
            # 用来在"知识库里确实没有"这句话里报出具体库名）和"哪些库真的查到了
            # 有用内容"（collections，isEmpty/result_count=0 时清空，UI 来源角标
            # 只认这个）是两件事，都要留着，缺一个都不够 _generate_node 组装
            # "抱歉模板"用。
            collections = [] if (structured_content.get("isEmpty") or not result_metadata.get("result_count")) else attempted_collections
            tool_execution_trace.append({
                "tool_name": name,
                "args": args,
                "result_preview": result.output[:200] if result.output else "",
                "latency_ms": latency_ms,
                "success": result.success,
                "iteration": state.get("iteration_count", 0),
                "timestamp": time.time(),
                "collections": collections,
                "attempted_collections": attempted_collections,
            })
            
            if not result.success:
                failed_tools.append(name)
        
        return {
            "internal_messages": new_messages,
            "tool_results": tool_results,
            "failed_tools": failed_tools,
            "tool_execution_trace": tool_execution_trace,
        }
    
    # ------------------------------------------------------------------
    # summarize_node: 整理工具执行结果为结构化摘要
    # ------------------------------------------------------------------
    async def summarize_node(state: ToolSubgraphState) -> Dict[str, Any]:
        """将多轮工具执行结果整理为结构化摘要。"""
        tool_results = state.get("tool_results", [])
        failed_tools = state.get("failed_tools", [])
        
        # 如果没有任何工具结果（如 LLM 直接 finish），返回空摘要
        if not tool_results:
            return {
                "tool_summary": "",
                "tool_execution_trace": state.get("tool_execution_trace", []),
            }

        # ACL 拒绝的结果必须原样透传，不能交给 LLM "总结"——本地模型总结时
        # 会把"无权访问"这种明确的拒绝，改写成一段看似合理但完全编造的
        # 通用回答（真实观察到的现象：模型编了一套"合同审批流程"出来），
        # 用户完全看不出自己其实是被权限挡住了。
        denial_outputs = [
            r["output"] for r in tool_results
            if r.get("output", "").startswith("## 无权访问")
        ]
        if denial_outputs:
            return {
                "tool_summary": "\n\n".join(denial_outputs),
                "tool_execution_trace": state.get("tool_execution_trace", []),
            }

        # 不再单独用 LLM 总结工具结果——这一步原来是"总结" + 后面 generate 节点
        # 再"基于总结生成回答"两次串行 LLM 往返，实测这一步单独就要 7 秒多。
        # generate 节点本来就是一次 LLM 调用，本来就在"综合信息、组织语言"，
        # 直接把工具原始结果（用户建议的做法，见下面 _build_fallback_summary
        # 已经做好的按工具分段 + 每条截 800 字的格式化）交给它一次做完，不需要
        # 先经过另一次 LLM 改写成"摘要"再改写成"回答"——多数 RAG 系统检索后
        # 也是直接把原始片段交给生成模型，不会先摘要一遍。ACL 拒绝分支（上面）
        # 原样透传不受影响；如果以后发现 generate 面对未摘要的原始结果质量
        # 下降（比如多个工具结果、内容很长时），再考虑恢复这一步或者调整
        # generate 的 prompt，不要重新加回一次独立的 LLM 摘要调用。
        _trace("tool_subgraph", "summarize", "running", {"tool_result_count": len(tool_results)})
        summary = _build_fallback_summary(tool_results, failed_tools)
        _trace("tool_subgraph", "summarize", "success", {"summary_length": len(summary)})

        return {
            "tool_summary": summary,
            "tool_execution_trace": state.get("tool_execution_trace", []),
        }
    
    # =================================================================
    # 构建图
    # =================================================================
    graph = StateGraph(ToolSubgraphState)
    
    graph.add_node("think", think_node)
    graph.add_node("tool", tool_node)
    graph.add_node("summarize", summarize_node)
    
    # 边：think 直接路由到 tool 或 summarize（根据 think_node 写入的 next_node）
    graph.add_edge(START, "think")
    graph.add_conditional_edges(
        "think",
        lambda state: "summarize" if state.get("iteration_count", 0) >= state.get("max_iterations", max_iterations) else state.get("next_node", "summarize"),
        {"tool": "tool", "summarize": "summarize"}
    )
    def _route_after_tool(state: ToolSubgraphState) -> str:
        """工具执行完之后，要不要再问模型一次"接下来怎么办"，还是直接进
        summarize——原来无条件回 think，实测这一轮 LLM 往返在"查一次知识库/
        考勤就够了"这种最常见的场景里，模型 100 次里 99 次都是回答 finish，
        白白多等几秒。

        只在满足下面全部条件时才跳过这轮确认，缩小到最没有歧义的场景，工具
        链式调用（先查 A 再拿 A 的结果去查 B）一律不碰，交给模型自己判断：
        1. 主图（意图分类）已经指定了唯一的目标工具（target_tool）——说明
           这本来就是"查一个东西"的单步意图，不是模型自己在探索该用哪个/
           要不要链式调用别的工具。
        2. 这一轮只调了一个工具，且正是这个目标工具——没有并列调用多个工具，
           没有跑偏调别的工具。
        3. 这个工具调用成功了——失败了必须让模型看到错误信息自己决定要不要
           换一种方式重试，不能替它做主直接收尾。
        """
        if state.get("iteration_count", 0) >= state.get("max_iterations", max_iterations):
            return "summarize"

        target_tool = state.get("target_tool")
        tool_calls = state.get("tool_calls") or []
        tool_results = state.get("tool_results") or []

        if (
            target_tool
            and len(tool_calls) == 1
            and tool_calls[0].get("name") == target_tool
            and tool_results
            and tool_results[-1].get("name") == target_tool
            and tool_results[-1].get("success")
        ):
            # 不在这里调 _trace：LangGraph 对同步的条件边路由函数走的是线程池
            # execute（跟其他节点的 async def 不一样），这个函数体运行时没有
            # 当前线程的 event loop，_trace 内部的 asyncio.create_task 会直接
            # 抛 RuntimeError: no running event loop，整条请求都会跟着崩掉
            # （已经实测踩过一次）。跳过与否从 trace 时间线上"tool 之后有没有
            # 紧跟着一次 think[N]"就能看出来，不额外埋点也不影响可观测性。
            return "summarize"

        return "think"

    graph.add_conditional_edges(
        "tool",
        _route_after_tool,
        {"think": "think", "summarize": "summarize"},
    )
    graph.add_edge("summarize", END)

    return graph.compile()


# =============================================================================
# Prompt Builders
# =============================================================================

def _build_system_prompt(
    query: str,
    available_tools: List[Dict[str, Any]],
    target_tool: Optional[str] = None,
) -> str:
    """构建 think_node 的系统提示。"""
    
    tools_desc = ""
    if available_tools:
        lines = []
        for t in available_tools:
            # OpenAI schema: {"type": "function", "function": {"name": ..., "description": ...}}
            func = t.get("function") or {}
            name = func.get("name") or t.get("name", "unknown")
            desc = func.get("description") or t.get("description", "无描述")
            lines.append(f"- {name}: {desc[:120]}")
        tools_desc = "\n".join(lines)
    else:
        tools_desc = "（当前无可用工具）"
    
    target_hint = ""
    if target_tool:
        target_hint = f"\n\n【注意】主图已指定目标工具: {target_tool}，请优先使用此工具。"
    
    today = date.today().isoformat()

    return f"""你是一个工具调用决策助手。你的任务是根据用户查询，决定是否需要调用工具，以及调用哪些工具。

今天的日期是 {today}。如果查询里出现"明天""上周""7.1日"这类相对日期或省略年份的日期，
调用工具时传的日期参数要先换算/补全成完整的 YYYY-MM-DD 格式。

用户查询：{query}

可用工具列表：
{tools_desc}{target_hint}

决策规则：
1. 如果查询可以通过已有信息直接回答，选择 "finish"
2. 如果查询需要调用工具获取信息，选择 "call_tool"，并指定 tool_calls
3. 每次最多调用 1-2 个工具
4. 只能从上面的【可用工具列表】中选择工具名，不能编造不存在的工具
5. 如果工具调用失败，可以重试或换用其他工具
6. 思考要简洁，不要重复已知的上下文

输出格式（JSON）：
{{
  "thought": "你的分析思考",
  "action": "call_tool" | "finish",
  "tool_calls": [{{"name": "工具名", "arguments": {{"参数名": "参数值"}}}}],
  "reasoning": "决策理由"
}}"""


def _build_fallback_summary(
    tool_results: List[Dict[str, Any]],
    failed_tools: List[str],
) -> str:
    """LLM 不可用时的人工摘要 fallback。"""
    lines = ["## 工具执行结果\n"]
    
    for r in tool_results:
        name = r.get("name", "unknown")
        status = "✅" if r.get("success") else "❌"
        lines.append(f"### {status} {name}")
        output = r.get("output", "")
        lines.append(output[:800] if output else "（无输出）")
        lines.append("")
    
    if failed_tools:
        lines.append(f"\n**失败工具**: {', '.join(failed_tools)}")
    
    return "\n".join(lines)
