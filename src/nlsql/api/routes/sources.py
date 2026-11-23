"""Data sources for the logged-in user's organisation: connect Postgres, upload CSV, list, delete.

Connecting is two steps, so nobody types schema or table names: `POST /postgres/discover`
checks the role is read-only and lists the schemas and tables it can read, then
`POST /postgres` saves the ones picked. A saved source's tables can be changed later with
`GET/PUT /{id}/tables` (the stored connection is reused and re-checked), and a CSV source
can take more files as extra tables with `POST /{id}/csv`.

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
from nlsql.sources.postgres import (
    READ_ONLY_HELP,
    SourceError,
    check_identifier,
    discover_postgres,
    inspect_postgres,
    normalize_dsn,
)
from nlsql.sources.values import forget_values, index_source_values
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


class DiscoverRequest(BaseModel):
    url: str = Field(
        min_length=10, max_length=1000, description="postgresql://user:pass@host:5432/db"
    )


class TablesRequest(BaseModel):
    tables: list[str] = Field(min_length=1, max_length=500)


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


@router.post("/postgres/discover")
async def discover(body: DiscoverRequest, user: CurrentUser) -> dict[str, object]:
    """Check a connection (read-only role) and list what it can read. Nothing is saved."""
    del user  # login required; nothing is stored for the organisation yet
    if not crypto.available():
        raise HTTPException(503, "database connections are disabled: NLSQL_FERNET_KEY is not set")
    try:
        schemas = await anyio.to_thread.run_sync(discover_postgres, normalize_dsn(body.url))
    except SourceError as e:
        raise HTTPException(422, str(e)) from e
    return {"schemas": schemas, "max_tables": get_settings().source_max_tables}


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


async def _read_upload(file: UploadFile) -> bytes:
    limit = get_settings().upload_max_mb * 1024 * 1024
    raw = await file.read(limit + 1)
    if len(raw) > limit:
        raise HTTPException(413, f"file larger than {get_settings().upload_max_mb} MB")
    return raw


@router.post("/csv", status_code=201)
async def add_csv(
    user: CurrentUser,
    file: Annotated[UploadFile, File()],
    name: Annotated[str | None, Form(max_length=100)] = None,
    table: Annotated[str | None, Form(max_length=50)] = None,
) -> dict[str, object]:
    raw = await _read_upload(file)
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


async def _get(source_id: str, organisation_id: str) -> registry.Source:
    try:
        return await anyio.to_thread.run_sync(registry.get_source, source_id, organisation_id)
    except registry.SourceNotFound as e:
        raise HTTPException(404, "unknown source") from e


@router.get("/{source_id}/tables")
async def source_tables(source_id: str, user: CurrentUser) -> dict[str, object]:
    """The tables a source exposes now, and the ones it could expose.

    Postgres: every table the saved role can read in the source's schema (the connection is
    re-checked as read-only). CSV: the uploaded tables (add more with POST /{id}/csv).
    """
    src = await _get(source_id, user.organisation_id)
    available = list(src.tables)
    if src.kind == "postgres":
        try:
            dsn = await anyio.to_thread.run_sync(
                registry.stored_dsn, source_id, user.organisation_id
            )
            schemas = await anyio.to_thread.run_sync(discover_postgres, dsn)
        except (SourceError, CryptoUnavailable) as e:
            raise HTTPException(422, str(e)) from e
        here = next((x for x in schemas if x["schema_name"] == src.schema_name), None)
        if here is None:
            raise HTTPException(422, f"schema {src.schema_name!r} is no longer readable")
        if here["blocked"]:
            raise HTTPException(422, f"{here['blocked']}. {READ_ONLY_HELP}")
        available = list(here["tables"])  # type: ignore[call-overload]
    return {
        "source_id": source_id,
        "kind": src.kind,
        "schema_name": src.schema_name,
        "selected": src.tables,
        "available": available,
        "max_tables": get_settings().source_max_tables,
    }


async def _apply_tables(
    src: registry.Source, organisation_id: str, tables: list[str]
) -> dict[str, object]:
    """Save the new table list, drop values of removed tables, re-index in the background."""
    removed = [t for t in src.tables if t not in set(tables)]
    updated = await anyio.to_thread.run_sync(
        registry.update_tables, src.source_id, organisation_id, tables
    )
    if removed:
        in_use = await anyio.to_thread.run_sync(
            registry.tables_in_use, organisation_id, src.source_id
        )
        await forget_values(organisation_id, [t for t in removed if t not in in_use])
    spawn(index_source_values(src.source_id, organisation_id), name="index_source_values")
    return _public(updated)


@router.put("/{source_id}/tables")
async def set_source_tables(
    source_id: str, body: TablesRequest, user: CurrentUser
) -> dict[str, object]:
    """Replace the tables a source exposes. Postgres re-checks the role is still read-only;
    a CSV table left out is dropped (it only exists in this app)."""
    src = await _get(source_id, user.organisation_id)
    wanted = list(dict.fromkeys(t.strip() for t in body.tables if t.strip()))
    if not wanted:
        raise HTTPException(422, "keep at least one table (or delete the source)")
    try:
        for t in wanted:
            check_identifier(t, "table")
        if src.kind == "postgres":
            dsn = await anyio.to_thread.run_sync(
                registry.stored_dsn, source_id, user.organisation_id
            )
            wanted = await anyio.to_thread.run_sync(inspect_postgres, dsn, src.schema_name, wanted)
        else:
            unknown = sorted(set(wanted) - set(src.tables))
            if unknown:
                raise SourceError(
                    f"not in this source: {', '.join(unknown)}; upload a CSV to add a table"
                )
    except (SourceError, CryptoUnavailable) as e:
        raise HTTPException(422, str(e)) from e
    out = await _apply_tables(src, user.organisation_id, wanted)
    if src.kind == "csv":
        for t in [t for t in src.tables if t not in set(wanted)]:
            await anyio.to_thread.run_sync(csv_upload.drop_csv_table, user.organisation_id, t)
    return out


@router.post("/{source_id}/csv", status_code=201)
async def add_csv_table(
    source_id: str,
    user: CurrentUser,
    file: Annotated[UploadFile, File()],
    table: Annotated[str | None, Form(max_length=50)] = None,
) -> dict[str, object]:
    """Upload another CSV into an existing CSV source as a new table (so they can be joined)."""
    src = await _get(source_id, user.organisation_id)
    if src.kind != "csv":
        raise HTTPException(422, "only CSV sources take uploaded tables")
    if len(src.tables) >= get_settings().source_max_tables:
        raise HTTPException(422, f"this source already has {len(src.tables)} tables (the maximum)")
    raw = await _read_upload(file)
    tbl = csv_upload.table_name(table or file.filename or "upload")
    if tbl in src.tables:
        raise HTTPException(422, f"table {tbl!r} is already in this source; pick another name")
    try:
        await anyio.to_thread.run_sync(csv_upload.load_csv, user.organisation_id, tbl, raw)
    except SourceError as e:
        raise HTTPException(422, str(e)) from e
    try:
        updated = await anyio.to_thread.run_sync(
            registry.update_tables, source_id, user.organisation_id, [*src.tables, tbl]
        )
    except Exception:
        # Not registered: remove the table so the name can be used again.
        await anyio.to_thread.run_sync(csv_upload.drop_csv_table, user.organisation_id, tbl)
        raise
    spawn(index_source_values(source_id, user.organisation_id), name="index_source_values")
    return _public(updated)


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
