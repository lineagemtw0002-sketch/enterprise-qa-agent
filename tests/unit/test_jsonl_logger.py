"""Tests for structured JSON Lines logger (F2).

Covers:
- JSONFormatter output structure
- get_trace_logger file handler setup（现已按天轮转）

2026-08-25：`write_trace` 随可观测性阶段一删除（D-10，全仓零生产调用点），
对应的 5 条用例一并移除。JSONL 追加改用 `get_trace_logger`。
"""

import json
import logging
from pathlib import Path

import pytest

from src.observability.logger import JSONFormatter, get_trace_logger


# ── JSONFormatter ────────────────────────────────────────────────────


class TestJSONFormatter:
    """Verify JSONFormatter produces valid JSON with required fields."""

    def _make_record(
        self, msg: str = "hello", level: int = logging.INFO, **extra: object
    ) -> logging.LogRecord:
        record = logging.LogRecord(
            name="test",
            level=level,
            pathname="test.py",
            lineno=1,
            msg=msg,
            args=(),
            exc_info=None,
        )
        for k, v in extra.items():
            setattr(record, k, v)
        return record

    def test_output_is_valid_json(self) -> None:
        fmt = JSONFormatter()
        record = self._make_record("test message")
        line = fmt.format(record)
        obj = json.loads(line)
        assert isinstance(obj, dict)

    def test_required_keys(self) -> None:
        fmt = JSONFormatter()
        obj = json.loads(fmt.format(self._make_record()))
        for key in ("timestamp", "level", "logger", "message"):
            assert key in obj, f"missing key: {key}"

    def test_message_value(self) -> None:
        fmt = JSONFormatter()
        obj = json.loads(fmt.format(self._make_record("hello world")))
        assert obj["message"] == "hello world"

    def test_level_value(self) -> None:
        fmt = JSONFormatter()
        obj = json.loads(fmt.format(self._make_record(level=logging.WARNING)))
        assert obj["level"] == "WARNING"

    def test_extra_fields_merged(self) -> None:
        fmt = JSONFormatter()
        record = self._make_record(trace_type="query", score=0.95)
        obj = json.loads(fmt.format(record))
        assert obj["trace_type"] == "query"
        assert obj["score"] == 0.95

    def test_non_serialisable_extra_converted(self) -> None:
        fmt = JSONFormatter()
        record = self._make_record(custom_obj=object())
        line = fmt.format(record)
        obj = json.loads(line)
        assert "custom_obj" in obj  # converted to str

    def test_single_line_output(self) -> None:
        fmt = JSONFormatter()
        line = fmt.format(self._make_record("no\nnewlines\nplease"))
        # json.dumps by default escapes newlines as \\n
        assert "\n" not in line


# ── get_trace_logger ────────────────────────────────────────────────


class TestGetTraceLogger:
    """Verify get_trace_logger sets up JSON Lines file handler."""

    def test_returns_logger(self, tmp_path: Path) -> None:
        p = tmp_path / "traces.jsonl"
        lgr = get_trace_logger(p, name="test.trace.1")
        assert isinstance(lgr, logging.Logger)

    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        p = tmp_path / "sub" / "deep" / "traces.jsonl"
        get_trace_logger(p, name="test.trace.2")
        assert p.parent.exists()

    def test_writes_json_line(self, tmp_path: Path) -> None:
        p = tmp_path / "traces.jsonl"
        lgr = get_trace_logger(p, name="test.trace.3")
        lgr.info("test event", extra={"trace_type": "query"})
        lines = p.read_text().strip().split("\n")
        assert len(lines) == 1
        obj = json.loads(lines[0])
        assert obj["message"] == "test event"
        assert obj["trace_type"] == "query"

    def test_no_duplicate_handlers_on_repeated_call(self, tmp_path: Path) -> None:
        p = tmp_path / "traces.jsonl"
        lgr1 = get_trace_logger(p, name="test.trace.4")
        lgr2 = get_trace_logger(p, name="test.trace.4")
        assert lgr1 is lgr2
        assert len(lgr2.handlers) == 1


    def test_handler_rotates(self, tmp_path: Path) -> None:
        """T-10 配套：trace 文件必须**轮转**。

        判别力：旧实现用的是 `logging.FileHandler`——不轮转、无保留期，
        这正是 `logs/traces.jsonl` 12 天涨到 3.2 MB、按万人企业外推
        约 48 GB/年/客户 的直接成因。这条断言在旧实现下必然失败。
        """
        import logging.handlers

        p = tmp_path / "traces.jsonl"
        lgr = get_trace_logger(p, name="test.trace.rotate", retention_days=7)
        handler = lgr.handlers[0]
        assert isinstance(handler, logging.handlers.TimedRotatingFileHandler)
        assert handler.backupCount == 7  # 保留期 = D-3 的 7 天

    def test_trace_logger_redacts_content(self, tmp_path: Path) -> None:
        """trace 文件曾是最大的原文落盘点，必须走脱敏。"""
        p = tmp_path / "traces.jsonl"
        lgr = get_trace_logger(p, name="test.trace.redact")
        lgr.info("retrieval", extra={"query": "董事长的薪酬是多少"})
        content = p.read_text()
        assert "董事长的薪酬是多少" not in content
        obj = json.loads(content.strip().split("\n")[-1])
        assert obj["query_len"] == 9
        assert len(obj["query_sha256"]) == 12


