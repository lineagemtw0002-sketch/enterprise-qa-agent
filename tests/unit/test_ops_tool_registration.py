"""智能运维三个工具的注册（`src/ops/tool_registration.py`，设计 §3.6）。

两条断言值得单独说明，都不是形式化的：

1. **三个 schema 里都不许出现 `org_id`/`user_id`。** 身份必须由服务端从会话注入，
   不能出现在 LLM 能填的工具入参里——这是 `query_knowledge_hub` 那条既有铁律
   （见它 `execute()` 的 docstring：「Only trust values that came from the
   server-side request/state, never from LLM-supplied tool arguments」）。
   schema 里给了这个字段，就等于把越权做成了一个模型可以直接填的参数。
2. **不传 toolset 时一个运维工具都不注册。** 跟 workflow_store/attendance_store
   同一个约定：能力没初始化时，不该让 LLM 看到一个调用了必然报错的工具。
"""

from __future__ import annotations

import json

import pytest

from src.ops.tool_registration import (
    EXECUTE_REMEDIATION_NAME,
    EXECUTE_REMEDIATION_SCHEMA,
    PROPOSE_REMEDIATION_NAME,
    PROPOSE_REMEDIATION_SCHEMA,
    QUERY_OPS_SYSTEM_NAME,
    QUERY_OPS_SYSTEM_SCHEMA,
    register_ops_tools,
)
from src.tool_agent.tool_registry import ToolRegistry

OPS_TOOL_NAMES = {QUERY_OPS_SYSTEM_NAME, PROPOSE_REMEDIATION_NAME, EXECUTE_REMEDIATION_NAME}


class _StubToolset:
    async def query_ops_system(self, **kw): ...
    async def propose_remediation(self, **kw): ...
    async def execute_approved_remediation(self, **kw): ...


def _registered_names(registry: ToolRegistry) -> set:
    tools = registry.list_tools() if hasattr(registry, "list_tools") else registry.get_all()
    return {t.name for t in tools}


class TestIdentityIsNotAnLLMParameter:
    @pytest.mark.parametrize("schema,name", [
        (QUERY_OPS_SYSTEM_SCHEMA, QUERY_OPS_SYSTEM_NAME),
        (PROPOSE_REMEDIATION_SCHEMA, PROPOSE_REMEDIATION_NAME),
        (EXECUTE_REMEDIATION_SCHEMA, EXECUTE_REMEDIATION_NAME),
    ])
    def test_schema_does_not_expose_identity_fields(self, schema, name):
        blob = json.dumps(schema)
        assert "org_id" not in blob, f"{name} 把 org_id 暴露成了 LLM 可填的参数"
        assert "user_id" not in blob, f"{name} 把 user_id 暴露成了 LLM 可填的参数"

    @pytest.mark.asyncio
    async def test_handler_refuses_when_identity_is_missing(self):
        """身份没注入进来时必须拒绝，不能退化成"查全部"或"当成某个默认 org"。"""
        registry = ToolRegistry()
        register_ops_tools(registry, _StubToolset())
        tool = registry.get(QUERY_OPS_SYSTEM_NAME)
        result = await tool.execute(target="order-service")
        assert "身份" in (result.output or "")


class TestRegistration:
    def test_all_three_tools_are_registered(self):
        registry = ToolRegistry()
        register_ops_tools(registry, _StubToolset())
        assert OPS_TOOL_NAMES <= _registered_names(registry)

    def test_not_registered_when_toolset_is_absent(self):
        """`register_builtin_tools` 不传 ops_toolset 时，一个运维工具都不该出现。"""
        from src.tool_agent.builtin_tools import register_builtin_tools

        registry = ToolRegistry()
        register_builtin_tools(registry)
        assert not (OPS_TOOL_NAMES & _registered_names(registry))

    def test_descriptions_tell_the_model_it_cannot_self_approve(self):
        """描述文案是模型行为的第一道闸——工具层的硬检查是第二道，两道都要有。"""
        from src.ops.tool_registration import (
            EXECUTE_REMEDIATION_DESCRIPTION,
            PROPOSE_REMEDIATION_DESCRIPTION,
        )
        assert "不会执行" in PROPOSE_REMEDIATION_DESCRIPTION
        assert "不能自己批准" in EXECUTE_REMEDIATION_DESCRIPTION
