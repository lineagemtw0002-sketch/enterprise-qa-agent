"""知识库 / 仪表盘 / 通知路由模块——批次 2 的验收证据。

同批次 1：router 挂在裸 `FastAPI()` 上、依赖全传假件，**不构造 `create_app()`、
不连 Postgres**。搬迁之前这些端点是 `create_app()` 的内嵌闭包，写不出这些测试。

⚠️ 数路由要用 `app.openapi()` 不能数 `app.routes`（这个 FastAPI 版本的
`include_router` 延迟展开，详见 `test_ops_router.py` 的说明）。
"""

import asyncio

import pytest
from fastapi import FastAPI

from src.ragent_backend.api.kb_dashboard_router import build_kb_dashboard_router


class _Rec:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _router(**overrides):
    deps = dict(
        org_store=None, user_store=None, role_store=None, workflow_store=None,
        org_collection_store=None, tenant_connector_store=None,
        dashboard_stats_service=None, kb_management_tool=None, settings=None,
        model_price_per_1m_usd={}, ingest_semaphore=asyncio.Semaphore(2),
        upload_progress={}, allowed_extensions={".pdf"},
        get_current_user=lambda: _Rec(user_id="u1"),
        require_org_admin=lambda: _Rec(user_id="u1"),
        require_platform_tier=lambda *a, **k: (lambda: _Rec(user_id="u1")),
        audit_log=None,
    )
    deps.update(overrides)
    return build_kb_dashboard_router(**deps)


class TestRouterShape:
    def test_carries_all_15_endpoints(self):
        assert len(_router().routes) == 15

    def test_mounts_onto_a_bare_app(self):
        app = FastAPI()
        app.include_router(_router())
        spec = app.openapi()["paths"]
        assert len([m for p in spec for m in spec[p]]) == 15

    def test_paths_stay_under_the_documented_domains(self):
        """路径一个字都不能变——改了会静默破坏前端和验证脚本，单测抓不到。"""
        for r in _router().routes:
            assert (r.path.startswith("/api/v1/admin/collections")
                    or r.path.startswith("/api/v1/admin/dashboard")
                    or r.path.startswith("/api/v1/collections")
                    or r.path.startswith("/api/v1/notifications")), r.path


class TestSharedMutableStateIsInjectedNotRecreated:
    """**这组是批次 2 最要紧的测试。**

    `ingest_semaphore` 和 `upload_progress` 是共享可变状态。如果 router 在自己
    内部新建一份，表现是：并发摄入上限悄悄翻倍、上传进度的读写两半看不见彼此
    ——**两者都不会报任何错**，只会在真实负载下变成难查的怪现象。

    所以这里断言的不是"能不能跑"，而是"router 用的是不是**我传进去的那一个
    对象**"。
    """

    def test_upload_progress_dict_is_the_very_same_object(self):
        """⚠️ 第一版这里只查了 `routes[0]` 的闭包——那恰好是别的端点，
        断言当场失败，我差点以为是搬迁出了问题。**要在所有端点里找**。
        """
        shared = {}
        r = _router(upload_progress=shared)
        cells = [c.cell_contents for route in r.routes
                 for c in (route.endpoint.__closure__ or ())]
        assert any(c is shared for c in cells), \
            "router 里的 upload_progress 不是传进来的那一个对象——另建了一份，" \
            "会导致写进度和读进度的两半看不见彼此"

    def test_semaphore_reaches_the_background_ingest_task(self):
        """⚠️ `INGEST_SEMAPHORE` **不在端点的闭包里**——它被后台摄入任务
        (`_run_collection_ingest_task`) 用，而那是工厂里的另一个内嵌函数。
        第一版按"端点闭包里应该有它"来断言，失败了；查清楚之后才知道是断言
        写错了位置，不是注入没生效。这条改成从工厂返回的整个闭包链里找。
        """
        sem = asyncio.Semaphore(2)
        r = _router(ingest_semaphore=sem)
        # 摄入任务是被端点闭包引用的函数，往下再挖一层
        seen = []
        for route in r.routes:
            for c in (route.endpoint.__closure__ or ()):
                seen.append(c.cell_contents)
                inner = getattr(c.cell_contents, "__closure__", None)
                if inner:
                    seen.extend(x.cell_contents for x in inner)
        assert any(c is sem for c in seen), \
            "INGEST_SEMAPHORE 不是传进来的那一个——并发摄入上限会悄悄翻倍"
