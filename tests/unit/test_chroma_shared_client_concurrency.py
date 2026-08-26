"""验证：一个共享的 `chromadb.PersistentClient` 在多线程 + 多 collection 并发
下是否安全——P1-8（检索链路每查询重建全套组件）优化前置验证。

背景：`ChromaStore.__init__` 现在每次查询都无条件 `chromadb.PersistentClient(
path=...)` 现建一个，即使查的是同一个 `persist_directory`。`chroma_store.py`
自己的注释写着"PersistentClient 底层是 SQLite，并发读是安全的（**多个
collection 各自开自己的 client，互不干扰**）"——这句话暗示"各开各的 client"
可能是刻意的并发安全设计，不只是没做优化。要把它改成"一个共享 client，
按需 `get_or_create_collection`"之前，必须先验证清楚共享是否真的安全，
不能凭直觉。

生产环境的检索链路是**真线程**、不是协作式协程：`query_knowledge_hub.py`
用 `asyncio.to_thread(_sync)` 把同步的 Chroma 调用甩给默认线程池执行，
多个用户并发提问会在真实 OS 线程上同时命中 Chroma。所以这里也必须用真线程
（`asyncio.to_thread` / `ThreadPoolExecutor`），不能只用协作式 `asyncio.gather`
在单线程事件循环里假并发——那样测不出真正的线程安全问题
（`CLAUDE.md` §7.2："并发缺陷必须用并发方式验证"）。

**结论（2026-08-26）**：共享 client 在本文件前四个测试类下全部安全；
`TestOldPatternMultipleClientsSamePathWasUnsafe` 那组对照实验则把"旧写法
在并发下可能有问题"从猜测坐实成**稳定复现的真实 bug**（5/5 复现 chromadb
底层竞态）。据此已经把 `src/libs/vector_store/chroma_store.py::ChromaStore`
改成从一个按 `persist_directory` 缓存的共享 client 取实例，不再每次构造都
新建 `PersistentClient`——**这份测试文件现在既是验证依据，也覆盖了这处
生产代码改动本身**（见 `TestChromaStoreFixUsesSharedClient`），不再是
"不改生产代码,只回答要不要做"的纯前置调研。
"""

from __future__ import annotations

import asyncio
import random
import time

import pytest

pytest.importorskip("chromadb")

import chromadb
from chromadb.config import Settings as ChromaSettings


def _make_client(path: str) -> "chromadb.ClientAPI":
    return chromadb.PersistentClient(
        path=path,
        settings=ChromaSettings(anonymized_telemetry=False, allow_reset=True),
    )


def _vector(seed: int, dim: int = 8) -> list[float]:
    rng = random.Random(seed)
    return [rng.random() for _ in range(dim)]


class TestSharedClientAcrossDifferentCollections:
    """核心场景：多个用户同时查不同的知识库（不同 collection），
    共用同一个 PersistentClient——这正是 P1-8 想优化掉的那次重复构造。"""

    @pytest.mark.asyncio
    async def test_concurrent_writes_to_different_collections_do_not_cross_contaminate(
        self, tmp_path
    ):
        client = _make_client(str(tmp_path))
        collection_names = [f"col_{i}" for i in range(6)]  # 对齐 CLAUDE.md "6 库并行" 场景

        def write_and_read(name: str, n: int) -> list[str]:
            col = client.get_or_create_collection(name=name)
            ids = [f"{name}_doc_{j}" for j in range(n)]
            col.upsert(
                ids=ids,
                embeddings=[_vector(hash((name, j))) for j in range(n)],
                metadatas=[{"collection": name} for _ in range(n)],
            )
            # 立刻查回来，检查这个 collection 看到的是不是只有自己写的东西。
            got = col.get(ids=ids)
            return got["ids"]

        results = await asyncio.gather(
            *[asyncio.to_thread(write_and_read, name, 20) for name in collection_names]
        )

        for name, ids in zip(collection_names, results):
            assert set(ids) == {f"{name}_doc_{j}" for j in range(20)}, (
                f"{name} 的写入/读回被别的 collection 污染或丢失: {ids}"
            )

    @pytest.mark.asyncio
    async def test_concurrent_queries_across_collections_return_isolated_results(
        self, tmp_path
    ):
        client = _make_client(str(tmp_path))
        collections = {}
        for i in range(4):
            name = f"kb_{i}"
            col = client.get_or_create_collection(name=name)
            col.upsert(
                ids=[f"{name}_c{j}" for j in range(10)],
                embeddings=[_vector(hash((name, j))) for j in range(10)],
                metadatas=[{"collection": name} for _ in range(10)],
            )
            collections[name] = col

        def query(name: str) -> set[str]:
            col = collections[name]
            out = set()
            # 反复查很多次，加大交错窗口，逼真实的"同一时刻多个请求都在读"。
            for _ in range(30):
                res = col.query(query_embeddings=[_vector(random.randint(0, 999))], n_results=10)
                out.update(res["ids"][0])
            return out

        results = await asyncio.gather(
            *[asyncio.to_thread(query, name) for name in collections]
        )

        for name, ids in zip(collections.keys(), results):
            assert ids, f"{name} 查询结果为空"
            assert all(i.startswith(f"{name}_") for i in ids), (
                f"{name} 的查询结果混进了别的 collection 的 id: {ids}"
            )


