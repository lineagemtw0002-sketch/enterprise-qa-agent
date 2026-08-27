"""结构化日志基础设施。

提供：

- ``configure_logging``：**显式、幂等**的一次性根 logger 配置（取代 ``basicConfig``）
- ``get_logger``：签名不变，内部改为委托 ``configure_logging``
- ``JSONFormatter``：一行一个 JSON 对象，``extra=`` 字段平铺进顶层
- ``ContextInjectingFilter``：把当前请求上下文（``request_id`` 等）自动贴到每条日志
- ``RedactingFilter``：按 S0/S1/S2 分级脱敏 ``extra=`` 字段
- ``get_trace_logger``：JSON Lines 文件 logger（**已改为按天轮转**）

为什么不是 ``logging.basicConfig``
-----------------------------------
``basicConfig`` 只在 root 没有 handler 时生效 → **第一个调用者决定全进程的 level
和格式，后来者的 ``log_level`` 参数静默失效**；而且它把格式写死成人类可读、输出写死
stderr，这条路上根本拿不到 JSON。``configure_logging`` 把这些都变成显式参数，
并且**重复调用会更新既有配置**而不是静默忽略。

为什么脱敏做成 Filter 而不是在每个调用点手动做
------------------------------------------------
风险 R3 是"新增字段忘了分级"。挂在 handler 上的过滤器对**所有**日志生效，
新加的 `extra=` 字段自动走同一套分级表（未知字段默认 S2）。
真正的脱敏逻辑在 `src/observability/redact.py`，是个零 IO 的纯函数，可以独立单测。

⚠️ 过滤器管不了日志 **message 本身**。敏感内容必须放进 ``extra=``，
绝不能拼进 message 字符串。

多进程约束
-----------
``uvicorn --workers>1`` 下多个进程各自持有文件句柄、各自 rollover，
``TimedRotatingFileHandler`` 会互相截断。当前是单进程。将来加 worker 时要么换
``WatchedFileHandler`` + 外部 logrotate，要么换 ``QueueHandler`` + 单写入进程。
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.core.settings import resolve_path
from src.observability.context import CONTEXT_FIELDS, context_as_dict
from src.observability.redact import redact

# Default path for traces file (absolute, CWD-independent)
_DEFAULT_TRACES_PATH = resolve_path("logs/traces.jsonl")

#: D-3：应用日志保留 **7 天**（审计日志 180 天由 `audit_logs` 表另行处理）。
DEFAULT_RETENTION_DAYS = 7

#: 被 ``configure_logging`` 装上的 handler 会打这个标记。
#: 只有带标记的才会被后续调用摘掉——**绝不动别人（如 pytest caplog）挂的 handler**。
_MANAGED_ATTR = "_ragent_managed"

#: 记住上一次的配置，用于判断"是否需要重建 handler"
_CURRENT_CONFIG: Dict[str, Any] = {}


def _env(name: str, default: str) -> str:
    return (os.getenv(name) or default).strip()


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# ── JSON Lines formatter ────────────────────────────────────────────


class JSONFormatter(logging.Formatter):
    """Logging formatter that outputs one JSON object per line.

    Each log record is serialised to a dict containing at least:
    ``timestamp``, ``level``, ``logger``, ``message``.  If the record
    carries an ``exc_info`` tuple the traceback is included as
    ``exception``.

    Extra attributes attached via *extra=* on the logger call are
    merged into the top-level dict (except internal Python fields).
    """

    _INTERNAL_ATTRS = frozenset({
        "args", "created", "exc_info", "exc_text", "filename",
        "funcName", "levelname", "levelno", "lineno", "module",
        "msecs", "message", "msg", "name", "pathname", "process",
        "processName", "relativeCreated", "stack_info", "thread",
        "threadName", "taskName",
    })

    def format(self, record: logging.LogRecord) -> str:  # noqa: D401
        """Return the log record as a single-line JSON string."""
        payload: Dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # merge extra fields the caller attached
        for key, val in record.__dict__.items():
            if key not in self._INTERNAL_ATTRS and key not in payload:
                try:
                    json.dumps(val)  # cheap serialisability test
                    payload[key] = val
                except (TypeError, ValueError):
                    payload[key] = str(val)

        if record.exc_info and record.exc_info[0] is not None:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, ensure_ascii=False)


# ── 上下文注入 ──────────────────────────────────────────────────────


class ContextInjectingFilter(logging.Filter):
    """把当前 `RequestContext` 的字段贴到每条 record 上。

    这是"53 个调用点不用手填 request_id"的实现方式：调用点只管
    ``logger.error("...", extra={...})``，链路 id 由这个过滤器统一补。

    两条纪律：

    - **不覆盖调用点显式传的值**。调用点若自己传了 ``request_id``，以它为准
      （例如后台重放任务要标注"这是在补哪一次请求"）。
    - **无上下文时补 ``None`` 而不是不补**。"缺字段"和"字段为 null"对下游查询
      是两回事——前者会让按 request_id 过滤的查询直接漏掉这些行。
    """

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        ctx = context_as_dict()
        for name in CONTEXT_FIELDS:
            if not hasattr(record, name):
                setattr(record, name, ctx[name])
        return True


class RedactingFilter(logging.Filter):
    """按分级表脱敏 record 上的 ``extra=`` 字段。

    ``log_content`` 对应 ``RAGENT_LOG_CONTENT`` 开关（默认 ``False``）。
    开着的时候 S2 记原文——但 **S2+（最终 prompt）任何开关下都不记原文**。

    脱敏是**就地改 record**：同一条 record 会被多个 handler 依次处理，
    脱敏必须对所有 sink 一致生效。`redact()` 是幂等的，重复处理无副作用。
    """

    def __init__(self, *, log_content: bool = False, strict: bool = False) -> None:
        super().__init__()
        self.log_content = log_content
        self.strict = strict

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        extras = {
            k: v
            for k, v in record.__dict__.items()
            if k not in JSONFormatter._INTERNAL_ATTRS
        }
        if not extras:
            return True

        cleaned = redact(extras, log_content=self.log_content, strict=self.strict)

        for key in extras:
            if key not in cleaned:
                delattr(record, key)
        for key, value in cleaned.items():
            setattr(record, key, value)
        return True


# ── 显式配置入口 ────────────────────────────────────────────────────


def _make_file_handler(log_dir: Path, retention_days: int) -> logging.Handler:
    """按天轮转的文件 handler。

    ``backupCount=retention_days`` 就是保留期实现：7 天以前的自动删掉。
    这是"日志无限增长"（`traces.jsonl` 12 天涨到 3.2 MB，万人企业外推约
    48 GB/年/客户）的直接对策。
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.TimedRotatingFileHandler(
        log_dir / "app.jsonl",
        when="midnight",
        backupCount=retention_days,
        encoding="utf-8",
        utc=True,
    )
    return handler


