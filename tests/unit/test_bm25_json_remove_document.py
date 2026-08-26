"""`BM25Indexer.remove_document` JSON 侧匹配修复的回归保护——CLAUDE.md §4
第 7 条那条 P0（2026-08-26）。

原始缺陷：JSON 侧用 `chunk_id.startswith(doc_id)` 匹配，但 `doc_id` 是文件
内容 SHA256（64 字符），`chunk_id` 形如 `65046ad1_0000_2a3ac7ab`
（`sha256(源路径)[:8]` 开头，本身就比 doc_id 短）——前缀匹配恒为 False，
`remove_document` 在 JSON 侧是死代码。

修法：`build()`/`add_documents()` 新增持久化字段 `chunk_doc_hash`
（chunk_id -> 所属文档哈希），`remove_document` 改成查这份映射做精确匹配，
不再猜前缀。

⚠️ **已知边界，这组测试不覆盖、也不该覆盖**：这份映射只覆盖"本次修复之后
（新）摄入或重新摄入过"的 chunk，修复前就已经在索引里、从未重新摄入过的
旧 chunk 没有映射，删不掉——这是 `test_bm25_sqlite_store.py::
TestDeletionActuallyWorks::test_json_backend_still_cannot_delete_this`
专门钉住的场景，不在这里重复。**修好 remove_document 本身不等于关闭
"文档更新后旧版本残留"这条 P0**——现在的流水线里没有任何地方会在内容更新
时调用它并传入旧哈希，那是一个独立的、还没做设计评审的问题（CLAUDE.md
§4 第 1 条）。
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from src.ingestion.storage.bm25_indexer import BM25Indexer


def _stat(chunk_id: str, terms: Dict[str, int]) -> Dict[str, Any]:
    return {
        "chunk_id": chunk_id,
        "term_frequencies": dict(terms),
        "doc_length": sum(terms.values()),
    }


@pytest.fixture()
def indexer(tmp_path) -> BM25Indexer:
    return BM25Indexer(index_dir=str(tmp_path / "bm25"))


class TestRemoveDocumentWithChunkDocHashMapping:
    """核心断言：映射存在时，remove_document 必须真的删掉，不能再恒为 False。"""

    def test_removes_all_chunks_belonging_to_the_doc(self, indexer):
        # 混进一个不相关的文档，避免删完之后 self._index 完全清空——
        # `query()` 对全空索引会抛 ValueError（`load()`/`build()` 前的既有
        # 行为，跟本次修复无关），这里只关心"被删文档的 chunk 是否真的没了"。
        indexer.add_documents(
            [_stat("keep_0000_0000", {"考勤": 1})], collection="p0", doc_id="keep" + "0" * 60,
        )
        doc_hash = "a" * 64
        indexer.add_documents(
            [
                _stat("65046ad1_0000_2a3ac7ab", {"年假": 3}),
                _stat("65046ad1_0001_9f1e2c3d", {"年假": 1, "申请": 2}),
            ],
            collection="p0",
            doc_id=doc_hash,
        )

        removed = indexer.remove_document(doc_hash, collection="p0")

        assert removed is True, "映射存在时必须能删掉，这是本次修复的核心断言"
        assert indexer.query(["年假"], top_k=10) == [], "两个 chunk 都该被清空"
        assert indexer.query(["考勤"], top_k=10), "不相关的文档不该被牵连"

    def test_does_not_touch_chunks_from_a_different_document(self, indexer):
        """精确匹配，不是"删得比预期多"——删文档 A 不能影响文档 B 的 chunk，
        即使两者共享同一个词条。"""
        indexer.add_documents(
            [_stat("aaaa_0000_1111", {"年假": 2})], collection="p0", doc_id="a" * 64,
        )
        indexer.add_documents(
            [_stat("bbbb_0000_2222", {"年假": 5})], collection="p0", doc_id="b" * 64,
        )

        removed = indexer.remove_document("a" * 64, collection="p0")

        assert removed is True
        remaining = indexer.query(["年假"], top_k=10)
        assert [r["chunk_id"] for r in remaining] == ["bbbb_0000_2222"]

    def test_unknown_doc_hash_is_a_noop(self, indexer):
        indexer.add_documents(
            [_stat("cccc_0000_3333", {"年假": 1})], collection="p0", doc_id="c" * 64,
        )

        removed = indexer.remove_document("d" * 64, collection="p0")

        assert removed is False
        assert indexer.query(["年假"], top_k=10), "不该删的不能被删"


class TestChunkDocHashPersistsAndPrunes:
    """映射本身要经得住落盘往返、经得住多次 build 叠加，且不能无限增长。"""

    def test_mapping_survives_a_json_round_trip(self, indexer, tmp_path):
        indexer.add_documents(
            [_stat("eeee_0000_4444", {"年假": 1})], collection="rt", doc_id="e" * 64,
        )

        reloaded = BM25Indexer(index_dir=str(tmp_path / "bm25"))
        assert reloaded._load_json_index(collection="rt") is True

        removed = reloaded.remove_document("e" * 64, collection="rt")
        assert removed is True, "映射必须落盘、重新加载后依然能用"

    def test_mapping_survives_unrelated_later_build_calls(self, indexer):
        """加第二份文档不能把第一份文档的映射冲掉——这是 build() 里
        "合并旧映射，不是整体替换"那条逻辑要保证的。"""
        indexer.add_documents(
            [_stat("ffff_0000_5555", {"年假": 1})], collection="merge", doc_id="f" * 64,
        )
        indexer.add_documents(
            [_stat("gggg_0000_6666", {"申请": 1})], collection="merge", doc_id="g" * 64,
        )

        removed_f = indexer.remove_document("f" * 64, collection="merge")
        assert removed_f is True, "先摄入的文档的映射不该被后摄入的文档冲掉"
        removed_g = indexer.remove_document("g" * 64, collection="merge")
        assert removed_g is True

    def test_removed_chunk_is_pruned_from_mapping_not_left_dangling(self, indexer):
        """删除后映射里不能留着已经不存在的 chunk_id——否则映射只增不减，
        规模上来后会变成新的内存/磁盘浪费源。"""
        indexer.add_documents(
            [_stat("hhhh_0000_7777", {"年假": 1})], collection="prune", doc_id="h" * 64,
        )
        indexer.remove_document("h" * 64, collection="prune")

        chunk_doc_hash = indexer._metadata.get("chunk_doc_hash", {})
        assert "hhhh_0000_7777" not in chunk_doc_hash, f"删除后残留: {chunk_doc_hash}"

    def test_overwriting_same_chunk_id_with_new_doc_hash_updates_mapping(self, indexer):
        """同一个 chunk_id 被新内容覆盖（`add_documents` 既有的"新值覆盖旧值"
        合并语义）时，chunk_doc_hash 也要跟着更新到新的 doc_hash，不能留着
        旧的——否则会出现"用旧文档的哈希却能删掉新内容"这种错配。"""
        indexer.add_documents(
            [_stat("shared_chunk", {"年假": 1})], collection="overwrite", doc_id="old" + "0" * 61,
        )
        indexer.add_documents(
            [_stat("shared_chunk", {"年假": 9})], collection="overwrite", doc_id="new" + "0" * 61,
        )

        # 用旧哈希删——不该命中，因为这个 chunk_id 现在归属新哈希了。
        removed_by_old = indexer.remove_document("old" + "0" * 61, collection="overwrite")
        assert removed_by_old is False, "chunk_id 已经被新内容接管，旧哈希不该还能删它"

        removed_by_new = indexer.remove_document("new" + "0" * 61, collection="overwrite")
        assert removed_by_new is True


class TestBuildWithoutDocIdLeavesNoMapping:
    """不传 doc_id 时（`build()` 直接调用，或不需要幂等清理的场景）不应该
    产生任何映射条目——这是 `test_bm25_sqlite_store.py` 里那条"故意断言
    现状是坏的"测试依赖的前提，这里正面测一遍，防止以后被误改成"总是记录"。
    """

    def test_build_without_doc_id_creates_empty_mapping(self, indexer):
        indexer.build([_stat("iiii_0000_8888", {"年假": 1})], collection="nomap")

        assert indexer._metadata.get("chunk_doc_hash", {}) == {}
        assert indexer.remove_document("i" * 64, collection="nomap") is False
