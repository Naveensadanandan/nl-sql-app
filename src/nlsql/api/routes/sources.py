"""Data sources for the logged-in user's organisation: connect Postgres, upload CSV, list, delete.

Responses never contain a connection URL. After a source is created its category values are
indexed into Elasticsearch in the background (`values_status`).
"""

from dataclasses import asdict
from typing import Annotated

import anyio
from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field
from sqlglot import exp

from nlsql.agent.nodes.fetch_schema import cached_table_schema
from nlsql.api.deps import CurrentUser
from nlsql.core.background import spawn
from nlsql.core.config import get_settings
from nlsql.retrieval.glossary_search import list_glossary
from nlsql.retrieval.value_search import summarize_values
from nlsql.sources import crypto, csv_upload, registry
from nlsql.sources.crypto import CryptoUnavailable
from nlsql.sources.postgres import READ_ONLY_HELP, SourceError, inspect_postgres, normalize_dsn
from nlsql.sources.values import index_source_values
from nlsql.sql.executor import SqlExecutionError, execute_readonly
from nlsql.sql.validator import allowed_from_schema, validate

router = APIRouter(prefix="/sources", tags=["sources"])


class PostgresSourceRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    url: str = Field(
        min_length=10, max_length=1000, description="postgresql://user:pass@host:5432/db"
    )
    schema_name: str = Field(default="public", max_length=63)
    tables: list[str] | None = Field(default=None, description="Default: every readable table")


def _public(src: registry.Source) -> dict[str, object]:
    return asdict(src)


@router.get("")
async def list_sources(user: CurrentUser) -> dict[str, object]:
    sources = await anyio.to_thread.run_sync(registry.list_sources, user.organisation_id)
    return {
        "sources": [_public(s) for s in sources],
        "shared_data_enabled": get_settings().shared_data_enabled,
        "postgres_enabled": crypto.available(),
        "postgres_disabled_reason": crypto.unavailable_reason(),
        "read_only_help": READ_ONLY_HELP,
    }


PREVIEW_MAX_TABLES = 10


@router.get("/preview")
async def preview_source(
    user: CurrentUser,
    source_id: str | None = None,
    rows: int = Query(default=5, ge=1, le=20),
) -> dict[str, object]:
    """Columns and the first few rows of each table in a source (default: the shared demo data).

    The SQL is built from reflected table and column names only, then goes through the same
    validator and read-only executor as model SQL. Rows are returned to the caller, never logged.
    """
    try:
        target = await anyio.to_thread.run_sync(
            registry.target_for, source_id, user.organisation_id
        )
    except registry.SourceNotFound as e:
        raise HTTPException(404, "unknown data source") from e
    except (SourceError, CryptoUnavailable) as e:
        raise HTTPException(422, str(e)) from e
    s = get_settings()
    tables: list[dict[str, object]] = []
    for name in target.tables[:PREVIEW_MAX_TABLES]:
        try:
            schema = await anyio.to_thread.run_sync(
                cached_table_schema, name, target.schema_name, target.sqlalchemy_url, target.key
            )
        except RuntimeError as e:
            tables.append({"table": name, "columns": [], "rows": [], "error": str(e)})
            continue
        columns = schema["columns"]
        if not columns:
            tables.append({"table": name, "columns": [], "rows": [], "error": None})
            continue
        query = (
            exp.select(*[exp.column(c["name"], quoted=True) for c in columns])
            .from_(exp.table_(name, quoted=True))
            .limit(rows)
            .sql(dialect="postgres")
        )
        check = validate(
            query,
            allowed_from_schema(schema),
            row_cap=s.sql_row_cap,
            schema_name=target.schema_name,
        )
        if not check.ok or not check.sql:
            tables.append({"table": name, "columns": columns, "rows": [], "error": check.error})
            continue
        try:
            result = await execute_readonly(
                check.sql, target.sqlalchemy_url, target.schema_name, target.role
            )
        except SqlExecutionError as e:
            tables.append({"table": name, "columns": columns, "rows": [], "error": str(e)})
            continue
        tables.append({"table": name, "columns": columns, "rows": result.rows, "error": None})
    return {
        "source_id": source_id,
        "schema_name": target.schema_name,
        "table_count": len(target.tables),
        "tables": tables,
    }


