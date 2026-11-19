from typing import Literal

from pydantic import BaseModel, Field

GuardCategory = Literal["data_question", "off_topic", "unsafe", "unclear"]


class GuardDecision(BaseModel):
    """Structured output of the guard node."""

    category: GuardCategory = Field(
        description=(
            "data_question: asks something answerable by querying tabular data. "
            "off_topic: unrelated to data (chit-chat, general knowledge, coding help). "
            "unsafe: asks to modify/delete data, change schema, or extract credentials. "
            "unclear: too vague or incomplete to tell what data is being asked for."
        )
    )
    reason: str = Field(description="One short sentence explaining the category.")


# --- Entity extraction + retrieval (system-design §4.1, §4.2, §5.2) -------------------------

EntityKind = Literal["db_value", "business_term", "metric", "time_period", "vague_quantifier"]
VAGUE_KINDS = ("time_period", "vague_quantifier")  # no lookup; generate_sql states a reading
GlossarySection = Literal["metric", "business_definition"]
MatchStatus = Literal["resolved", "ambiguous", "not_found", "skipped"]


class Entity(BaseModel):
    """A span of the question that must be grounded before SQL is written."""

    text: str = Field(description="The phrase exactly as the user wrote it.")
    kind: EntityKind = Field(
        description=(
            "db_value: a literal that should match a stored value in a text column "
            "('5l coconut oil', 'Kerala', 'Acme Ltd'). "
            "business_term: domain jargon or a coined label ('best customers', 'super routes'). "
            "metric: a named measure whose formula lives in the glossary ('profit', 'AOV'). "
            "time_period: a vague period with no explicit dates ('recently', 'last season'). "
            "vague_quantifier: a cutoff with no number ('high', 'many', 'top' without a count)."
        )
    )
    column_hint: str | None = Field(
        default=None,
        description="For db_value only: what kind of thing it is ('product', 'region'), if clear.",
    )


class EntityExtraction(BaseModel):
    """Structured output of the extract_entities node: ALL entities found, of any mix of kinds."""

    entities: list[Entity] = Field(
        default_factory=list,
        description=(
            "Every phrase in the question that needs a lookup or a stated reading, one item per "
            "phrase. A question "
            "can mix kinds, e.g. 'total revenue for electronic item' gives two items: "
            "'revenue' (metric) and 'electronic item' (db_value). Do not stop after the first. "
            "Empty list if nothing needs a lookup."
        ),
    )


class IndexedItem(BaseModel):
    """Common shape of anything we index for lookup (ES value docs, Qdrant glossary points)."""

    id: str
    name: str  # for a value: the literal as stored in the DB; for a term: its canonical name
    description: str = ""
    synonyms: list[str] = Field(default_factory=list)
    score: float = 0.0


class ValueItem(IndexedItem):
    """An Elasticsearch hit: one distinct value of one column."""

    table: str | None = None
    column: str | None = None


class ValueMatch(BaseModel):
    """Result of resolving one db_value entity against the value index."""

    text: str  # the phrase from the question
    status: MatchStatus  # skipped = value index not configured / no organisation
    match: ValueItem | None = None  # set only when status == "resolved"
    candidates: list[ValueItem] = Field(default_factory=list)  # options to show when ambiguous
    # lexical = BM25 on spelling; semantic = embedding fallback (a guess: shown as assumption)
    method: Literal["lexical", "semantic"] = "lexical"


class GlossaryHit(IndexedItem):
    """A Qdrant hit from the glossary collection."""

    query: str  # the entity text that retrieved this hit
    section: GlossarySection
    # approved = confirmed by a user/admin. proposed = drafted by define_terms on an earlier
    # question and saved so every later question uses the same reading until a user edits it.
    status: Literal["approved", "proposed"] = "approved"
    sql_expression: str = ""  # set for drafted definitions; optional for uploaded ones


# --- SQL generation + debugging (system-design §5.2) ----------------------------------------


