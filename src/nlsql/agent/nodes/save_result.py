"""save_result node (no LLM): persist the question, its answer and interpretation (query store).

Runs last on every path (reject and respond). Only the query_log INSERT runs before the
response; the result rows are written in the background (core/background.py), and readers see
rows_status = 'saving' until they land. A storage failure must never lose the user's answer, so
errors are logged by type only (never rows, SQL parameters or connection strings).
"""

import asyncio
import logging
from typing import Any

from nlsql.agent.state import AgentState, outcome_of
from nlsql.core.background import spawn
from nlsql.core.query_store import new_question_id, save_log, save_rows

log = logging.getLogger(__name__)


async def save_result(state: AgentState) -> dict[str, Any]:
    question_id = state.get("question_id") or new_question_id()
    gen = state.get("generation")
    interp = state.get("interpretation")
    result = state.get("result")
    record = {
        "question_id": question_id,
        "organisation_id": state.get("organisation_id"),
        "user_id": state.get("user_id"),
        "question": state["question"],
        "category": state["guard"].category,
        "status": outcome_of(state),
        "sql": state.get("sql") or None,
        "error": state.get("sql_error"),
        "debug_attempts": state.get("debug_attempts", 0),
        "assumptions": gen.assumptions if gen else [],
        "answer": state.get("answer"),
        "interpretation": interp.model_dump(mode="json") if interp else None,
    }
    try:
        rows_follow = await asyncio.to_thread(save_log, record, result)
    except Exception as e:  # noqa: BLE001 - never fail the request because logging failed
        log.warning("query store write failed for %s: %s", question_id, type(e).__name__)
        return {"question_id": question_id}
    if rows_follow and result is not None:
        spawn(asyncio.to_thread(save_rows, question_id, result), name=f"save_rows:{question_id}")
    return {"question_id": question_id}