def configure_logging(
    *,
    level: Optional[str] = None,
    fmt: Optional[str] = None,
    dest: Optional[str] = None,
    log_dir: Optional[str | Path] = None,
    retention_days: Optional[int] = None,
    log_content: Optional[bool] = None,
    strict: Optional[bool] = None,
    stream: Optional[Any] = None,
    force: bool = False,
) -> logging.Logger:
    """配置 root logger。**显式、幂等、参数可注入**。

    所有参数都能从环境变量取（容器化预留，§2.5）：

    ==========================  ==========================================
    ``RAGENT_LOG_LEVEL``        ``INFO``
    ``RAGENT_LOG_FORMAT``       ``json`` | ``text``
    ``RAGENT_LOG_DEST``         ``stdout`` | ``stderr`` | ``file`` | ``both``
    ``RAGENT_LOG_DIR``          默认 ``logs/app``
    ``RAGENT_LOG_RETENTION_DAYS`` 默认 7（D-3）
    ``RAGENT_LOG_CONTENT``      默认 ``false``（S2 只记 len+hash）
    ==========================  ==========================================

    与 ``basicConfig`` 的关键差别：**重复调用会更新配置**。
    ``get_logger("a", "DEBUG")`` 之后 ``get_logger("b", "WARNING")``，
    第二次也生效（旧实现下第二次静默失效）。

    Returns:
        配置好的 root logger。
    """
    level_name = (level or _env("RAGENT_LOG_LEVEL", "INFO")).upper()
    level_no = getattr(logging, level_name, logging.INFO)
    fmt_name = (fmt or _env("RAGENT_LOG_FORMAT", "json")).lower()
    dest_name = (dest or _env("RAGENT_LOG_DEST", "stdout")).lower()
    dir_path = Path(log_dir) if log_dir else resolve_path(_env("RAGENT_LOG_DIR", "logs/app"))
    if retention_days is None:
        try:
            retention_days = int(_env("RAGENT_LOG_RETENTION_DAYS", str(DEFAULT_RETENTION_DAYS)))
        except ValueError:
            retention_days = DEFAULT_RETENTION_DAYS
    if log_content is None:
        log_content = _env_bool("RAGENT_LOG_CONTENT", False)
    if strict is None:
        strict = False

    desired = {
        "level": level_no,
        "fmt": fmt_name,
        "dest": dest_name,
        "dir": str(dir_path),
        "retention_days": retention_days,
        "log_content": log_content,
        "strict": strict,
        "stream": id(stream) if stream is not None else None,
    }

    root = logging.getLogger()

    if not force and _CURRENT_CONFIG == desired:
        return root  # 幂等：配置没变就什么都不做

    # 只摘掉自己装的 handler；别人（pytest caplog、宿主应用）挂的一律不动
    for handler in list(root.handlers):
        if getattr(handler, _MANAGED_ATTR, False):
            root.removeHandler(handler)
            try:
                handler.close()
            except Exception:  # noqa: BLE001 - 关闭失败不该影响日志配置
                pass

    formatter: logging.Formatter
    if fmt_name == "json":
        formatter = JSONFormatter()
    else:
        formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")

    handlers: List[logging.Handler] = []
    degraded_reason: Optional[str] = None

    if stream is not None:
        handlers.append(logging.StreamHandler(stream))
    elif dest_name in {"stdout", "both"}:
        handlers.append(logging.StreamHandler(sys.stdout))
    elif dest_name == "stderr":
        # MCP stdio 服务必须走这条：stdout 是 JSON-RPC 通信信道，
        # 往里写一行日志就会破坏报文边界（见 `src/mcp_server/server.py`）。
        handlers.append(logging.StreamHandler(sys.stderr))

    if dest_name in {"file", "both"}:
        try:
            handlers.append(_make_file_handler(dir_path, retention_days))
        except OSError as exc:
            # 容器里不假设进程有写文件权限：降级到 stdout 并留下明确记录，绝不崩。
            degraded_reason = f"{type(exc).__name__}: {exc}"
            if not handlers:
                handlers.append(logging.StreamHandler(sys.stdout))

    if not handlers:  # dest 传了个不认识的值
        handlers.append(logging.StreamHandler(sys.stdout))

    ctx_filter = ContextInjectingFilter()
    redact_filter = RedactingFilter(log_content=bool(log_content), strict=bool(strict))
    for handler in handlers:
        handler.setFormatter(formatter)
        # 顺序有意义：先注入上下文，再脱敏（注入进来的 org_id 也要过分级表）
        handler.addFilter(ctx_filter)
        handler.addFilter(redact_filter)
        setattr(handler, _MANAGED_ATTR, True)
        root.addHandler(handler)

    root.setLevel(level_no)

    # httpx 的 INFO 日志里带完整请求 URL（含内网端点），压到 WARNING
    logging.getLogger("httpx").setLevel(logging.WARNING)

    _CURRENT_CONFIG.clear()
    _CURRENT_CONFIG.update(desired)

    if degraded_reason:
        root.warning(
            "log file sink unavailable, degraded to stdout",
            extra={"event": "logging.file_sink.degraded", "error_type": degraded_reason},
        )
    if log_content:
        # 开关状态本身必须留痕：事后才知道"这段时间的日志是全量口径的"
        root.warning(
            "RAGENT_LOG_CONTENT is enabled: S2 content fields are logged verbatim",
            extra={"event": "logging.content.enabled", "log_content_enabled": True},
        )

    return root


