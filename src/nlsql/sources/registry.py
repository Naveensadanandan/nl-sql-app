"""Data sources: what one organisation's questions run against.

    postgres  the user's own database, via a read-only role. URL Fernet-encrypted at rest.
    csv       an uploaded file, loaded into ws_<organisation_id>.<table> in the app Postgres.

The agent never sees a URL: AgentState carries only `source_id` (state is traced), and nodes
call `target_for(source_id, organisation_id)` to get where to run. That lookup re-checks the
organisation, so a source id from another tenant is refused even if it reaches the graph.
With no source_id the configured shared tables are used (settings.shared_data_enabled).
"""

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from nlsql.core import app_db
from nlsql.core.config import _with_driver, get_settings
from nlsql.sources import crypto
from nlsql.sources.csv_upload import workspace_role
from nlsql.sources.postgres import SourceError

SHARED = "shared"  # Target.key of the configured (non-user) data
_TTL = 60.0  # seconds a resolved Target is reused by the agent


@dataclass(frozen=True)
class Source:
    """Public view of a source (safe to return from the API: no URL)."""

    source_id: str
    organisation_id: str
    kind: str
    name: str
    schema_name: str
    tables: list[str]
    values_status: str
    created_by: str
    created_at: str


@dataclass(frozen=True)
class Target:
    """Where the agent runs SQL. Never log or return `url`."""

    key: str  # cache key: source_id, or SHARED
    schema_name: str
    tables: list[str]
    url: str = field(repr=False)  # plain postgresql:// URL for psycopg
    role: str | None = None  # SET LOCAL ROLE for model SQL (csv: ws_<org>_reader)

    @property
    def sqlalchemy_url(self) -> str:
        return _with_driver(self.url, "psycopg")


class SourceNotFound(LookupError):
    pass


def _source(row: dict[str, Any]) -> Source:
    return Source(
        source_id=row["source_id"],
        organisation_id=row["organisation_id"],
        kind=row["kind"],
        name=row["name"],
        schema_name=row["schema_name"],
        tables=list(row["tables"]),
        values_status=row["values_status"],
        created_by=row["created_by"],
        created_at=row["created_at"].isoformat(),
    )


def new_source_id() -> str:
    return f"src_{uuid.uuid4().hex[:16]}"


def add_source(
    *,
    organisation_id: str,
    user_id: str,
    kind: str,
    name: str,
    schema_name: str,
    tables: list[str],
    dsn: str | None = None,
) -> Source:
    source_id = new_source_id()
    dsn_enc = crypto.encrypt(dsn) if dsn else None
    with app_db.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            app_db.q(
                """
                INSERT INTO {s}.data_sources (source_id, organisation_id, created_by, kind, name,
                    dsn_enc, schema_name, tables)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING *
                """
            ),
            (source_id, organisation_id, user_id, kind, name, dsn_enc, schema_name, Jsonb(tables)),
        )
        row = cur.fetchone()
    assert row is not None
    return _source(row)


def _row(source_id: str, organisation_id: str) -> dict[str, Any]:
    with app_db.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            app_db.q(
                "SELECT * FROM {s}.data_sources WHERE source_id = %s AND organisation_id = %s"
            ),
            (source_id, organisation_id),
        )
        row = cur.fetchone()
    if row is None:
        raise SourceNotFound(source_id)
    return row


def get_source(source_id: str, organisation_id: str) -> Source:
    return _source(_row(source_id, organisation_id))


def list_sources(organisation_id: str) -> list[Source]:
    with app_db.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            app_db.q(
                "SELECT * FROM {s}.data_sources WHERE organisation_id = %s ORDER BY created_at"
            ),
            (organisation_id,),
        )
        return [_source(r) for r in cur.fetchall()]


def delete_source(source_id: str, organisation_id: str) -> Source:
    with app_db.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            app_db.q(
                "DELETE FROM {s}.data_sources WHERE source_id = %s AND organisation_id = %s "
                "RETURNING *"
            ),
            (source_id, organisation_id),
        )
        row = cur.fetchone()
    if row is None:
        raise SourceNotFound(source_id)
    forget(source_id)
    return _source(row)


def stored_dsn(source_id: str, organisation_id: str) -> str:
    """The saved (decrypted) URL of a postgres source, for re-checking it. Never return or log."""
    row = _row(source_id, organisation_id)
    if row["kind"] != "postgres" or not row["dsn_enc"]:
        raise SourceError("this source has no database connection")
    return crypto.decrypt(row["dsn_enc"])


def update_tables(source_id: str, organisation_id: str, tables: list[str]) -> Source:
    """Replace the tables a source exposes. Values go back to 'pending' (they are re-indexed)."""
    with app_db.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            app_db.q(
                "UPDATE {s}.data_sources SET tables = %s, values_status = 'pending' "
                "WHERE source_id = %s AND organisation_id = %s RETURNING *"
            ),
            (Jsonb(tables), source_id, organisation_id),
        )
        row = cur.fetchone()
    if row is None:
        raise SourceNotFound(source_id)
    forget(source_id)
    return _source(row)


def tables_in_use(organisation_id: str, exclude_source_id: str | None = None) -> set[str]:
    """Table names used by the organisation's other sources (they share one value index)."""
    return {
        t
        for s in list_sources(organisation_id)
        if s.source_id != exclude_source_id
        for t in s.tables
    }


def set_values_status(source_id: str, status: str) -> None:
    with app_db.connection() as conn, conn.cursor() as cur:
        cur.execute(
            app_db.q("UPDATE {s}.data_sources SET values_status = %s WHERE source_id = %s"),
            (status, source_id),
        )


# --- agent side -------------------------------------------------------------------------------

_cache: dict[str, tuple[float, Target]] = {}
_lock = threading.Lock()


def shared_target() -> Target:
    s = get_settings()
    return Target(
        key=SHARED, schema_name=s.data_schema, tables=list(s.data_tables), url=s.psycopg_url
    )


def _build(row: dict[str, Any]) -> Target:
    role = None
    if row["kind"] == "postgres":
        url = crypto.decrypt(row["dsn_enc"])  # the user's own read-only role
    else:  # csv: the app database, narrowed to this organisation's reader role
        url = get_settings().psycopg_url
        role = workspace_role(row["organisation_id"])
    return Target(
        key=row["source_id"],
        schema_name=row["schema_name"],
        tables=list(row["tables"]),
        url=url,
        role=role,
    )


def knowledge_organisation(source_id: str | None, organisation_id: str | None) -> str | None:
    """Organisation whose glossary and value index serve this question.

    The shared demo data has its own knowledge (settings.demo_organisation_id), seeded at API
    start; a user's own source uses the user's organisation.
    """
    return get_settings().demo_organisation_id if not source_id else organisation_id


def target_for(source_id: str | None, organisation_id: str | None) -> Target:
    """Where to run this question. Raises SourceError / SourceNotFound when it cannot run."""
    if not source_id:
        if not get_settings().shared_data_enabled:
            raise SourceError("connect a database or upload a CSV first")
        return shared_target()
    if not organisation_id:
        raise SourceNotFound(source_id)
    key, now = f"{organisation_id}|{source_id}", time.monotonic()
    with _lock:
        hit = _cache.get(key)
    if hit and now - hit[0] < _TTL:
        return hit[1]
    target = _build(_row(source_id, organisation_id))
    with _lock:
        _cache[key] = (now, target)
    return target


def forget(source_id: str) -> None:
    with _lock:
        for k in [k for k in _cache if k.endswith(f"|{source_id}")]:
            del _cache[k]
