from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.config import get_settings
from db.models import Chunk, Document
from vectorstore.embedder import embed_query, embed_texts


def add_chunks(db: Session, document_id: str, texts: list[str]) -> None:
    embeddings = embed_texts(texts)
    for i, (text, embedding) in enumerate(zip(texts, embeddings)):
        db.add(Chunk(document_id=document_id, content=text, chunk_index=i, embedding=embedding))
    db.commit()


@dataclass
class RetrievedChunk:
    document_id: str
    filename: str
    content: str
    distance: float


def similarity_search(db: Session, query: str, top_k: int | None = None) -> list[RetrievedChunk]:
    top_k = top_k or get_settings().retrieval_top_k
    query_embedding = embed_query(query)

    distance = Chunk.embedding.cosine_distance(query_embedding).label("distance")
    stmt = (
        select(Chunk, Document.filename, distance)
        .join(Document, Chunk.document_id == Document.id)
        .where(Document.status == "done")
        .order_by(distance)
        .limit(top_k)
    )

    return [
        RetrievedChunk(document_id=chunk.document_id, filename=filename, content=chunk.content, distance=dist)
        for chunk, filename, dist in db.execute(stmt).all()
    ]
