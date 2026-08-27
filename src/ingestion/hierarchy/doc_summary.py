"""文档级摘要——层次化检索索引的"粗粒度"那一层。

这个项目原来的检索是完全平铺的：一个知识库 collection 里所有文档的所有
chunk 混在一起，问一句话就得对全部 chunk 做一次 embedding 相似度计算。
文档数一多（尤其是委托模式企业那种几百个 chunk 的语料库），既浪费算力，
也容易让"哪几个 chunk 最相关"被"哪个文档的 chunk 数最多"带偏——一份文档
切出 30 个 chunk 和另一份切出 3 个 chunk，前者天然有更多机会在候选里刷屏，
跟内容是否真的更相关无关。

这里补的是经典的"先文档、后片段"两级索引里的第一级：每份文档在摄入时
额外生成一条摘要，单独存进 `{collection}__summary` 这个专属 collection
（只存摘要，不跟正文 chunk 混在一起，纯向量检索、不建 BM25——这一层要的是
"哪几份文档大致相关"的粗粒度信号，不需要关键词精确匹配）。检索时
（见 query_knowledge_hub.py `_narrow_by_document_summary`）先在摘要层
找出最相关的几份文档，再只在这几份文档的 chunk 范围内做原来那套
hybrid search + rerank——摘要层找不到东西（比如这个 collection 还没有
任何摘要，老数据没有补跑过）就整体退回原来的全量平铺检索，不会因为这层
是新加的就让老数据查不到东西。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, List, Optional

from src.core.types import Chunk, Document
from src.libs.llm.llm_factory import LLMFactory
from src.libs.llm.base_llm import BaseLLM, Message
from src.core.settings import resolve_path
from src.observability.logger import get_logger

if TYPE_CHECKING:
    from src.core.settings import Settings

logger = get_logger(__name__)

SUMMARY_MAX_CHARS = 600  # 规则兜底摘要的正文截断长度（防止长文档摘要本身就很长，失去"粗粒度"的意义）
_PROMPT_PATH = str(resolve_path("config/prompts/doc_summary.txt"))


def summary_collection_name(collection: str) -> str:
    """摘要层专属 collection 的命名约定——跟正文 collection 用同一个
    persist_directory（本地检索路径都用平台/企业共享的那个本地 Chroma 库），
    单用后缀区分，不需要额外的物理隔离配置。pipeline.py 和
    query_knowledge_hub.py 都从这里导入，保证两边用的是同一个名字。"""
    return f"{collection}__summary"


class DocumentSummarizer:
    """生成文档级摘要：规则兜底 + 可选 LLM 增强，跟 ChunkRefiner/MetadataEnricher
    是同一套"降级不阻断摄入"的设计（LLM 未启用/调用失败都退回规则版本）。"""

    def __init__(self, settings: "Settings", llm: Optional[BaseLLM] = None):
        self.settings = settings
        self._llm = llm
        self._prompt_template: Optional[str] = None
        cfg = getattr(getattr(settings, "ingestion", None), "doc_summary", None) or {}
        self.use_llm: bool = cfg.get("use_llm", False)
        # 这里原来还读了一个 `self.top_docs`——摄入侧从来没有用过它（检索侧的
        # 粗筛预算现在读 `doc_summary.narrow`，见 core/query_engine/narrow_plan.py），
        # 留着只会让人以为"摘要生成也受这个数影响"。2026-08-26 删除。

    @property
    def llm(self) -> Optional[BaseLLM]:
        if self._llm is None and self.use_llm:
            try:
                self._llm = LLMFactory.create(self.settings)
            except Exception:
                logger.warning("DocumentSummarizer: LLM init failed, falling back to rule-based", exc_info=True)
        return self._llm

    def summarize(self, document: Document, chunks: List[Chunk]) -> str:
        """chunks 传入的应该是精炼/去重后的最终 chunk 列表——摘要要反映
        真正会被检索到的内容，不是切分刚出来、可能已经被判定重复丢弃的
        原始版本。"""
        rule_summary = self._rule_based_summary(document, chunks)
        if not self.use_llm or self.llm is None:
            return rule_summary
        try:
            llm_summary = self._llm_summary(chunks)
            return llm_summary or rule_summary
        except Exception:
            logger.warning("DocumentSummarizer: LLM summarization failed, using rule-based fallback", exc_info=True)
            return rule_summary

    @staticmethod
    def _rule_based_summary(document: Document, chunks: List[Chunk]) -> str:
        title = (chunks[0].metadata.get("title") if chunks else None) or document.metadata.get("title") or ""
        body = " ".join(c.text for c in chunks)
        body = body[:SUMMARY_MAX_CHARS]
        return f"{title}。{body}" if title and title != "Untitled" else body

    def _llm_summary(self, chunks: List[Chunk]) -> Optional[str]:
        full_text = "\n\n".join(c.text for c in chunks)
        if not full_text.strip():
            return None
        prompt_template = self._load_prompt()
        if not prompt_template:
            return None
        prompt = prompt_template.replace("{text}", full_text)
        response = self.llm.chat([Message(role="user", content=prompt)])
        content = response if isinstance(response, str) else response.content
        return content.strip() if content and content.strip() else None

    def _load_prompt(self) -> Optional[str]:
        if self._prompt_template is not None:
            return self._prompt_template
        from pathlib import Path

        path = Path(_PROMPT_PATH)
        if not path.exists():
            logger.warning(f"doc_summary prompt not found: {_PROMPT_PATH}")
            return None
        self._prompt_template = path.read_text(encoding="utf-8")
        return self._prompt_template
