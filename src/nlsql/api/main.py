import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from nlsql.api.routes import ask
from nlsql.core import background, query_store
from nlsql.core.config import get_settings
from nlsql.retrieval.glossary_search import warmup

log = logging.getLogger(__name__)


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
    background.spawn(warmup(), name="glossary_warmup")  # load fastembed off the first request
    yield
    await background.drain()
    query_store.close_pool()


app = FastAPI(title="NL-SQL", lifespan=lifespan)
app.include_router(ask.router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
