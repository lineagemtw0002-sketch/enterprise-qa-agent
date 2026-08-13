from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import Document
from db.session import SessionLocal, get_db
from ingestion.parser import SUPPORTED_CONTENT_TYPES
from ingestion.pipeline import ingest_document

router = APIRouter(tags=["documents"])


def _run_ingestion(document_id: str, content: bytes) -> None:
    db = SessionLocal()
    try:
        document = db.get(Document, document_id)
        if document:
            ingest_document(db, document, content)
    finally:
        db.close()


@router.post("/documents")
async def upload_document(
    background_tasks: BackgroundTasks,
    file: UploadFile,
    db: Session = Depends(get_db),
):
    if file.content_type not in SUPPORTED_CONTENT_TYPES:
        raise HTTPException(400, f"Unsupported content type: {file.content_type}")

    content = await file.read()
    document = Document(filename=file.filename, content_type=file.content_type, status="pending")
    db.add(document)
    db.commit()
    db.refresh(document)

    background_tasks.add_task(_run_ingestion, document.id, content)

    return {"id": document.id, "filename": document.filename, "status": document.status}


@router.get("/documents")
def list_documents(db: Session = Depends(get_db)):
    documents = db.execute(select(Document).order_by(Document.created_at.desc())).scalars().all()
    return [
        {
            "id": d.id,
            "filename": d.filename,
            "status": d.status,
            "error": d.error,
            "created_at": d.created_at.isoformat(),
        }
        for d in documents
    ]
