from collections.abc import Generator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from core.config import get_settings

settings = get_settings()

engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

readonly_engine = create_engine(settings.database_url_readonly, pool_pre_ping=True)

# Tables the agent's text-to-SQL tool is allowed to query. Keep this in sync with
# agent/tools.py — granting SELECT here is what actually enforces the boundary,
# the prompt-level whitelist is only a hint to the model.
SQL_TOOL_WHITELISTED_TABLES = ["customers", "orders"]


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def grant_readonly_access() -> None:
    tables = ", ".join(SQL_TOOL_WHITELISTED_TABLES)
    with engine.begin() as conn:
        conn.execute(text(f"GRANT SELECT ON {tables} TO eqa_readonly"))