class TestSharedClientOnSameCollectionUnderLoad:
    """次要场景：多个用户同时查**同一个**热门知识库——同一个 collection 对象
    本身要不要扛得住高并发读，以及读写交错时是否会崩/损坏数据。"""

    @pytest.mark.asyncio
    async def test_concurrent_reads_on_the_same_collection_do_not_crash(self, tmp_path):
        client = _make_client(str(tmp_path))
        col = client.get_or_create_collection(name="hot_kb")
        col.upsert(
            ids=[f"c{j}" for j in range(50)],
            embeddings=[_vector(j) for j in range(50)],
            metadatas=[{"i": j} for j in range(50)],
        )

        def query_many() -> int:
            count = 0
            for _ in range(20):
                res = col.query(query_embeddings=[_vector(random.randint(0, 999))], n_results=5)
                count += len(res["ids"][0])
            return count

        results = await asyncio.gather(*[asyncio.to_thread(query_many) for _ in range(8)])

        assert all(c > 0 for c in results), f"某个并发查询线程完全没拿到结果: {results}"

    @pytest.mark.asyncio
    async def test_concurrent_read_write_on_same_collection_does_not_corrupt(self, tmp_path):
        """读写交错——一个线程持续写入新文档，另几个线程同时查询，
        验证不崩溃、且最终数据量对得上（没有丢写/重复写）。"""
        client = _make_client(str(tmp_path))
        col = client.get_or_create_collection(name="churning_kb")

        write_count = 40

        def writer() -> None:
            for j in range(write_count):
                col.upsert(
                    ids=[f"w{j}"],
                    embeddings=[_vector(1000 + j)],
                    metadatas=[{"j": j}],
                )
                time.sleep(0)  # 让出，增加交错概率

        def reader() -> int:
            errors = 0
            for _ in range(30):
                try:
                    col.query(query_embeddings=[_vector(random.randint(0, 999))], n_results=5)
                except Exception:
                    errors += 1
            return errors

        results = await asyncio.gather(
            asyncio.to_thread(writer),
            asyncio.to_thread(reader),
            asyncio.to_thread(reader),
        )

        reader_errors = results[1] + results[2]
        assert reader_errors == 0, f"读写交错时查询抛了 {reader_errors} 次异常"
        assert col.count() == write_count, f"写入数量对不上，可能存在丢写/竞态: {col.count()}"


