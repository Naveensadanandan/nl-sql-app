from typing_extensions import TypedDict

from nlsql.agent.schemas import (
    AmbiguityReport,
    Entity,
    GlossaryHit,
    GuardDecision,
    ProposedDefinition,
    SqlGeneration,
    ValueMatch,
)
from nlsql.sql.executor import QueryResult


class AgentState(TypedDict, total=False):
    question: str
    question_id: str  # q_<uuid hex>; set by save_result. Also the name of the result table
    organisation_id: str  # tenant key: ES index + mandatory Qdrant filter
    user_id: str  # lets Qdrant return this user's private definitions too
    business_context: str  # user-supplied description of the business / data definitions
    guard: GuardDecision
    ambiguity: AmbiguityReport
    schema: dict
    entities: list[Entity]
    resolved_values: list[ValueMatch]
    glossary_hits: list[GlossaryHit]
    proposed_definitions: list[ProposedDefinition]  # define_terms: schema-derived, unconfirmed
    generation: SqlGeneration  # generate_sql output: sql + used terms/values + assumptions
    sql: str  # current candidate; validate_sql replaces it with the normalised, LIMITed form
    sql_error: str | None  # validator / database error awaiting debug_sql; None when clean
    debug_attempts: int  # debug_sql runs so far (cap: settings.sql_max_debug_attempts)
    sql_history: list[dict[str, str]]  # failed {sql, error} pairs, so the debugger doesn't repeat
    result: QueryResult
    answer: str
