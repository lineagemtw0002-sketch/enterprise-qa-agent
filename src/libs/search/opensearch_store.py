"""OpenSearch 检索存储层 —— `docs/opensearch_migration_design.md` 阶段 1。

**当前阶段：影子写。读仍然全部走现有的 BM25 + Chroma 链路。**
本模块被写入，但还没有任何生产查询路径读它。这是刻意的：先让两边数据并存，
好在切读之前逐库比对命中集合（设计文档 §7 阶段 2）。

## 为什么换

三个已实测的缺陷，根因都是"用 Python 手写检索引擎"这个选型，不是实现问题：

- **全量加载**：143K 块单库单次查询 5926ms（load 5365 + query 560），
  6 库串行 35.6s，而 TTFT SLO 是 3s。
- **GIL convoy**：`sqlite3` 每返回一行都释放/重取 GIL，10K 块 hot 查询
  6 线程 2633ms vs 1 线程 25ms（103.4x），比 JSON 还慢。
  OpenSearch 同规模同查询是 109ms（4.8x）—— 扫描不在 Python 进程里，
  这个问题在结构上不可能出现。
- **按文档删除恒失败**：`chunk_id.startswith(doc_id)` 永远不成立
  （22 字符的串不可能以 64 字符的串开头），三轮独立测试均返回 False。

## 两个设计要点，都是"让问题不成立"而不是"写代码修"

### 1. 确定性 `_id` 解决旧版本残留（P0#1）

现状 `doc_id = 文件内容 SHA256`，**内容一变就被当成新文档**，旧版本片段永久
残留在库里 —— 这个设计本身制造了那条 P0。

这里改成 `_id = sha256(source_path + ":" + chunk_index)`：**跟内容无关**，
所以同一份文件重新摄入时，相同位置的 chunk 直接覆盖（`index` 天然是 upsert）。
文档变短导致多出来的旧 chunk，由 `delete_stale_chunks` 按位置清掉。

实测：同 `_id` 重写后 count 不变、`_version` 递增、内容已替换。

### 2. `owner_user_id` 是必传参数，不是可选

现状 `acl.py::is_collection_allowed` 对任何 `conv_` 前缀**无条件返回 True**，
安全性建立在"只有对话使用者知道 conversation_id"上。核查过：**当前没有漏洞**
（collection 名由服务端从已校验的 conversation_id 拼出，`_require_conversation_owner`
在 11 个端点上都调了）。

但那是**约定式防护** —— ACL 层自己没有保护，依赖 11 个调用点各自记得先校验。
迁移后对话私有库从"一对话一物理存储"变成"一企业一 index + 字段过滤"，
隔离强度下降一档，约定式防护就不够了。

所以 `search_conv()` 把 `owner_user_id` 设计成**位置参数**：漏传直接 TypeError，
不可能编译通过还漏掉。这是把"靠人记得"换成"漏了就跑不起来"。
"""

from __future__ import annotations

import hashlib
import logging
import os
from typing import Any, Dict, Iterable, List, Optional, Sequence

logger = logging.getLogger(__name__)

# 业务知识库：一库一 index，保留 CLAUDE.md §3.3 的物理隔离。
#
# 刻意**不**在名字里拼 org：collection 名在 `org_collections` 表里已全局唯一，
# 拼上 org 会在"库改归属"时被迫改 index 名（等于重建整个索引）。
# 企业边界由 `_org_owned_collections()` 在检索前收敛，那一层不变。
_KB_PREFIX = "kb_"

# 对话私有库：**每企业一个 index**，内部按 conversation_id 路由。
#
# 不能一对话一 index —— 它随使用无界增长，而每个只装一两份上传文档。
# 这是业界公认的 index explosion 反模式：每个 index 的元数据由 master 维护、
# 每个 shard 吃堆内存，而推荐 shard 大小是 10–50GB，本项目的对话库比这小
# 六个数量级。
#
# 也不能全局共享一个 —— 那会把不同企业的对话文档放进同一个 index，
# 降级 §3.3 那条已验证的跨企业物理隔离。
_CONV_PREFIX = "conv_"

_DEFAULT_URL = "http://localhost:9200"

# 分词器实例复用：jieba 首次加载词典要几百毫秒，不该每次查询都付。
# ⚠️ 索引侧与查询侧是**两个不同的分词器**，见 tokenize_for_query 的说明。
_INDEX_TOKENIZER: Any = None
_QUERY_TOKENIZER: Any = None


def kb_index_name(collection: str) -> str:
    return f"{_KB_PREFIX}{collection}"


def conv_index_name(org_id: str) -> str:
    return f"{_CONV_PREFIX}{org_id}"


