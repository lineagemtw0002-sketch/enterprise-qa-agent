"""
Tool Subgraph State — 工具子智能体的独立状态定义。

关键设计：
- internal_messages: 子图内部 ReAct 循环的消息，不返回主图
- tool_summary: 子图结束时产生的结构化摘要，写入主图 State
- tool_execution_trace: 执行轨迹，用于主图的可观测性
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, TypedDict, Annotated
from langchain_core.messages import AnyMessage, ToolMessage
from langgraph.graph import add_messages


class ToolSubgraphState(TypedDict, total=False):
    """工具子智能体状态。
    
    与主图 RAGState 隔离，避免消息污染。
    子图结束时，只将 tool_summary + tool_execution_trace 写回主图。
    """
    
    # === 输入（从主图传入）===
    query: str
    target_tool: Optional[str]           # 主图指定的目标工具（可选，None 则自主决策）
    available_tools: List[Dict[str, Any]]  # 可用工具 schema（用于 think_node 的 LLM 决策）
    user_id: str                         # 调用者身份，仅用于 tool_node 内部的 ACL 校验，
                                          # 不会出现在 available_tools 的 schema 里，
                                          # LLM 看不到也改不了这个值（见 tool_node）
    intent_confidence: float             # 主图意图分类的置信度——supervisor_node 用它
                                          # 判断"要不要跳过第一次 think"（见该函数旁的
                                          # 说明），跟 target_tool 一样直接从主图 RAGState
                                          # 透传进来，本状态类没有单独的字段校验/默认值。
    rewritten_query: str                 # 主图 analyze_query 产出的改写后查询——直接路由
                                          # 跳过 think_node 时，拿这个当工具调用的 query
                                          # 参数，不需要 think_node 再重新措辞一遍。

    # === 多智能体协作编排（supervisor_node 写入，见 subgraph.py） ===
    active_agent: Optional[str]          # "retrieval_agent" / "action_agent" / "general_agent"

    # === 内部状态（子图内部循环，不返回主图）===
    internal_messages: Annotated[List[AnyMessage], add_messages]
    tool_calls: List[Dict[str, Any]]     # LLM 决定的 tool calls [{name, arguments}]
    tool_results: List[Dict[str, Any]]   # 工具执行结果
    failed_tools: List[str]              # 执行失败的工具名
    iteration_count: int                 # ReAct 迭代计数（防无限循环）
    max_iterations: int                  # 最大迭代次数（默认 5）
    
    # === 输出（返回主图）===
    tool_summary: str                    # 结构化摘要
    tool_execution_trace: List[Dict[str, Any]]  # 执行轨迹
    
    # === 控制流 ===
    next_node: Optional[str]             # router_node 写入，控制下一个节点
