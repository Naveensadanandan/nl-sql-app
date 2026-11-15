"""Read-only SQL execution (system-design §7, steps 5-6).

Only call this with SQL that has passed `nlsql.sql.validator.validate`. The transaction is
READ ONLY, has a statement timeout, is always rolled back, and an EXPLAIN cost guard runs first.
Never log the rows: they are user data.
"""

import asyncio
import json
import re
from datetime import date, datetime, time
from decimal import Decimal
from functools import lru_cache
from typing import Any

from pydantic import BaseModel
from sqlalchemy import Engine, create_engine

from nlsql.core.config import get_settings

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


@lru_cache
def _engine(url: str) -> Engine:
    return create_engine(url, pool_pre_ping=True)


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
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


def _run(sql: str) -> QueryResult:
    s = get_settings()
    with _engine(s.sqlalchemy_url).connect() as conn:
        try:
            # READ ONLY must be the first statement of the transaction.
            conn.exec_driver_sql("SET TRANSACTION READ ONLY", execution_options=_NO_PARAMS)
            conn.exec_driver_sql(
                f"SET LOCAL statement_timeout = {int(s.sql_timeout_seconds) * 1000}",
                execution_options=_NO_PARAMS,
            )
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


async def execute_readonly(sql: str) -> QueryResult:
    """Run `sql` in a read-only transaction off the event loop. Raises SqlExecutionError."""
    return await asyncio.to_thread(_run, sql)
