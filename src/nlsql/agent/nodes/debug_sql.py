"""debug_sql node (strong model): repair SQL that failed validation or execution.

The loop lives in graph.py: validate_sql / execute_sql -> debug_sql -> validate_sql, at most
`settings.sql_max_debug_attempts` (3) times. Each failure (sql, error, diagnosis) is kept in
`sql_history`, so the model never retries a query it already saw fail and the answer can show
every repair.
"""

from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from nlsql.agent.grounding import grounding_context, question_block
from nlsql.agent.prompting import block, line, load_prompt
from nlsql.agent.schemas import SqlFix
from nlsql.agent.state import AgentState
from nlsql.core.llm import strong_llm


async def debug_sql(state: AgentState) -> dict[str, Any]:
    failed, error = state["sql"], state.get("sql_error") or "unknown error"
    earlier = "\n".join(
        f"- sql: {line(h['sql'])}\n  error: {line(h['error'])}"
        for h in state.get("sql_history", [])
    )
    gen = state.get("generation")
    human = (
        f"{grounding_context(state)}\n"
        f"{question_block(state)}\n"
        f"{block('failed_sql', failed)}\n"
        f"{block('error', error)}\n"
        f"<earlier_attempts>\n{earlier or '(none)'}\n</earlier_attempts>\n"
        f"<current_assumptions>\n"
        f"{chr(10).join(f'- {line(a)}' for a in (gen.assumptions if gen else [])) or '(none)'}\n"
        f"</current_assumptions>"
    )
    fix: SqlFix = await strong_llm(SqlFix).ainvoke(
        [SystemMessage(content=load_prompt("debug_sql")), HumanMessage(content=human)]
    )
    update: dict[str, Any] = {
        "sql": fix.sql.strip(),
        "sql_error": None,
        "debug_attempts": state.get("debug_attempts", 0) + 1,
        "sql_history": [
            *state.get("sql_history", []),
            {"sql": failed, "error": error, "diagnosis": fix.diagnosis},
        ],
    }
    if gen is not None:  # keep the explanation in step with the SQL that will actually run
        update["generation"] = gen.model_copy(
            update={"sql": fix.sql.strip(), "assumptions": fix.assumptions or gen.assumptions}
        )
    return update
