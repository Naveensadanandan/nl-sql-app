from fastapi import APIRouter
from pydantic import BaseModel, Field

from nlsql.agent.graph import graph
from nlsql.agent.schemas import GuardCategory, TermToDefine

router = APIRouter()


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    business_context: str | None = Field(
        default=None,
        max_length=8000,
        description="Your business and data definitions, so the LLM knows your domain.",
    )


class AskResponse(BaseModel):
    question: str
    category: GuardCategory
    reason: str
    answer: str | None = None
    terms_to_define: list[TermToDefine] = []


@router.post("/ask", response_model=AskResponse)
async def ask(body: AskRequest) -> AskResponse:
    result = await graph.ainvoke(
        {"question": body.question, "business_context": body.business_context or ""}
    )
    decision = result["guard"]
    report = result.get("ambiguity")
    return AskResponse(
        question=body.question,
        category=decision.category,
        reason=decision.reason,
        answer=result.get("answer"),
        terms_to_define=report.terms if report else [],
    )