class TestOldPatternMultipleClientsSamePathWasUnsafe:
    """对照组：修复前的实现是"每次查询都新建一个 `PersistentClient`，但都指向
    同一个 `persist_directory`"。chromadb 对"同一路径被多个 `PersistentClient`
    实例并发持有"是有名的已知限制（底层 sqlite 连接/文件锁），这组测试反过来
    验证：那种写法在并发下是不是已经有问题。

    ⚠️ **结论已经从"理论风险"变成"稳定复现的真实 bug"**（2026-08-26）：
    独立跑 5 次，**5 次全部失败**，报错为 chromadb 底层竞态（
    `ValueError: Could not connect to tenant default_tenant`、
    `AttributeError: 'RustBindingsAPI' object has no attribute 'bindings'`、
    `KeyError` 命中 tmp 路径）。这不是"新引入风险"，是发现并顺带修掉一个
    **已经在生产代码里的真实并发 bug**——`src/libs/vector_store/chroma_store.py`
    的 `ChromaStore.__init__` 正是这个模式，而 `query_knowledge_hub.py` 每次
    查询都会重新构造 `ChromaStore`，多用户并发提问时天然触发这条路径。

    这个类本身**保留下来当回归证据**，不修（它就是在模拟"如果退回旧写法会
    怎样"），标 `xfail(strict=False)` 而不是删除或改断言方向——删掉等于抹掉
    "旧写法不安全"这个曾经真实发生过的证据；`strict=False` 是因为竞态本身
    不保证每次都触发，万一某次环境巧合躲过去也不该让测试套件变红。
    真正的回归保护在下面 `TestChromaStoreFixUsesSharedClient`，它测的是
    **修复后的 `ChromaStore` 本身**。"""

    @pytest.mark.xfail(
        strict=False,
        reason="记录已修复的真实 bug：旧写法（并发同路径新建多个 PersistentClient）"
        "在本机 5/5 复现竞态错误，不代表环境保证每次都触发",
    )
    @pytest.mark.asyncio
    async def test_concurrent_new_clients_to_same_path_different_collections(self, tmp_path):
        path = str(tmp_path)

        def new_client_write_and_read(name: str, n: int) -> list[str]:
            # 完全模拟 ChromaStore.__init__ 现在的写法：每次都新建一个
            # PersistentClient，指向同一个 persist_directory。
            client = _make_client(path)
            col = client.get_or_create_collection(name=name)
            ids = [f"{name}_doc_{j}" for j in range(n)]
            col.upsert(
                ids=ids,
                embeddings=[_vector(hash((name, j))) for j in range(n)],
                metadatas=[{"collection": name} for _ in range(n)],
            )
            return col.get(ids=ids)["ids"]

        names = [f"col_{i}" for i in range(6)]
        results = await asyncio.gather(
            *[asyncio.to_thread(new_client_write_and_read, name, 20) for name in names],
            return_exceptions=True,
        )

        exceptions = [r for r in results if isinstance(r, Exception)]
        assert not exceptions, (
            f"现有写法（每次新建 client，同路径并发）在 {len(exceptions)} 个线程里抛了异常: "
            f"{exceptions[:3]}"
        )


class TestChromaStoreFixUsesSharedClient:
    """验证 P1-8 的实际修复：`ChromaStore.__init__` 改成从 `_get_or_create_client`
    缓存里取共享 client 之后，`TestOldPatternMultipleClientsSamePathWasUnsafe`
    复现的竞态不再出现，且构造多个指向同一路径的 `ChromaStore` 实例确实拿到的是
    同一个底层 `chromadb.ClientAPI` 对象（不是"恰好没触发"，是"从根上就没有并发
    构造多个 client"）。"""

    def test_same_persist_directory_shares_one_client_object(self, tmp_path):
        from src.libs.vector_store.chroma_store import ChromaStore

        class _Cfg:
            collection_name = "col_a"
            persist_directory = str(tmp_path)

        class _Settings:
            vector_store = _Cfg()

        store1 = ChromaStore(settings=_Settings())
        store2 = ChromaStore(settings=_Settings())
        assert store1.client is store2.client, (
            "两个指向同一 persist_directory 的 ChromaStore 实例应共享同一个 "
            "PersistentClient 对象，而不是各自新建"
        )

    @pytest.mark.asyncio
    async def test_concurrent_chromastore_construction_same_path_different_collections(
        self, tmp_path
    ):
        """把上面对照组的场景原样复刻一遍，但这次构造的是真实 ChromaStore
        （生产代码路径），而不是直接调 chromadb.PersistentClient。"""
        from src.libs.vector_store.chroma_store import ChromaStore

        path = str(tmp_path)

        def build_store_write_and_read(name: str, n: int) -> list[str]:
            class _Cfg:
                collection_name = name
                persist_directory = path

            class _Settings:
                vector_store = _Cfg()

            store = ChromaStore(settings=_Settings())
            records = [
                {
                    "id": f"{name}_doc_{j}",
                    "vector": _vector(hash((name, j))),
                    "metadata": {"collection": name},
                }
                for j in range(n)
            ]
            store.upsert(records)
            return [r["id"] for r in store.get_by_ids([f"{name}_doc_{j}" for j in range(n)])]

        names = [f"store_col_{i}" for i in range(6)]
        results = await asyncio.gather(
            *[asyncio.to_thread(build_store_write_and_read, name, 20) for name in names],
            return_exceptions=True,
        )

        exceptions = [r for r in results if isinstance(r, Exception)]
        assert not exceptions, (
            f"修复后的 ChromaStore 在 {len(exceptions)} 个线程里仍然抛了异常: "
            f"{exceptions[:3]}"
        )
        for name, ids in zip(names, results):
            assert ids == [f"{name}_doc_{j}" for j in range(20)]
