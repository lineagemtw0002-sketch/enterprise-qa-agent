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
    build_chunk_doc,
    chunk_doc_id,
    conv_index_name,
    kb_index_name,
    tokenize_for_index,
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
    """走真实的 build_chunk_doc + 索引侧分词器，不要在测试里另拼一份文档结构。

    `content` 字段用的是 whitespace 分析器，写入前必须先分词；测试里绕过这一步
    会让测试通过而生产检索不到 —— 那正是最坏的一类假绿。
    """
    return build_chunk_doc(
        text=text,
        tokens=tokenize_for_index(text),
        source_path=source,
        chunk_index=idx,
        chunk_id=f"{source}#{idx}",
        **extra,
    )


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
        # ⚠️ 查询词要选**两侧分词器产出一致**的。索引侧切出 `顺延到`、
        # 查询 "顺延" 切出 `顺延`，whitespace 分析器下二者不等 —— 匹配不上。
        # 这不是本模块的 bug，是现有 SparseEncoder / QueryProcessor 分词不一致
        # 的直接后果（见 tokenize_for_query 的说明），现有 BM25 同样中招，
        # 只是从来没人测过。本次刻意不修，如实复刻。
        hits = store.search_kb(name, "次年")
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
        remaining = store.search_kb(name, "关键词", top_k=99)
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


# ───────────────── 分词一致性：如实记录现状，不是修复 ─────────────────


class TestTokenizerMismatchIsFaithfullyReproduced:
    """⚠️ **这组断言的是"现状是坏的"，不是"我们修好了"。**

    索引侧（`SparseEncoder`，分词器）与查询侧（`QueryProcessor`，关键词抽取器）
    产出不一致，是现有系统的既有状态。对同一批 22 条真实 chunk 实测：
    两侧都产出 132 词（69%）、只有索引侧 41 词（21%）、只有查询侧 29 词（15%）。

    而 `sparse_encoder.py::_tokenize` 的注释声称"必须与查询侧一致" ——
    **那个声称与实现不符**，属注释里的未证实断言（`CLAUDE.md` §7.2 明令
    这类断言要么验证要么标为假设）。

    本次迁移**刻意不修**：修分词会同时改变检索结果，与"换存储引擎"两个变量
    一起动就无法归因。这里如实复刻，切读时的新旧比对才有意义。

    **修好之后这组测试会变红，那正是提醒删掉它的信号** ——
    不要在修分词之前把它改绿。
    """

    def test_index_and_query_tokenizers_currently_disagree(self):
        from src.libs.search.opensearch_store import (
            tokenize_for_index,
            tokenize_for_query,
        )

        text = "年假可以顺延到次年三月"
        idx = set(tokenize_for_index(text))
        qry = set(tokenize_for_query(text))

        assert idx != qry, (
            "两侧分词已经一致了 —— 说明分词问题被修好了。"
            "这是好事，但请连同这整组测试一起删掉，别改绿它。"
        )
        assert qry - idx, "查询侧独有的词（单字/数字）消失了"
        assert idx - qry, "索引侧独有的词消失了"

    def test_mismatched_term_cannot_be_retrieved(self, store, kb):
        """具体后果：索引里是 `顺延到`，用户搜 `顺延` 搜不到。

        现有 BM25 有完全相同的问题（词条级精确匹配），只是从来没被测过。
        """
        name, index = kb
        store.index_chunks(
            index, [_chunk("/docs/x.pdf", 0, "年假可以顺延到次年三月")], refresh=True
        )

        assert store.search_kb(name, "顺延") == [], (
            "如果这条通过了，说明分词已对齐 —— 请同上，删掉这组测试"
        )
        assert store.search_kb(name, "次年"), "两侧一致的词应该能检索到"


# ───────────── 摄入侧影子写：dense 向量 + conv_ 路由 ─────────────


