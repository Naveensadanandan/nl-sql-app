"""Index builders for Qdrant (glossary) and Elasticsearch (category values).

Shared by the admin server and, later, `python -m nlsql.worker.reindex`. The layouts match
`retrieval/glossary_search.py` and `retrieval/value_search.py`; change them together.
"""

import csv
import io
import uuid
from collections.abc import AsyncIterator
from typing import Any, Literal

import psycopg
from elasticsearch import AsyncElasticsearch
from elasticsearch.helpers import async_bulk
from psycopg import sql
from pydantic import BaseModel, Field, ValidationError, field_validator
from qdrant_client import AsyncQdrantClient, models

from nlsql.core.config import get_settings
from nlsql.retrieval.value_search import index_name

_NAMESPACE = uuid.UUID("6f1c2b0e-5d3a-4a55-9a43-0d6a1b9c7e11")
_TEXT_TYPES = ("text", "character varying", "character")
_PAYLOAD_INDEXES = ("organisation_id", "section", "status", "user_id")
BATCH = 256

# ---------------------------------------------------------------- glossary -> Qdrant


class GlossaryRow(BaseModel):
    """One CSV row. Columns: name, description, section, synonyms (a|b|c), status, user_id."""

    name: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=2000)
    section: Literal["metric", "business_definition"]
    synonyms: list[str] = Field(default_factory=list)
    status: Literal["approved", "proposed", "rejected"] = "approved"
    user_id: str | None = None

    @field_validator("synonyms", mode="before")
    @classmethod
    def _split(cls, v: Any) -> Any:
        if isinstance(v, str):
            return [s.strip() for s in v.split("|") if s.strip()]
        return v

    @field_validator("user_id", mode="before")
    @classmethod
    def _blank_is_none(cls, v: Any) -> Any:
        return v or None


class CsvError(ValueError):
    """The upload is not usable. Message is safe to show to the admin."""


def parse_glossary_csv(raw: bytes, max_rows: int = 5000) -> list[GlossaryRow]:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise CsvError("file must be UTF-8") from e
    reader = csv.DictReader(io.StringIO(text))
    missing = {"name", "description", "section"} - set(reader.fieldnames or [])
    if missing:
        raise CsvError(f"missing required column(s): {', '.join(sorted(missing))}")
    rows: list[GlossaryRow] = []
    for line, rec in enumerate(reader, start=2):
        if len(rows) >= max_rows:
            raise CsvError(f"more than {max_rows} rows")
        clean = {k.strip(): (v or "").strip() for k, v in rec.items() if k}
        clean = {k: v for k, v in clean.items() if v or k in ("name", "description", "section")}
        try:
            rows.append(GlossaryRow.model_validate(clean))
        except ValidationError as e:
            first = e.errors()[0]
            raise CsvError(f"line {line}: {first['loc'][0]}: {first['msg']}") from e
    if not rows:
        raise CsvError("no data rows")
    return rows


def point_id(organisation_id: str, row: GlossaryRow) -> str:
    """Deterministic, so re-uploading the same term updates it instead of duplicating."""
    key = f"{organisation_id}|{row.user_id or ''}|{row.section}|{row.name.lower()}"
    return str(uuid.uuid5(_NAMESPACE, key))


async def ensure_glossary_collection(qdrant: AsyncQdrantClient) -> None:
    s = get_settings()
    if await qdrant.collection_exists(s.glossary_collection):
        return
    await qdrant.create_collection(
        collection_name=s.glossary_collection,
        vectors_config=models.VectorParams(
            size=qdrant.get_embedding_size(s.embedding_model), distance=models.Distance.COSINE
        ),
    )
    for field in _PAYLOAD_INDEXES:
        await qdrant.create_payload_index(
            s.glossary_collection, field, models.PayloadSchemaType.KEYWORD
        )


async def index_glossary(
    qdrant: AsyncQdrantClient, organisation_id: str, rows: list[GlossaryRow]
) -> int:
    """Upsert rows for one organisation. Embedding runs inside qdrant-client (fastembed)."""
    s = get_settings()
    await ensure_glossary_collection(qdrant)
    for i in range(0, len(rows), BATCH):
        points = []
        for row in rows[i : i + BATCH]:
            pid = point_id(organisation_id, row)
            text = " ".join([row.name, *row.synonyms, row.description])
            payload: dict[str, Any] = {
                "id": f"gl_{pid[:8]}",
                "name": row.name,
                "description": row.description,
                "synonyms": row.synonyms,
                "section": row.section,
                "organisation_id": organisation_id,
                "status": row.status,
            }
            if row.user_id:
                payload["user_id"] = row.user_id
            points.append(
                models.PointStruct(
                    id=pid,
                    vector=models.Document(text=text, model=s.embedding_model),
                    payload=payload,
                )
            )
        await qdrant.upsert(s.glossary_collection, points=points)
    return len(rows)


