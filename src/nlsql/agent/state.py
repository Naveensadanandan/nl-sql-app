from typing_extensions import TypedDict

from nlsql.agent.schemas import GuardDecision


class AgentState(TypedDict, total=False):
    question: str
    guard: GuardDecision
    schema: dict
    answer: str
