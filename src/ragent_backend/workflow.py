"""
RAG 工作流 - 滑动窗口记忆版本

核心改进：
1. 使用 Annotated + add_messages 管理消息列表
2. 使用 RemoveMessage 实现滑动窗口压缩
3. 滚动摘要：旧消息合并到 summary 中
4. 分离 checkpoint（给模型）和 PostgreSQL（给用户）
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import date, timedelta
from typing import Any, AsyncGenerator, Dict, List, Optional

import pydantic
from langchain_core.messages import HumanMessage, AIMessage, RemoveMessage, AnyMessage
from langchain_core.prompts import ChatPromptTemplate
from langgraph.graph import StateGraph, START, END

from src.ragent_backend.schemas import RAGState, ensure_message_ids
from src.ragent_backend.memory_manager import RollingMemoryManager
from src.ragent_backend.store import ConversationArchiveStore
from src.ragent_backend.intent import detect_intent, analyze_query
from src.ragent_backend.ltm_store import LTMStore
from src.ragent_backend.workflow_store import WorkflowStore
from src.mcp_server.tools.query_knowledge_hub import QueryKnowledgeHubTool
from src.tool_agent.tool_registry import ToolRegistry, get_default_registry
from src.tool_agent.subgraph import build_tool_subgraph

# 多轮收集阶段，用户想放弃当前工作流时的关键词（规则检测，不过 LLM，
# 对齐 intent.py 里"模糊代词"这类硬规则检查的风格，见 work-flow.md 6.1 步骤 1）
_WORKFLOW_CANCEL_KEYWORDS = ["取消", "算了", "不填了", "不申请了", "先不弄了"]


class RAGWorkflow:
    """
    RAG 工作流实现
    
    节点流程：
    session -> intent -> retrieve -> generate -> memory_manage -> archive -> END
    
    记忆管理：
    - messages 使用 Annotated[list, add_messages] 管理
    - 超出 max_messages 时，使用 RemoveMessage 删除旧消息
    - 被删除的消息合并到 summary 中
    - 所有消息（包括本轮）异步归档到 PostgreSQL
    """
    
    def __init__(
        self,
        store: ConversationArchiveStore,
        llm: Any,
        checkpointer: Any = None,
        max_messages: int = 20,
        keep_recent: int = 4,
        ltm_store: Optional[LTMStore] = None,
        tool_registry: Optional[ToolRegistry] = None,
        workflow_store: Optional[WorkflowStore] = None,
    ) -> None:
        self._store = store
        self._llm = llm
        self._checkpointer = checkpointer
        self._ltm_store = ltm_store
        self._workflow_store = workflow_store
        # asyncio only holds a *weak* reference to a task; one created and never
        # stored anywhere (as the archive/LTM background tasks below are) can be
        # garbage-collected before it finishes running. Keeping a strong reference
        # here until each task completes is the documented fix.
        self._background_tasks: set[asyncio.Task] = set()
        self._token_queue: Optional[asyncio.Queue[str]] = None
        self._trace_queue: Optional[asyncio.Queue[Dict[str, Any]]] = None
        self._memory_manager = RollingMemoryManager(
            max_messages=max_messages,
            keep_recent=keep_recent
        )
        # 初始化 RAG 检索工具
        self._retrieval_tool = QueryKnowledgeHubTool()
        # 工具注册表（可外部传入，或使用默认全局实例）
        self._tool_registry = tool_registry or get_default_registry()
        self._compiled = self._build_graph()

    def _build_graph(self):
        """构建工作流图（四分支：clarify / rag / tool / workflow）"""
        graph = StateGraph(RAGState)

        # 添加主图节点
        graph.add_node("session", self._session_node)
        graph.add_node("intent", self._intent_node)
        graph.add_node("clarify", self._clarify_node)
        graph.add_node("retrieve", self._retrieve_node)
        graph.add_node("generate", self._generate_node)
        graph.add_node("workflow", self._workflow_node)
        graph.add_node("memory_manage", self._memory_manage_node)
        graph.add_node("archive", self._archive_node)

        # 添加工具子图节点（子图编译后作为一个节点）
        if self._llm is not None:
            self._tool_subgraph = build_tool_subgraph(
                tool_registry=self._tool_registry,
                llm=self._llm,
                max_iterations=5,
                # think/tool/summarize 每一步的起止都推给 TracePanel（见
                # subgraph.py build_tool_subgraph 的 emit_trace 参数说明）——
                # self._emit_trace 是绑定方法，子图虽然在这里（构造期）只编译
                # 一次，但每次调用时读的是当次请求实时设置的 self._trace_queue，
                # 不会把某一次请求的队列锁死进闭包里，多个并发请求互不串。
                emit_trace=self._emit_trace,
            )
            # 不直接把编译后的子图注册成节点——子图内部（think_node/tool_node/
            # summarize_node，见 src/tool_agent/subgraph.py）原来完全没有埋点，
            # TracePanel 只能看到它前面的"意图解析"和后面的"最终生成"，中间这段（通常是全链路
            # 最长的一段：ReAct 决策 + 实际检索/工具调用，可能好几轮）在前端一直
            # 显示成"待处理"，看起来像是卡在意图解析或者凭空消失了十几秒。这里用一层
            # 薄包装补上子图整体的 start/end 埋点（不改子图内部逻辑），至少能在
            # TracePanel 上看到"工具调用"这一步真实的起止时间和耗时。
            graph.add_node("tool_subgraph", self._tool_subgraph_node)

        # 添加边
        graph.add_edge(START, "session")
        graph.add_edge("session", "intent")
        graph.add_conditional_edges(
            "intent",
            self._route_after_intent,
            {"clarify": "clarify", "retrieve": "retrieve", "tool_subgraph": "tool_subgraph", "workflow": "workflow"}
        )
        # 分支路由：rag/tool 需要 generate，clarify/workflow 直接跳过
        graph.add_edge("retrieve", "generate")
        if self._llm is not None:
            graph.add_edge("tool_subgraph", "generate")
        graph.add_edge("generate", "memory_manage")
        # clarify/workflow 直接到 memory_manage（跳过 generate，避免重复生成——
        # 工作流的追问话术/提交确认语要精确到字段名/工单号，不能让通用生成节点
        # 二次改写，见 work-flow.md 5.4 节）
        graph.add_edge("clarify", "memory_manage")
        graph.add_edge("workflow", "memory_manage")
        graph.add_edge("memory_manage", "archive")
        graph.add_edge("archive", END)

        return graph.compile(checkpointer=self._checkpointer)

    def _route_after_intent(self, state: RAGState) -> str:
        """根据意图判断结果决定下一步走向（四分支）"""
        # 有未完成的工作流时，优先继续填表，不管这一轮意图分类器猜成什么——
        # 分类器面对"事假"这种孤立词很容易误判成 clarify/rag，续填的确定性
        # 应该压过通用分类结果（work-flow.md 5.4 节）。
        if state.get("active_workflow"):
            return "workflow"
        intent_type = state.get("intent_type", "rag")
        # need_clarify is the authoritative signal; intent_type=="clarify" alone is not
        # trustworthy on smaller local models, which sometimes set intent_type="clarify"
        # while need_clarify=False, causing a spurious short-circuit to an empty reply.
        if state.get("need_clarify"):
            return "clarify"
        if intent_type == "workflow":
            return "workflow"
        if intent_type == "tool":
            # 如果 LLM 不可用，无法运行 tool_subgraph，回退到 retrieve
            if self._llm is None:
                return "retrieve"
            return "tool_subgraph"
        if intent_type == "clarify":
            # 走到这里说明上面 need_clarify 那道拦截没触发（need_clarify=False），
            # 即分类器自己也不确定、输出了自相矛盾的结果——这不是"真的要澄清"，
            # 是分类失败。原来无差别 fall through 到最下面的 return "retrieve"，
            # 隐含假设是"当分类器判不准，八成是在问自己上传的文件"，但两次真实
            # 复现（"入职第一天怎么办""收到钓鱼邮件应该怎么处理"）都是明显该查
            # 企业知识库的问题，retrieve 只查这次对话自己上传的附件（多数场景
            # 压根没传过文件），查到 0 条后 generate 会在没有任何检索依据的情况
            # 下凭自己的训练知识编一段看似合理的通用回答——用户完全看不出这不是
            # 知识库查出来的。分类器判不准时，交给工具子图，让它按可用工具列表
            # （包含 query_knowledge_hub）自己再判断一次要不要查企业知识库，是
            # 更安全的默认；真正该走"针对自己上传文件提问"的场景，用户措辞通常
            # 会明确提到"这份文档""我上传的"，分类器对这类表述判得比较准，不容易
            # 踩进这个自相矛盾的分支。
            if self._llm is not None:
                return "tool_subgraph"
        return "retrieve"

    def _emit_trace(
        self,
        node: str,
        step: str,
        status: str = "running",
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        """将 trace 事件推送到 trace_queue（仅在流式模式下）"""
        if self._trace_queue is not None:
            asyncio.create_task(
                self._trace_queue.put({
                    "type": "trace",
                    "node": node,
                    "step": step,
                    "status": status,
                    "payload": payload or {},
                    "ts": time.time(),
                })
            )

    async def run(
        self, 
        initial_state: Dict[str, Any], 
        thread_id: str
    ) -> RAGState:
        """
        运行工作流
        
        Args:
            initial_state: 初始状态，必须包含 query
            thread_id: 对话 ID（用于 checkpoint 加载）
        """
        config = {"configurable": {"thread_id": thread_id}}
        
        # 添加用户输入到 messages
        user_message = HumanMessage(content=initial_state["query"])
        initial_state.setdefault("messages", []).append(user_message)
        
        return await self._compiled.ainvoke(initial_state, config)

    async def run_stream(
        self,
        initial_state: Dict[str, Any],
        thread_id: str,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """
        流式运行工作流。
        
        在 generate 节点内部通过 llm.astream() 实时产生 token，
        并通过 asyncio.Queue 逐 token yield 给调用方。
        
        Yields:
            {"type": "token", "content": str}
            {"type": "done", "state": RAGState}
        """
        config = {"configurable": {"thread_id": thread_id}}
        
        # 添加用户输入到 messages
        user_message = HumanMessage(content=initial_state["query"])
        initial_state.setdefault("messages", []).append(user_message)
        
        self._token_queue = asyncio.Queue()
        self._trace_queue = asyncio.Queue()
        graph_task = asyncio.create_task(self._compiled.ainvoke(initial_state, config))
        token_yielded = False
        
        try:
            while True:
                if graph_task.done():
                    # 清空剩余 trace
                    while not self._trace_queue.empty():
                        yield self._trace_queue.get_nowait()
                    # 清空剩余 token
                    while not self._token_queue.empty():
                        token = self._token_queue.get_nowait()
                        yield {"type": "token", "content": token}
                        token_yielded = True
                    break
                
                token_task = asyncio.create_task(self._token_queue.get())
                trace_task = asyncio.create_task(self._trace_queue.get())
                done, pending = await asyncio.wait(
                    [graph_task, token_task, trace_task],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                
                for t in pending:
                    if t is graph_task:
                        continue
                    t.cancel()
                    try:
                        await t
                    except asyncio.CancelledError:
                        pass
                
                if token_task in done:
                    try:
                        token = token_task.result()
                        yield {"type": "token", "content": token}
                        token_yielded = True
                    except asyncio.CancelledError:
                        pass
                
                if trace_task in done:
                    try:
                        yield trace_task.result()
                    except asyncio.CancelledError:
                        pass
            
            final_state = await graph_task
            
            # 兜底：如果 generate 节点被跳过（如 need_clarify）或出错未吐 token，
            # 直接把 final_answer 作为 token 吐出，避免前端空屏卡死
            if not token_yielded and final_state.get("final_answer"):
                yield {"type": "token", "content": final_state["final_answer"]}
            
            yield {"type": "done", "state": final_state}
        finally:
            self._token_queue = None
            self._trace_queue = None
            if graph_task and not graph_task.done():
                graph_task.cancel()
                try:
                    await graph_task
                except asyncio.CancelledError:
                    pass

    async def _session_node(self, state: RAGState) -> Dict[str, Any]:
        """
        会话初始化节点
        
        注意：
        - LangGraph 会自动从 checkpointer 加载 messages 和 summary
        - 这里只需要确保 conversation_id 存在
        - 确保所有消息都有 ID（RemoveMessage 依赖）
        """
        self._emit_trace("session", "node_start", "running")
        
        # 确保 conversation_id
        if not state.get("conversation_id"):
            state["conversation_id"] = str(uuid.uuid4())
        
        # 确保 task_id
        if not state.get("task_id"):
            state["task_id"] = str(uuid.uuid4())
        
        # 确保所有消息都有 ID（关键！）
        if state.get("messages"):
            state["messages"] = ensure_message_ids(state["messages"])
        
        # 初始化默认值
        state.setdefault("messages", [])
        state.setdefault("summary", "")
        state.setdefault("memories", [])
        # 每轮都生成新的 turn_id，用于后续三层时间裁剪回滚
        state["current_turn_id"] = str(uuid.uuid4())

        # 每轮清空上一轮遗留的工具执行轨迹——tool_execution_trace 是普通字段
        # （没有 Annotated 累加 reducer），会被 checkpointer 原样带到下一轮；
        # tool_subgraph 的 tool_node 是"读出来再 append"（见 subgraph.py
        # tool_node），如果这里不清空，本轮之前任何一轮（甚至是很久以前，
        # 比如企业知识库委托连接器还没配好时打到 "default" 库、或者委托模式
        # 打到 tenant_*_kb 的那次）残留的调用记录会一直留在这里，被
        # _generate_node 的 kb_sources（本轮"来源知识库"角标，见该节点的
        # 说明）当成"这轮也查了这个库"一并渲染出来——用户这一轮问题明明只命中
        # 了一个部门知识库，角标却会多出好几个跟本轮回答毫无关系的库。
        state["tool_execution_trace"] = []
        state["tool_summary"] = ""
        
        # 召回长期记忆（跨会话认知连续）
        if self._ltm_store and state.get("user_id"):
            try:
                query = state.get("rewritten_query") or state.get("query", "")
                memories = await self._ltm_store.retrieve_facts(
                    user_id=state["user_id"],
                    query=query,
                    top_k=3,
                )
                if memories:
                    state["memories"] = memories
                    print(f"[Session] Recalled {len(memories)} LTM facts for user {state['user_id']}")
            except Exception as e:
                print(f"[Session] LTM recall failed: {e}")
        
        state.setdefault("trace_events", []).append(
            {"node": "session", "ts": time.time(), "ok": True}
        )
        
        self._emit_trace("session", "node_end", "success")
        return state

    async def _intent_node(self, state: RAGState) -> Any:
        """意图识别节点：结构化 LLM 一次完成指代消解 + 子查询拆分 + 四分类"""
        self._emit_trace("intent", "node_start", "running")

        query = state["query"]
        messages = state.get("messages", [])

        # 续填中的工作流：路由（_route_after_intent）会无条件把这一轮送进 workflow
        # 节点，这里不需要重新做指代消解/分类，省两次 LLM 往返（work-flow.md 5.4 节
        # "优化项"）。
        if state.get("active_workflow"):
            self._emit_trace("intent", "node_end", "success", {"skipped": "active_workflow_continuation"})
            return {"rewritten_query": query}

        # 前端"发起工作流"入口显式带了 workflow_type：跳过分类，直接确定意图
        # （work-flow.md 5.1 节短路）。续填场景已经在上面被拦掉，这里只处理首次发起。
        hint = state.get("workflow_type_hint")
        if hint and self._workflow_store is not None:
            template = await self._workflow_store.get_template_by_type(hint)
            if template is not None:
                self._emit_trace("intent", "node_end", "success", {
                    "intent_type": "workflow", "workflow_type": hint, "source": "explicit_hint",
                })
                return {
                    "rewritten_query": query,
                    "sub_queries": [query],
                    "intent_type": "workflow",
                    "intent_confidence": 1.0,
                    "need_clarify": False,
                    "clarify_prompt": "",
                    "target_tool": None,
                    "tool_args": None,
                    "target_workflow_type": hint,
                    "available_tools": self._tool_registry.to_openai_tools() if self._tool_registry else [],
                    "trace_events": [
                        *state.get("trace_events", []),
                        {
                            "node": "intent", "ts": time.time(), "intent_type": "workflow",
                            "workflow_type": hint, "reasoning": "前端显式指定，跳过分类",
                        },
                    ],
                }
            # hint 指向一个不存在/已下线的模板（前端缓存的类型列表过期了）——
            # 不能假设前端传来的值一定合法，忽略 hint，退回下面的正常分类兜底

        # 单次结构化调用：重写 + 拆分
        #
        # 这里本来想合并成一次 LLM 调用（省一次往返，见 intent.py 里还留着的
        # analyze_and_route()），线上验证发现合并后本地小模型（qwen2.5:7b）
        # 分类质量明显变差——"新员工入职怎么办"这类问题，两次独立调用稳定判成
        # "tool"（查知识库），合并成一次调用后 3/3 次全部误判成 "clarify"（还
        # 反而更自信，confidence=1.0），调整过 prompt 措辞也没能纠正。省下来的
        # 几秒钟不值得拿分类准确率去换，所以保留两次调用，analyze_and_route()
        # 留在 intent.py 里不接线，后续想再尝试合并可以从那份代码继续。
        self._emit_trace("intent", "query_rewrite", "running", {"original_query": query})
        try:
            analysis = await analyze_query(
                query=query,
                messages=messages,
                llm=self._llm
            )
            rewritten_query = analysis.rewritten_query
            sub_queries = analysis.sub_queries
        except Exception as e:
            print(f"[Intent] Structured analysis failed: {e}")
            rewritten_query = query
            sub_queries = [query]

        # 意图识别（四分支：clarify / rag / tool / workflow）
        self._emit_trace("intent", "intent_detect", "running")
        # 从注册表获取可用工具 schema，供 LLM 判断 tool 意图
        available_tools = self._tool_registry.to_openai_tools() if self._tool_registry else []
        # 从 WorkflowStore 获取可用流程模板，供 LLM 判断 workflow 意图
        available_workflows: List[Dict[str, Any]] = []
        if self._workflow_store is not None:
            try:
                templates = await self._workflow_store.list_templates()
                available_workflows = [
                    {"workflow_type": t.workflow_type, "display_name": t.display_name, "description": t.description}
                    for t in templates
                ]
            except Exception as e:
                print(f"[Intent] Failed to load workflow templates: {e}")
        intent = await detect_intent(
            rewritten_query=rewritten_query,
            llm=self._llm,
            available_tools=available_tools,
            available_workflows=available_workflows,
        )
        self._emit_trace("intent", "intent_detect", "success", {
            "intent_type": intent.intent_type,
            "confidence": intent.confidence,
            "need_clarify": intent.need_clarify,
            "target_tool": intent.target_tool,
            "workflow_type": intent.workflow_type,
        })

        # 如果 intent_type=tool/workflow/clarify，收窄成单一 query，不用 LLM 拆出来的
        # sub_queries 列表——工具调用/工作流表单填写/澄清追问都是针对这一整句原始
        # 意图操作，拆分出的并列子查询在这三种分支里没有意义（只对 rag 检索有用）。
        if intent.intent_type == "tool":
            sub_queries = [intent.rewritten_query]
            self._emit_trace("intent", "tool_intent", "running", {
                "target_tool": intent.target_tool or "",
                "reasoning": intent.reasoning or "",
            })
        elif intent.intent_type == "workflow":
            sub_queries = [intent.rewritten_query]
            self._emit_trace("intent", "workflow_intent", "running", {
                "workflow_type": intent.workflow_type or "",
                "reasoning": intent.reasoning or "",
            })
        elif intent.need_clarify:
            sub_queries = [intent.rewritten_query]
            self._emit_trace("intent", "clarify_shortcircuit", "running", {
                "clarify_prompt": intent.clarify_prompt or "",
            })

        update = {
            "rewritten_query": rewritten_query,
            "sub_queries": sub_queries,
            "intent_type": intent.intent_type,
            "intent_confidence": intent.confidence,
            "need_clarify": intent.need_clarify,
            "clarify_prompt": intent.clarify_prompt or "",
            "target_tool": intent.target_tool,
            "tool_args": intent.tool_args,
            "target_workflow_type": intent.workflow_type,
            "available_tools": available_tools,
            "trace_events": [
                *state.get("trace_events", []),
                {
                    "node": "intent",
                    "ts": time.time(),
                    "intent_type": intent.intent_type,
                    "confidence": intent.confidence,
                    "need_clarify": intent.need_clarify,
                    "target_tool": intent.target_tool,
                    "workflow_type": intent.workflow_type,
                    "sub_query_count": len(sub_queries),
                    "rewritten": rewritten_query != query,
                    "original_query": query,
                    "reasoning": intent.reasoning,
                }
            ],
        }

        # 如果需要澄清，提前设置 final_answer
        if intent.need_clarify:
            update["final_answer"] = intent.clarify_prompt or "请补充更多信息。"
            update["used_model"] = "intent-shortcircuit"

        self._emit_trace("intent", "node_end", "success", {
            "intent_type": intent.intent_type,
            "need_clarify": intent.need_clarify,
            "sub_query_count": len(sub_queries),
        })
        return update

    async def _tool_subgraph_node(self, state: RAGState) -> Any:
        """`self._tool_subgraph`（ReAct 工具子图：think → tool → summarize，最多
        5 轮）的埋点包装——子图内部节点不产生任何 `_emit_trace` 事件，直接把编译
        后的子图注册成主图节点会导致 TracePanel 上"工具调用"这一步全程显示成
        "待处理"，看不出它什么时候开始、跑了多久，实际耗时全部无声地发生在这
        两条 trace 之间（见 `_build_graph` 里改用这个包装方法的说明）。"""
        self._emit_trace("tool_subgraph", "node_start", "running")
        result = await self._tool_subgraph.ainvoke(state)
        self._emit_trace("tool_subgraph", "node_end", "success", {
            "target_tool": state.get("target_tool"),
            "iteration_count": result.get("iteration_count") if isinstance(result, dict) else None,
        })
        return result

    async def _clarify_node(self, state: RAGState) -> Dict[str, Any]:
        """澄清节点：当意图为 clarify 时，生成澄清提示并准备进入 generate。"""
        self._emit_trace("clarify", "node_start", "running")
        
        # state["clarify_prompt"] is set (possibly to "") by _intent_node, so the
        # dict.get default never fires for a missing key -- it must fire on falsy too.
        clarify_prompt = state.get("clarify_prompt") or "请补充更多信息。"
        
        self._emit_trace("clarify", "node_end", "success", {
            "clarify_prompt": clarify_prompt,
        })
        return {
            "final_answer": clarify_prompt,
            "used_model": "intent-clarify",
            "trace_events": [
                *state.get("trace_events", []),
                {"node": "clarify", "ts": time.time(), "ok": True}
            ],
        }

    async def _workflow_node(self, state: RAGState) -> Dict[str, Any]:
        """工作流节点：多轮收集结构化字段 -> 提交实例。风格对齐 `_clarify_node`
        （短路直接给 final_answer，不经过 `_generate_node`），实现细节见
        work-flow.md 第 6.1 节。"""
        self._emit_trace("workflow", "node_start", "running")

        query = state.get("query", "")
        active = state.get("active_workflow")
        user_id = state.get("user_id")
        conversation_id = state.get("conversation_id")
        trace_events = state.get("trace_events", [])

        if self._workflow_store is None:
            self._emit_trace("workflow", "node_end", "error", {"reason": "no_workflow_store"})
            return {
                "active_workflow": None,
                "final_answer": "工作流功能暂时不可用，请稍后再试。",
                "used_model": "workflow-unavailable",
                "trace_events": [*trace_events, {"node": "workflow", "ts": time.time(), "ok": False}],
            }

        # 1. 取消检测（规则，不过 LLM）
        if active and any(kw in query for kw in _WORKFLOW_CANCEL_KEYWORDS):
            display_name = active.get("display_name", "")
            self._emit_trace("workflow", "node_end", "success", {"event": "cancelled"})
            return {
                "active_workflow": None,
                "final_answer": f"已取消本次「{display_name}」申请。",
                "used_model": "workflow-cancelled",
                "trace_events": [*trace_events, {"node": "workflow", "ts": time.time(), "event": "cancelled"}],
            }

        if active is None:
            # 2. 首次发起：intent 节点已经确定了 target_workflow_type
            workflow_type = state.get("target_workflow_type")
            template = await self._workflow_store.get_template_by_type(workflow_type) if workflow_type else None
            if template is None:
                # 防御性兜底：路由已保证 intent_type=="workflow" 时 target_workflow_type
                # 合法，正常不会走到这里
                self._emit_trace("workflow", "node_end", "error", {"reason": "no_target_workflow_type"})
                return {
                    "final_answer": "抱歉，没识别出具体是哪种流程，能再说清楚一点吗？",
                    "used_model": "workflow-fallback",
                    "trace_events": [*trace_events, {"node": "workflow", "ts": time.time(), "ok": False}],
                }

            if not template.approver_role_id:
                self._emit_trace("workflow", "node_end", "success", {"event": "blocked_no_approver"})
                return {
                    "final_answer": f"「{template.display_name}」暂未配置审批人，请联系管理员配置后再试。",
                    "used_model": "workflow-blocked",
                    "trace_events": [*trace_events, {"node": "workflow", "ts": time.time(), "event": "blocked_no_approver"}],
                }

            existing = await self._workflow_store.get_in_flight_instance(user_id, workflow_type)
            if existing is not None:
                status_label = "待审批" if existing.status == "pending_approval" else "已被打回，待补充材料"
                self._emit_trace("workflow", "node_end", "success", {"event": "blocked_in_flight"})
                return {
                    "final_answer": (
                        f"你有一条「{template.display_name}」申请正在处理中"
                        f"（编号 #{existing.instance_id[:8]}，当前{status_label}），处理完再发起新的。"
                    ),
                    "used_model": "workflow-blocked",
                    "trace_events": [*trace_events, {"node": "workflow", "ts": time.time(), "event": "blocked_in_flight"}],
                }

            extracted = await self._extract_workflow_fields(query, template.required_fields)
            collected = {
                k: v for k, v in extracted.items()
                if self._validate_workflow_field(template.required_fields, k, v)
            }
        else:
            # 3. 续填答复：只针对上一轮还缺的字段做抽取
            template = await self._workflow_store.get_template_by_type(active["workflow_type"])
            if template is None:
                self._emit_trace("workflow", "node_end", "error", {"reason": "template_missing"})
                return {
                    "active_workflow": None,
                    "final_answer": "抱歉，这条流程模板已经不存在了，本次申请已取消，请重新发起。",
                    "used_model": "workflow-fallback",
                    "trace_events": [*trace_events, {"node": "workflow", "ts": time.time(), "ok": False}],
                }
            fields_to_extract = [f for f in template.required_fields if f["key"] in active.get("missing_field_keys", [])]
            extracted = await self._extract_workflow_fields(
                query, fields_to_extract, awaiting_field_key=active.get("awaiting_field_key"),
            )
            collected = dict(active.get("collected_fields", {}))
            for k, v in extracted.items():
                if self._validate_workflow_field(template.required_fields, k, v):
                    collected[k] = v

        # 4. 计算还缺哪些必填字段
        missing = self._compute_missing_workflow_fields(template.required_fields, collected)
        total_required = sum(1 for f in template.required_fields if f.get("required"))
        active_workflow = {
            "workflow_type": template.workflow_type,
            "display_name": template.display_name,
            "collected_fields": collected,
            "missing_field_keys": missing,
            "awaiting_field_key": missing[0] if missing else None,
            "total_required_count": total_required,
        }

        if missing:
            # 5. 还缺字段：追问下一个（一次只问一个，体验更像聊天而不是表单）
            field_def = next(f for f in template.required_fields if f["key"] == missing[0])
            question = self._build_workflow_field_question(field_def)
            self._emit_trace("workflow", "node_end", "success", {
                "event": "collecting", "missing_count": len(missing), "total_count": total_required,
            })
            return {
                "active_workflow": active_workflow,
                "final_answer": question,
                "used_model": "workflow-collect",
                "trace_events": [
                    *trace_events,
                    {"node": "workflow", "ts": time.time(), "event": "collecting",
                     "missing_count": len(missing), "awaiting_field_key": missing[0]},
                ],
            }

        # 6. 字段齐了：提交
        from src.ragent_backend.role_store import RoleStore
        role_store = RoleStore()
        try:
            instance = await self._workflow_store.create_instance(
                workflow_type=template.workflow_type,
                requester_user_id=user_id,
                conversation_id=conversation_id,
                fields=collected,
                role_store=role_store,
            )
        except ValueError as e:
            # 竞态下"同类型只能一条在途"校验在提交这一刻才真正失败（比如同一用户
            # 在另一个对话里几乎同时提交了同一类型）——不是常见路径，兜底给出
            # 明确提示而不是让异常冒泡成 500。
            self._emit_trace("workflow", "node_end", "error", {"reason": str(e)})
            return {
                "active_workflow": None,
                "final_answer": f"提交失败：{e}",
                "used_model": "workflow-blocked",
                "trace_events": [*trace_events, {"node": "workflow", "ts": time.time(), "ok": False, "error": str(e)}],
            }

        confirm_lines = [f"已为你提交「{template.display_name}」申请（编号 #{instance.instance_id[:8]}）。"]
        if template.attachments_note:
            confirm_lines.append(template.attachments_note)
        confirm_lines.append("已抄送审批人，材料不齐会被打回，结果会在对话里通知你。")

        self._emit_trace("workflow", "node_end", "success", {
            "event": "submitted", "instance_id": instance.instance_id,
        })
        return {
            "active_workflow": None,
            "final_answer": "\n".join(confirm_lines),
            "used_model": "workflow-submitted",
            "trace_events": [
                *trace_events,
                {"node": "workflow", "ts": time.time(), "event": "submitted", "instance_id": instance.instance_id},
            ],
        }

    async def _extract_workflow_fields(
        self, query: str, fields: List[Dict[str, Any]], awaiting_field_key: Optional[str] = None,
    ) -> Dict[str, str]:
        """用结构化 LLM 调用从这句话里抽取给定字段的值，抽不到的字段不出现在
        返回结果里。没有 LLM 或没有要抽取的字段时直接返回空字典（work-flow.md
        6.1 节步骤 2/3）。

        `awaiting_field_key`：续填轮次里，上一轮追问话术具体问的是哪个字段
        （`_build_workflow_field_question` 生成问题时用的那个 `missing[0]`）。
        续填时 `fields` 传入的是"当前还缺的全部字段"，不只是刚问的那一个——如果
        用户的回复很短、本身脱离上下文根本看不出是在答哪个字段（比如追问"开始
        日期是？"，用户就回一句"明天"），模型面对一堆候选字段时容易抽不出来，
        白白回一句"明天"却没被识别、又被追问一遍同一个问题。显式点出"这轮问的
        就是这个字段"，让模型有个锚点，而不是硬猜。"""
        if not fields or self._llm is None:
            return {}

        field_defs: Dict[str, Any] = {}
        field_lines = []
        for f in fields:
            desc = f.get("description") or f["label"]
            field_defs[f["key"]] = (Optional[str], pydantic.Field(default=None, description=desc))
            line = f"- {f['key']}（{f['label']}）：{desc}"
            if f.get("options"):
                line += f"，可选值：{', '.join(f['options'])}"
            field_lines.append(line)

        model_cls = pydantic.create_model("WorkflowFieldExtraction", **field_defs)
        today_dt = date.today()
        today = today_dt.isoformat()
        # 光靠"今天日期是 X + 明天/下周一举例"不够可靠——小模型经常连"今天"本身
        # 都换算不出来（把"今天请假"里的"今天"当成抽不到，继续追问日期）。这里
        # 把最常用的几个相对日期直接算好、原样喂给模型当作对照表，不指望模型
        # 自己心算日期加减，只有再往外的相对表达（"下周一""这个月底"之类）才
        # 真的需要模型自己推算。
        relative_dates = {
            "前天": today_dt - timedelta(days=2),
            "昨天": today_dt - timedelta(days=1),
            "今天": today_dt,
            "明天": today_dt + timedelta(days=1),
            "后天": today_dt + timedelta(days=2),
            "大后天": today_dt + timedelta(days=3),
        }
        relative_date_lines = "；".join(f"{label}={d.isoformat()}" for label, d in relative_dates.items())
        awaiting_field_hint = ""
        if awaiting_field_key:
            awaiting_field = next((f for f in fields if f["key"] == awaiting_field_key), None)
            if awaiting_field is not None:
                awaiting_field_hint = (
                    f"\n用户刚才被追问的是「{awaiting_field['label']}」这一项，这句话大概率就是在直接回答"
                    f"这一项——如果这句话本身很短、看不出在说别的内容，就优先把它当成"
                    f"{awaiting_field_key}（{awaiting_field['label']}）的答案；如果这句话里明显还带了其它"
                    f"字段的信息，那些字段也一并抽取，不要漏。"
                )
        prompt = f"""从下面这句话里抽取以下字段的值，能抽到就填，抽不到就留空（null）。
今天的日期是 {today}。句子里如果出现相对日期表达，请换算成 YYYY-MM-DD 格式，常见的对照如下（直接查表，不要自己心算）：
{relative_date_lines}。
这些相对日期一律以今天 {today} 为基准折算，不要以对话里之前已经填过的其它日期字段（比如已经填好的开始日期）为基准去累加——
比如就算开始日期已经填了"明天"，用户接着说"后天"，"后天"仍然是 {relative_dates['后天'].isoformat()}（今天 + 2 天），不是"明天 + 2 天"。
再往外的相对表达（如"下周一""这个月底"）同样只以今天 {today} 为基准推算，不要以其它已填日期为基准。
枚举类字段必须原样输出给定的可选值之一，不要改写措辞。{awaiting_field_hint}

字段列表：
{chr(10).join(field_lines)}

这句话：{query}

只输出 JSON，不要解释。"""

        try:
            structured_llm = self._llm.with_structured_output(model_cls, method="json_mode")
            result = await structured_llm.ainvoke([HumanMessage(content=prompt)])
            return {k: v for k, v in result.model_dump().items() if v}
        except Exception as e:
            print(f"[Workflow] Field extraction failed: {e}")
            return {}

    @staticmethod
    def _validate_workflow_field(required_fields: List[Dict[str, Any]], key: str, value: Any) -> bool:
        """日期/枚举/数字字段做基础格式校验，不合法的值当作"没抽到"，
        重新计入缺失字段继续追问，避免脏数据落库（work-flow.md 6.1 步骤 4）。"""
        if not value:
            return False
        field_def = next((f for f in required_fields if f["key"] == key), None)
        if field_def is None:
            return False
        field_type = field_def.get("type")
        if field_type == "date":
            try:
                date.fromisoformat(str(value))
            except ValueError:
                return False
        elif field_type == "enum":
            options = field_def.get("options") or []
            if value not in options:
                return False
        elif field_type == "number":
            try:
                float(value)
            except (TypeError, ValueError):
                return False
        return True

    @staticmethod
    def _compute_missing_workflow_fields(required_fields: List[Dict[str, Any]], collected: Dict[str, Any]) -> List[str]:
        return [f["key"] for f in required_fields if f.get("required") and not collected.get(f["key"])]

    @staticmethod
    def _build_workflow_field_question(field_def: Dict[str, Any]) -> str:
        label = field_def["label"]
        if field_def.get("type") == "enum" and field_def.get("options"):
            return f"{label}是？（{'/'.join(field_def['options'])}）"
        return f"请补充一下：{label}"

    async def _retrieve_node(self, state: RAGState) -> Dict[str, Any]:
        """检索节点 - 接入真实的 RAG MCP 检索

        意图节点(_intent_node)会把并列主题拆分成多个 sub_queries（例如"退款政策和年假
        制度分别是什么" -> ["退款政策是什么", "年假制度是什么"]），但这里过去只用了
        rewritten_query 这一个字符串，sub_queries 从没被读取过——并行子查询检索一直是
        没接上的死代码。真正有多个子查询时，并行检索后合并结果；只有一个时走原来的单查询
        路径，行为不变。
        """
        self._emit_trace("retrieve", "node_start", "running")

        conversation_id = state["conversation_id"]
        collection = f"conv_{conversation_id}"
        top_k = state.get("top_k", 5)
        sub_queries = [q.strip() for q in state.get("sub_queries", []) if q and q.strip()]

        if len(sub_queries) > 1:
            return await self._retrieve_multi(state, sub_queries, collection, top_k)

        query = state.get("rewritten_query") or state["query"]

        self._emit_trace("retrieve", "knowledge_retrieval", "running", {
            "query": query,
            "collection": collection,
            "top_k": top_k,
        })

        try:
            # 调用 RAG MCP 检索工具
            retrieval_result = await self._retrieval_tool.execute(
                query=query,
                collection=collection,
                top_k=top_k,
            )
            
            # retrieval_result 是 MCPToolResponse 对象
            context_text = retrieval_result.content
            
            self._emit_trace("retrieve", "knowledge_retrieval", "success", {
                "collection": collection,
                "result_count": retrieval_result.metadata.get("result_count", 0) if hasattr(retrieval_result, "metadata") else 0,
            })
            self._emit_trace("retrieve", "node_end", "success")
            return {
                "retrieval_context": context_text,
                "retrieval_contexts": [context_text],
                "trace_events": [
                    *state.get("trace_events", []),
                    {
                        "node": "retrieve", 
                        "ts": time.time(), 
                        "ok": True, 
                        "collection": collection,
                        "result_count": retrieval_result.metadata.get("result_count", 0) if hasattr(retrieval_result, "metadata") else 0,
                    }
                ],
            }
        except Exception as e:
            # 检索失败时返回提示，不中断工作流
            print(f"[Retrieve] Error: {e}")
            self._emit_trace("retrieve", "knowledge_retrieval", "error", {"error": str(e)})
            self._emit_trace("retrieve", "node_end", "error")
            return {
                "retrieval_context": "该对话暂无文件或检索服务暂时不可用。",
                "retrieval_contexts": [],
                "trace_events": [
                    *state.get("trace_events", []),
                    {"node": "retrieve", "ts": time.time(), "ok": False, "error": str(e)}
                ],
            }

    async def _retrieve_multi(
        self,
        state: RAGState,
        sub_queries: List[str],
        collection: str,
        top_k: int,
    ) -> Dict[str, Any]:
        """并行检索多个子查询，逐个失败互不影响，最后合并成一份带子查询标签的上下文。"""
        self._emit_trace("retrieve", "knowledge_retrieval", "running", {
            "sub_queries": sub_queries,
            "collection": collection,
            "top_k": top_k,
        })

        async def _run_one(q: str) -> tuple[str, Optional[str], int, Optional[str]]:
            try:
                result = await self._retrieval_tool.execute(query=q, collection=collection, top_k=top_k)
                count = result.metadata.get("result_count", 0) if hasattr(result, "metadata") else 0
                return q, result.content, count, None
            except Exception as e:
                return q, None, 0, str(e)

        results = await asyncio.gather(*[_run_one(q) for q in sub_queries])

        contexts: List[str] = []
        total_count = 0
        errors: List[Dict[str, str]] = []
        for q, content, count, err in results:
            if err:
                errors.append({"query": q, "error": err})
                continue
            if content:
                contexts.append(f"[子查询: {q}]\n{content}")
                total_count += count

        context_text = "\n\n---\n\n".join(contexts) if contexts else "该对话暂无文件或检索服务暂时不可用。"

        self._emit_trace("retrieve", "knowledge_retrieval", "success" if contexts else "error", {
            "collection": collection,
            "result_count": total_count,
            "sub_query_count": len(sub_queries),
            "errors": errors,
        })
        self._emit_trace("retrieve", "node_end", "success")
        return {
            "retrieval_context": context_text,
            "retrieval_contexts": contexts,
            "trace_events": [
                *state.get("trace_events", []),
                {
                    "node": "retrieve",
                    "ts": time.time(),
                    "ok": True,
                    "collection": collection,
                    "result_count": total_count,
                    "sub_query_count": len(sub_queries),
                }
            ],
        }

    async def _generate_node(self, state: RAGState) -> Dict[str, Any]:
        """生成回复节点（支持内部流式输出）"""
        self._emit_trace("generate", "node_start", "running")

        # 这轮回答实际用到了哪些知识库——从 tool_subgraph 留下的
        # tool_execution_trace 里挑 query_knowledge_hub 且真的查到结果的那几条
        # （result.structured_data.get("collection")，见 subgraph.py tool_node
        # 旁的说明），去重后给前端渲染"来源知识库"角标用。检索失败/ACL 拒绝的
        # 那次调用不会有 collection（MCPToolResponse._build_metadata 只在有
        # collection 参数时才写入，空结果/拒绝走的是另一个分支），不会误标。
        kb_sources = sorted({
            c
            for t in state.get("tool_execution_trace", [])
            if t.get("tool_name") == "query_knowledge_hub"
            for c in (t.get("collections") or [])
        })

        # ACL 拒绝的提示必须原样透传给用户，不能再交给 LLM"基于工具结果生成回答"——
        # 那一步的 prompt 措辞（"请给出准确、有用的回答"）会诱导本地模型把明确的拒绝
        # 重新包装成一段编造的正面回答，用户完全看不出自己被权限挡住了。这里直接跳过
        # LLM 调用，保证拒绝原因原封不动地到达用户。
        tool_summary = state.get("tool_summary", "")
        if tool_summary.startswith("## 无权访问"):
            if self._token_queue is not None:
                await self._token_queue.put(tool_summary)
            assistant_message = AIMessage(content=tool_summary)
            self._emit_trace("generate", "node_end", "success", {"short_circuit": "access_denied"})
            return {
                "messages": [assistant_message],
                "final_answer": tool_summary,
                "used_model": "n/a (access denied, no LLM call)",
                "kb_sources": kb_sources,
                "trace_events": [
                    *state.get("trace_events", []),
                    {"node": "generate", "ts": time.time(), "model": "n/a"}
                ],
            }

        # 构建 prompt
        self._emit_trace("generate", "prompt_build", "running")
        prompt = self._build_prompt(state)
        self._emit_trace("generate", "prompt_build", "success", {"prompt_length": len(prompt)})
        
        # 调用 LLM（流式收集，同时透传 token）
        self._emit_trace("generate", "llm_stream", "running")
        try:
            chunks = []
            async for chunk in self._llm.astream([HumanMessage(content=prompt)]):
                chunks.append(chunk)
                if self._token_queue is not None:
                    await self._token_queue.put(chunk.content)
            
            answer = "".join(c.content for c in chunks)
            model_name = getattr(self._llm, "model_name", "unknown")
            self._emit_trace("generate", "llm_stream", "success", {
                "model": model_name,
                "token_count": len(chunks),
            })
        except Exception as e:
            answer = f"生成失败：{str(e)}"
            model_name = "error"
            self._emit_trace("generate", "llm_stream", "error", {"error": str(e)})
        
        # 添加助手回复到 messages
        assistant_message = AIMessage(content=answer)
        
        self._emit_trace("generate", "node_end", "success" if model_name != "error" else "error")
        return {
            "messages": [assistant_message],  # add_messages 会追加
            "final_answer": answer,
            "used_model": model_name,
            "kb_sources": kb_sources,
            "trace_events": [
                *state.get("trace_events", []),
                {"node": "generate", "ts": time.time(), "model": model_name}
            ],
        }

    def _build_prompt(self, state: RAGState) -> str:
        """构建生成 prompt"""

        # 格式化最近对话历史（仅用于展示，实际 history 通过 messages 传递）
        recent_history = self._format_recent_messages(state.get("messages", []))

        # 判断是否有工具执行结果需要注入
        tool_summary = state.get("tool_summary", "")
        tool_section = ""
        if tool_summary:
            tool_section = f"""
【工具执行结果】
{tool_summary}
"""

        # 意图分类判定这是该查公司知识库的问题（target_tool=query_knowledge_hub），
        # 但最终没能拿到任何有用的知识库内容时，明确要求 LLM 用固定模板告知
        # 用户——不能像之前那样，不管有没有查到、查没查过，都照样生成一段看起来
        # 像是"查出来的"通用建议（真实踩过的坑：VPN/钓鱼邮件那两次复现，见调试
        # 记录）。这里没查到有用内容分两种情况，都要覆盖：
        # 1. 真的查了但没查到相关的（attempted_collections 非空、collections
        #    空，两个字段的区别见 subgraph.py tool_node 旁的说明）——能报出
        #    具体查了哪几个知识库。
        # 2. 工具子图的 think 节点自己判断"这个问题不像是知识库能答的"，压根
        #    没调用 query_knowledge_hub（tool_execution_trace 里连一条
        #    query_knowledge_hub 记录都没有）——报不出具体查了哪个库，只能
        #    笼统说"公司知识库"。
        # 只处理 query_knowledge_hub 这一个工具，_retrieve_node（用户自己上传
        # 文件的 rag 分支）不在这个模板范围内，两者语义不同，不能共用一套措辞。
        empty_kb_notice = ""
        if state.get("target_tool") == "query_knowledge_hub":
            kb_trace_entries = [
                t for t in state.get("tool_execution_trace", [])
                if t.get("tool_name") == "query_knowledge_hub"
            ]
            attempted = sorted({c for t in kb_trace_entries for c in (t.get("attempted_collections") or [])})
            hit = sorted({c for t in kb_trace_entries for c in (t.get("collections") or [])})
            if not hit:
                if attempted:
                    from src.mcp_server.tools.query_knowledge_hub import DEPARTMENT_KB_COLLECTIONS

                    def _kb_label(c: str) -> str:
                        if c in DEPARTMENT_KB_COLLECTIONS:
                            return DEPARTMENT_KB_COLLECTIONS[c]
                        # 委托模式（tenant_{org_id}_kb[:子库标签]）——原始 slug 带着
                        # 企业的 org_id（一串 UUID），直接展示会很生硬，退回跟前端
                        # kbMeta() 一致的"本企业知识库"泛称（见 TopNav.jsx）。
                        if c.startswith("tenant_") and "_kb" in c:
                            sub_label = c.split(":", 1)[1] if ":" in c else ""
                            return f"本企业知识库 · {sub_label}" if sub_label else "本企业知识库"
                        return c

                    labels = [_kb_label(c) for c in attempted]
                    labels_text = "、".join(f"【{label}】" for label in labels)
                else:
                    # 工具压根没被调用，报不出具体库名，用泛称
                    labels_text = "【公司知识库】"
                original_query = state.get("query", "")
                empty_kb_notice = f"""
【重要】公司知识库（{labels_text}）里没有查到跟用户问题直接相关的内容。你的回答必须
严格按下面的格式组织，不能把接下来给的通用建议包装成"知识库里查到的"内容：
1. 第一句明确说："抱歉，在公司内部{labels_text}中未找到关于"{original_query}"的直接相关内容。"
2. 空一行，另起一段以"🔍 基于行业通用经验，您可以尝试以下方向："开头，给 2-3 条通用性建议
   （只给业界通用的常识性做法，不要编造具体的公司制度、流程、联系方式或网址）。
3. 这两部分之间不要出现"根据检索结果""知识库显示""公司规定"之类暗示这是公司内部
   资料的措辞——用户必须能一眼看出第二部分是你自己的通用知识，不是公司知识库内容。
"""

        prompt = ChatPromptTemplate.from_template("""你是企业级知识库助手，基于检索结果、工具执行结果、对话历史和用户长期记忆回答用户问题。

【用户长期记忆】
{memories}

【历史摘要】
{summary}

【最近对话】
{recent_history}
{tool_section}
{empty_kb_notice}
【检索上下文】
{context}

【用户问题】
{query}

请给出准确、有用的回答。如果回答内容是结构化/规则化的多条记录（比如考勤打卡记录、
多天/多个对象的数据罗列，每条记录字段相同），用 Markdown 表格呈现，不要写成
一条条并列的自然语言句子；只有一两条零散信息、或者内容本身不是表格结构时，
照常用普通文字或列表回答，不要为了用表格而硬凑表格：""")

        memories_text = "\n".join(f"- {m}" for m in state.get("memories", [])) or "无"
        return prompt.format(
            memories=memories_text,
            summary=state.get("summary", ""),
            recent_history=recent_history,
            tool_section=tool_section,
            empty_kb_notice=empty_kb_notice,
            context=state.get("retrieval_context", ""),
            query=state.get("query", ""),
        )

    def _format_recent_messages(self, messages: List[AnyMessage]) -> str:
        """格式化最近的消息为文本"""
        return "\n".join([
            f"User: {m.content}" if isinstance(m, HumanMessage) else f"Assistant: {m.content}"
            for m in messages[-6:]  # 最近3轮（6条消息）
        ])

    async def _memory_manage_node(self, state: RAGState) -> Dict[str, Any]:
        """
        记忆管理节点
        
        核心逻辑：
        1. 检查消息数量是否超出限制
        2. 如果超出，使用 RemoveMessage 删除旧消息
        3. 将删除的消息合并到 summary 中
        4. 标记待归档的消息供 archive 节点使用
        """
        self._emit_trace("memory_manage", "node_start", "running")
        messages = state.get("messages", [])
        
        # 检查结果
        result = {
            "_to_archive": [],  # 待归档的消息
        }
        
        # 检查是否需要压缩
        self._emit_trace("memory_manage", "compact_check", "running", {"message_count": len(messages)})
        if not self._memory_manager.should_compact(messages):
            # 不需要压缩，但本轮新消息仍需归档
            # archive 节点会处理
            self._emit_trace("memory_manage", "compact_check", "success", {"need_compact": False})
            self._emit_trace("memory_manage", "node_end", "success")
            return result
        
        self._emit_trace("memory_manage", "compact_check", "success", {"need_compact": True})
        
        # 执行压缩
        self._emit_trace("memory_manage", "memory_compact", "running")
        try:
            to_keep, new_summary, archived_data = await self._memory_manager.compact(
                messages=messages,
                current_summary=state.get("summary", ""),
                llm=self._llm
            )
            
            # 生成 RemoveMessage 操作（关键！）
            keep_ids = {m.id for m in to_keep}
            delete_ops = [
                RemoveMessage(id=m.id)
                for m in messages
                if m.id not in keep_ids
            ]
            
            print(f"[MemoryManage] Compacting: {len(messages)} -> {len(to_keep)} messages, "
                  f"archived {len(archived_data)}, summary length {len(new_summary)}")
            
            self._emit_trace("memory_manage", "memory_compact", "success", {
                "before_count": len(messages),
                "after_count": len(to_keep),
                "archived_count": len(archived_data),
            })
            self._emit_trace("memory_manage", "node_end", "success")
            
            return {
                "messages": delete_ops,           # LangGraph 会处理删除
                "summary": new_summary,           # 更新摘要
                "_to_archive": archived_data,     # 标记待归档
            }
        except Exception as e:
            print(f"[MemoryManage] Compact failed: {e}")
            self._emit_trace("memory_manage", "memory_compact", "error", {"error": str(e)})
            self._emit_trace("memory_manage", "node_end", "error")
            return result

    async def _archive_node(self, state: RAGState) -> Dict[str, Any]:
        """
        归档节点
        
        总是运行，负责：
        1. 将被压缩的消息归档到 PostgreSQL
        2. 将本轮新消息归档到 PostgreSQL
        3. 从本轮 Q&A 中提取长期记忆（LTM）
        
        使用 asyncio.create_task 异步执行，不阻塞响应
        """
        self._emit_trace("archive", "node_start", "running")
        conversation_id = state["conversation_id"]
        
        # 1. 获取被压缩的消息（如果有）
        archived = state.pop("_to_archive", [])
        
        # 2. 准备本轮的新消息（从 messages 中提取本轮的对话）
        messages = state.get("messages", [])
        current_turn_msgs = []
        
        # 本轮最后两条应该是 user query 和 assistant answer
        if len(messages) >= 2:
            for m in messages[-2:]:
                current_turn_msgs.append({
                    "role": "user" if isinstance(m, HumanMessage) else "assistant",
                    "content": m.content,
                    "message_id": m.id,
                    "ts": time.time()
                })
        
        # 3. 合并：压缩的消息 + 本轮消息
        all_to_archive = archived + current_turn_msgs
        
        turn_id = state.get("current_turn_id")
        
        # 4. 异步保存（添加异常处理回调）
        if all_to_archive:
            task = asyncio.create_task(
                self._store.append_to_history(conversation_id, all_to_archive, turn_id=turn_id)
            )
            self._background_tasks.add(task)

            # 添加完成回调，处理异常
            def on_done(t):
                self._background_tasks.discard(t)
                try:
                    t.result()
                    print(f"[Archive] Saved {len(all_to_archive)} messages for {conversation_id}")
                except Exception as e:
                    print(f"[Archive] Failed to save history: {e}")

            task.add_done_callback(on_done)

        # 5. 长期记忆提取（异步，不阻塞响应）
        user_id = state.get("user_id")
        if self._ltm_store and user_id and len(messages) >= 2:
            query = state.get("query", "")
            answer = state.get("final_answer", "")
            if query and answer:
                async def _extract_and_save():
                    try:
                        facts = await self._ltm_store.extract_facts(query, answer, self._llm)
                        if facts:
                            await self._ltm_store.save_facts(user_id, facts, conversation_id=conversation_id, turn_id=turn_id)
                            print(f"[Archive] Extracted {len(facts)} LTM facts for user {user_id}")
                    except Exception as e:
                        print(f"[Archive] LTM extraction failed: {e!r}")

                ltm_task = asyncio.create_task(_extract_and_save())
                self._background_tasks.add(ltm_task)
                ltm_task.add_done_callback(self._background_tasks.discard)
        
        # 添加追踪事件
        state.setdefault("trace_events", []).append(
            {"node": "archive", "ts": time.time(), "ok": True, "archived_count": len(all_to_archive)}
        )
        
        self._emit_trace("archive", "node_end", "success")
        return {}

    def get_memory_stats(self, state: RAGState) -> Dict:
        """获取记忆统计信息"""
        return self._memory_manager.get_stats(
            state.get("messages", []),
            state.get("summary", "")
        )
