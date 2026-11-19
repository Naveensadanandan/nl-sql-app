"""validate_sql and execute_sql nodes (no LLM) plus the routers that drive the debug loop.

generate_sql -> validate_sql -+- ok ------> execute_sql -+- ok ------> respond
                    ^         |                 |        |
                    |         +- error -+       |        +- error -+
                    |                   v       |                  v
                    +------------- debug_sql <--+------------------+   (max 3, then respond)
"""

from typing import Any, Literal

from nlsql.agent.state import AgentState
from nlsql.core.config import get_settings
from nlsql.sources.registry import target_for
from nlsql.sql.executor import SqlExecutionError, execute_readonly
from nlsql.sql.validator import allowed_from_schema, validate


def validate_sql(state: AgentState) -> dict[str, Any]:
    s = get_settings()
    target = target_for(state.get("source_id"), state.get("organisation_id"))
    result = validate(
        state["sql"],
        allowed_from_schema(state["schema"]),
        row_cap=s.sql_row_cap,
        schema_name=target.schema_name,
    )
    if result.ok:
        return {"sql": result.sql, "sql_error": None}
    return {"sql_error": result.error}


async def execute_sql(state: AgentState) -> dict[str, Any]:
    target = target_for(state.get("source_id"), state.get("organisation_id"))
    try:
        result = await execute_readonly(
            state["sql"], target.sqlalchemy_url, target.schema_name, target.role
        )
        return {"result": result, "sql_error": None}
    except SqlExecutionError as e:
        return {"sql_error": str(e)}


def _next_on_error(state: AgentState) -> Literal["debug", "respond"]:
    if state.get("debug_attempts", 0) < get_settings().sql_max_debug_attempts:
        return "debug"
    return "respond"  # attempts exhausted; respond reports the last error


def route_after_validate(state: AgentState) -> Literal["execute", "debug", "respond"]:
    return _next_on_error(state) if state.get("sql_error") else "execute"


def route_after_execute(state: AgentState) -> Literal["debug", "respond"]:
    return _next_on_error(state) if state.get("sql_error") else "respond"
