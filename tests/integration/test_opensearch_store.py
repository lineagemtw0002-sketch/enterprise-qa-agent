"""OpenSearch 存储层的行为级测试 —— `docs/opensearch_migration_design.md` §6。

**这组测试刻意写成行为级，不断言任何内部状态。**

理由写在设计文档 §6.3：上一轮 SQLite 迁移写了 29 条测试，全是白盒
（断言 `_index` 内部结构、postings 条数、逐 bit 分数），换引擎之后**一条都留不下**。
这次的判据统一成"给定语料和操作，期望看到什么检索结果"——这样下次再换引擎
（Qdrant、Tantivy、随便什么）这些测试仍然有效。

对应设计文档 §6.1 的用例编号：
- T-1 重摄入后旧版本片段消失      —— 现状会失败（P0#1，已实测）
- T-2 按文档删除真的删得掉        —— 现状会失败（P0#7，恒返回 False）
- T-5 企业内对话隔离              —— **守的是本次主动降级的那道防线**
- T-7 服务不可用时明确报错，不静默返回空

判别力分类（交付时必须分开报，见 §6.4）：
T-1 / T-2 / T-7 对应真缺陷，迁移前会失败；
T-5 是**回归保护**，不是修复验证 —— 现状靠物理隔离天然成立。
"""

from __future__ import annotations

import os
import uuid

import pytest

from src.libs.search.opensearch_store import (
    OpenSearchStore,
    chunk_doc_id,
    conv_index_name,
    kb_index_name,
)

pytestmark = pytest.mark.integration


def _store() -> OpenSearchStore:
    s = OpenSearchStore()
    if not s.ping():
        pytest.skip(
            "OpenSearch 未运行。起：docker compose up -d opensearch"
        )
    return s


@pytest.fixture()
def store():
    return _store()


@pytest.fixture()
def kb(store):
    """每个测试用独立 collection，跑完删干净——避免测试之间互相污染。"""
    name = f"pytest_{uuid.uuid4().hex[:12]}"
    index = kb_index_name(name)
    store.ensure_index(index)
    yield name, index
    store.drop_index(index)


def _chunk(source: str, idx: int, text: str, **extra):
    return {
        "content": text,
        "source_path": source,
        "chunk_index": idx,
        "chunk_id": f"{source}#{idx}",
        **extra,
    }


# ───────────────── T-1：重摄入后旧版本片段必须消失 ─────────────────


class TestStaleChunksDisappear:
    """P0#1：现状是"改一句话重传，库中两版各 1 条"（已实测确认）。"""

    def test_reingesting_same_document_replaces_not_duplicates(self, store, kb):
        name, index = kb
        src = "/docs/年假制度.pdf"

        store.index_chunks(index, [_chunk(src, 0, "年假可以顺延到次年三月")], refresh=True)
        assert store.count(index) == 1

        # 同一份文件，内容改了一句话，重新摄入
        store.index_chunks(index, [_chunk(src, 0, "年假可以顺延到次年六月")], refresh=True)

        assert store.count(index) == 1, "旧版本没被覆盖，库里现在有两版"
        hits = store.search_kb(name, "年假 顺延")
        assert len(hits) == 1
        assert "六月" in hits[0]["content"]
        assert "三月" not in hits[0]["content"], "旧版本内容仍可被检索到"

    def test_shortened_document_drops_orphan_chunks(self, store, kb):
        """文档变短时，多出来的旧 chunk 不会被任何新 chunk 覆盖。

        **少了 `delete_stale_chunks`，P0#1 只解决一半** —— 前 N 个 chunk 覆盖了，
        第 N+1 个之后的旧内容还留在库里，而且检索得到。
        """
        name, index = kb
        src = "/docs/长文档.pdf"

        store.index_chunks(
            index,
            [_chunk(src, i, f"第{i}段 关键词{i}") for i in range(5)],
            refresh=True,
        )
        assert store.count(index) == 5

        # 编辑后只剩 2 段
        store.index_chunks(
            index, [_chunk(src, i, f"新第{i}段 关键词{i}") for i in range(2)], refresh=True
        )
        deleted = store.delete_stale_chunks(index, src, keep_count=2, refresh=True)

        assert deleted == 3
        assert store.count(index) == 2

        # ⚠️ 断言要精确到内容，不能靠 match 查询"查不到"来证明删干净了。
        # 默认 standard 分析器把中文切成**单字**，"关键词4" 会匹配上 "关键词0"
        # （共享 关/键/词）—— 第一版这条测试就是这么写错的，红了才发现分析器
        # 这个设计缺口。见 opensearch_migration_design.md §4 第 4 条。
        remaining = store.search_kb(name, "段", top_k=99)
        contents = {h["content"] for h in remaining}
        assert contents == {"新第0段 关键词0", "新第1段 关键词1"}
        assert not any("第4段" in c for c in contents), "被删掉那段仍可检索到"

    def test_id_is_content_independent(self):
        """`_id` 必须只由 (source_path, chunk_index) 决定。

        现状 `doc_id = 文件内容 SHA256`，内容一变就是新文档——**那个设计本身
        制造了 P0#1**。这条钉住新解法不要退回去。
        """
        a = chunk_doc_id("/docs/x.pdf", 0)
        b = chunk_doc_id("/docs/x.pdf", 0)
        assert a == b
        assert a != chunk_doc_id("/docs/x.pdf", 1)
        assert a != chunk_doc_id("/docs/y.pdf", 0)


