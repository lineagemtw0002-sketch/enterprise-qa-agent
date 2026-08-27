"""公共辅助函数层（`src/ragent_backend/api_helpers.py`）。

## 这个文件本身就是分层的验收证据

`docs/app_layering_design.md` 给每一批定了三条验收，其中第二条是
**"新增至少一条『不建整个 app 就能测这批端点』的测试——否则这一批等于只搬了
文件"**。这个文件就是批次 0 的那条证据：下面每一个用例都**没有构造 `create_app()`、
没有连 Postgres、没有起任何服务**，依赖全是几十行的假件。

在提取之前这些函数是 `create_app()` 的内嵌闭包，Store 藏在局部作用域里，
**这里的任何一条都写不出来**。
"""

import pytest
from fastapi import HTTPException

from src.ragent_backend import api_helpers


class _Rec:
    """够用的假记录——用 `type` 造对象比引真实 dataclass 更能说明
    这些函数只依赖少数几个属性。"""

    def __init__(self, **kw):
        self.__dict__.update(kw)


class _FakeAuditStore:
    def __init__(self, raises=None):
        self.records = []
        self._raises = raises

    async def record(self, **kw):
        if self._raises:
            raise self._raises
        self.records.append(kw)


class _FakeOrgStore:
    def __init__(self, org=None):
        self._org = org

    async def get_org_for_user(self, user_id):
        return self._org


class _FakeUserStore:
    def __init__(self, user=None):
        self._user = user

    async def get_user_by_id(self, user_id):
        return self._user


class TestAuditLog:
    @pytest.mark.asyncio
    async def test_fills_in_org_id_and_username(self):
        """审计表里要有 org_id 和 username，而调用方只传得出 user_id——
        补全这两项正是这个函数存在的理由。"""
        audit = _FakeAuditStore()
        await api_helpers.audit_log(
            audit_store=audit, org_store=_FakeOrgStore(_Rec(org_id="org1")),
            user_store=_FakeUserStore(_Rec(username="alice")),
            user_id="u1", action="approve", resource_type="action",
            resource_id="a1", detail={},
        )
        assert audit.records[0]["org_id"] == "org1"
        assert audit.records[0]["username"] == "alice"

    @pytest.mark.asyncio
    async def test_anonymous_call_does_not_query_stores(self):
        """`user_id` 为 None 时不该去查库——系统级事件没有归属用户，
        拿 None 去查会白白发两次查询。"""
        class _Boom:
            async def get_org_for_user(self, _):
                raise AssertionError("不该被调用")

            async def get_user_by_id(self, _):
                raise AssertionError("不该被调用")

        audit = _FakeAuditStore()
        await api_helpers.audit_log(
            audit_store=audit, org_store=_Boom(), user_store=_Boom(),
            user_id=None, action="startup", resource_type="system",
            resource_id=None, detail={},
        )
        assert audit.records[0]["org_id"] is None

    @pytest.mark.asyncio
    async def test_write_failure_never_propagates(self):
        """**审计写失败绝不能让业务操作跟着失败。**

        一次批准动作因为审计表写不进去而回滚，比丢一条审计记录严重得多。
        这条在旧实现下也成立（`try/except` 是照搬的），但它是这个函数最重要
        的行为契约，提取之后必须有测试钉住——否则下一个人"顺手把异常抛出去
        让调用方知道"就悄悄改掉了它。
        """
        await api_helpers.audit_log(
            audit_store=_FakeAuditStore(raises=RuntimeError("库挂了")),
            org_store=_FakeOrgStore(_Rec(org_id="o")), user_store=_FakeUserStore(_Rec(username="u")),
            user_id="u1", action="x", resource_type="y", resource_id=None, detail={},
        )   # 不抛异常即通过


class _FakeConvStore:
    def __init__(self, conv):
        self._conv = conv

    async def get_conversation(self, cid):
        return self._conv


