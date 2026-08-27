"""智能运维路由模块（`src/ragent_backend/api/ops_router.py`）——批次 1 的验收证据。

`docs/app_layering_design.md` 给每批定的第二条验收是**"新增至少一条『不建整个
app 就能测这批端点』的测试，否则这一批等于只搬了文件"**。这个文件就是那条证据。

下面的用例把 router 挂在一个**裸 `FastAPI()`** 上，依赖全传假件：
没有 `create_app()`、没有 Postgres、没有 Ollama、没有预热任何模型。
搬迁之前这 23 个端点是 `create_app()` 的内嵌闭包，**这里一条都写不出来**。

⚠️ **数路由要用 `app.openapi()`，不能数 `app.routes`。** 这个 FastAPI 版本的
`include_router` 是延迟展开的——挂载后 `app.routes` 里放的是一个
`_IncludedRouter` 包装对象，真实路由要到生成 OpenAPI（或应用启动）时才铺开。
搬迁验证时我在这里卡了很久，一直以为"路由丢了"，实际是排查方法错了。
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.ragent_backend.api.ops_router import build_ops_router


class _Rec:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _router(**overrides):
    """造一个挂在裸 app 上的 router。默认依赖全是"什么都不做"的假件，
    需要哪个就 override 哪个——这正是搬迁换来的能力。"""
    deps = dict(
        ops_store=None, ops_toolset=None, ops_engine=None,
        role_store=None, org_store=None,
        get_current_user=lambda: _Rec(user_id="u1", username="alice"),
        require_org_admin=lambda: _Rec(user_id="u1", username="alice"),
        audit_log=None,
    )
    deps.update(overrides)
    return build_ops_router(**deps)


class TestRouterShape:
    def test_carries_all_23_endpoints(self):
        """23 这个数字是搬迁时实测的端点数。少了就是漏搬，多了就是夹带。"""
        assert len(_router().routes) == 23

    def test_all_paths_stay_under_the_documented_prefixes(self):
        """路径**一个字都不能变**——分层是纯搬迁，改路径会静默破坏所有前端调用
        和验证脚本，而单测抓不到。"""
        paths = {r.path for r in _router().routes}
        assert all(p.startswith("/api/v1/admin/ops") or "ops-permissions" in p
                   for p in paths), paths

    def test_mounts_onto_a_bare_app_without_create_app(self):
        """**这条本身就是批次 1 的验收**：挂在裸 app 上、不碰 `create_app()`。"""
        app = FastAPI()
        app.include_router(_router())
        spec = app.openapi()["paths"]
        assert len([m for p in spec for m in spec[p]]) == 23


class TestDependencyInjectionActuallyWorks:
    """搬迁的目的是"依赖可替换"。下面这条证明假件真的被用上了——
    如果依赖仍然靠闭包捕获，传进来的假件会被忽略，这条就会失败。"""

    def test_injected_store_is_the_one_the_endpoint_talks_to(self):
        calls = []

        class _FakeOpsStore:
            async def list_connectors_for_org(self, org_id):
                calls.append(org_id)
                return []

            async def is_module_enabled(self, org_id):
                return True

        class _FakeOrgStore:
            async def get_org_for_user(self, user_id):
                return _Rec(org_id="org-from-fake", name="假企业")

        app = FastAPI()
        app.include_router(_router(ops_store=_FakeOpsStore(), org_store=_FakeOrgStore()))
        client = TestClient(app)
        resp = client.get("/api/v1/admin/ops/connectors")

        # 端点自己的鉴权用的是注入进来的 require_org_admin（返回假用户），
        # 所以这里应当走到业务逻辑、并把假 store 用上。
        assert resp.status_code == 200, resp.text
        assert calls == ["org-from-fake"], f"假 store 没被调用：{calls}"