class TestIngestionMirror:
    """`mirror_ingestion_to_opensearch` 的行为。

    这是唯一会被生产摄入路径调用的入口（pipeline.py 阶段 7 的 6b-2），
    所以它的失败模式比查询侧更要紧：写错了要过很久才会在检索时暴露。
    """

    @staticmethod
    def _chunks(n: int):
        from src.core.types import Chunk

        return [
            Chunk(
                id=f"c{i}",
                text=f"年假制度第{i}段 次年 顺延",
                metadata={"source_path": "/t/a.pdf", "chunk_index": i},
            )
            for i in range(n)
        ]

    @staticmethod
    def _stats(n: int):
        return [{"chunk_id": f"vec_{i}"} for i in range(n)]

    @staticmethod
    def _doc():
        from src.core.types import Document

        return Document(id="d" * 64, text="x", metadata={"source_path": "/t/a.pdf"})

    def test_business_kb_gets_dense_vectors(self, store):
        from src.libs.search.opensearch_store import mirror_ingestion_to_opensearch

        col = f"pytest_dense_{uuid.uuid4().hex[:8]}"
        index = kb_index_name(col)
        try:
            mirror_ingestion_to_opensearch(
                collection=col,
                chunks=self._chunks(3),
                sparse_stats=self._stats(3),
                document=self._doc(),
                dense_vectors=[[0.01 * i] * 768 for i in range(3)],
            )
            assert store.count(index) == 3
            mapping = store._client.indices.get_mapping(index=index)
            emb = mapping[index]["mappings"]["properties"]["embedding"]
            assert emb["type"] == "knn_vector"
            assert emb["dimension"] == 768
        finally:
            store.drop_index(index)

    def test_conversation_kb_routes_to_shared_org_index(self, store):
        """`conv_*` 必须进 `conv_{org}`，**不能**一对话一 index。

        一对话一 index 是 index explosion 反模式：每个 index 的元数据由 master
        维护、每个 shard 吃堆内存，而对话私有库只装一两份上传文档。
        """
        from src.libs.search.opensearch_store import mirror_ingestion_to_opensearch

        org = f"pytest_org_{uuid.uuid4().hex[:8]}"
        conv = f"conv_{uuid.uuid4().hex[:8]}"
        try:
            mirror_ingestion_to_opensearch(
                collection=conv,
                chunks=self._chunks(2),
                sparse_stats=self._stats(2),
                document=self._doc(),
                org_id=org,
                owner_user_id="u_alice",
            )
            assert store.count(conv_index_name(org)) == 2
            assert store.count(kb_index_name(conv)) == 0, (
                "给对话单独建了 index —— 这正是要避免的 index explosion"
            )

            cid = conv[len("conv_"):]
            assert len(store.search_conv(org, cid, "u_alice", "次年")) == 2
            assert store.search_conv(org, cid, "u_bob", "次年") == [], "越权可读"
        finally:
            store.drop_index(conv_index_name(org))
            store.drop_index(kb_index_name(conv))

    def test_conversation_kb_without_owner_writes_nothing(self, store, caplog):
        """缺 `owner_user_id` 时宁可影子写失败，也不能写进无法过滤的数据。

        写进去了但没有 owner 字段 = 查询时的 filter 匹配不到任何东西，
        表现为"文档上传成功但检索不到"；更糟的是如果将来放宽 filter，
        这批数据会变成**所有人可读**。失败比脏数据安全。
        """
        import logging

        from src.libs.search.opensearch_store import mirror_ingestion_to_opensearch

        org = f"pytest_org_{uuid.uuid4().hex[:8]}"
        try:
            with caplog.at_level(logging.ERROR):
                mirror_ingestion_to_opensearch(
                    collection=f"conv_{uuid.uuid4().hex[:8]}",
                    chunks=self._chunks(2),
                    sparse_stats=self._stats(2),
                    document=self._doc(),
                    org_id=org,
                    # owner_user_id 故意不传
                )
            assert store.count(conv_index_name(org)) == 0, "写进了无法过滤的脏数据"
            assert any("owner_user_id" in r.getMessage() for r in caplog.records), (
                "失败被静默了 —— 切读时就无从知道哪批数据缺字段"
            )
        finally:
            store.drop_index(conv_index_name(org))