def chunk_doc_id(source_path: str, chunk_index: int) -> str:
    """确定性 `_id` —— **必须与内容无关**，否则 P0#1 的解法不成立。

    改这个函数等于让所有既有索引的 `_id` 失效（重摄入会产生重复而不是覆盖），
    属于破坏性变更，要走迁移。
    """
    return hashlib.sha256(f"{source_path}:{chunk_index}".encode()).hexdigest()[:32]


def tokenize_for_index(text: str) -> List[str]:
    """**索引侧**分词 —— 复用 `SparseEncoder` 的 jieba 实现。

    不要在这里另写一份：迁移的判据是"检索结果与现状一致"，而现状的索引
    就是 `SparseEncoder._tokenize` 建的。换个分词器会让比对失去意义。
    """
    from src.ingestion.embedding.sparse_encoder import SparseEncoder

    global _INDEX_TOKENIZER
    if _INDEX_TOKENIZER is None:
        _INDEX_TOKENIZER = SparseEncoder()
    return _INDEX_TOKENIZER._tokenize(text)


def tokenize_for_query(text: str) -> List[str]:
    """**查询侧**分词 —— 复用 `QueryProcessor`。

    ⚠️ **索引侧和查询侧用的是两个不同的东西，而且产出确实不一致。**
    这是现有系统的既有状态，不是本次引入的。对同一批 22 条真实 chunk 实测：

        两侧都产出      132 词 (69%)
        只有索引侧       41 词 (21%)   hr / it / 主管 / 丢失 / 为期 …
        只有查询侧       29 词 (15%)   3 / 5 / b / 不 / 为 / 假 …

    根因是两者定位不同：`SparseEncoder` 是分词器（过滤单字），
    `QueryProcessor` 是关键词抽取器（保留单字、丢弃部分实词）。
    而 `sparse_encoder.py::_tokenize` 的注释声称"必须与查询侧一致"——
    **那个声称与实现不符**，属注释里的未证实断言（`CLAUDE.md` §7.2）。

    **本次刻意不修。** 迁移要回答的是"换存储引擎会不会出问题"，
    修分词一致性会同时改变检索结果，两个变量一起动就无法归因。
    这里如实复刻现状：索引走 SparseEncoder，查询走 QueryProcessor，
    连同这个缺陷一起搬过去，切读时的比对才有意义。
    修它是独立课题，判据应该是黄金测试集通过率。
    """
    from src.core.query_engine.query_processor import QueryProcessor

    global _QUERY_TOKENIZER
    if _QUERY_TOKENIZER is None:
        _QUERY_TOKENIZER = QueryProcessor()
    result = _QUERY_TOKENIZER.process(text)
    return list(getattr(result, "keywords", result))


def _as_query_text(query: str, tokens: Optional[Sequence[str]]) -> str:
    return " ".join(tokens if tokens is not None else tokenize_for_query(query))


def build_chunk_doc(
    *,
    text: str,
    tokens: Sequence[str],
    source_path: str,
    chunk_index: int,
    chunk_id: str,
    doc_hash: Optional[str] = None,
    conversation_id: Optional[str] = None,
    owner_user_id: Optional[str] = None,
    embedding: Optional[Sequence[float]] = None,
) -> Dict[str, Any]:
    """把一个 chunk 组装成 OpenSearch 文档。

    `tokens` 必须来自 `tokenize_for_index()`（即 `SparseEncoder._tokenize`）—— **不要在这里自己分词**，
    与摄入侧用不同的分词器会让 BM25 匹配不上，而且这种错误在小语料上测不出来。
    """
    doc: Dict[str, Any] = {
        "content": " ".join(tokens),
        "content_raw": text,
        "source_path": source_path,
        "chunk_index": int(chunk_index),
        "chunk_id": chunk_id,
    }
    if doc_hash:
        doc["doc_hash"] = doc_hash
    if conversation_id:
        doc["conversation_id"] = conversation_id
    if owner_user_id:
        doc["owner_user_id"] = owner_user_id
    if embedding is not None:
        doc["embedding"] = list(embedding)
    return doc


