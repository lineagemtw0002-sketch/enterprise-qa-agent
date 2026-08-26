"""全库并行召回只把 query embed 一次（`_execute_local_multi`）。

改这一处之前：粗筛 1 次 + 每个候选库的 DenseRetriever 各 1 次 = 6 库 7 次
Ollama 往返，而 `OLLAMA_NUM_PARALLEL=1`（默认）下这些调用**完全串行**
（CLAUDE.md §4 第 3 条 2026-08-26 实测：并发 1/2/4/6 聚合吞吐恒定 ~21 tok/s），
每次 ~76ms，也就是约 460ms 花在把同一句话反复算成同一个向量上。

**它在旧实现下会失败吗**（CLAUDE.md §7.2）：**前两条会，已实测**——
把三个文件 `git checkout HEAD --` 回退后重跑，
`test_query_is_embedded_once_for_all_collections`（数到 6 次而不是 1 次）与
`test_precomputed_vector_reaches_every_collection`（拿到的全是 None）失败。
第三条 `test_embedding_failure_falls_back_...` **在旧实现下是通过的**
（旧实现本来就不传向量，"全是 None"恒成立）——它是回归保护，不是判别式，
作用是防止将来有人把降级路径改成抛异常。

用假件跑，不连 Ollama/Chroma。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from src.core.types import RetrievalResult

SIX_KBS = [f"kb_{i}" for i in range(6)]


class CountingEmbedding:
    """数 embed 调用次数——这条测试的全部意义就在这个计数上。"""

    def __init__(self) -> None:
        self.calls: List[List[str]] = []

    def embed(self, texts: List[str], trace: Optional[Any] = None) -> List[List[float]]:
        self.calls.append(list(texts))
        return [[0.1, 0.2, 0.3] for _ in texts]


class RecordingHybridSearch:
    """记下调用方有没有把预计算向量透传下来。"""

    def __init__(self, collection: str, seen: Dict[str, Any]) -> None:
        self.collection = collection
        self._seen = seen

    def search(self, query: str, top_k: int = 10, filters=None, trace=None,
               return_details: bool = False, query_vector=None) -> List[RetrievalResult]:
        self._seen[self.collection] = query_vector
        return [RetrievalResult(
            chunk_id=f"{self.collection}_c1", score=0.5,
            text=f"{self.collection} 的内容", metadata={"source_ref": "d1"},
        )]


@pytest.fixture
def tool_and_probes():
    from src.core.settings import load_settings
    from src.mcp_server.tools.query_knowledge_hub import QueryKnowledgeHubTool

    tool = QueryKnowledgeHubTool(settings=load_settings())
    embedding = CountingEmbedding()
    seen: Dict[str, Any] = {}

    tool._ensure_shared_clients = lambda: None  # type: ignore[method-assign]
    tool._embedding_client = embedding
    tool._reranker = None  # 没有 reranker 时 _apply_rerank 直接原样返回，不需要 cross-encoder
    tool._build_hybrid_search_for = (  # type: ignore[method-assign]
        lambda collection, **kw: RecordingHybridSearch(collection, seen)
    )
    return tool, embedding, seen


async def _run(tool):
    from src.core.trace import TraceContext
    return await tool._execute_local_multi("域账号密码多久强制更换一次？", 5, SIX_KBS, TraceContext(trace_type="query"))


@pytest.mark.asyncio
async def test_query_is_embedded_once_for_all_collections(tool_and_probes):
    tool, embedding, _ = tool_and_probes
    await _run(tool)
    assert len(embedding.calls) == 1, (
        f"6 个候选库应该只 embed 一次，实际 {len(embedding.calls)} 次——"
        "每多一次都是一整个 Ollama 串行往返"
    )


@pytest.mark.asyncio
async def test_precomputed_vector_reaches_every_collection(tool_and_probes):
    tool, _, seen = tool_and_probes
    await _run(tool)
    assert set(seen) == set(SIX_KBS), "每个候选库都要被检索到（粗筛不得删库）"
    assert all(v is not None for v in seen.values()), (
        f"这些库没拿到预计算向量，会各自重新 embed: "
        f"{[k for k, v in seen.items() if v is None]}"
    )
    assert len({tuple(v) for v in seen.values()}) == 1, "所有库必须用同一个向量"


@pytest.mark.asyncio
async def test_embedding_failure_falls_back_to_per_retriever_embedding(tool_and_probes):
    """共享 embedding 失败不能让整次检索失败——退回各 retriever 自己算（旧行为）。"""
    tool, embedding, seen = tool_and_probes

    def boom(texts, trace=None):
        raise RuntimeError("ollama down")

    embedding.embed = boom  # type: ignore[method-assign]
    resp = await _run(tool)

    assert set(seen) == set(SIX_KBS), "embedding 失败也不该少查任何一个库"
    assert all(v is None for v in seen.values()), "拿不到向量时必须传 None，让下游自己算"
    assert resp is not None