class TestRequireConversationOwner:
    @pytest.mark.asyncio
    async def test_missing_is_404(self):
        with pytest.raises(HTTPException) as e:
            await api_helpers.require_conversation_owner(
                conversation_store=_FakeConvStore(None),
                conversation_id="c1", current_user=_Rec(user_id="u1"))
        assert e.value.status_code == 404

    @pytest.mark.asyncio
    async def test_someone_elses_is_403_not_404(self):
        """**存在但不是自己的要 403，不能也报 404。**

        这两个码在这里是刻意区分的：404 用于"连接器/资源不该让你知道它存在"
        （跨企业），403 用于"你知道它存在、但这不是你的"（同企业内的别人的对话）。
        混用会让排查问题的人分不清是打错了 id 还是权限不够。
        """
        with pytest.raises(HTTPException) as e:
            await api_helpers.require_conversation_owner(
                conversation_store=_FakeConvStore(_Rec(user_id="other")),
                conversation_id="c1", current_user=_Rec(user_id="me"))
        assert e.value.status_code == 403


class TestRequireAiopsEnabledOrg:
    @pytest.mark.asyncio
    async def test_no_org_is_403(self):
        class _Ops:
            async def is_module_enabled(self, _):
                raise AssertionError("没有 org 就不该再问模块开关")

        with pytest.raises(HTTPException) as e:
            await api_helpers.require_aiops_enabled_org(
                org_store=_FakeOrgStore(None), ops_store=_Ops(), current_user=_Rec(user_id="u"))
        assert e.value.status_code == 403

    @pytest.mark.asyncio
    async def test_module_disabled_is_403_with_actionable_message(self):
        """未开通的提示要说清"找谁"——否则管理员只知道被拒了，不知道下一步。"""
        class _Ops:
            async def is_module_enabled(self, _):
                return False

        with pytest.raises(HTTPException) as e:
            await api_helpers.require_aiops_enabled_org(
                org_store=_FakeOrgStore(_Rec(org_id="o")), ops_store=_Ops(),
                current_user=_Rec(user_id="u"))
        assert e.value.status_code == 403
        assert "平台管理员" in e.value.detail

    @pytest.mark.asyncio
    async def test_enabled_returns_the_org_so_callers_need_not_requery(self):
        class _Ops:
            async def is_module_enabled(self, _):
                return True

        org = _Rec(org_id="o1")
        got = await api_helpers.require_aiops_enabled_org(
            org_store=_FakeOrgStore(org), ops_store=_Ops(), current_user=_Rec(user_id="u"))
        assert got is org


class TestGetOwnedConnector:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("connector", [None, _Rec(org_id="other")])
    async def test_missing_and_cross_org_are_both_404(self, connector):
        """**跨企业访问必须跟"不存在"给出完全相同的响应。**

        报 403 等于告诉对方"这个连接器存在，只是不是你的"——那本身就是
        跨企业的信息泄露。这条是原实现 docstring 里明写的约定，提取后钉住它。
        """
        class _Ops:
            async def get_connector(self, cid):
                return connector

        with pytest.raises(HTTPException) as e:
            await api_helpers.get_owned_connector(
                ops_store=_Ops(), org_id="mine", connection_id="c1")
        assert e.value.status_code == 404


class TestPureResponseBuilders:
    def test_role_ops_permission_response_maps_all_four_fields(self):
        out = api_helpers.role_ops_permission_response(
            _Rec(role_id="r", connection_id="c", can_view=True, can_approve=False))
        assert (out.role_id, out.connection_id, out.can_view, out.can_approve) == ("r", "c", True, False)

    def test_workflow_template_response_maps_all_fields(self):
        """⚠️ `required_fields` 是 `WorkflowFieldSpec` 的列表，不是字符串列表。
        第一版这里塞了 `["a"]`，Pydantic 当场拒收——**本产品线又一次"猜字段形状"翻车**——
        而且连猜两次（先是把它当字符串列表，再是把字段名写成 `name` 而实际是
        `key`）。这次是被测试当场抓住的，好过在真机上表现为静默的空列表。
        所以这里引真实模型构造，不自己编。"""
        from src.ragent_backend.schemas import WorkflowFieldSpec

        spec = WorkflowFieldSpec(key="days", label="天数", type="number", required=True)
        out = api_helpers.workflow_template_response(_Rec(
            template_id="t", workflow_type="leave", display_name="请假",
            description="d", required_fields=[spec], attachments_note="n",
            is_system=True, created_at=1.0))
        assert out.template_id == "t" and out.is_system is True
        assert [f.key for f in out.required_fields] == ["days"]
