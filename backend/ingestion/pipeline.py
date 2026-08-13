import logging

from sqlalchemy.orm import Session

from core.config import get_settings
from db.models import Document
from ingestion.chunker import chunk_text
from ingestion.parser import extract_text
from vectorstore.store import add_chunks

logger = logging.getLogger(__name__)


def ingest_document(db: Session, document: Document, content: bytes) -> None:
    settings = get_settings()
    try:
        text = extract_text(content, document.content_type)
        if not text.strip():
            raise ValueError("No extractable text found in document")

        chunks = chunk_text(text, settings.chunk_size, settings.chunk_overlap)
        add_chunks(db, document.id, chunks)

        document.status = "done"
        document.error = None
    except Exception as exc:  # noqa: BLE001 - surfaced to the user via document.error
        logger.exception("Ingestion failed for document %s", document.id)
        document.status = "error"
        document.error = str(exc)
    finally:
        db.add(document)
        db.commit()
