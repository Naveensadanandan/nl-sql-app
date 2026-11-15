"""fetch_schema node (no LLM): reflect the configured data tables.

Tables come from settings (data_schema, data_tables). Reflection runs on the shared pooled
engine and is cached per process for `schema_cache_ttl_seconds`, so a question costs no
catalog queries in the common case. Call `clear_schema_cache()` after a schema change.
"""

import threading
import time
from typing import Any

from sqlalchemy import inspect

from nlsql.agent.state import AgentState
from nlsql.core.config import get_settings
from nlsql.core.db import engine

_cache: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
_lock = threading.Lock()


def get_table_schema(table: str, schema: str = "public") -> dict[str, Any]:
    insp = inspect(engine(get_settings().sqlalchemy_url))
    return {
        "table": table,
        "columns": [
            {"name": c["name"], "type": str(c["type"]), "nullable": c["nullable"]}
            for c in insp.get_columns(table, schema=schema)
        ],
        "primary_key": insp.get_pk_constraint(table, schema=schema)["constrained_columns"],
        "foreign_keys": insp.get_foreign_keys(table, schema=schema),
    }


def cached_table_schema(table: str, schema: str) -> dict[str, Any]:
    ttl = get_settings().schema_cache_ttl_seconds
    key, now = (schema, table), time.monotonic()
    with _lock:
        hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    value = get_table_schema(table, schema)
    with _lock:
        _cache[key] = (now, value)
    return value


def clear_schema_cache() -> None:
    with _lock:
        _cache.clear()


def fetch_schema(state: AgentState) -> dict[str, Any]:
    s = get_settings()
    return {"schema": [cached_table_schema(t, s.data_schema) for t in s.data_tables]}