class TestConversationReadPath:
    """`conv_*` 的**读**路径 —— 写入路由已在 TestIngestionMirror 覆盖。

    这组守的是隔离降级后最危险的那条缝：企业内隔离从"一对话一物理存储"
    变成"一企业一 index + owner_user_id 过滤"，**漏了过滤条件就是越权返回**。
    """

    @pytest.fixture()
    def seeded(self, store):
        from src.libs.search.opensearch_store import mirror_ingestion_to_opensearch
        from src.core.types import Chunk, Document

        org = f"pytest_org_{uuid.uuid4().hex[:8]}"
        doc = Document(id="d" * 64, text="x", metadata={"source_path": "/up/x.pdf"})

        def chunks(tag: str):
            return [
                Chunk(
                    id=f"{tag}_0",
                    text=f"{tag}的机密文档 次年 顺延",
                    metadata={"source_path": f"/up/{tag}.pdf", "chunk_index": 0},
                )
            ]

        for conv, user, tag in [("conv_aaa", "alice", "爱丽丝"), ("conv_bbb", "bob", "鲍勃")]:
            mirror_ingestion_to_opensearch(
                collection=conv,
                chunks=chunks(tag),
                sparse_stats=[{"chunk_id": f"{tag}_vec"}],
                document=doc,
                dense_vectors=[[0.1] * 8],
                org_id=org,
                owner_user_id=user,
            )
        yield org
        store.drop_index(conv_index_name(org))

    def test_sparse_retriever_isolates_by_owner(self, seeded, store):
        from src.core.query_engine.opensearch_retrievers import (
            OpenSearchSparseRetriever,
        )

        mine = OpenSearchSparseRetriever(
            "conv_aaa", store, org_id=seeded, owner_user_id="alice"
        ).retrieve(["次年", "顺延"], top_k=9)
        assert len(mine) == 1
        assert "爱丽丝" in mine[0].text

        # 鲍勃知道爱丽丝的 conversation_id，但不是所有者
        stolen = OpenSearchSparseRetriever(
            "conv_aaa", store, org_id=seeded, owner_user_id="bob"
        ).retrieve(["次年", "顺延"], top_k=9)
        assert stolen == [], "拿到别人的 conversation_id 就读到了内容 —— 越权"

    def test_dense_retriever_isolates_by_owner(self, seeded, store):
        from src.core.query_engine.opensearch_retrievers import OpenSearchDenseRetriever

        class FakeEmbedding:
            def embed(self, texts):
                return [[0.1] * 8 for _ in texts]

        mine = OpenSearchDenseRetriever(
            "conv_aaa", store, FakeEmbedding(), org_id=seeded, owner_user_id="alice"
        ).retrieve("机密")
        assert len(mine) == 1 and "爱丽丝" in mine[0].text

        stolen = OpenSearchDenseRetriever(
            "conv_aaa", store, FakeEmbedding(), org_id=seeded, owner_user_id="bob"
        ).retrieve("机密")
        assert stolen == [], "向量检索侧越权"

    def test_missing_identity_raises_not_silently_unfiltered(self, seeded, store):
        """缺身份时必须炸，**不能不过滤地查**。

        不过滤地查会返回同企业其他用户的对话文档 —— 越权。
        炸掉最多是"这次检索失败"，代价小得多。
        调用方（`_build_hybrid_search_for`）在缺身份时已经回退旧链路了，
        所以正常不会走到这里；这条断言是最后一道。
        """
        from src.core.query_engine.opensearch_retrievers import (
            OpenSearchSparseRetriever,
        )

        with pytest.raises(AssertionError, match="越权"):
            OpenSearchSparseRetriever("conv_aaa", store).retrieve(["次年"])
