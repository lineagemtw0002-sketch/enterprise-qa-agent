"""智能运维三个工具的注册（`src/ops/tool_registration.py`，设计 §3.6）。

两条断言值得单独说明，都不是形式化的：

1. **三个 schema 里都不许出现 `org_id`/`user_id`。** 身份必须由服务端从会话注入，
   不能出现在 LLM 能填的工具入参里——这是 `query_knowledge_hub` 那条既有铁律
   （见它 `execute()` 的 docstring：「Only trust values that came from the
   server-side request/state, never from LLM-supplied tool arguments」）。
   schema 里给了这个字段，就等于把越权做成了一个模型可以直接填的参数。
2. **不传 toolset 时一个运维工具都不注册。** 跟 workflow_store/attendance_store
   同一个约定：能力没初始化时，不该让 LLM 看到一个调用了必然报错的工具。

⚠️ **2026-08-26 补的判别力缺口**：原版本只测了"身份完全缺失时拒绝"，没有测
"身份注入齐全时真的能查到东西"——而这条正是当时踩过的真实 bug：`tool_node`
（`src/tool_agent/subgraph.py`）只注入 `user_id`，从未注入过 `org_id`，但三个
handler 原来直接接收一个不存在的 `org_id` 形参，实战中恒为 `None`，三个工具
调用一次挂一次。`TestOrgIdResolvedFromInjectedUserId` 就是补这个洞的判别式——
把 `_resolve_org_id` 改回"直接读一个不存在的 org_id kwarg"会让它变红。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from src.ops.tools import ToolOutcome as _ToolOutcome
from src.ops.tool_registration import (
    ANALYZE_OPS_INCIDENT_NAME,
    ANALYZE_OPS_INCIDENT_SCHEMA,
    EXECUTE_REMEDIATION_NAME,
    EXECUTE_REMEDIATION_SCHEMA,
    PROPOSE_REMEDIATION_NAME,
    PROPOSE_REMEDIATION_SCHEMA,
    QUERY_OPS_SYSTEM_NAME,
    QUERY_OPS_SYSTEM_SCHEMA,
    register_ops_tools,
)
from src.tool_agent.tool_registry import ToolRegistry

OPS_TOOL_NAMES = {QUERY_OPS_SYSTEM_NAME, PROPOSE_REMEDIATION_NAME,
                  EXECUTE_REMEDIATION_NAME, ANALYZE_OPS_INCIDENT_NAME}


class _StubToolset:
    def __init__(self):
        self.calls = []

    async def query_ops_system(self, **kw):
        self.calls.append(("query", kw))
        return _ToolOutcome(ok=True, message="ok")

    async def propose_remediation(self, **kw):
        self.calls.append(("propose", kw))
        return _ToolOutcome(ok=True, message="ok")

    async def execute_approved_remediation(self, **kw):
        self.calls.append(("execute", kw))
        return _ToolOutcome(ok=True, message="ok")


class _StubOrgStore:
    """模拟 `OrgStore.get_org_for_user`——只认一个 user_id，其余一律查不到，
    用来钉住"org_id 是查出来的、不是随便信一个字符串"这件事。"""

    def __init__(self, user_id: str, org_id: str):
        self._user_id = user_id
        self._org_id = org_id

    async def get_org_for_user(self, user_id: str):
        if user_id == self._user_id:
            return SimpleNamespace(org_id=self._org_id)
        return None


def _registered_names(registry: ToolRegistry) -> set:
    tools = registry.list_tools() if hasattr(registry, "list_tools") else registry.get_all()
    return {t.name for t in tools}


class TestIdentityIsNotAnLLMParameter:
    @pytest.mark.parametrize("schema,name", [
        (QUERY_OPS_SYSTEM_SCHEMA, QUERY_OPS_SYSTEM_NAME),
        (PROPOSE_REMEDIATION_SCHEMA, PROPOSE_REMEDIATION_NAME),
        (EXECUTE_REMEDIATION_SCHEMA, EXECUTE_REMEDIATION_NAME),
        (ANALYZE_OPS_INCIDENT_SCHEMA, ANALYZE_OPS_INCIDENT_NAME),
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


class TestOrgIdResolvedFromInjectedUserId:
    """真实调用路径只会注入 `user_id`（见 `subgraph.py::tool_node`），
    `org_id` 必须由 handler 内部反查得到，不能指望有人替它注入一个不存在的
    参数。三条各覆盖一个工具，判别力：把 `_resolve_org_id` 改回直接读
    `org_id` kwarg（旧实现），这三条会失败——因为下面只传 `user_id`，
    旧实现读到的 `org_id` kwarg 恒为 None，工具会拒绝而不是真的调用 toolset。
    """

    @pytest.mark.asyncio
    async def test_query_resolves_org_id_and_forwards_to_toolset(self):
        registry = ToolRegistry()
        toolset = _StubToolset()
        register_ops_tools(registry, toolset, _StubOrgStore("u1", "org_acme"))
        tool = registry.get(QUERY_OPS_SYSTEM_NAME)

        result = await tool.execute(target="order-service", user_id="u1")

        assert result.error is None, result.output
        assert toolset.calls == [("query", {
            "org_id": "org_acme", "target": "order-service",
            "metric": "error_rate", "window_minutes": 60,
        })]

    @pytest.mark.asyncio
    async def test_propose_resolves_org_id_and_forwards_to_toolset(self):
        registry = ToolRegistry()
        toolset = _StubToolset()
        register_ops_tools(registry, toolset, _StubOrgStore("u1", "org_acme"))
        tool = registry.get(PROPOSE_REMEDIATION_NAME)

        result = await tool.execute(
            connection_id="conn1", action_type="restart_service",
            intent="服务卡死", plan={"target": "order-service"}, user_id="u1",
        )

        assert result.error is None, result.output
        assert toolset.calls == [("propose", {
            "org_id": "org_acme", "connection_id": "conn1", "proposed_by": "u1",
            "action_type": "restart_service", "intent": "服务卡死",
            "plan": {"target": "order-service"}, "impact_radius": None,
        })]

    @pytest.mark.asyncio
    async def test_execute_resolves_org_id_and_forwards_to_toolset(self):
        registry = ToolRegistry()
        toolset = _StubToolset()
        register_ops_tools(registry, toolset, _StubOrgStore("u1", "org_acme"))
        tool = registry.get(EXECUTE_REMEDIATION_NAME)

        result = await tool.execute(action_id="remact_1", user_id="u1")

        assert result.error is None, result.output
        assert toolset.calls == [("execute", {
            "org_id": "org_acme", "action_id": "remact_1", "action_type": None,
        })]

    @pytest.mark.asyncio
    async def test_unrecognized_user_id_still_refuses(self):
        """org_store 查不到这个 user_id（比如账号已删）时不能瞎猜一个 org。"""
        registry = ToolRegistry()
        toolset = _StubToolset()
        register_ops_tools(registry, toolset, _StubOrgStore("u1", "org_acme"))
        tool = registry.get(QUERY_OPS_SYSTEM_NAME)

        result = await tool.execute(target="order-service", user_id="ghost-user")

        assert "身份" in (result.output or "")
        assert toolset.calls == []


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

    def test_analysis_description_forbids_presenting_leads_as_conclusions(self):
        """分析产出是排查线索不是结论；降级/数据缺失必须原样转述给用户——
        隐瞒"这次分析没有模型参与"或"有数据源挂了"，比不分析更糟。"""
        from src.ops.tool_registration import ANALYZE_OPS_INCIDENT_DESCRIPTION as D
        assert "不是结论" in D
        assert "必须原样告诉用户" in D
