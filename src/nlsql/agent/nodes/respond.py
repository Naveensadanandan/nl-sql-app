"""respond node (no LLM): terminal reply for all three SQL outcomes.

The table itself is in state["result"] and the SQL in state["sql"]; `answer` is the short text.
"""

from typing import Any

from nlsql.agent.state import AgentState


def respond(state: AgentState) -> dict[str, Any]:
    gen = state.get("generation")
    if gen and gen.missing_definitions:
        terms = ", ".join(f'"{t}"' for t in gen.missing_definitions)
        return {
            "answer": (
                f"I don't have a definition for {terms}. "
                "How should I calculate or identify it? Once you tell me, I'll save it."
            )
        }

    result = state.get("result")
    if state.get("sql_error") or result is None:
        attempts = state.get("debug_attempts", 0)
        return {
            "answer": (
                f"I couldn't produce a working query after {attempts} fix attempt(s). "
                f"Last error: {(state.get('sql_error') or 'no result')[:300]} "
                "Try rephrasing the question or naming the columns you mean."
            )
        }

    n = result.row_count
    text = f"Returned {n} row{'' if n == 1 else 's'}."
    if result.truncated:
        text += f" Results are capped at {n} rows; narrow the question to see the rest."
    if gen and gen.assumptions:
        text += " Assumptions: " + "; ".join(gen.assumptions) + "."
    proposed = [d for d in state.get("proposed_definitions", []) if d.feasible]
    if proposed:
        text += (
            " Proposed definitions (unconfirmed): "
            + "; ".join(f"{d.term} = {d.definition}" for d in proposed)
            + "."
        )
    return {"answer": text}
