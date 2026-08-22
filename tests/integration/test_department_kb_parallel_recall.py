"""验证知识库权限路由的两条路径：

1. 平台自己（org_platform）2026-08-22 起不再有任何本地业务知识库——不管
   持有什么角色，平台账号对任何知识库问题都应该拿到"无权访问"（或没有任何
   候选 collection），不应该意外查到内容。这是这次改造要保证的核心行为
   （见 `src/mcp_server/tools/query_knowledge_hub.py` `_org_owned_collections`
   顶部说明）。
2. 委托模式企业（Acme/Globex）的知识库改用跟平台之前本地部门库同一套 6 类
   分类（人力资源与行政/财务与报销制度/IT支持与技术运维/销售话术与市场/
   研发与产品代码/客户成功与售后服务），角色和类目严格一对一——用真实种子
   账号（bob_acme=finance_kb、dave_globex=hr_admin_kb）验证过滤按角色正确
   生效，且两家企业共用同一份映射（见 DEPARTMENT_ROLE_TO_REMOTE_CATEGORIES）。

依赖本地 Ollama（embedding + reranker）和已经跑过的
`scripts/generate_tenant_kb_corpus.py` + `scripts/ingest_tenant_kb_corpus.py`；
委托企业的模拟微服务（`scripts/tenant_service_supervisor.py`）不可用时相关
测试会跳过，不阻断整体测试套件。
"""

from __future__ import annotations

import pytest

# 种子账号 id，见 scripts/seed_tenant_kb_demo.py / DB 里的实际值。
ADMIN_USER_ID = "de221485-cf9b-4a68-9614-5e9ded7eba11"  # admin，super_admin，平台
BOB_ACME_USER_ID = "e1c677b8-3068-41f0-b6aa-dc8a2f6cecee"  # bob_acme，finance_kb，Acme（委托模式）
ALICE_ACME_USER_ID = "5a5cb2cb-a870-4399-b486-1a9da045b627"  # alice_acme，org_admin，Acme
DAVE_GLOBEX_USER_ID = "62f85655-4887-416c-8977-2d05717928cd"  # dave_globex，hr_admin_kb，Globex（委托模式）

# 每个类目一个针对性问题，措辞跟语料原句不同（避免变成关键词照抄的假阳性）。
ACME_QUERIES_BY_CATEGORY = {
    "finance_kb 应该看得到（财务与报销制度）": "报销发票有什么要求",
    "finance_kb 不该看到（IT支持与技术运维）": "域账号密码多久强制更换",
}
GLOBEX_QUERIES_BY_CATEGORY = {
    "hr_admin_kb 应该看得到（人力资源与行政）": "驾驶员年假怎么算",
    "hr_admin_kb 不该看到（客户成功与售后服务）": "客户投诉货物破损怎么处理",
}


@pytest.fixture
def tool():
    # 每个测试函数一个实例，不跨测试共享——pytest-asyncio 默认每个测试函数
    # 各自起一个新的 event loop，class 级共享的实例会把上一个测试（旧 loop）
    # 里建好的连接带到下一个测试（新 loop）里用，报 "Event loop is closed"。
    try:
        from src.mcp_server.tools.query_knowledge_hub import QueryKnowledgeHubTool
        from src.core.settings import load_settings

        settings = load_settings()
        instance = QueryKnowledgeHubTool(settings=settings)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"无法初始化 QueryKnowledgeHubTool: {e}")
    return instance


class TestPlatformHasNoLocalKnowledgeBase:
    """平台运营方不代表任何具体企业，不该有任何本地业务知识库内容可查。"""

    @pytest.mark.asyncio
    async def test_platform_admin_gets_denied(self, tool) -> None:
        try:
            response = await tool.execute(query="报销流程是什么", user_id=ADMIN_USER_ID)
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"检索服务不可用: {e}")
        assert response.is_empty, (
            f"平台账号不该查到任何本地知识库内容，实际: {response.content[:200]}"
        )


class TestDelegatedCompanyCategoryRouting:
    """委托模式企业按角色过滤类目——用真实种子账号验证，不新建临时账号。"""

    @pytest.mark.asyncio
    async def test_acme_org_admin_unrestricted(self, tool) -> None:
        try:
            response = await tool.execute(query="报销发票有什么要求", user_id=ALICE_ACME_USER_ID)
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"委托模式微服务不可用（tenant_service_supervisor 没起？）: {e}")
        assert not response.is_empty, f"企业管理员应该能查到内容: {response.content[:200]}"

    @pytest.mark.asyncio
    async def test_bob_acme_finance_role_allowed(self, tool) -> None:
        try:
            response = await tool.execute(query="报销发票有什么要求", user_id=BOB_ACME_USER_ID)
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"委托模式微服务不可用: {e}")
        assert not response.is_empty, (
            f"finance_kb 角色应该能查到财务与报销制度类目内容: {response.content[:200]}"
        )

    @pytest.mark.asyncio
    async def test_bob_acme_finance_role_denied_it_content(self, tool) -> None:
        try:
            response = await tool.execute(query="域账号密码多久强制更换", user_id=BOB_ACME_USER_ID)
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"委托模式微服务不可用: {e}")
        assert response.is_empty, (
            f"finance_kb 角色不该查到 IT 支持与技术运维类目内容: {response.content[:200]}"
        )

    @pytest.mark.asyncio
    async def test_dave_globex_hr_role_allowed(self, tool) -> None:
        try:
            response = await tool.execute(query="驾驶员年假怎么算", user_id=DAVE_GLOBEX_USER_ID)
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"委托模式微服务不可用: {e}")
        assert not response.is_empty, (
            f"hr_admin_kb 角色应该能查到人力资源与行政类目内容: {response.content[:200]}"
        )

    @pytest.mark.asyncio
    async def test_dave_globex_hr_role_denied_customer_service_content(self, tool) -> None:
        try:
            response = await tool.execute(query="客户投诉货物破损怎么处理", user_id=DAVE_GLOBEX_USER_ID)
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"委托模式微服务不可用: {e}")
        assert response.is_empty, (
            f"hr_admin_kb 角色不该查到客户成功与售后服务类目内容: {response.content[:200]}"
        )
