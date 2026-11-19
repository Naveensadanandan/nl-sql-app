"""Read-only SQL execution (system-design §7, steps 5-6).

Only call this with SQL that has passed `nlsql.sql.validator.validate`. The transaction is
READ ONLY, has a statement timeout, is always rolled back, and an EXPLAIN cost guard runs first.
Never log the rows: they are user data.
"""

import asyncio
import json
import re
from datetime import date, datetime, time
from typing import Any

from pydantic import BaseModel

from nlsql.core.config import get_settings
from nlsql.core.db import engine

_NO_PARAMS = {
    "no_parameters": True
}  # pass the SQL to the driver verbatim ('%' and ':' stay literal)


class QueryResult(BaseModel):
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool  # more rows existed than the row cap


class SqlExecutionError(Exception):
    """Database or cost-guard failure; the message is safe to show to the debugger."""


def _json_safe(value: Any) -> Any:
    # Decimal stays Decimal (exact money values); the query store saves it as numeric.
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    if isinstance(value, bytes | memoryview):
        return "<binary>"
    return value


def _safe_error(e: Exception) -> str:
    """Primary message + SQLSTATE only. Drops DETAIL/CONTEXT (can hold row data) and host info."""
    orig = getattr(e, "orig", e)
    diag = getattr(orig, "diag", None)
    msg = getattr(diag, "message_primary", None) or type(orig).__name__
    msg = re.sub(r"(for type [\w ]+): .*", r"\1", msg)  # 'invalid input syntax for type x: "val"'
    msg = re.sub(r"'[^']*'", "'...'", msg)  # quoted literals may be data values
    state = getattr(orig, "sqlstate", None)
    return f"{type(orig).__name__}{f' [{state}]' if state else ''}: {msg.strip()[:300]}"


def _plan_cost(raw: Any) -> float:
    plan = json.loads(raw) if isinstance(raw, str) else raw
    return float(plan[0]["Plan"]["Total Cost"])


_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


def _search_path(schema: str) -> str:
    """SET LOCAL search_path to exactly the source's schema (pg_catalog is always implicit).

    Unqualified table names then resolve inside the validated schema only, never `public` or
    another workspace's ws_ schema.
    """
    if not _IDENT.match(schema):  # letters, digits, '_' only: safe inside double quotes
        raise SqlExecutionError("invalid schema name")
    return f'SET LOCAL search_path = "{schema}"'


def _set_role(role: str) -> str:
    """SET LOCAL ROLE: Postgres checks every privilege as this role until the transaction ends.

    The validator rejects SET / RESET / set_config, so model SQL cannot switch back.
    """
    if not _IDENT.match(role):
        raise SqlExecutionError("invalid role name")
    return f'SET LOCAL ROLE "{role}"'


def _run(sql: str, url: str | None, schema: str | None, role: str | None) -> QueryResult:
    s = get_settings()
    try:
        conn = engine(url or s.sqlalchemy_url).connect()
    except Exception as e:
        # Driver text names the host and user: keep it out of state, traces and the debugger.
        name = type(getattr(e, "orig", e)).__name__
        raise SqlExecutionError(f"could not connect to the data source ({name})") from None
    with conn:
        try:
            # READ ONLY must be the first statement of the transaction.
            conn.exec_driver_sql("SET TRANSACTION READ ONLY", execution_options=_NO_PARAMS)
            conn.exec_driver_sql(
                f"SET LOCAL statement_timeout = {int(s.sql_timeout_seconds) * 1000}",
                execution_options=_NO_PARAMS,
            )
            conn.exec_driver_sql(
                _search_path(schema or s.data_schema), execution_options=_NO_PARAMS
            )
            if role:
                conn.exec_driver_sql(_set_role(role), execution_options=_NO_PARAMS)
            cost = _plan_cost(
                conn.exec_driver_sql(
                    f"EXPLAIN (FORMAT JSON) {sql}", execution_options=_NO_PARAMS
                ).scalar_one()
            )
            if cost > s.sql_max_cost:
                raise SqlExecutionError(
                    f"Query rejected: estimated cost {cost:,.0f} exceeds the limit "
                    f"{s.sql_max_cost:,.0f}. Filter earlier, aggregate, or select fewer rows."
                )
            result = conn.exec_driver_sql(sql, execution_options=_NO_PARAMS)
            columns = list(result.keys())
            fetched = result.fetchmany(s.sql_row_cap + 1)
        except SqlExecutionError:
            raise
        except Exception as e:  # driver errors: syntax, missing column, timeout, permissions
            raise SqlExecutionError(_safe_error(e)) from e
        finally:
            conn.rollback()
    truncated = len(fetched) > s.sql_row_cap
    rows = [[_json_safe(v) for v in row] for row in fetched[: s.sql_row_cap]]
    return QueryResult(columns=columns, rows=rows, row_count=len(rows), truncated=truncated)


async def execute_readonly(
    sql: str, url: str | None = None, schema: str | None = None, role: str | None = None
) -> QueryResult:
    """Run `sql` in a read-only transaction off the event loop. Raises SqlExecutionError.

    `url` (SQLAlchemy form) and `schema` select the data source; default: the configured one.
    `role` narrows privileges for the transaction (an organisation's CSV reader role).
    """
    return await asyncio.to_thread(_run, sql, url, schema, role)
