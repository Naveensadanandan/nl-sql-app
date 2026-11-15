"""Query store: every question is logged, and its result rows are saved as their own table.

    <results_schema>.query_log        one row per question (who, what, SQL, status, columns)
    <results_schema>.q_<32 hex>       the result rows of that question (table name = question_id)

This is the only WRITE path in the app and it never touches the user's data tables. The SQL
that produced the rows was already validated and ran read-only (nlsql.sql.executor); here the
rows are plain values inserted with bound parameters, and every identifier is built with
psycopg.sql.Identifier from a generated id or a sanitised column name.

Result rows are user data: never log them. Admin endpoints read them back (admin/main.py).
"""

import json
import re
import uuid
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from nlsql.core.config import get_settings
from nlsql.sql.executor import QueryResult

_ID = re.compile(r"^q_[0-9a-f]{32}$")
_SCHEMA = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_LOCK_KEY = 727_001  # advisory lock so concurrent first-time DDL cannot race
_ready = False


def new_question_id() -> str:
    return f"q_{uuid.uuid4().hex}"


def is_question_id(value: str) -> bool:
    return bool(_ID.match(value))


def _schema() -> str:
    name = get_settings().results_schema
    if not _SCHEMA.match(name):
        raise ValueError("results_schema must match [a-z_][a-z0-9_]{0,62}")
    return name


def _connect() -> psycopg.Connection[Any]:
    s = get_settings()
    url = (s.results_database_url or s.database_url).replace("+psycopg", "", 1)
    return psycopg.connect(url)


def ensure_store() -> None:
    """Create the schema and query_log table once per process (idempotent)."""
    global _ready
    if _ready:
        return
    schema = sql.Identifier(_schema())
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_KEY,))
        cur.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(schema))
        cur.execute(
            sql.SQL(
                """
                CREATE TABLE IF NOT EXISTS {}.query_log (
                    question_id     text PRIMARY KEY,
                    organisation_id text,
                    user_id         text,
                    question        text NOT NULL,
                    category        text,
                    status          text NOT NULL,
                    sql             text,
                    error           text,
                    row_count       integer NOT NULL DEFAULT 0,
                    truncated       boolean NOT NULL DEFAULT false,
                    debug_attempts  integer NOT NULL DEFAULT 0,
                    assumptions     jsonb NOT NULL DEFAULT '[]',
                    columns         jsonb NOT NULL DEFAULT '[]',
                    created_at      timestamptz NOT NULL DEFAULT now()
                )
                """
            ).format(schema)
        )
        cur.execute(
            sql.SQL(
                "CREATE INDEX IF NOT EXISTS query_log_org_created "
                "ON {}.query_log (organisation_id, created_at DESC)"
            ).format(schema)
        )
    _ready = True


# --- result table layout ------------------------------------------------------------------


def _column_names(names: list[str]) -> list[str]:
    """Safe, unique, lower-case Postgres column names. The originals are kept in query_log."""
    out: list[str] = []
    seen: set[str] = set()
    for i, name in enumerate(names, start=1):
        col = re.sub(r"[^a-z0-9_]+", "_", name.lower()).strip("_")[:50] or f"col_{i}"
        if col[0].isdigit():
            col = f"c_{col}"
        base, n = col, 2
        while col in seen:
            col, n = f"{base}_{n}", n + 1
        seen.add(col)
        out.append(col)
    return out


def _pg_type(values: list[Any]) -> str:
    vals = [v for v in values if v is not None]
    if vals and all(isinstance(v, bool) for v in vals):
        return "boolean"
    if vals and all(isinstance(v, int) and not isinstance(v, bool) for v in vals):
        return "bigint" if all(abs(v) < 2**63 for v in vals) else "numeric"
    if vals and all(isinstance(v, int | float) and not isinstance(v, bool) for v in vals):
        return "double precision"
    return "text"


def _cell(value: Any, pg_type: str) -> Any:
    if value is None:
        return None
    if pg_type == "text" and not isinstance(value, str):
        return json.dumps(value) if isinstance(value, dict | list) else str(value)
    return value


# --- write --------------------------------------------------------------------------------