# --------------------------------------------------------------- values -> Elasticsearch

_VALUE_MAPPING: dict[str, Any] = {
    "properties": {
        "name": {"type": "text", "fields": {"raw": {"type": "keyword", "ignore_above": 512}}},
        "synonyms": {"type": "text"},
        "description": {"type": "text"},
        "table": {"type": "keyword"},  # filter field: restricts search to the selected tables
        "column": {"type": "keyword"},
    }
}


async def ensure_values_index(es: AsyncElasticsearch, organisation_id: str) -> str:
    index = index_name(organisation_id)
    if not await es.indices.exists(index=index):
        await es.indices.create(index=index, mappings=_VALUE_MAPPING)
    return index


def _category_columns(
    cur: psycopg.Cursor[Any], schema: str, tables: list[str] | None
) -> list[tuple[str, str]]:
    cur.execute(
        """
        SELECT c.table_name, c.column_name
        FROM information_schema.columns c
        JOIN information_schema.tables t
          ON t.table_schema = c.table_schema AND t.table_name = c.table_name
        WHERE c.table_schema = %s AND t.table_type = 'BASE TABLE'
          AND c.data_type = ANY(%s) AND (%s::text[] IS NULL OR c.table_name = ANY(%s))
        ORDER BY c.table_name, c.ordinal_position
        """,
        (schema, list(_TEXT_TYPES), tables, tables),
    )
    return [(r[0], r[1]) for r in cur.fetchall()]


def list_tables(schema: str) -> list[str]:
    with psycopg.connect(get_settings().database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = %s AND table_type = 'BASE TABLE' ORDER BY 1",
            (schema,),
        )
        return [r[0] for r in cur.fetchall()]


def collect_values(
    schema: str, tables: list[str] | None, max_distinct: int
) -> tuple[list[dict[str, Any]], list[str]]:
    """Distinct values of text columns that look like categories.

    A column is skipped when it has more than `max_distinct` distinct values (names, notes,
    ids). Runs in a read-only transaction with a statement timeout. Returns (docs, skipped).
    """
    docs: list[dict[str, Any]] = []
    skipped: list[str] = []
    with psycopg.connect(get_settings().database_url) as conn:
        conn.read_only = True
        with conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = '30s'")
            for table, column in _category_columns(cur, schema, tables):
                cur.execute(
                    sql.SQL(
                        "SELECT DISTINCT {col} FROM {sch}.{tbl} WHERE {col} IS NOT NULL LIMIT %s"
                    ).format(
                        col=sql.Identifier(column),
                        sch=sql.Identifier(schema),
                        tbl=sql.Identifier(table),
                    ),
                    (max_distinct + 1,),
                )
                values = [str(r[0]).strip() for r in cur.fetchall() if str(r[0]).strip()]
                if len(values) > max_distinct:
                    skipped.append(f"{table}.{column}")
                    continue
                docs.extend(
                    {
                        "id": f"{table}.{column}:{v}",
                        "name": v,
                        "description": "",
                        "synonyms": [],
                        "table": table,
                        "column": column,
                    }
                    for v in values
                )
    return docs, skipped


async def _actions(index: str, docs: list[dict[str, Any]]) -> AsyncIterator[dict[str, Any]]:
    for d in docs:
        # ES ids are capped at 512 bytes; hash the long ones.
        doc_id = d["id"] if len(d["id"].encode()) <= 400 else str(uuid.uuid5(_NAMESPACE, d["id"]))
        yield {"_index": index, "_id": doc_id, "_source": d}


async def index_values(
    es: AsyncElasticsearch, organisation_id: str, docs: list[dict[str, Any]], tables: list[str]
) -> int:
    """Replace the indexed values of `tables` (so values deleted upstream disappear)."""
    index = await ensure_values_index(es, organisation_id)
    if tables:
        await es.delete_by_query(
            index=index, query={"terms": {"table": tables}}, refresh=True, conflicts="proceed"
        )
    ok, _ = await async_bulk(es, _actions(index, docs), chunk_size=1000, refresh="wait_for")
    return ok
