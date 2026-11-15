"""save_result node (no LLM): persist the question, its SQL and its result rows (query store).

Runs last on every path (reject and respond). A storage failure must never lose the user's
answer, so errors are logged by type only (never rows, SQL parameters or connection strings).
"""

import asyncio
import logging
from typing import Any

from nlsql.agent.state import AgentState
from nlsql.core.query_store import new_question_id, save_query

log = logging.getLogger(__name__)


def _status(state: AgentState) -> str:
    if state["guard"].category != "data_question":
        return "rejected"
    gen = state.get("generation")
    if gen and gen.missing_definitions:
        return "needs_definition"
    if state.get("sql_error") or state.get("result") is None:
        return "failed"
    return "success"


async def save_result(state: AgentState) -> dict[str, Any]:
    question_id = state.get("question_id") or new_question_id()
    gen = state.get("generation")
    record = {
        "question_id": question_id,
        "organisation_id": state.get("organisation_id"),
        "user_id": state.get("user_id"),
        "question": state["question"],
        "category": state["guard"].category,
        "status": _status(state),
        "sql": state.get("sql") or None,
        "error": state.get("sql_error"),
        "debug_attempts": state.get("debug_attempts", 0),
        "assumptions": gen.assumptions if gen else [],
    }
    try:
        await asyncio.to_thread(save_query, record, state.get("result"))
    except Exception as e:  # noqa: BLE001 - never fail the request because logging failed
        log.warning("query store write failed for %s: %s", question_id, type(e).__name__)
    return {"question_id": question_id}
