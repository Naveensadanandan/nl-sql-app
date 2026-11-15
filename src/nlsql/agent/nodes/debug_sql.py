"""debug_sql node (strong model): repair SQL that failed validation or execution.

The loop lives in graph.py: validate_sql / execute_sql -> debug_sql -> validate_sql, at most
`settings.sql_max_debug_attempts` (3) times. Each failure is kept in `sql_history` so the model
never retries a query it already saw fail.
"""

from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from nlsql.agent.nodes.generate_sql import clean, grounding_context, strong_llm
from nlsql.agent.schemas import SqlFix
from nlsql.agent.state import AgentState

_PROMPT = (Path(__file__).parent.parent / "prompts" / "debug_sql.md").read_text(encoding="utf-8")


async def debug_sql(state: AgentState) -> dict[str, Any]:
    failed, error = state["sql"], state.get("sql_error") or "unknown error"
    history = [*state.get("sql_history", []), {"sql": failed, "error": error}]
    earlier = "\n".join(
        f"- sql: {clean(h['sql'])}\n  error: {clean(h['error'])}" for h in history[:-1]
    )
    human = (
        f"{grounding_context(state)}\n"
        f"<question>\n{clean(state['question'])}\n</question>\n"
        f"<failed_sql>\n{clean(failed)}\n</failed_sql>\n"
        f"<error>\n{clean(error)}\n</error>\n"
        f"<earlier_attempts>\n{earlier or '(none)'}\n</earlier_attempts>"
    )
    fix: SqlFix = await strong_llm(SqlFix).ainvoke(
        [SystemMessage(content=_PROMPT), HumanMessage(content=human)]
    )
    return {
        "sql": fix.sql.strip(),
        "sql_error": None,
        "debug_attempts": state.get("debug_attempts", 0) + 1,
        "sql_history": history,
    }
