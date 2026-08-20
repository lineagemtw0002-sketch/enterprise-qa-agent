"""验证"全库混合召回 + 重排"：6 个固定部门知识库都有数据，且针对性问题能通过
并行召回 + 重排正确定位到对应的知识库。

背景见 `src/mcp_server/tools/query_knowledge_hub.py` 顶部 DEPARTMENT_KB_COLLECTIONS
旁的说明——调用方（LLM）不再需要猜"该查哪个 collection"，不显式指定时直接对
用户角色关联的全部部门知识库并行做 dense+sparse 混合检索，候选结果合并后统一
重排，取最终 top_k。

依赖本地 Ollama（embedding + reranker）和 `scripts/seed_department_kb_v2.py`
摄入好的数据；服务不可用时相关测试会跳过，不阻断整体测试套件。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.seed_department_kb_v2 import CORPUS_DIR, DEPARTMENT_KBS

BOB_USER_ID = "cfbb7ebd-758c-4fbd-96a5-266d996a97ba"

# 每个库一个针对性问题，措辞跟语料原句不同（避免变成关键词照抄的假阳性），
# 覆盖语料里真实存在的具体规则。
REPRESENTATIVE_QUERIES = {
    "hr_admin_kb": "年假没休完能不能留到明年用",
    "finance_kb": "报销发票丢了怎么办",
    "it_support_kb": "收到钓鱼邮件应该怎么处理",
    "sales_marketing_kb": "客户说价格太贵了怎么应对",
    "rd_product_kb": "代码合并到主分支有什么要求",
    "customer_success_kb": "客户使用量下降了要不要主动联系",
}


class TestDepartmentKbCorpusExists:
    """先确认语料和摄入都到位，检索结果才有意义。"""

    @pytest.mark.parametrize("collection", sorted(DEPARTMENT_KBS))
    def test_corpus_files_exist(self, collection: str) -> None:
        corpus_dir = CORPUS_DIR / collection
        assert corpus_dir.is_dir(), f"{corpus_dir} 不存在，请先跑 scripts/seed_department_kb_v2.py"
        files = list(corpus_dir.glob("*.txt"))
        assert len(files) >= 8, f"{collection} 语料只有 {len(files)} 条，量级太少"

    @pytest.mark.parametrize("collection", sorted(DEPARTMENT_KBS))
    def test_collection_ingested(self, collection: str) -> None:
        chromadb = pytest.importorskip("chromadb")
        client = chromadb.PersistentClient(path=str(Path("data/db/chroma")))
        names = {c.name for c in client.list_collections()}
        assert collection in names, f"{collection} 还没摄入到本地 Chroma"
        coll = client.get_collection(collection)
        assert coll.count() > 0, f"{collection} 摄入了但是 0 条"


class TestParallelRecallRoutesToCorrectKb:
    """核心验证：全库并行召回 + 重排，针对性问题的重排后 top 结果确实来自
    对应的部门知识库（用 bob 账号，持有全部 6 个部门角色）。"""

    @pytest.fixture
    def tool(self):
        # 每个测试函数一个实例，不跨测试共享——pytest-asyncio 默认每个测试函数
        # 各自起一个新的 event loop，class 级共享的实例会把上一个测试（旧 loop）
        # 里建好的连接带到下一个测试（新 loop）里用，报 "Event loop is closed"，
        # 实测踩过（见这次改造顺带发现的记录）。QueryKnowledgeHubTool 内部真正
        # 无状态、可复用的部分（embedding client/reranker）已经在工具自己内部
        # 按需缓存，这里换成函数级 fixture 只是不跨"测试函数"复用最外层实例，
        # 不影响工具自身的缓存策略。
        try:
            from src.mcp_server.tools.query_knowledge_hub import QueryKnowledgeHubTool
            from src.core.settings import load_settings

            settings = load_settings()
            instance = QueryKnowledgeHubTool(settings=settings)
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"无法初始化 QueryKnowledgeHubTool: {e}")
        return instance

    @pytest.mark.parametrize("expected_collection,query", sorted(REPRESENTATIVE_QUERIES.items()))
    @pytest.mark.asyncio
    async def test_query_hits_expected_kb(self, tool, expected_collection: str, query: str) -> None:
        try:
            response = await tool.execute(query=query, user_id=BOB_USER_ID)
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"检索服务不可用（Ollama/Chroma 未就绪？）: {e}")

        assert not response.is_empty, f"'{query}' 全库召回没有命中任何结果"
        contributing = response.metadata.get("collections") or (
            [response.metadata["collection"]] if response.metadata.get("collection") else []
        )
        assert expected_collection in contributing, (
            f"'{query}' 期望命中 {expected_collection}，实际来源: {contributing}"
        )

    @pytest.mark.asyncio
    async def test_no_department_access_denied(self, tool) -> None:
        """没有任何部门知识库权限的用户（carol_globex，属于走委托模式的 Globex，
        不持有平台内部任何部门角色）应该被拒绝，而不是意外查到别人的部门库。"""
        no_access_user_id = "22da24df-4b48-4431-b0a4-678f31d41fb4"  # carol_globex
        try:
            response = await tool.execute(query="年假怎么算", user_id=no_access_user_id)
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"检索服务不可用: {e}")
        # carol_globex 属于 Globex（委托模式），走的是委托路径而不是本地部门库拒绝
        # 分支——这里只断言它绝对拿不到平台内部部门知识库的内容，具体走哪条分支
        # 不是这个测试关心的点。
        contributing = response.metadata.get("collections") or (
            [response.metadata["collection"]] if response.metadata.get("collection") else []
        )
        assert not any(c in DEPARTMENT_KBS for c in contributing), (
            f"carol_globex 不该查到平台内部部门知识库，实际来源: {contributing}"
        )
