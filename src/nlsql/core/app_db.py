"""App tables for users, sessions and data sources, in schema <settings.app_schema>.

    organisations(organisation_id)                one row per tenant (created at sign-up)
    users(user_id, username, password_hash, ...)  login accounts; password is scrypt-hashed
    sessions(token_hash, user_id, expires_at)     login tokens; only the SHA-256 is stored
    data_sources(source_id, organisation_id, kind, dsn_enc, schema_name, tables, ...)
                                                  what the agent may query; DSN Fernet-encrypted

These tables are written, so they use the query store's writer connection
(results_database_url, falling back to database_url). The read-only query role never needs them.
"""

import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from typing import Any

from psycopg import Connection, errors, sql
from psycopg_pool import ConnectionPool

from nlsql.core.config import get_settings

_SCHEMA = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_LOCK_KEY = 727_002  # advisory lock so concurrent first-time DDL cannot race
_ready = False
_ready_lock = threading.Lock()

_DDL = (
    """
    CREATE TABLE IF NOT EXISTS {s}.organisations (
        organisation_id text PRIMARY KEY,
        created_at      timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS {s}.users (
        user_id         text PRIMARY KEY,
        username        text NOT NULL UNIQUE,
        password_hash   text NOT NULL,
        organisation_id text NOT NULL REFERENCES {s}.organisations,
        created_at      timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS {s}.sessions (
        token_hash  text PRIMARY KEY,
        user_id     text NOT NULL REFERENCES {s}.users ON DELETE CASCADE,
        expires_at  timestamptz NOT NULL,
        created_at  timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS {s}.data_sources (
        source_id       text PRIMARY KEY,
        organisation_id text NOT NULL REFERENCES {s}.organisations,
        created_by      text NOT NULL,
        kind            text NOT NULL CHECK (kind IN ('postgres', 'csv')),
        name            text NOT NULL,
        dsn_enc         text,
        schema_name     text NOT NULL,
        tables          jsonb NOT NULL DEFAULT '[]',
        values_status   text NOT NULL DEFAULT 'pending',
        created_at      timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS data_sources_org ON {s}.data_sources (organisation_id)",
    "CREATE INDEX IF NOT EXISTS sessions_expiry ON {s}.sessions (expires_at)",
)


def schema() -> sql.Identifier:
    name = get_settings().app_schema
    if not _SCHEMA.match(name):
        raise ValueError("app_schema must match [a-z_][a-z0-9_]{0,62}")
    return sql.Identifier(name)


def q(text: str) -> sql.Composed:
    """SQL with `{s}` replaced by the quoted app schema."""
    return sql.SQL(text).format(s=schema())


@lru_cache
def _pool() -> ConnectionPool:
    s = get_settings()
    return ConnectionPool(
        s.results_psycopg_url, min_size=1, max_size=s.store_pool_size, open=True, name="app"
    )


def close_pool() -> None:
    if _pool.cache_info().currsize:
        _pool().close()
        _pool.cache_clear()


def ensure_tables() -> None:
    """Create the schema and tables once per process (idempotent)."""
    global _ready
    if _ready:
        return
    with _ready_lock:
        if _ready:
            return
        with _pool().connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_KEY,))
            cur.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(schema()))
            for ddl in _DDL:
                cur.execute(q(ddl))
        _ready = True


@contextmanager
def connection() -> Iterator[Connection[Any]]:
    """A pooled connection; the block runs in one transaction (commit on success)."""
    global _ready
    ensure_tables()
    with _pool().connection() as conn:
        try:
            yield conn
        except (errors.UndefinedTable, errors.InvalidSchemaName):
            # The schema was dropped while this process ran (e.g. a DB reset). Forget the
            # cached "ready" so the next call recreates it instead of failing until restart.
            _ready = False
            raise
