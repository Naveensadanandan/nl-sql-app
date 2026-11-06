from typing_extensions import TypedDict

from nlsql.agent.schemas import AmbiguityReport, GuardDecision


class AgentState(TypedDict, total=False):
    question: str
    business_context: str  # user-supplied description of the business / data definitions
    guard: GuardDecision
    ambiguity: AmbiguityReport
    schema: dict
    answer: str
