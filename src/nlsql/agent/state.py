from typing_extensions import TypedDict

from nlsql.agent.schemas import (
    AmbiguityReport,
    Entity,
    GlossaryHit,
    GuardDecision,
    ValueMatch,
)


class AgentState(TypedDict, total=False):
    question: str
    organisation_id: str  # tenant key: ES index + mandatory Qdrant filter
    user_id: str  # lets Qdrant return this user's private definitions too
    business_context: str  # user-supplied description of the business / data definitions
    guard: GuardDecision
    ambiguity: AmbiguityReport
    schema: dict
    entities: list[Entity]
    resolved_values: list[ValueMatch]
    glossary_hits: list[GlossaryHit]
    answer: str
