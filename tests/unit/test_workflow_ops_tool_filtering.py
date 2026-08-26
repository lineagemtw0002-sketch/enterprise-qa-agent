"""`RAGWorkflow._available_tools_for` —— 工具列表按调用者动态过滤。

补的是一个 CLAUDE.md §5 长期记录在案的已知缺口：智能运维三个工具是全局
注册的，`aiops_module_enabled=False` 的企业用户此前也能在 LLM 可用工具
列表里看到 `query_ops_system`/`propose_remediation`/`analyze_ops_incident`/
`execute_approved_remediation`。执行层早有兜底（`OpsToolset` 内部拒绝），
这里补的是展示层：未开通模块时把这几个工具名从 `available_tools` 里摘掉。

判别力：`TestFiltersOpsToolsWhenModuleDisabled` 与
`TestKeepsNonOpsToolsAlwaysVisible` 两条直接证明了"过滤确实发生"和
"过滤不会误伤其它工具"；`TestSkipsDbLookupWhenNoOpsToolsRegistered` 证明
不挂 `ops_toolset` 的部署（比如独立跑的 MCP server）不会为这条逻辑多付一次
数据库往返——回退到"总是查一次 org/模块开关"的旧写法会让这条断言失败
（call_count 从 0 变成 >0）。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from src.ops.tool_registration import OPS_TOOL_NAMES
from src.ragent_backend.workflow import RAGWorkflow
from src.tool_agent.adapters import wrap_function_tool
from src.tool_agent.tool_registry import ToolRegistry

class _FakeStore:
    pass


class _FakeLLM:
    pass


class _FakeOrg:
    def __init__(self, org_id: str) -> None:
        self.org_id = org_id


class _FakeOrgStore:
    """记录调用次数，供"没注册运维工具时不该查库"这条判别式使用。"""

    call_count = 0

    def __init__(self) -> None:
        pass

    async def get_org_for_user(self, user_id: str) -> Optional[_FakeOrg]:
        type(self).call_count += 1
        return _FakeOrg("org-1") if user_id else None


class _FakeOpsStore:
    call_count = 0
    enabled = False

    def __init__(self) -> None:
        pass

    async def is_module_enabled(self, org_id: str) -> bool:
        type(self).call_count += 1
        return type(self).enabled


async def _dummy_handler(**_: Any) -> str:
    return "ok"


def _registry_with(names: List[str]) -> ToolRegistry:
    registry = ToolRegistry()
    for name in names:
        registry.register(wrap_function_tool(
            name=name, description=f"desc for {name}", handler=_dummy_handler,
            input_schema={"type": "object", "properties": {}},
        ))
    return registry


def _tool_names(tools: List[Dict[str, Any]]) -> set:
    return {t["function"]["name"] for t in tools}


@pytest.fixture(autouse=True)
def _reset_fake_counters():
    _FakeOrgStore.call_count = 0
    _FakeOpsStore.call_count = 0
    _FakeOpsStore.enabled = False
    yield


@pytest.fixture
def _patch_stores(monkeypatch):
    monkeypatch.setattr("src.ragent_backend.org_store.OrgStore", _FakeOrgStore)
    monkeypatch.setattr("src.ragent_backend.ops_store.OpsStore", _FakeOpsStore)


@pytest.mark.asyncio
class TestFiltersOpsToolsWhenModuleDisabled:
    async def test_ops_tools_removed_when_module_not_enabled(self, _patch_stores):
        registry = _registry_with(["query_knowledge_hub", *OPS_TOOL_NAMES])
        workflow = RAGWorkflow(store=_FakeStore(), llm=_FakeLLM(), tool_registry=registry)
        _FakeOpsStore.enabled = False

        tools = await workflow._available_tools_for("user-1")

        names = _tool_names(tools)
        assert names == {"query_knowledge_hub"}
        assert names.isdisjoint(OPS_TOOL_NAMES)

    async def test_ops_tools_kept_when_module_enabled(self, _patch_stores):
        registry = _registry_with(["query_knowledge_hub", *OPS_TOOL_NAMES])
        workflow = RAGWorkflow(store=_FakeStore(), llm=_FakeLLM(), tool_registry=registry)
        _FakeOpsStore.enabled = True

        tools = await workflow._available_tools_for("user-1")

        assert _tool_names(tools) == {"query_knowledge_hub", *OPS_TOOL_NAMES}


@pytest.mark.asyncio
class TestKeepsNonOpsToolsAlwaysVisible:
    async def test_non_ops_tools_unaffected_by_disabled_module(self, _patch_stores):
        registry = _registry_with(["query_knowledge_hub", "query_attendance", *OPS_TOOL_NAMES])
        workflow = RAGWorkflow(store=_FakeStore(), llm=_FakeLLM(), tool_registry=registry)
        _FakeOpsStore.enabled = False

        tools = await workflow._available_tools_for("user-1")

        assert _tool_names(tools) == {"query_knowledge_hub", "query_attendance"}


@pytest.mark.asyncio
class TestSkipsDbLookupWhenNoOpsToolsRegistered:
    async def test_no_org_lookup_without_ops_tools(self, _patch_stores):
        registry = _registry_with(["query_knowledge_hub", "query_attendance"])
        workflow = RAGWorkflow(store=_FakeStore(), llm=_FakeLLM(), tool_registry=registry)

        tools = await workflow._available_tools_for("user-1")

        assert _tool_names(tools) == {"query_knowledge_hub", "query_attendance"}
        assert _FakeOrgStore.call_count == 0
        assert _FakeOpsStore.call_count == 0


@pytest.mark.asyncio
class TestHandlesMissingUserOrOrg:
    async def test_no_user_id_returns_full_unfiltered_list(self, _patch_stores):
        registry = _registry_with(["query_knowledge_hub", *OPS_TOOL_NAMES])
        workflow = RAGWorkflow(store=_FakeStore(), llm=_FakeLLM(), tool_registry=registry)

        tools = await workflow._available_tools_for(None)

        assert _tool_names(tools) == {"query_knowledge_hub", *OPS_TOOL_NAMES}
        assert _FakeOrgStore.call_count == 0

    async def test_user_with_no_org_hides_ops_tools(self, _patch_stores, monkeypatch):
        registry = _registry_with(["query_knowledge_hub", *OPS_TOOL_NAMES])
        workflow = RAGWorkflow(store=_FakeStore(), llm=_FakeLLM(), tool_registry=registry)

        class _NoOrgStore(_FakeOrgStore):
            async def get_org_for_user(self, user_id: str) -> Optional[_FakeOrg]:
                type(self).call_count += 1
                return None

        monkeypatch.setattr("src.ragent_backend.org_store.OrgStore", _NoOrgStore)

        tools = await workflow._available_tools_for("orphan-user")

        assert _tool_names(tools) == {"query_knowledge_hub"}


@pytest.mark.asyncio
class TestLookupFailureFailsClosed:
    async def test_exception_resolving_org_hides_ops_tools(self, monkeypatch):
        registry = _registry_with(["query_knowledge_hub", *OPS_TOOL_NAMES])
        workflow = RAGWorkflow(store=_FakeStore(), llm=_FakeLLM(), tool_registry=registry)

        class _BrokenOrgStore:
            def __init__(self) -> None:
                pass

            async def get_org_for_user(self, user_id: str):
                raise RuntimeError("db unavailable")

        monkeypatch.setattr("src.ragent_backend.org_store.OrgStore", _BrokenOrgStore)
        monkeypatch.setattr("src.ragent_backend.ops_store.OpsStore", _FakeOpsStore)

        tools = await workflow._available_tools_for("user-1")

        assert _tool_names(tools) == {"query_knowledge_hub"}


def test_default_unfiltered_matches_old_behavior():
    registry = _registry_with(["a", "b", "c"])
    assert _tool_names(registry.to_openai_tools()) == {"a", "b", "c"}


def test_exclude_names_filters():
    registry = _registry_with(["a", "b", "c"])
    assert _tool_names(registry.to_openai_tools(exclude_names={"b"})) == {"a", "c"}


def test_empty_exclude_set_is_noop():
    registry = _registry_with(["a", "b"])
    assert _tool_names(registry.to_openai_tools(exclude_names=set())) == {"a", "b"}
