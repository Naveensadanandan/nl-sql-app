"""generate_sql node (strong model): schema + definitions + resolved values -> SQL.

The fact blocks come from agent/grounding.py, shared with debug_sql so both see the same facts.
"""

from typing import Any, Literal

from langchain_core.messages import HumanMessage, SystemMessage

from nlsql.agent.grounding import grounding_context, question_block
from nlsql.agent.prompting import load_prompt
from nlsql.agent.schemas import SqlGeneration
from nlsql.agent.state import AgentState
from nlsql.core.llm import strong_llm


async def generate_sql(state: AgentState) -> dict[str, Any]:
    human = f"{grounding_context(state)}\n{question_block(state)}"
    gen: SqlGeneration = await strong_llm(SqlGeneration).ainvoke(
        [SystemMessage(content=load_prompt("generate_sql")), HumanMessage(content=human)]
    )
    usable = bool(gen.sql and gen.sql.strip()) and not gen.missing_definitions
    return {
        "generation": gen,
        "sql": gen.sql.strip() if usable and gen.sql else "",  # "" clears any earlier turn's SQL
        "sql_error": None,
        "debug_attempts": 0,
        "sql_history": [],
    }


def route_after_generate(state: AgentState) -> Literal["validate", "respond"]:
    """No SQL means the model asked for a definition (or gave up): skip straight to the reply."""
    return "validate" if state.get("sql") else "respond"