def _mapping(dense_dims: Optional[int]) -> Dict[str, Any]:
    props: Dict[str, Any] = {
        # ⚠️ `whitespace` 分析器 + 索引侧写入**项目自己 jieba 分好的文本**。
        #
        # 不用 OpenSearch 内置分析器是刻意的。实测同一句话三种切法：
        #   standard（默认）    年 假 可 以 顺 延 到 次 年 三 月   ← 逐字切
        #   cjk（内置）         年假 假可 可以 以顺 顺延 …          ← 二元组
        #   项目 SparseEncoder  年 假 顺延到 次年 三月              ← jieba 真分词
        # 默认分析器会让「关键词4」匹配上「关键词0」（共享 关/键/词）。
        #
        # 更重要的是**隔离变量**：这次迁移要回答"换存储引擎会不会出问题"，
        # 不是"换分词器好不好"。tokenization 保持完全不变，任何检索质量变化
        # 都能归因到存储层。analysis-ik 作为迁移后的独立课题，判据是黄金测试集。
        #
        # 这意味着写入方必须先分词（见 `build_chunk_doc`），且
        # `SparseEncoder._tokenize` / `QueryProcessor` 都不能删。
        "content": {"type": "text", "analyzer": "whitespace"},
        # 原文只存不索引 —— 检索走 content，展示走这里。
        "content_raw": {"type": "text", "index": False},
        "source_path": {"type": "keyword"},
        "chunk_index": {"type": "integer"},
        "doc_hash": {"type": "keyword"},
        "chunk_id": {"type": "keyword"},      # 与 Chroma 向量 id 对齐，迁移期需要
        "conversation_id": {"type": "keyword"},
        "owner_user_id": {"type": "keyword"},
        "ingested_at": {"type": "date"},
    }
    if dense_dims:
        props["embedding"] = {
            "type": "knn_vector",
            "dimension": dense_dims,
        }
    return {"properties": props}