class SqlGeneration(BaseModel):
    """Structured output of the generate_sql node."""

    sql: str | None = Field(
        default=None,
        description=(
            "One read-only PostgreSQL SELECT (or WITH ... SELECT) statement. Null only when "
            "`missing_definitions` is non-empty."
        ),
    )
    used_terms: list[str] = Field(
        default_factory=list, description="Names of the provided definitions the SQL applies."
    )
    used_values: list[str] = Field(
        default_factory=list, description="Resolved stored values the SQL filters on, as stored."
    )
    assumptions: list[str] = Field(
        default_factory=list,
        description="Anything guessed that the user did not state and no definition covers.",
    )
    missing_definitions: list[str] = Field(
        default_factory=list,
        description=(
            "Business terms from <undefined_terms> that cannot be answered without a definition. "
            "If any are listed, leave `sql` null."
        ),
    )


class ProposedDefinition(BaseModel):
    """A definition drafted from the table schema for a term with no glossary entry."""

    term: str = Field(description="The term exactly as listed in <undefined_terms>.")
    feasible: bool = Field(
        description=(
            "True only if the schema has the columns needed to compute or identify the term. "
            "False if it would need data that is not in the schema (then give no expression)."
        )
    )
    definition: str = Field(
        default="",
        description="Plain-language definition with the formula or rule. Empty if infeasible.",
    )
    sql_expression: str = Field(
        default="",
        description=(
            "A SQL fragment (expression or predicate) over schema columns. Empty if infeasible."
        ),
    )
    columns_used: list[str] = Field(
        default_factory=list,
        description="Every column relied on, as 'table.column', exactly as in <schema>.",
    )
    reasoning: str = Field(default="", description="One short sentence on why this reading fits.")
    status: Literal["proposed"] = "proposed"  # becomes approved only after user confirmation


class DefinitionDrafts(BaseModel):
    """Structured output of the define_terms node."""

    definitions: list[ProposedDefinition] = Field(
        default_factory=list, description="One item per term in <undefined_terms>."
    )


class SqlFix(BaseModel):
    """Structured output of the debug_sql node."""

    diagnosis: str = Field(description="One or two sentences: what caused the error.")
    sql: str = Field(description="The corrected single read-only PostgreSQL statement.")
    assumptions: list[str] = Field(
        default_factory=list,
        description=(
            "The complete assumptions list for the corrected query (carry over the earlier ones "
            "that still hold, add any new guess the fix introduced)."
        ),
    )


# --- Interpretation: how the question was read (stored in query_log, shown by the frontend) --

Outcome = Literal["success", "needs_definition", "no_sql", "failed", "rejected"]


class DefinitionUse(BaseModel):
    """One definition the SQL writer was given, and whether it applied it."""

    term: str
    definition: str
    sql_expression: str = ""
    status: Literal["approved", "proposed"]
    origin: Literal["glossary", "drafted_now"]  # drafted_now = define_terms on this question
    used: bool  # listed in the generator's used_terms
    score: float | None = None  # glossary similarity
    glossary_id: str | None = None


class ValueMapping(BaseModel):
    """How a phrase from the question was matched to a stored value."""

    phrase: str
    status: MatchStatus
    table: str | None = None
    column: str | None = None
    value: str | None = None  # the stored literal used in SQL (resolved only)
    alternatives: list[str] = Field(default_factory=list)  # "table.column = 'value'"
    method: Literal["lexical", "semantic"] = "lexical"  # how the match was found


class Repair(BaseModel):
    """One debug_sql round: the SQL that failed, why, and the model's diagnosis."""

    sql: str
    error: str
    diagnosis: str = ""


class Interpretation(BaseModel):
    """Deterministic (no LLM) record of how the question was answered. Built by respond."""

    outcome: Outcome
    sql: str | None = None
    definitions: list[DefinitionUse] = Field(default_factory=list)
    values: list[ValueMapping] = Field(default_factory=list)
    unclear_terms: list[str] = Field(default_factory=list)  # vague periods/cutoffs given a reading
    assumptions: list[str] = Field(default_factory=list)
    missing_definitions: list[str] = Field(default_factory=list)
    repairs: list[Repair] = Field(default_factory=list)
    # used_terms the generator claimed but that match no definition it was given. Should stay
    # empty; anything here is a grounding violation worth reviewing.
    ungrounded_terms: list[str] = Field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
