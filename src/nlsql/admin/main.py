"""Admin server (port 8001): load glossary CSVs into Qdrant, category values into Elasticsearch.

    uvicorn nlsql.admin.main:app --port 8001

Bind to localhost / a private network only. Every /admin call needs `X-Admin-Key`
(NLSQL_ADMIN_API_KEY). The page at `/` is static and holds no secrets.
"""

import hmac
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, TypeVar

import anyio
from elasticsearch import AsyncElasticsearch
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from qdrant_client import AsyncQdrantClient

from nlsql.core.config import get_settings
from nlsql.core.tenant import normalize_organisation_id
from nlsql.worker import indexing

T = TypeVar("T")
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    s = get_settings()
    app.state.qdrant = AsyncQdrantClient(url=s.qdrant_url) if s.qdrant_url else None
    app.state.es = AsyncElasticsearch(s.es_url) if s.es_url else None
    yield
    if app.state.qdrant:
        await app.state.qdrant.close()
    if app.state.es:
        await app.state.es.close()


app = FastAPI(title="NL-SQL Admin", lifespan=lifespan)


def require_key(x_admin_key: Annotated[str | None, Header()] = None) -> None:
    key = get_settings().admin_api_key
    if not key or not x_admin_key or not hmac.compare_digest(x_admin_key, key):
        raise HTTPException(401, "invalid or missing X-Admin-Key")


def _org(organisation_id: str) -> str:
    try:
        return normalize_organisation_id(organisation_id)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e


def _client(client: T | None, name: str) -> T:
    if client is None:
        raise HTTPException(503, f"{name} is not configured")
    return client


@app.get("/", response_class=HTMLResponse)
async def page() -> str:
    return (Path(__file__).parent / "page.html").read_text(encoding="utf-8")


@app.get("/admin/health", dependencies=[Depends(require_key)])
async def health(request: Request) -> dict[str, str]:
    out: dict[str, str] = {}
    for name, client in (
        ("qdrant", request.app.state.qdrant),
        ("elasticsearch", request.app.state.es),
    ):
        if client is None:
            out[name] = "not configured"
            continue
        try:
            await (client.get_collections() if name == "qdrant" else client.info())
            out[name] = "ok"
        except Exception:  # noqa: BLE001 - report, never raise, on a health probe
            out[name] = "unreachable"
    return out


@app.post("/admin/glossary/upload", dependencies=[Depends(require_key)])
async def upload_glossary(
    request: Request,
    organisation_id: Annotated[str, Form()],
    file: Annotated[UploadFile, File()],
) -> dict[str, int]:
    """CSV columns: name, description, section (+ optional synonyms 'a|b', status, user_id)."""
    org = _org(organisation_id)
    qdrant = _client(request.app.state.qdrant, "qdrant")
    limit = get_settings().admin_max_upload_mb * 1024 * 1024
    raw = await file.read(limit + 1)
    if len(raw) > limit:
        raise HTTPException(413, "file too large")
    try:
        rows = indexing.parse_glossary_csv(raw)
    except indexing.CsvError as e:
        raise HTTPException(422, str(e)) from e
    return {"indexed": await indexing.index_glossary(qdrant, org, rows)}


class ValuesRequest(BaseModel):
    organisation_id: str
    schema_name: str = Field(description="Postgres schema to read, e.g. ws_acme")
    tables: list[str] | None = Field(default=None, description="Default: every table")


@app.get("/admin/values/tables", dependencies=[Depends(require_key)])
async def list_tables(schema_name: str) -> list[str]:
    if not _IDENT.match(schema_name):
        raise HTTPException(422, "invalid schema name")
    docs = await anyio.to_thread.run_sync(indexing.list_tables, schema_name)
    return docs


@app.post("/admin/values/index", dependencies=[Depends(require_key)])
async def index_values(req: ValuesRequest, request: Request) -> dict[str, object]:
    """Index distinct values of text (category) columns so questions can be resolved to them."""
    org = _org(req.organisation_id)
    es = _client(request.app.state.es, "elasticsearch")
    if not _IDENT.match(req.schema_name) or any(not _IDENT.match(t) for t in req.tables or []):
        raise HTTPException(422, "invalid schema or table name")
    docs, skipped = await anyio.to_thread.run_sync(
        indexing.collect_values, req.schema_name, req.tables, get_settings().value_max_distinct
    )
    tables = req.tables or sorted({d["table"] for d in docs})
    indexed = await indexing.index_values(es, org, docs, tables)
    return {"indexed": indexed, "tables": tables, "skipped_high_cardinality": skipped}