class OpenSearchStore:
    """薄封装。**刻意不做成 `BaseVectorStore` 的实现类。**

    设计文档 §1 的决策是"按 OpenSearch 惯用法设计，不做适配层把它包装成现有
    抽象的替身"。硬塞进 `BaseVectorStore` 会同时失去它的原生能力
    （`delete_by_query`、routing、ILM）和现有抽象的意义，还会把老设计的
    缺陷一起带过去。
    """

    def __init__(
        self,
        url: Optional[str] = None,
        dense_dims: Optional[int] = None,
        k1: float = 1.5,
        b: float = 0.75,
    ):
        from opensearchpy import OpenSearch

        self.url = url or os.getenv("RAGENT_OPENSEARCH_URL", _DEFAULT_URL)
        self.dense_dims = dense_dims
        # 默认值与 BM25Indexer 一致（k1=1.5, b=0.75），不是 Lucene 的默认。
        self.k1 = k1
        self.b = b
        self._client = OpenSearch(
            hosts=[self.url],
            http_compress=True,
            # 影子写阶段不该让 OpenSearch 的抖动拖慢摄入
            timeout=10,
            max_retries=2,
            retry_on_timeout=True,
        )

    # ────────────────────────── index 管理 ──────────────────────────

    def ensure_index(self, index: str) -> bool:
        """幂等建 index。已存在则不动（**不覆盖 mapping**——那会静默丢字段）。"""
        if self._client.indices.exists(index=index):
            return False
        self._client.indices.create(
            index=index,
            body={
                "settings": {
                    "index": {
                        "number_of_shards": 1,
                        "number_of_replicas": 0,
                        # knn_vector 字段要求打开这个开关，即使暂时不查
                        "knn": bool(self.dense_dims),
                        # ⚠️ 对齐项目现有 BM25 的参数。**Lucene 默认 k1=1.2，
                        # 而 BM25Indexer 用的是 k1=1.5** —— 不对齐的话，
                        # 迁移比对会看到排序差异，却分不清是"换引擎导致的"
                        # 还是"参数没对齐导致的"。可配置的东西必须先对齐，
                        # 剩下的差异才归因得到引擎本身。
                        "similarity": {
                            "default": {
                                "type": "BM25",
                                "k1": self.k1,
                                "b": self.b,
                            }
                        },
                    }
                },
                "mappings": _mapping(self.dense_dims),
            },
        )
        logger.info(
            "OpenSearch index 已创建 index=%s", index,
            extra={"event": "opensearch.index.created", "index": index},
        )
        return True

    # ────────────────────────── 写入 ──────────────────────────

    def index_chunks(
        self,
        index: str,
        chunks: Sequence[Dict[str, Any]],
        *,
        refresh: bool = False,
    ) -> int:
        """批量写入。每条 chunk 需要 `source_path` / `chunk_index` / `content`。

        `_id` 由 `chunk_doc_id()` 算出，所以**重复调用是幂等的**：同一位置的
        chunk 覆盖而不是新增。这正是 P0#1 的解法。
        """
        if not chunks:
            return 0
        self.ensure_index(index)

        body: List[Dict[str, Any]] = []
        for c in chunks:
            doc_id = chunk_doc_id(c["source_path"], c["chunk_index"])
            body.append({"index": {"_index": index, "_id": doc_id}})
            body.append(c)

        resp = self._client.bulk(body=body, refresh=refresh)
        if resp.get("errors"):
            failed = [
                it["index"] for it in resp["items"]
                if it.get("index", {}).get("error")
            ]
            raise RuntimeError(
                f"OpenSearch bulk 写入部分失败：{len(failed)}/{len(chunks)} 条，"
                f"首例 {failed[0].get('error') if failed else '?'}"
            )
        return len(chunks)

    def delete_by_source(self, index: str, source_path: str, *, refresh: bool = False) -> int:
        """按源文档删除它的全部 chunk。

        这是 `BM25Indexer.remove_document` 那条 P0 的正解 —— 不再拿 chunk_id
        去和 doc_id 做字符串前缀匹配，而是按显式记录的 `source_path` 删。
        """
        if not self._client.indices.exists(index=index):
            return 0
        resp = self._client.delete_by_query(
            index=index,
            body={"query": {"term": {"source_path": source_path}}},
            refresh=refresh,
        )
        return int(resp.get("deleted", 0))

    def delete_stale_chunks(
        self, index: str, source_path: str, keep_count: int, *, refresh: bool = False
    ) -> int:
        """删掉同一文档中 `chunk_index >= keep_count` 的残留。

        场景：文档被编辑后**变短了**。前 N 个 chunk 会被确定性 `_id` 覆盖，
        但原来更长的那部分不会被任何新 chunk 覆盖，必须显式删除。
        **少了这一步，P0#1 只解决了一半。**
        """
        if not self._client.indices.exists(index=index):
            return 0
        resp = self._client.delete_by_query(
            index=index,
            body={
                "query": {
                    "bool": {
                        "must": [
                            {"term": {"source_path": source_path}},
                            {"range": {"chunk_index": {"gte": keep_count}}},
                        ]
                    }
                }
            },
            refresh=refresh,
        )
        return int(resp.get("deleted", 0))

    def delete_by_conversation(
        self, index: str, conversation_id: str, *, refresh: bool = False
    ) -> int:
        """对话被删除时清掉它的全部私有文档。

        这是 `conv_*` 数据的**主回收机制**，不是 ILM。按年龄自动删对这类数据
        是错的：对话上传的文档有明确归属对象，用户的心智是"这份文档属于这个
        对话"。按年龄删会导致"对话还在、文件名还看得见、问它却答不出来"——
        静默失效，最难排查的那种。ILM 只做孤儿数据的兜底。
        """
        if not self._client.indices.exists(index=index):
            return 0
        resp = self._client.delete_by_query(
            index=index,
            body={"query": {"term": {"conversation_id": conversation_id}}},
            refresh=refresh,
        )
        return int(resp.get("deleted", 0))

    # ────────────────────────── 读取（阶段 1 仅供比对与测试）──────────────────────────

    def search_kb(
        self,
        collection: str,
        query: str,
        top_k: int = 10,
        *,
        query_tokens: Optional[Sequence[str]] = None,
    ) -> List[Dict[str, Any]]:
        """业务知识库检索。企业边界由调用方的 `_org_owned_collections()` 收敛。

        ⚠️ `content` 用的是 `whitespace` 分析器，所以**查询串必须先用同一个
        jieba 分词器切好**，否则整句会被当成一个 token，什么都匹配不上。
        生产调用方应传 `query_tokens`（来自 `QueryProcessor`）；
        不传时这里兜底调 `SparseEncoder._tokenize`，行为一致但多一次分词。
        """
        index = kb_index_name(collection)
        if not self._client.indices.exists(index=index):
            return []
        resp = self._client.search(
            index=index,
            body={
                "size": top_k,
                "query": {"match": {"content": _as_query_text(query, query_tokens)}},
            },
        )
        return self._hits(resp)

    def search_conv(
        self,
        org_id: str,
        conversation_id: str,
        owner_user_id: str,
        query: str,
        top_k: int = 10,
        *,
        query_tokens: Optional[Sequence[str]] = None,
    ) -> List[Dict[str, Any]]:
        """对话私有库检索。

        ⚠️ **`owner_user_id` 是位置参数，不是可选。** 漏传直接 TypeError。

        这不是风格洁癖：迁移后对话私有库从"一对话一物理存储"变成"一企业一
        index + 字段过滤"，隔离强度下降一档（跨企业不变，企业内从物理降为逻辑）。
        物理隔离时漏了过滤条件顶多查不到数据；字段过滤时漏了就是**越权返回**。
        必传参数把这个错误从"运行时才发现"提前到"根本调不通"。

        值必须来自 JWT（`current_user.user_id`），**不要从请求体取**。
        """
        index = conv_index_name(org_id)
        if not self._client.indices.exists(index=index):
            return []
        resp = self._client.search(
            index=index,
            routing=conversation_id,
            body={
                "size": top_k,
                "query": {
                    "bool": {
                        "must": [{"match": {"content": _as_query_text(query, query_tokens)}}],
                        "filter": [
                            {"term": {"conversation_id": conversation_id}},
                            {"term": {"owner_user_id": owner_user_id}},
                        ],
                    }
                },
            },
        )
        return self._hits(resp)

    @staticmethod
    def _hits(resp: Dict[str, Any]) -> List[Dict[str, Any]]:
        return [
            {
                "chunk_id": h["_source"].get("chunk_id", h["_id"]),
                "score": h["_score"],
                # 返回原文而不是分词后的 content —— 后者是给检索用的，不能给人看
                "content": h["_source"].get("content_raw", h["_source"].get("content", "")),
                "source_path": h["_source"].get("source_path", ""),
            }
            for h in resp["hits"]["hits"]
        ]

    # ────────────────────────── 自检 ──────────────────────────

    def count(self, index: str) -> int:
        if not self._client.indices.exists(index=index):
            return 0
        self._client.indices.refresh(index=index)
        return int(self._client.count(index=index)["count"])

    def ping(self) -> bool:
        try:
            return bool(self._client.ping())
        except Exception:
            return False

    def drop_index(self, index: str) -> bool:
        """仅供测试与迁移回滚。生产路径不该调这个。"""
        if not self._client.indices.exists(index=index):
            return False
        self._client.indices.delete(index=index)
        return True


