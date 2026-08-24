"""提示词破解 / 幻觉相关的规则式检测。

对应 `docs/security_prompt_injection_test_report.md` 确认的问题、
`docs/prompt_injection_remediation_plan.md` 给出的修复方案——本模块只放
"检测"这一层（正则/关键词匹配），"检测到之后怎么处理"（拒绝摄入 / 拦截
替换 / 只打审计标记）由各自的调用方（`src/ingestion/pipeline.py`、
`src/ragent_backend/workflow.py`）决定。两边共用同一份规则，避免测试报告
问题1指出的"英文话术能过、中文话术被拦"这种语言相关的实现分叉——报告
发现现有防护对这两种语言的敏感度不一致，排查后确认当时代码里其实完全没
有防护，这里统一按中英文两套规则收录，不做语言特判。

这里全部是规则/关键词匹配，不是可靠的绝对防御——LLM 层面的提示词注入
目前没有 100% 可靠的检测手段，这一层的目标是"显著提高攻破门槛、挡住测试
中验证过的高频手法"，需要跟 Prompt 模板里的指令层级声明配合使用，互为
补充，不能只靠这一层。
"""

from __future__ import annotations

import re
from typing import Optional

# ---------------------------------------------------------------------------
# 问题2：文档摄入时的注入特征检测（ingestion/pipeline.py 用）
# 覆盖测试报告案例2里使用的"伪装系统声明"手法。
# ---------------------------------------------------------------------------
_DOCUMENT_INJECTION_PATTERNS = [
    re.compile(r"SYSTEM\s+INSTRUCTION", re.IGNORECASE),
    re.compile(r"PRIORITY\s+OVERRIDE", re.IGNORECASE),
    re.compile(r"AUTHORIZED\s+BY", re.IGNORECASE),
    re.compile(r"\[\s*END\s+SYSTEM", re.IGNORECASE),
    re.compile(r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions", re.IGNORECASE),
    re.compile(r"you\s+are\s+now\s+in\s+developer\s+(debug\s+)?mode", re.IGNORECASE),
    re.compile(r"忽略(你)?(之前|以上|上述|原本)(收到的|设定的)?(所有)?指令"),
    re.compile(r"(无限制模式|开发者调试模式)"),
    re.compile(r"跳过(所有)?(企业)?知识库的?权限校验"),
]


def detect_document_injection(text: str) -> Optional[str]:
    """扫描待摄入文档的原始文本，命中任一特征时返回匹配到的原始片段
    （前后各扩 20~40 字，供错误提示/日志使用）；全部不命中返回 None。"""
    for pattern in _DOCUMENT_INJECTION_PATTERNS:
        m = pattern.search(text)
        if m:
            start = max(0, m.start() - 20)
            end = min(len(text), m.end() + 40)
            return text[start:end].strip()
    return None


# ---------------------------------------------------------------------------
# 问题1：模型输出侧"系统提示词泄露"检测（ragent_backend/workflow.py 用）
# 命中模板专属分隔符，或者跟模板开头几句话逐字重合，判定为疑似真实泄露——
# 模型编造的假提示词措辞相似但对不上这几个具体分隔符/开头字样（见测试
# 报告案例1的对照组）。
# ---------------------------------------------------------------------------
_PROMPT_LEAK_MARKERS = (
    "【用户长期记忆】",
    "【历史摘要】",
    "【最近对话】",
    "【检索上下文】",
    "【工具执行结果】",
)
# 模板开头这句话，真实泄露会逐字复现；模型编造的假提示词措辞相似但对不上。
_PROMPT_TEMPLATE_OPENING = "你是企业级知识库助手，基于检索结果"


def looks_like_prompt_leak(text: str) -> bool:
    """判断一段模型输出是否疑似真实泄露了系统 Prompt 模板本身。"""
    if _PROMPT_TEMPLATE_OPENING in text:
        return True
    return any(marker in text for marker in _PROMPT_LEAK_MARKERS)


# ---------------------------------------------------------------------------
# 问题3：用户输入侧"冒充身份要求跳过权限"检测（ragent_backend/workflow.py
# 用，只用于审计打标，不拦截——真正的权限判断永远在工具调用层的 ACL，
# 命中这里不改变本次请求的处理结果，只是多记一条审计事件，方便后续做
# 异常检测，见 remediation_plan.md 问题3方案2）。
# ---------------------------------------------------------------------------
_PRIVILEGE_CLAIM_PATTERNS = [
    re.compile(r"(我是|作为)\s*(super[_\s]?admin|超级管理员|管理员|admin)\b", re.IGNORECASE),
    re.compile(
        r"(跳过|绕过|disable|skip|bypass)[^\n]{0,15}(权限|校验|检查|限制|permission|check|restriction)",
        re.IGNORECASE,
    ),
    re.compile(r"(临时)?提升(我的)?权限"),
]


def detect_privilege_claim(text: str) -> bool:
    """判断用户这句话是否在尝试用"自称身份"操纵权限判断。命中不代表真的
    越权成功（工具层 ACL 才是唯一的权限判断依据），只用于打审计标记。"""
    return any(pattern.search(text) for pattern in _PRIVILEGE_CLAIM_PATTERNS)
