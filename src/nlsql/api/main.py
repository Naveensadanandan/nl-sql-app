import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from nlsql.api.routes import ask, auth, sources
from nlsql.core import app_db, background, query_store
from nlsql.core.config import get_settings
from nlsql.retrieval.glossary_search import warmup

log = logging.getLogger(__name__)


def _check_query_role() -> None:
    """Warn when the role behind NLSQL_DATABASE_URL (which runs model SQL) can write.

    CSV sources are still isolated by per-organisation reader roles (SET LOCAL ROLE), but the
    shared demo tables then rely on the validator and the READ ONLY transaction alone.
    """
    import psycopg

    with psycopg.connect(get_settings().psycopg_url, connect_timeout=5) as conn:
        row = conn.execute(
            "SELECT rolsuper FROM pg_roles WHERE rolname = current_user"
        ).fetchone()
    if row and row[0]:
        log.warning(
            "NLSQL_DATABASE_URL uses a SUPERUSER. Use a read-only role in any shared deployment "
            "(docs/ui-and-auth.md, 'Production notes')."
        )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    s = get_settings()
    if not s.results_database_url:
        # The query store WRITES; sharing the read-only query role's URL defeats that role.
        log.warning("NLSQL_RESULTS_DATABASE_URL is unset: query store reuses NLSQL_DATABASE_URL")
    try:
        await asyncio.to_thread(query_store.ensure_store)  # migrate once, before traffic
    except Exception as e:  # noqa: BLE001 - the API still answers; save_result logs failures
        log.warning("query store unavailable at startup: %s", type(e).__name__)
    try:
        await asyncio.to_thread(_check_query_role)
    except Exception as e:  # noqa: BLE001
        log.warning("could not check the query role: %s", type(e).__name__)
    try:
        await asyncio.to_thread(app_db.ensure_tables)  # users, sessions, data_sources
    except Exception as e:  # noqa: BLE001 - log-in fails with 500 until the DB is reachable
        log.warning("app tables unavailable at startup: %s", type(e).__name__)
    background.spawn(warmup(), name="glossary_warmup")  # load fastembed off the first request
    yield
    await background.drain()
    query_store.close_pool()
    app_db.close_pool()


app = FastAPI(title="NL-SQL", lifespan=lifespan)
app.include_router(auth.router)
app.include_router(sources.router)
app.include_router(ask.router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
