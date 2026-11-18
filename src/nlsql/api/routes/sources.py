"""Data sources for the logged-in user's organisation: connect Postgres, upload CSV, list, delete.

Responses never contain a connection URL. After a source is created its category values are
indexed into Elasticsearch in the background (`values_status`).
"""

from dataclasses import asdict
from typing import Annotated

import anyio
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from nlsql.api.deps import CurrentUser
from nlsql.core.background import spawn
from nlsql.core.config import get_settings
from nlsql.sources import crypto, csv_upload, registry
from nlsql.sources.postgres import READ_ONLY_HELP, SourceError, inspect_postgres, normalize_dsn
from nlsql.sources.values import index_source_values

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
