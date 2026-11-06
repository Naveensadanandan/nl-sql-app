from fastapi import APIRouter
from pydantic import BaseModel, Field

from nlsql.agent.graph import ambiguity_graph
from nlsql.agent.schemas import GuardCategory, TermToDefine

router = APIRouter()


class AmbiguityRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    business_context: str | None = Field(
        default=None,
        max_length=8000,
        description="Your business and data definitions, so the LLM knows your domain.",
    )


class AmbiguityResponse(BaseModel):
    question: str
    category: GuardCategory
    is_ambiguous: bool
    terms_to_define: list[TermToDefine] = []
    defined_by_context: list[str] = []
    answer: str | None = None  # set when the guard rejected the question


@router.post("/analyze-ambiguity", response_model=AmbiguityResponse)
async def analyze_ambiguity_endpoint(body: AmbiguityRequest) -> AmbiguityResponse:
    result = await ambiguity_graph.ainvoke(
        {"question": body.question, "business_context": body.business_context or ""}
    )
    report = result.get("ambiguity")
    return AmbiguityResponse(
        question=body.question,
        category=result["guard"].category,
        is_ambiguous=report.is_ambiguous if report else False,
        terms_to_define=report.terms if report else [],
        defined_by_context=report.defined_by_context if report else [],
        answer=result.get("answer"),
    )