def save_query(record: dict[str, Any], result: QueryResult | None) -> None:
    """Insert the query_log row and, when there is a result, create and fill its table.

    `record` keys: question_id, organisation_id, user_id, question, category, status, sql, error,
    debug_attempts, assumptions. One transaction: either both the log row and table exist or neither.
    """
    question_id = record["question_id"]
    if not is_question_id(question_id):
        raise ValueError("invalid question_id")
    ensure_store()
    schema = sql.Identifier(_schema())
    columns: list[dict[str, str]] = []
    if result is not None and result.columns:
        names = _column_names(result.columns)
        types = [_pg_type([row[i] for row in result.rows]) for i in range(len(names))]
        columns = [
            {"name": orig, "column": col, "type": t}
            for orig, col, t in zip(result.columns, names, types, strict=True)
        ]

    with _connect() as conn, conn.cursor() as cur:
        if columns:
            table = sql.Identifier(question_id)
            cur.execute(
                sql.SQL("CREATE TABLE {}.{} ({})").format(
                    schema,
                    table,
                    sql.SQL(", ").join(
                        sql.SQL("{} {}").format(sql.Identifier(c["column"]), sql.SQL(c["type"]))
                        for c in columns
                    ),
                )
            )
            if result and result.rows:
                cur.executemany(
                    sql.SQL("INSERT INTO {}.{} ({}) VALUES ({})").format(
                        schema,
                        table,
                        sql.SQL(", ").join(sql.Identifier(c["column"]) for c in columns),
                        sql.SQL(", ").join(sql.Placeholder() for _ in columns),
                    ),
                    [
                        [_cell(v, c["type"]) for v, c in zip(row, columns, strict=True)]
                        for row in result.rows
                    ],
                )
        cur.execute(
            sql.SQL(
                """
                INSERT INTO {}.query_log (question_id, organisation_id, user_id, question,
                    category, status, sql, error, row_count, truncated, debug_attempts,
                    assumptions, columns)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """
            ).format(schema),
            (
                question_id,
                record.get("organisation_id"),
                record.get("user_id"),
                record["question"],
                record.get("category"),
                record["status"],
                record.get("sql"),
                record.get("error"),
                result.row_count if result else 0,
                result.truncated if result else False,
                record.get("debug_attempts", 0),
                Jsonb(record.get("assumptions", [])),
                Jsonb(columns),
            ),
        )


# --- read (admin) -------------------------------------------------------------------------


def list_queries(organisation_id: str | None, limit: int = 50) -> list[dict[str, Any]]:
    ensure_store()
    with _connect() as conn:
        conn.read_only = True
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                sql.SQL(
                    """
                    SELECT question_id, organisation_id, user_id, question, category, status,
                           row_count, truncated, debug_attempts, created_at
                    FROM {}.query_log
                    WHERE (%s::text IS NULL OR organisation_id = %s)
                    ORDER BY created_at DESC LIMIT %s
                    """
                ).format(sql.Identifier(_schema())),
                (organisation_id, organisation_id, limit),
            )
            return cur.fetchall()


def get_query(question_id: str, row_limit: int = 200) -> dict[str, Any] | None:
    """The log row plus up to `row_limit` saved result rows, or None if the id is unknown."""
    if not is_question_id(question_id):
        raise ValueError("invalid question_id")
    ensure_store()
    schema = sql.Identifier(_schema())
    with _connect() as conn:
        conn.read_only = True
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                sql.SQL("SELECT * FROM {}.query_log WHERE question_id = %s").format(schema),
                (question_id,),
            )
            meta = cur.fetchone()
            if meta is None:
                return None
            rows: list[list[Any]] = []
            cols = meta["columns"]
            if cols:
                cur.execute(
                    sql.SQL("SELECT {} FROM {}.{} LIMIT %s").format(
                        sql.SQL(", ").join(sql.Identifier(c["column"]) for c in cols),
                        schema,
                        sql.Identifier(question_id),
                    ),
                    (row_limit,),
                )
                rows = [list(r.values()) for r in cur.fetchall()]
    meta["result_columns"] = [c["name"] for c in cols]
    meta["rows"] = rows
    return meta
