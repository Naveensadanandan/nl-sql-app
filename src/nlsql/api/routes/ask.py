from fastapi import APIRouter
from pydantic import BaseModel, Field

from nlsql.agent.graph import graph
from nlsql.agent.schemas import GuardCategory

router = APIRouter()


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


class AskResponse(BaseModel):
    question: str
    category: GuardCategory
    reason: str
    answer: str | None = None


@router.post("/ask", response_model=AskResponse)
async def ask(body: AskRequest) -> AskResponse:
    result = await graph.ainvoke({"question": body.question})
    decision = result["guard"]
    return AskResponse(
        question=body.question,
        category=decision.category,
        reason=decision.reason,
        answer=result.get("answer"),
    )