def reset_logging() -> None:
    """摘掉本模块装的所有 handler 并清空配置记忆（测试用）。"""
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, _MANAGED_ATTR, False):
            root.removeHandler(handler)
            try:
                handler.close()
            except Exception:  # noqa: BLE001
                pass
    _CURRENT_CONFIG.clear()


# ── Human-readable logger (existing signature, new internals) ────────


def get_logger(name: str = "modular-rag", log_level: Optional[str] = None) -> logging.Logger:
    """Get a configured logger.

    **签名与旧版一字不变**，保住 `ingestion` / `mcp_server` 那 7 处现有调用方。
    内部从 ``logging.basicConfig`` 改为 ``configure_logging``——所以
    ``log_level`` 参数现在**每次都生效**（旧实现下只有第一次调用生效）。

    Args:
        name: Logger name.
        log_level: Optional log level string (e.g., "INFO").

    Returns:
        Configured logger instance.
    """
    configure_logging(level=log_level)
    return logging.getLogger(name)


# ── Trace logger ────────────────────────────────────────────────────


def get_trace_logger(
    traces_path: str | Path = _DEFAULT_TRACES_PATH,
    *,
    name: str = "modular-rag.trace",
    retention_days: Optional[int] = None,
) -> logging.Logger:
    """Return a logger that writes JSON Lines to *traces_path*.

    使用 :class:`JSONFormatter`，并**按天轮转**（旧版是不轮转的
    ``FileHandler`` —— 这正是 `logs/traces.jsonl` 无限增长的成因）。
    ``backupCount`` 即保留期，默认 7 天（D-3）。

    Repeated calls with the same *name* return the same logger
    (standard :mod:`logging` semantics).

    Args:
        traces_path: File path for the JSONL output.  Parent directories
            are created automatically.
        name: Logger name.
        retention_days: 保留几天的轮转文件；默认取 ``RAGENT_LOG_RETENTION_DAYS``。

    Returns:
        A :class:`logging.Logger` ready for JSON Lines output.
    """
    path = Path(traces_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    if retention_days is None:
        try:
            retention_days = int(_env("RAGENT_LOG_RETENTION_DAYS", str(DEFAULT_RETENTION_DAYS)))
        except ValueError:
            retention_days = DEFAULT_RETENTION_DAYS

    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)

    # Avoid adding duplicate handlers on repeated calls
    if not logger.handlers:
        handler = logging.handlers.TimedRotatingFileHandler(
            path,
            when="midnight",
            backupCount=retention_days,
            encoding="utf-8",
            utc=True,
        )
        handler.setFormatter(JSONFormatter())
        handler.addFilter(ContextInjectingFilter())
        handler.addFilter(RedactingFilter(log_content=_env_bool("RAGENT_LOG_CONTENT", False)))
        logger.addHandler(handler)
        logger.propagate = False  # don't echo to console

    return logger


# ── 已删除 ──────────────────────────────────────────────────────────
# ``write_trace`` 于 2026-08-25 删除（可观测性方案 D-10）。
# 它全仓零生产调用点，唯一引用方是自己的单测；`TraceCollector` 才是实际的
# traces.jsonl 写入方。留着一个"看起来是当前方案"的空壳有真实成本
# （`CLAUDE.md` §7.4）。要往 JSONL 追加请用 `get_trace_logger`。
