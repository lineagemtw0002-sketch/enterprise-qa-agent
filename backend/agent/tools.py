import logging

from langchain_core.tools import tool
from sqlalchemy import text as sql_text

from core.config import get_settings
from db.session import SessionLocal, readonly_engine
from llm.provider import get_chat_model
from vectorstore.store import similarity_search

logger = logging.getLogger(__name__)

# Keep in sync with db/session.py:SQL_TOOL_WHITELISTED_TABLES — that's what actually
# enforces the boundary (via GRANT). This description is only a hint to the model.
_SCHEMA_DESCRIPTION = """\
customers(id, name, email, segment, created_at)
orders(id, customer_id -> customers.id, product, amount, status, created_at)
"""

_FORBIDDEN_KEYWORDS = (
    "insert", "update", "delete", "drop", "alter", "grant", "revoke",
    "create", "truncate", "attach", "copy", "--", "/*",
)


def _is_safe_select(sql: str) -> bool:
    lowered = sql.strip().lower()
    if not lowered.startswith("select"):
        return False
    if ";" in lowered.rstrip(";"):
        return False
    return not any(word in lowered for word in _FORBIDDEN_KEYWORDS)


@tool
def retrieve_documents(query: str) -> str:
    """Search the enterprise document knowledge base for passages relevant to the query.
    Returns text snippets together with their source filename for citation."""
    db = SessionLocal()
    try:
        results = similarity_search(db, query)
    finally:
        db.close()

    if not results:
        return "No relevant documents found in the knowledge base."

    return "\n\n---\n\n".join(f"[Source: {r.filename}]\n{r.content}" for r in results)


@tool
def query_structured_data(question: str) -> str:
    """Answer a question against the structured business database (customers, orders)
    by generating and executing a read-only SQL query. Use this for questions about
    specific customers, orders, revenue, counts, or other structured business facts."""
    llm = get_chat_model(streaming=False)
    prompt = (
        "You generate PostgreSQL SELECT statements for a read-only role.\n"
        f"Schema:\n{_SCHEMA_DESCRIPTION}\n"
        "Rules: single SELECT statement only, no comments, no semicolons, no CTEs that write.\n"
        f"Question: {question}\n"
        "Return only the SQL, nothing else."
    )
    raw = llm.invoke(prompt).content.strip()
    sql = raw.strip("`")
    if sql.lower().startswith("sql"):
        sql = sql[3:].strip()

    if not _is_safe_select(sql):
        logger.warning("Refused unsafe SQL from text-to-SQL tool: %s", sql)
        return f"Refused to execute non-SELECT or unsafe query: {sql}"

    limit = get_settings().sql_row_limit
    if "limit" not in sql.lower():
        sql = f"{sql} LIMIT {limit}"

    try:
        with readonly_engine.connect() as conn:
            rows = [dict(r) for r in conn.execute(sql_text(sql)).mappings().all()]
    except Exception as exc:  # noqa: BLE001 - surfaced back to the agent as a tool result
        return f"SQL execution error: {exc}\nSQL used: {sql}"

    if not rows:
        return f"Query returned no rows.\nSQL used: {sql}"

    return f"SQL used: {sql}\nResults ({len(rows)} row(s)):\n" + "\n".join(str(r) for r in rows)
