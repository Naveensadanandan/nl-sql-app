"""respond node (no LLM): build the Interpretation and the short answer text for every outcome.

The table is in state["result"], the SQL in state["sql"]; `interpretation` says how the question
was read (definitions, value matches, assumptions, repairs) and is stored with the answer.
"""

from typing import Any

from nlsql.agent.grounding import build_interpretation
from nlsql.agent.state import AgentState


def respond(state: AgentState) -> dict[str, Any]:
    interp = build_interpretation(state)

    if interp.outcome == "needs_definition":
        terms = ", ".join(f'"{t}"' for t in interp.missing_definitions)
        text = (
            f"I don't have a definition for {terms}, and the tables don't have the columns to "
            "draft one. How should I calculate or identify it? Once you tell me, I'll save it."
        )
    elif interp.outcome == "no_sql":
        text = (
            "I couldn't write a query for this question. "
            "Try naming the measure, the filter, or the columns you mean."
        )
    elif interp.outcome == "failed":
        text = (
            f"I couldn't produce a working query after {state.get('debug_attempts', 0)} fix "
            f"attempt(s). Last error: {(state.get('sql_error') or 'no result')[:300]} "
            "Try rephrasing the question or naming the columns you mean."
        )
    else:
        n = interp.row_count
        text = f"Returned {n} row{'' if n == 1 else 's'}."
        if interp.truncated:
            text += f" Results are capped at {n} rows; narrow the question to see the rest."
        if interp.assumptions:
            text += " Assumptions: " + "; ".join(interp.assumptions) + "."
        proposed = [
            d
            for d in interp.definitions
            if d.status == "proposed" and (d.used or d.origin == "drafted_now")
        ]
        if proposed:
            text += (
                " Using proposed definitions (edit them in the glossary if they're wrong): "
                + "; ".join(f"{d.term} = {d.definition}" for d in proposed)
                + "."
            )
    return {"answer": text, "interpretation": interp}