# ────────────────────────── 摄入侧影子写 ──────────────────────────


def mirror_ingestion_to_opensearch(
    *,
    collection: str,
    chunks: Sequence[Any],
    sparse_stats: Sequence[Dict[str, Any]],
    document: Any,
) -> None:
    """把本次摄入的 chunk 镜像进 OpenSearch。**失败不阻断摄入。**

    **刻意做成模块级函数而不是 `Pipeline` 的方法。** 它只需要 `collection`
    一个字段，不需要 pipeline 状态；而挂成方法会让
    `tests/unit/test_pipeline_progress.py` 里那个鸭子类型的假对象每次新增方法
    都要跟着补 —— 那条测试就是这么红的。参数显式传入也让这段逻辑本身可测。

    阶段 2 里 OpenSearch 没有任何生产读者，一次镜像失败不该让整篇文档摄入失败
    —— 那是拿"还没人用的新后端"去阻断"正在用的老链路"。
    但**必须以 ERROR 记下来**：两边静默分歧正是让切读变危险的东西，
    切读之前要能从日志里查到哪个 collection 什么时候掉过队。
    （这个取舍和 SQLite 那一轮一致，当时的经验是：fail-soft 没问题，静默才是问题。）
    """
    if os.getenv("RAGENT_OPENSEARCH_DUAL_WRITE", "true").strip().lower() == "false":
        return
    if not chunks:
        return
    try:
        store = OpenSearchStore()
        index = kb_index_name(collection)
        docs = [
            build_chunk_doc(
                text=chunk.text,
                tokens=tokenize_for_index(chunk.text),
                source_path=chunk.metadata["source_path"],
                chunk_index=chunk.metadata.get("chunk_index", i),
                chunk_id=stat["chunk_id"],
                doc_hash=getattr(document, "id", None),
            )
            for i, (chunk, stat) in enumerate(zip(chunks, sparse_stats))
        ]
        store.index_chunks(index, docs, refresh=False)

        # ⚠️ 这一步不能省：文档被编辑后**变短**时，前 N 个 chunk 会被确定性
        # _id 覆盖，但原来更长的那部分不会被任何新 chunk 覆盖。
        # 少了它，P0#1 只解决一半。
        stale = store.delete_stale_chunks(
            index, chunks[0].metadata["source_path"], keep_count=len(chunks)
        )
        logger.info(
            "      OpenSearch 影子写 %d 条%s",
            len(docs),
            f"，清理旧残留 {stale} 条" if stale else "",
        )
    except Exception as exc:  # noqa: BLE001 —— 见 docstring，刻意不向上抛
        logger.error(
            "OpenSearch 影子写失败，该 collection 的副本已与主存储分歧；"
            "切读前必须重建。collection=%s err=%s",
            collection,
            exc,
            extra={
                "event": "opensearch.dual_write_failed",
                "collection": collection,
            },
        )
