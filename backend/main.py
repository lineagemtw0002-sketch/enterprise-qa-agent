import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.chat import router as chat_router
from api.documents import router as documents_router
from api.health import router as health_router
from core.config import get_settings
from core.logging import configure_logging
from db.models import Base
from db.seed import seed_demo_data
from db.session import SessionLocal, engine, grant_readonly_access

configure_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Creating tables (if not present)")
    Base.metadata.create_all(bind=engine)
    grant_readonly_access()

    db = SessionLocal()
    try:
        seed_demo_data(db)
    finally:
        db.close()

    yield


app = FastAPI(title="Enterprise QA Agent", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().frontend_origin_list,
    # Local dev frontends commonly land on whatever port is free (Vite auto-picks
    # one when 5173 is taken). Lock this down to explicit origins in production.
    allow_origin_regex=r"http://localhost:\d+",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health_router)
app.include_router(documents_router, prefix="/api")
app.include_router(chat_router, prefix="/api")