# ── configure_logging / get_logger ──────────────────────────────────


class TestConfigureLogging:
    """T-10：`get_logger` 从 `basicConfig` 改为 `configure_logging` 的回归保护。"""

    @pytest.fixture(autouse=True)
    def _isolate(self):
        """每条用例前后都把本模块装的 handler 摘干净，避免污染其他测试。"""
        from src.observability.logger import reset_logging

        reset_logging()
        yield
        reset_logging()

    def test_second_call_with_different_level_takes_effect(self) -> None:
        """**这条在旧实现下会失败。**

        旧代码走 `logging.basicConfig`，而 basicConfig 只在 root 无 handler
        时生效——第一个调用者决定全进程 level，后来者的 `log_level` 参数
        静默失效（不报错、不告警，只是不生效）。
        """
        from src.observability.logger import get_logger

        get_logger("t.a", log_level="DEBUG")
        assert logging.getLogger().level == logging.DEBUG

        get_logger("t.b", log_level="WARNING")
        assert logging.getLogger().level == logging.WARNING

    def test_repeated_calls_do_not_stack_handlers(self) -> None:
        """幂等：同参数重复调用不应该让每条日志被打印 N 遍。"""
        from src.observability.logger import _MANAGED_ATTR, get_logger

        for _ in range(5):
            get_logger("t.c", log_level="INFO")
        managed = [
            h for h in logging.getLogger().handlers if getattr(h, _MANAGED_ATTR, False)
        ]
        assert len(managed) == 1

    def test_does_not_remove_foreign_handlers(self) -> None:
        """绝不摘别人挂的 handler（pytest caplog、宿主应用都会挂）。"""
        from src.observability.logger import configure_logging

        foreign = logging.NullHandler()
        root = logging.getLogger()
        root.addHandler(foreign)
        try:
            configure_logging(level="INFO")
            configure_logging(level="ERROR")
            assert foreign in root.handlers
        finally:
            root.removeHandler(foreign)

    def test_file_sink_degrades_instead_of_crashing(self, tmp_path: Path) -> None:
        """容器里不假设进程有写文件权限：失败要降级到 stdout，不能崩。"""
        from src.observability.logger import _MANAGED_ATTR, configure_logging

        blocker = tmp_path / "not-a-dir"
        blocker.write_text("i am a file")  # mkdir 会因此失败

        configure_logging(level="INFO", dest="file", log_dir=blocker / "sub")

        managed = [
            h for h in logging.getLogger().handlers if getattr(h, _MANAGED_ATTR, False)
        ]
        assert len(managed) == 1
        assert isinstance(managed[0], logging.StreamHandler)

    def test_file_dest_rotates_with_retention(self, tmp_path: Path) -> None:
        import logging.handlers

        from src.observability.logger import _MANAGED_ATTR, configure_logging

        configure_logging(level="INFO", dest="file", log_dir=tmp_path, retention_days=7)
        managed = [
            h for h in logging.getLogger().handlers if getattr(h, _MANAGED_ATTR, False)
        ]
        assert isinstance(managed[0], logging.handlers.TimedRotatingFileHandler)
        assert managed[0].backupCount == 7