@router.get("/knowledge")
async def source_knowledge(user: CurrentUser, source_id: str | None = None) -> dict[str, object]:
    """Glossary terms and indexed values that questions on this source use.

    The shared demo data (no source_id) uses the demo organisation's knowledge; a user's source
    uses the user's organisation (values limited to that source's tables).
    """
    try:
        target = await anyio.to_thread.run_sync(
            registry.target_for, source_id, user.organisation_id
        )
    except registry.SourceNotFound as e:
        raise HTTPException(404, "unknown data source") from e
    except (SourceError, CryptoUnavailable) as e:
        raise HTTPException(422, str(e)) from e
    s = get_settings()
    org = registry.knowledge_organisation(source_id, user.organisation_id)
    out: dict[str, object] = {
        "demo": not source_id,
        "glossary_enabled": bool(s.qdrant_url),
        "values_enabled": bool(s.es_url),
        "glossary": [],
        "values": [],
    }
    if not org:
        return out
    if s.qdrant_url:
        try:
            out["glossary"] = await list_glossary(org, None if not source_id else user.user_id)
        except Exception as e:  # noqa: BLE001 - show the rest; the type only, no payloads
            out["glossary_error"] = f"glossary unavailable ({type(e).__name__})"
    if s.es_url:
        try:
            out["values"] = await summarize_values(org, target.tables)
        except Exception as e:  # noqa: BLE001
            out["values_error"] = f"value index unavailable ({type(e).__name__})"
    return out


@router.post("/postgres", status_code=201)
async def add_postgres(body: PostgresSourceRequest, user: CurrentUser) -> dict[str, object]:
    if not crypto.available():
        raise HTTPException(503, "database connections are disabled: NLSQL_FERNET_KEY is not set")
    try:
        dsn = normalize_dsn(body.url)
        tables = await anyio.to_thread.run_sync(
            inspect_postgres, dsn, body.schema_name, body.tables
        )
        src = await anyio.to_thread.run_sync(
            lambda: registry.add_source(
                organisation_id=user.organisation_id,
                user_id=user.user_id,
                kind="postgres",
                name=body.name.strip(),
                schema_name=body.schema_name,
                tables=tables,
                dsn=dsn,
            )
        )
    except SourceError as e:
        raise HTTPException(422, str(e)) from e
    spawn(index_source_values(src.source_id, user.organisation_id), name="index_source_values")
    return _public(src)


@router.post("/csv", status_code=201)
async def add_csv(
    user: CurrentUser,
    file: Annotated[UploadFile, File()],
    name: Annotated[str | None, Form(max_length=100)] = None,
    table: Annotated[str | None, Form(max_length=50)] = None,
) -> dict[str, object]:
    limit = get_settings().upload_max_mb * 1024 * 1024
    raw = await file.read(limit + 1)
    if len(raw) > limit:
        raise HTTPException(413, f"file larger than {get_settings().upload_max_mb} MB")
    tbl = csv_upload.table_name(table or file.filename or "upload")
    try:
        schema, _rows = await anyio.to_thread.run_sync(
            csv_upload.load_csv, user.organisation_id, tbl, raw
        )
        src = await anyio.to_thread.run_sync(
            lambda: registry.add_source(
                organisation_id=user.organisation_id,
                user_id=user.user_id,
                kind="csv",
                name=(name or file.filename or tbl).strip()[:100],
                schema_name=schema,
                tables=[tbl],
            )
        )
    except SourceError as e:
        raise HTTPException(422, str(e)) from e
    spawn(index_source_values(src.source_id, user.organisation_id), name="index_source_values")
    return _public(src)


@router.delete("/{source_id}", status_code=204)
async def delete_source(source_id: str, user: CurrentUser) -> None:
    try:
        src = await anyio.to_thread.run_sync(
            registry.delete_source, source_id, user.organisation_id
        )
    except registry.SourceNotFound as e:
        raise HTTPException(404, "unknown source") from e
    if src.kind == "csv":
        for t in src.tables:
            await anyio.to_thread.run_sync(csv_upload.drop_csv_table, user.organisation_id, t)
