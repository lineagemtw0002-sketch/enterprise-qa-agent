"""Pytest configuration and shared fixtures.

This module contains pytest configuration and fixtures that are shared
across all test modules.
"""

import sys
from pathlib import Path

import pytest

# Add the project root to the Python path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Postgres 测试隔离 fixture（`postgres_test_db` / `clean_postgres`）。
# 实现放在单独的 `tests/db_fixtures.py` 而不是直接写在这里，两个理由：
# ① 它自带一份很长的"怎么用 / 为什么这么设计 / 什么情况下会 skip"文档，
#    塞进 conftest 会把这个文件淹掉；
# ② 它是个普通模块，`scripts/` 下那些"改 DSN + 清两层池缓存"的验证脚本
#    将来可以直接 import 复用，conftest 做不到这一点。
# 在这里 import 是为了让这两个 fixture 在整个 tests/ 树下都能直接用。
# ⚠️ `db_fixtures` 顶层**不 import 任何 src.ragent_backend 模块**（那些
#    import 都写在函数体里），所以 `tests/unit` 不会因此变慢，也不会因为
#    本机 Postgres 不可达而受影响。
from tests.db_fixtures import clean_postgres, postgres_test_db  # noqa: E402,F401


@pytest.fixture
def project_root() -> Path:
    """Return the project root directory path.
    
    Returns:
        Path to the project root directory.
    """
    return PROJECT_ROOT


@pytest.fixture
def sample_documents_dir(project_root: Path) -> Path:
    """Return the sample documents directory path.
    
    Args:
        project_root: The project root directory path.
        
    Returns:
        Path to the sample documents directory.
    """
    return project_root / "tests" / "fixtures" / "sample_documents"


@pytest.fixture
def capture_json_logs():
    """捕获**渲染成 JSON 之后**的日志行。

    为什么不用 pytest 内建的 `caplog`：`caplog` 拿到的是 `LogRecord` 对象，
    而"敏感原文有没有漏进日志"必须在**序列化之后**断言——record 上还挂着
    原始属性、过滤器可能只改了其中一部分，只看 record 会漏判。
    这个 fixture 走的是和生产完全相同的一条链路：
    `ContextInjectingFilter` → `RedactingFilter` → `JSONFormatter`。

    Yields:
        一个 list，测试期间实时追加渲染后的 JSON 字符串。
        另带 `.records` 属性给需要看结构化 dict 的用例。
    """
    import json as _json
    import logging as _logging

    from src.observability.logger import (
        ContextInjectingFilter,
        JSONFormatter,
        RedactingFilter,
    )

    class _CaptureHandler(_logging.Handler):
        def __init__(self) -> None:
            super().__init__()
            self.lines: list[str] = []

        def emit(self, record: _logging.LogRecord) -> None:
            self.lines.append(self.format(record))

    def _make(*, log_content: bool = False, strict: bool = False):
        handler = _CaptureHandler()
        handler.setFormatter(JSONFormatter())
        handler.addFilter(ContextInjectingFilter())
        handler.addFilter(RedactingFilter(log_content=log_content, strict=strict))
        root = _logging.getLogger()
        previous_level = root.level
        root.addHandler(handler)
        root.setLevel(_logging.DEBUG)
        created.append((root, handler, previous_level))

        class _View:
            @property
            def lines(self) -> list[str]:
                return handler.lines

            @property
            def records(self) -> list[dict]:
                return [_json.loads(line) for line in handler.lines]

            @property
            def blob(self) -> str:
                """全部日志行拼成一个字符串——用于'原文一个字都不许出现'的兜底断言。"""
                return "\n".join(handler.lines)

        return _View()

    created: list = []
    try:
        yield _make
    finally:
        for root, handler, previous_level in created:
            root.removeHandler(handler)
            root.setLevel(previous_level)


@pytest.fixture(autouse=True)
def _clear_request_context():
    """每条用例前后都清空请求上下文。

    contextvar 泄漏到下一条用例会让"并发不串"这类断言假绿——上一条用例
    残留的 request_id 恰好等于期望值时，测试根本没验证到东西。
    """
    from src.observability.context import clear_request_context

    clear_request_context()
    yield
    clear_request_context()


@pytest.fixture
def config_dir(project_root: Path) -> Path:
    """Return the config directory path.
    
    Args:
        project_root: The project root directory path.
        
    Returns:
        Path to the config directory.
    """
    return project_root / "config"
