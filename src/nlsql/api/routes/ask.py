from fastapi import APIRouter
from pydantic import BaseModel, Field, field_validator

from nlsql.agent.graph import graph
from nlsql.agent.schemas import Entity, GlossaryHit, GuardCategory, TermToDefine, ValueMatch
from nlsql.core.tenant import normalize_organisation_id

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
    category: GuardCategory
    reason: str
    answer: str | None = None
    terms_to_define: list[TermToDefine] = []
    entities: list[Entity] = []
    resolved_values: list[ValueMatch] = []
    glossary_hits: list[GlossaryHit] = []


@router.post("/ask", response_model=AskResponse)
async def ask(body: AskRequest) -> AskResponse:
    state: dict[str, str] = {
        "question": body.question,
        "business_context": body.business_context or "",
    }
    if body.organisation_id:
        state["organisation_id"] = body.organisation_id
    if body.user_id:
        state["user_id"] = body.user_id
    result = await graph.ainvoke(state)
    decision = result["guard"]
    report = result.get("ambiguity")
    return AskResponse(
        question=body.question,
        category=decision.category,
        reason=decision.reason,
        answer=result.get("answer"),
        terms_to_define=report.terms if report else [],
        entities=result.get("entities", []),
        resolved_values=result.get("resolved_values", []),
        glossary_hits=result.get("glossary_hits", []),
    )
