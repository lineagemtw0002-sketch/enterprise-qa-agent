"""日志脱敏 —— `docs/observability_design.md` §2.4 的三级分级落地。

设计约束（§4 第 3 条）：**`redact()` 必须是纯函数**，输入输出都是 dict、零 IO、
不读环境变量、不看时钟。开关由调用方以参数传入。这样它能像 `resolve_jwt_secret`
那样被直接单测——不需要起 app、不需要挂 handler、不需要 DB。

分级
----
========  ====================================================  ==================================
级别      内容                                                  策略
========  ====================================================  ==================================
S0        id / 计数 / 耗时 / 模型名 / 分数 / 状态 / 布尔标志     永远记原值
S1        chunk_id / collection / source / user_id / org_id      记原值；`strict=True` 时降级为哈希
S2        用户提问 / 改写 query / chunk 原文 / 模型回答 /        默认只记 ``_len`` + ``_sha256``；
          LTM 事实 / 工作流表单值 / thought                     ``log_content=True`` 时才记原文
S2P       **最终 prompt 原文**、泄露缓冲区                       **任何开关下都不记原文**
========  ====================================================  ==================================

为什么 S2 记 hash 不是形式主义
--------------------------------
- 能判断"这两次是不是同一个问题 / 同一份 prompt" → 可做 A/B 归因
- 用户报障时把原文给你，你自己 hash 一遍去 grep → 能定位到具体请求
- 而日志本身不含内容

**内容的权威副本在业务库**（`conversation_archive` 表存着模型说了什么），
日志只负责链路归因，不必再存一份。这是 S2 策略能成立的根本原因。

为什么 S2+ 的最终 prompt 一条路都不留
--------------------------------------
最终 prompt = 系统提示词 + 检索到的 chunk 原文 + 历史对话 + LTM 事实，
是本系统里敏感度最高的**单个字符串**；而 `docs/security_prompt_injection_test_report.md`
已确认系统提示词泄露是真实问题。日志的访问控制天然弱于 API（会被采集、被截图、
进备份），记进去等于多开一个泄露面。
重建它靠 ``prompt_sha256`` + ``prompt_template_version`` + ``prompt_len`` +
``context_chunk_ids`` 就够了——**重建需要有权限的人主动做，而不是躺在日志里等人读。**

未知字段默认 S2
----------------
风险 R3 是"新增字段忘了分级"。所以**未在表里的字符串字段一律按 S2 处理**，
宁可把一个无害字段哈希掉，也不要把一个内容字段漏出去。数值/布尔天然不承载内容，
按 S0 放行。要让新字段记原值，就必须显式加进 ``S0_FIELDS`` / ``S1_FIELDS``——
这个"必须显式登记"正是想要的摩擦。

⚠️ 本模块只管 ``extra=`` 里的**结构化字段**，管不了日志 message 本身。
敏感内容必须放进 extra，**绝不能拼进 message 字符串**。
（`ltm_store.py` 原来的 ``print(f"... raw content={content!r}")`` 就是反例。）
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Mapping, Optional

__all__ = [
    "S0",
    "S1",
    "S2",
    "S2_PLUS",
    "classify_field",
    "hash_value",
    "redact",
    "redact_value",
    "sensitive_digest",
]

S0 = "S0"
S1 = "S1"
S2 = "S2"
S2_PLUS = "S2+"

#: 哈希摘要保留位数（§2.4 定的 12 位；足够区分，不足以暴力还原短文本以外的内容）
_HASH_PREFIX_LEN = 12


# ── 分级表 ──────────────────────────────────────────────────────────
# 新增字段请在这里显式登记，否则按未知字段（S2）处理。

S0_FIELDS = frozenset({
    # 链路标识
    "request_id", "task_id", "trace_id", "turn_id", "span_id",
    "event", "node", "step", "status", "level", "logger", "message", "timestamp",
    "trace_type", "stage", "stages", "phase",
    # 模型与配置（模型名不是内容）
    "model", "used_model", "intent_model", "embedding_model", "reranker_model",
    "llm_model", "prompt_template_version", "config_sha256", "provider",
    # 意图/路由判定（本项目双模型分工的归因字段）
    "intent_type", "intent_confidence", "need_clarify", "target_tool",
    "target_workflow_type", "rewrite_applied", "chitchat_whitelist_hit",
    "fallback_path", "fallback_used", "short_circuit", "clarify_reason",
    "prompt_leak_blocked", "workflow_event", "agent", "tool_name", "action",
    # 计数/耗时/分数
    "top_k", "result_count", "sub_query_count", "candidate_count",
    "archived_count", "ltm_facts_count", "ltm_recalled_count", "message_count",
    "message_count_before", "message_count_after", "need_compact", "compact_ok",
    "has_summary", "is_new_conversation", "iteration", "iterations_used",
    "hit_max_iterations", "success", "rerank_applied", "injection_filtered_count",
    "filtered_by_relevance_count", "stream_chunk_count", "field_extract_ok",
    "missing_field_count", "bg_task_failed", "estimated", "error_type",
    "retention_days", "log_content_enabled",
})

S1_FIELDS = frozenset({
    # 准标识：能查案但不泄露内容。这正是 OWASP LLM08 要的
    # 「谁、在什么租户上下文下、检索到了哪些文档 ID」。
    "org_id", "user_id", "username", "conversation_id", "route",
    "collection", "collection_name", "candidate_collections", "collections",
    "chunk_id", "chunk_ids",
    "source", "sources", "kb_sources", "document_id", "file_id", "title",
    "template_id", "instance_id", "approver_role_id", "requester_org_id",
    "resource_id", "resource_type", "path", "method",
})

S2_FIELDS = frozenset({
    "query", "original_query", "rewritten_query", "sub_queries", "question",
    "answer", "response", "content", "text", "chunk_text",
    "summary", "existing_summary", "thought", "reasoning", "clarify_prompt",
    "fact", "facts", "ltm_facts", "memory", "detail_text", "form_values",
    "field_values", "raw_content", "tool_output", "output", "input",
    "user_agent", "client_ip",
})

S2_PLUS_FIELDS = frozenset({
    # 无论 log_content 开关，都不记原文
    "prompt", "final_prompt", "system_prompt", "prompt_text", "full_prompt",
    "buffer_preview", "leaked_buffer", "messages",
})

#: 这些字段是「参数字典」：只留键名，值一律丢弃。
#: 由来：`subgraph.py` 原本记 `{"args": {...}}` 全值，对 `query_knowledge_hub`
#: 而言那个值就是用户的问题。
ARG_DICT_FIELDS = frozenset({"args", "tool_args", "kwargs", "params", "payload_args"})

#: 后缀规则：形如 `xxx_ms` / `xxx_count` 的字段天然是 S0，不必逐个登记。
_S0_SUFFIXES = (
    "_ms", "_count", "_len", "_length", "_score", "_tokens", "_ratio",
    "_version", "_sha256", "_size", "_bytes", "_index", "_seconds",
)
_S0_PREFIXES = ("is_", "has_", "can_", "should_", "n_", "num_")
#: 任何 `xxx_id` / `xxx_ids` 都是标识符而不是内容——按 S1（准标识）处理。
#: 显式表优先级更高，所以 `request_id` / `task_id` 仍是 S0。
#: ⚠️ 刻意**不含** `_name`：`file_name` / `doc_name` 这类是用户上传的文件名，
#: 属于内容。要放行就显式登记（见 `collection_name`）。
_S1_SUFFIXES = ("_id", "_ids", "_type", "_role")


def classify_field(name: str, value: Any = None) -> str:
    """判定字段的敏感级别。

    优先级：显式表 > 后缀/前缀规则 > 容器（递归）> 值类型 > **未知字符串默认 S2**。
    """
    if name in S2_PLUS_FIELDS:
        return S2_PLUS
    if name in S0_FIELDS:
        return S0
    if name in S1_FIELDS:
        return S1
    if name in S2_FIELDS or name in ARG_DICT_FIELDS:
        return S2
    if name.endswith(_S0_SUFFIXES) or name.startswith(_S0_PREFIXES):
        return S0
    if name.endswith(_S1_SUFFIXES):
        return S1

    # 容器：不在这一层判，而是**递归进去逐个叶子判**。
    # 这样 `chunks=[{"chunk_id": ..., "text": ...}]` 能做到 id 留下、原文哈希掉，
    # 而不是把整个列表一刀切成一个摘要。
    if isinstance(value, Mapping):
        return S0
    if isinstance(value, (list, tuple)):
        # ⚠️ 但"列表里直接躺着字符串"必须当内容处理——递归对标量是无操作的，
        # 放行就等于漏。例如 `notes: ["用户说他要离职"]`。
        if any(isinstance(v, str) for v in value):
            return S2
        return S0

    # 数值/布尔/None 不承载内容
    if value is None or isinstance(value, (bool, int, float)):
        return S0
    return S2


def hash_value(value: Any) -> str:
    """内容的稳定摘要：``sha256`` 前 12 位。

    非字符串先做**确定性** JSON 序列化（``sort_keys``），保证同一内容永远同一摘要。
    """
    if isinstance(value, str):
        raw = value
    else:
        try:
            raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        except (TypeError, ValueError):
            raw = str(value)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:_HASH_PREFIX_LEN]


def _content_len(value: Any) -> int:
    """内容"长度"：字符串按字符数，容器按元素数，其余按 str 化后的字符数。"""
    if isinstance(value, str):
        return len(value)
    if isinstance(value, (list, tuple, dict, set)):
        return len(value)
    return len(str(value))


def _digest_pair(name: str, value: Any) -> Dict[str, Any]:
    """把一个内容字段替换成 ``{name}_len`` + ``{name}_sha256`` 两个 S0 字段。

    ``None`` 单独处理：长度 0、摘要 ``None``。否则 ``None`` 会被 hash 成
    ``str(None)`` 的摘要，让"字段缺失"和"内容恰好是字符串 'None'"分不开。
    """
    if value is None:
        return {f"{name}_len": 0, f"{name}_sha256": None}
    return {f"{name}_len": _content_len(value), f"{name}_sha256": hash_value(value)}


def redact_value(
    name: str,
    value: Any,
    *,
    log_content: bool = False,
    strict: bool = False,
) -> Dict[str, Any]:
    """脱敏单个字段，返回**替换后的若干字段**（可能是 0、1 或 2 个）。

    返回 dict 而非单值，是因为一个 S2 字段会展开成 ``_len`` + ``_sha256`` 两个。
    """
    level = classify_field(name, value)

    if level == S0:
        return {name: _redact_container(value, log_content=log_content, strict=strict)}

    if level == S1:
        if strict:
            # `_unassigned`（不知道属于谁）的日志按最严处理：连准标识也降级。
            # "不知道属于谁"的日志最危险——没有 org 边界可以约束谁能读它。
            return _digest_pair(name, value)
        return {name: value}

    if name in ARG_DICT_FIELDS:
        # 参数字典：只留键名。键名是 S0（`collection_name` 这种），值才是内容。
        if isinstance(value, Mapping):
            return {f"{name}_keys": sorted(str(k) for k in value.keys())}
        return _digest_pair(name, value)

    if level == S2 and log_content:
        # 排障期临时开关：S2 记原文（S2+ 不受影响，见下）
        return {name: value}

    return _digest_pair(name, value)


def _redact_container(value: Any, *, log_content: bool, strict: bool) -> Any:
    """S0 字段内部若还嵌套着 dict/list，继续往下脱敏。

    典型场景：`extra={"payload": {...}}` —— payload 本身是结构，里面才是内容。
    """
    if isinstance(value, Mapping):
        return redact(value, log_content=log_content, strict=strict)
    if isinstance(value, list):
        return [
            _redact_container(v, log_content=log_content, strict=strict)
            if isinstance(v, (Mapping, list))
            else v
            for v in value
        ]
    return value


def redact(
    event: Mapping[str, Any],
    *,
    log_content: bool = False,
    strict: bool = False,
) -> Dict[str, Any]:
    """按分级表脱敏一个事件 dict，返回**新** dict（不改入参）。

    Args:
        event: 结构化事件/日志 extra 字段。
        log_content: ``RAGENT_LOG_CONTENT`` 开关。``True`` 时 S2 记原文，
            **S2+ 仍然不记**——这条没有任何开关能打开。
        strict: 最严模式（无 org 归属的日志）。S1 也降级为哈希。

    Returns:
        脱敏后的新 dict。**幂等**：对已脱敏结果再跑一遍结果不变
        （``xxx_len`` / ``xxx_sha256`` 命中 S0 后缀规则，原字段已消失）。
    """
    out: Dict[str, Any] = {}
    for name, value in event.items():
        out.update(redact_value(str(name), value, log_content=log_content, strict=strict))
    return out


def sensitive_digest(name: str, value: Any) -> Dict[str, Any]:
    """便捷函数：显式给一个内容字段生成 ``_len`` + ``_sha256``。

    给调用点用（例如 `ltm_store` 记模型原始输出时），让"我知道这是敏感内容"
    这件事在调用点就写明白，而不是全靠脱敏器兜底。
    """
    return _digest_pair(name, value)
