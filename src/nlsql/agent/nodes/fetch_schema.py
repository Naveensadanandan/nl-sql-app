"""fetch_schema node (no LLM): reflect the data source's tables.

The source is the question's `source_id` (a user's database or uploaded CSVs), else the
configured shared tables (data_schema, data_tables); see sources/registry.target_for.
Reflection runs on a pooled engine per source and is cached per process for
`schema_cache_ttl_seconds`, so a question costs no catalog queries in the common case.
Call `clear_schema_cache()` after a schema change.
"""

import threading
import time
from typing import Any

from sqlalchemy import inspect

from nlsql.agent.state import AgentState
from nlsql.core.config import get_settings
from nlsql.core.db import engine
from nlsql.sources.registry import target_for

_cache: dict[tuple[str, str, str], tuple[float, dict[str, Any]]] = {}
_lock = threading.Lock()


def get_table_schema(
    table: str, schema: str = "public", url: str | None = None
) -> dict[str, Any]:
    try:
        insp = inspect(engine(url or get_settings().sqlalchemy_url))
        return _describe(insp, table, schema)
    except Exception as e:
        # Driver text can name the host and user; it would land in traces. Type only.
        name = type(e).__name__
        raise RuntimeError(f"could not read the data source's tables ({name})") from None


def _describe(insp: Any, table: str, schema: str) -> dict[str, Any]:
    return {
        "table": table,
        "columns": [
            {"name": c["name"], "type": str(c["type"]), "nullable": c["nullable"]}
            for c in insp.get_columns(table, schema=schema)
        ],
        "primary_key": insp.get_pk_constraint(table, schema=schema)["constrained_columns"],
        "foreign_keys": insp.get_foreign_keys(table, schema=schema),
    }


def cached_table_schema(
    table: str, schema: str, url: str | None = None, source_key: str = "shared"
) -> dict[str, Any]:
    ttl = get_settings().schema_cache_ttl_seconds
    key, now = (source_key, schema, table), time.monotonic()
    with _lock:
        hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    value = get_table_schema(table, schema, url)
    with _lock:
        _cache[key] = (now, value)
    return value


def clear_schema_cache() -> None:
    with _lock:
        _cache.clear()


def fetch_schema(state: AgentState) -> dict[str, Any]:
    t = target_for(state.get("source_id"), state.get("organisation_id"))
    return {
        "schema": [
            cached_table_schema(table, t.schema_name, t.sqlalchemy_url, t.key)
            for table in t.tables
        ]
    }
