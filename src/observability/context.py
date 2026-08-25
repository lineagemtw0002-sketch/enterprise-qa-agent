"""请求上下文（`request_id` / `org_id` / …）—— 基于 contextvars 的按请求隔离。

这是 `docs/observability_design.md` §2.1 的落地：日志里的 `request_id` 不靠 53 个
调用点手填，而是绑在**当前异步上下文**上，由 `ContextInjectingFilter` 统一注入。

为什么用 contextvars 而不是实例属性/全局变量
--------------------------------------------------
`create_app()` 全进程只构造一个 `RAGWorkflow` 给所有请求共用（P0-1 的成因）。
任何"挂在实例上"的 per-request 状态在并发下必然互相覆盖。contextvars 是
asyncio 原生的按 Task 隔离机制：

- `asyncio.create_task(...)` **复制创建时刻的上下文**，后台任务自动继承 request_id
- `asyncio.to_thread(...)` 内部走 `contextvars.copy_context()`，线程里也能读到
- 不同 Task 之间彼此独立，天然不串

沿用 P0-1 已验证的两条纪律（`src/ragent_backend/workflow.py` 顶部同一套模式）
--------------------------------------------------
1. **`set()` 必须在 `asyncio.create_task(...)` 之前** —— 子任务复制的是"创建那一刻"
   的上下文快照，先建任务再 set，子任务读到的是 None。
2. **清理用 `set(None)`，不用 `reset(token)`** —— 异步生成器的 finally 所处的上下文
   未必是当初 set 的那个，`reset` 跨上下文会抛 `ValueError`，反而把真正的退出原因
   盖掉。所以本模块**刻意不提供** `reset(token)` 形态的 API。

一个反直觉但正确的行为：主协程 `clear_request_context()` **不会**影响已经
`create_task` 出去的后台任务（归档、LTM 抽取），它们持有的是创建时刻的副本。
这正是想要的——请求结束了，后台任务的日志仍然要带着那次请求的 id。
"""

from __future__ import annotations

import contextvars
import re
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import Any, Dict, Iterator, Optional

__all__ = [
    "RequestContext",
    "CONTEXT_FIELDS",
    "bind_request_context",
    "get_request_context",
    "clear_request_context",
    "context_as_dict",
    "request_context",
    "new_request_id",
    "sanitize_request_id",
]


# 入站 `X-Request-Id` 是**外部可控字符串**，不校验就直接进日志等于开了一个日志注入面
# （换行伪造整行日志、超长字符串撑爆行）。限长 + 字符白名单。
_REQUEST_ID_MAX_LEN = 128
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass(frozen=True)
class RequestContext:
    """一次请求的链路标识。

    **frozen** 是刻意的：上下文只能整体替换，不能被某个节点就地改掉一个字段——
    否则"谁改了我的 org_id"会变成一类极难排查的问题。
    """

    request_id: Optional[str] = None
    org_id: Optional[str] = None
    user_id: Optional[str] = None
    conversation_id: Optional[str] = None
    turn_id: Optional[str] = None
    # D-1：`task_id` 与 `request_id` **并存**。task_id 是既有的业务侧标识
    # （前端可指定、已落进 messages 归档表），request_id 是新的链路主键。
    task_id: Optional[str] = None
    route: Optional[str] = None


#: 日志里会被自动注入的字段名（顺序即 JSON 里的出现顺序）
CONTEXT_FIELDS = (
    "request_id",
    "org_id",
    "user_id",
    "conversation_id",
    "turn_id",
    "task_id",
    "route",
)

_REQUEST_CTX: contextvars.ContextVar[Optional[RequestContext]] = contextvars.ContextVar(
    "ragent_request_context", default=None
)


def new_request_id() -> str:
    """生成一个 16 位十六进制 id。

    刻意对齐既有 `task_id` 的风格（`app.py` 里的 `os.urandom(8).hex()`），
    这样两个 id 放在一起时长得一样、不会让人误以为是两种东西。
    """
    return uuid.uuid4().hex[:16]


def sanitize_request_id(raw: Optional[str]) -> Optional[str]:
    """校验入站 request id；不合法返回 ``None``（调用方应改为自己生成）。

    规则：非空、≤128 字符、只允许 ``[A-Za-z0-9_-]``。
    换行/空格/控制字符一律拒绝——它们是日志注入的载体。
    """
    if not raw or not isinstance(raw, str):
        return None
    raw = raw.strip()
    if not raw or len(raw) > _REQUEST_ID_MAX_LEN:
        return None
    if not _REQUEST_ID_RE.match(raw):
        return None
    return raw


def get_request_context() -> Optional[RequestContext]:
    """读取当前上下文；没有绑定过则返回 ``None``（常驻任务/健康检查就是这种）。"""
    return _REQUEST_CTX.get()


def bind_request_context(**fields: Any) -> RequestContext:
    """绑定/补充当前上下文，返回绑定后的新对象。

    **合并语义**：只传 `org_id` 时保留已有的 `request_id`。这样中间件先绑
    `request_id`，端点拿到用户身份后再补 `org_id`/`user_id`，不必重复传。

    未传 `request_id` 且当前也没有时，自动生成一个——保证"日志里永远有 id"。
    """
    unknown = set(fields) - set(CONTEXT_FIELDS)
    if unknown:
        raise TypeError(f"unknown request context field(s): {sorted(unknown)}")

    current = _REQUEST_CTX.get()
    if current is None:
        ctx = RequestContext(**fields)
    else:
        ctx = replace(current, **fields)

    if not ctx.request_id:
        ctx = replace(ctx, request_id=new_request_id())

    _REQUEST_CTX.set(ctx)
    return ctx


def clear_request_context() -> None:
    """清理当前上下文。

    ⚠️ 用 ``set(None)`` 而不是 ``reset(token)``——见模块 docstring 第 2 条纪律。
    """
    _REQUEST_CTX.set(None)


def context_as_dict() -> Dict[str, Optional[str]]:
    """当前上下文的字段字典；未绑定时全部为 ``None``（**字段存在、值为 null**）。

    "缺字段"和"字段为 null"对下游查询是两回事：前者会让
    `jq 'select(.request_id==null)'` 之类的查询漏掉这些行。所以这里始终给全字段。
    """
    ctx = _REQUEST_CTX.get()
    if ctx is None:
        return {name: None for name in CONTEXT_FIELDS}
    return {name: getattr(ctx, name) for name in CONTEXT_FIELDS}


@contextmanager
def request_context(**fields: Any) -> Iterator[RequestContext]:
    """`with` 形态的绑定（测试与脚本用）。

    退出时**恢复进入前的值**（用 `set(prev)`，不是 `reset(token)`）。
    """
    previous = _REQUEST_CTX.get()
    try:
        yield bind_request_context(**fields)
    finally:
        _REQUEST_CTX.set(previous)
