"""POST /ask runs the agent and returns a short summary plus `question_id`.

The full answer (SQL, rows, how the question was read) is stored by save_result and read with
GET /questions/{question_id}. The frontend polls that endpoint while `rows_status` is 'saving'.
"""

import logging

import anyio
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from nlsql.agent.graph import graph
from nlsql.agent.schemas import (
    Entity,
    GlossaryHit,
    GuardCategory,
    Outcome,
    ValueMatch,
)
from nlsql.agent.state import AgentState, outcome_of
from nlsql.core import query_store
from nlsql.core.tenant import normalize_organisation_id

log = logging.getLogger(__name__)
router = APIRouter()


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    business_context: str | None = Field(
        default=None,
        max_length=8000,
        description="Your business and data definitions, so the LLM knows your domain.",
    )
    # TODO(auth): take these from the authenticated session, not the request body.
    organisation_id: str | None = Field(default=None, max_length=64)
    user_id: str | None = Field(default=None, max_length=64)

    @field_validator("organisation_id")
    @classmethod
    def _normalize_org(cls, v: str | None) -> str | None:
        return normalize_organisation_id(v) if v else None  # lowercase; 422 if invalid


class AskResponse(BaseModel):
    question: str
    question_id: str | None = Field(
        default=None, description="GET /questions/{question_id} for the SQL, rows and reasoning."
    )
    category: GuardCategory
    reason: str
    outcome: Outcome | None = None
    answer: str | None = None
    entities: list[Entity] = []
    resolved_values: list[ValueMatch] = []
    glossary_hits: list[GlossaryHit] = []


@router.post("/ask", response_model=AskResponse)
async def ask(body: AskRequest) -> AskResponse:
    question_id = query_store.new_question_id()  # known up front: the same id a worker would use
    state: AgentState = {
        "question_id": question_id,
        "question": body.question,
        "business_context": body.business_context or "",
    }
    if body.organisation_id:
        state["organisation_id"] = body.organisation_id
    if body.user_id:
        state["user_id"] = body.user_id
    try:
        result = await graph.ainvoke(state)
    except Exception as e:
        # LLM provider down / rate-limited, ES or Qdrant unreachable. Type only: no payloads.
        log.warning("ask %s failed: %s", question_id, type(e).__name__)
        raise HTTPException(503, "The assistant is temporarily unavailable. Try again.") from e
    decision = result["guard"]
    return AskResponse(
        question=body.question,
        question_id=result.get("question_id"),
        category=decision.category,
        reason=decision.reason,
        outcome=outcome_of(result),  # type: ignore[arg-type]  # ainvoke returns the state dict
        answer=result.get("answer"),
        entities=result.get("entities", []),
        resolved_values=result.get("resolved_values", []),
        glossary_hits=result.get("glossary_hits", []),
    )


_PUBLIC_FIELDS = (
    "question_id", "question", "status", "answer", "sql", "error", "interpretation",
    "row_count", "truncated", "rows_status", "result_columns", "rows", "created_at",
)  # fmt: skip


@router.get("/questions/{question_id}")
async def get_question(
    question_id: str,
    organisation_id: str | None = None,
    rows: int = Query(default=200, ge=1, le=1000),
) -> dict[str, object]:
    """The stored answer: SQL, interpretation, and result rows once rows_status == 'saved'.

    TODO(auth): organisation_id must come from the session. Until then a question saved for
    an organisation is only returned when the same organisation_id is passed.
    """
    if not query_store.is_question_id(question_id):
        raise HTTPException(422, "invalid question_id")
    org = None
    if organisation_id:
        try:
            org = normalize_organisation_id(organisation_id)
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
    found = await anyio.to_thread.run_sync(query_store.get_query, question_id, rows)
    if found is None or (found.get("organisation_id") and found["organisation_id"] != org):
        raise HTTPException(404, "unknown question_id")
    return {k: found.get(k) for k in _PUBLIC_FIELDS}