# ───────────────── T-2：按文档删除真的删得掉 ─────────────────


class TestDeletionActuallyWorks:
    """P0#7：现状 `remove_document` 三轮独立测试恒返回 False。"""

    def test_delete_by_source_removes_all_its_chunks(self, store, kb):
        name, index = kb
        store.index_chunks(
            index,
            [_chunk("/docs/a.pdf", i, f"甲文档第{i}段 报销") for i in range(3)]
            + [_chunk("/docs/b.pdf", i, f"乙文档第{i}段 报销") for i in range(2)],
            refresh=True,
        )
        assert store.count(index) == 5

        deleted = store.delete_by_source(index, "/docs/a.pdf", refresh=True)

        assert deleted == 3
        remaining = store.search_kb(name, "报销", top_k=99)
        assert len(remaining) == 2
        assert all(h["source_path"] == "/docs/b.pdf" for h in remaining), "误删了别的文档"

    def test_delete_unknown_source_is_noop(self, store, kb):
        name, index = kb
        store.index_chunks(index, [_chunk("/docs/a.pdf", 0, "内容")], refresh=True)
        assert store.delete_by_source(index, "/docs/不存在.pdf", refresh=True) == 0
        assert store.count(index) == 1


# ───────────────── T-5：企业内对话隔离（守本次降级的防线）─────────────────


class TestConversationIsolation:
    """⚠️ **这组是本设计最关键的测试。**

    对话私有库从"一对话一物理存储"变成"一企业一 index + 字段过滤"，
    企业内隔离强度下降一档。物理隔离时漏了过滤条件顶多查不到数据；
    字段过滤时漏了就是**越权返回**。
    """

    @pytest.fixture()
    def conv_index(self, store):
        org = f"pytest_org_{uuid.uuid4().hex[:8]}"
        index = conv_index_name(org)
        store.ensure_index(index)
        yield org, index
        store.drop_index(index)

    def test_user_cannot_read_another_users_conversation(self, store, conv_index):
        org, index = conv_index
        store.index_chunks(
            index,
            [
                _chunk("/up/alice.pdf", 0, "爱丽丝的机密报销单",
                       conversation_id="conv_A", owner_user_id="alice"),
                _chunk("/up/bob.pdf", 0, "鲍勃的机密报销单",
                       conversation_id="conv_B", owner_user_id="bob"),
            ],
            refresh=True,
        )

        alice = store.search_conv(org, "conv_A", "alice", "机密 报销单", top_k=99)
        assert len(alice) == 1
        assert "爱丽丝" in alice[0]["content"]

        # 鲍勃知道爱丽丝的 conversation_id，但不是所有者
        stolen = store.search_conv(org, "conv_A", "bob", "机密 报销单", top_k=99)
        assert stolen == [], "拿到别人的 conversation_id 就能读到内容 —— 越权"

    def test_owner_user_id_is_a_required_positional_argument(self, store, conv_index):
        """漏传 `owner_user_id` 必须是 TypeError，而不是"查到全部"。

        这条把"靠每个调用点记得过滤"换成"漏了根本调不通"。
        现有 `acl.py` 对 `conv_` 前缀无条件返回 True，防护完全是约定式的
        （11 个调用点各自记得先校验），迁移后不能再依赖那种约定。
        """
        org, _ = conv_index
        with pytest.raises(TypeError):
            store.search_conv(org, "conv_A", "机密")  # type: ignore[call-arg]

    def test_deleting_conversation_removes_its_documents(self, store, conv_index):
        """对话生命周期是 `conv_*` 数据的主回收机制，不是 ILM。"""
        org, index = conv_index
        store.index_chunks(
            index,
            [
                _chunk("/up/a.pdf", 0, "甲对话文档", conversation_id="conv_A", owner_user_id="u1"),
                _chunk("/up/b.pdf", 0, "乙对话文档", conversation_id="conv_B", owner_user_id="u1"),
            ],
            refresh=True,
        )
        deleted = store.delete_by_conversation(index, "conv_A", refresh=True)

        assert deleted == 1
        assert store.search_conv(org, "conv_A", "u1", "对话文档") == []
        assert store.search_conv(org, "conv_B", "u1", "对话文档"), "误删了别的对话"


# ───────────────── T-7：服务不可用时明确报错 ─────────────────


class TestFailureIsLoud:
    def test_unreachable_server_raises_not_returns_empty(self):
        """**静默返回空结果是最坏的失败模式** —— 用户看到的是"知识库里没有"，
        而不是"检索挂了"，运维也不会收到任何信号。
        """
        dead = OpenSearchStore(url="http://127.0.0.1:59999")
        assert dead.ping() is False
        with pytest.raises(Exception):
            dead.search_kb("whatever", "查询")

    def test_bulk_partial_failure_raises(self, store, kb):
        """bulk 部分失败不能当成功——那会让索引静默缺数据。"""
        _, index = kb
        with pytest.raises(RuntimeError, match="bulk"):
            store.index_chunks(
                index,
                [_chunk("/docs/x.pdf", 0, "正常"),
                 {"content": "坏数据", "source_path": "/docs/y.pdf",
                  "chunk_index": 0, "ingested_at": "这不是日期"}],
                refresh=True,
            )
