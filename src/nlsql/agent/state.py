from typing_extensions import TypedDict

from nlsql.agent.schemas import (
    Entity,
    GlossaryHit,
    GuardDecision,
    Interpretation,
    Outcome,
    ProposedDefinition,
    SqlGeneration,
    ValueMatch,
)
from nlsql.sql.executor import QueryResult


class AgentState(TypedDict, total=False):
    question: str
    question_id: str  # q_<uuid hex>; set by the API (or save_result). Also the result table name
    organisation_id: str  # tenant key: ES index + mandatory Qdrant filter
    user_id: str  # lets Qdrant return this user's private definitions too
    business_context: str  # user-supplied description of the business / data definitions
    guard: GuardDecision
    schema: list[dict]  # type: ignore[type-arg]  # one fetch_schema dict per table
    entities: list[Entity]
    resolved_values: list[ValueMatch]
    glossary_hits: list[GlossaryHit]
    proposed_definitions: list[ProposedDefinition]  # define_terms: schema-derived, unconfirmed
    generation: SqlGeneration  # generate_sql output: sql + used terms/values + assumptions
    sql: str  # current candidate; validate_sql replaces it with the normalised, LIMITed form
    sql_error: str | None  # validator / database error awaiting debug_sql; None when clean
    debug_attempts: int  # debug_sql runs so far (cap: settings.sql_max_debug_attempts)
    sql_history: list[
        dict[str, str]
    ]  # failed {sql, error, diagnosis}, so the debugger doesn't repeat
    result: QueryResult
    answer: str
    interpretation: Interpretation  # respond: how the question was read (stored for the UI)


def outcome_of(state: AgentState) -> Outcome:
    """The single rule for how a run ended. respond and save_result both use it."""
    guard = state.get("guard")
    if guard is None or guard.category != "data_question":
        return "rejected"
    gen = state.get("generation")
    if gen and gen.missing_definitions:
        return "needs_definition"
    if gen is None or not (gen.sql and gen.sql.strip()):
        return "no_sql"
    if state.get("sql_error") or state.get("result") is None:
        return "failed"
    return "success"
