from typing import Literal

from pydantic import BaseModel, Field

GuardCategory = Literal["data_question", "off_topic", "unsafe", "unclear"]
TermKind = Literal["business_term", "metric", "time_period", "vague_quantifier"]


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


class TermToDefine(BaseModel):
    """A phrase in the question that needs a precise definition before SQL can be written."""

    term: str = Field(description="The phrase exactly as the user wrote it.")
    kind: TermKind = Field(
        description=(
            "business_term: domain jargon or a coined label ('best customers', 'super routes'). "
            "metric: a measure whose formula is not obvious ('profit', 'growth'). "
            "time_period: a vague period ('recently', 'last season'). "
            "vague_quantifier: an unspecified cutoff ('top', 'high', 'many')."
        )
    )
    why: str = Field(description="One short sentence on why SQL cannot be written without it.")
    question_to_user: str = Field(
        description="A short, concrete question to ask the user to get the definition."
    )


class AmbiguityReport(BaseModel):
    """Structured output of the analyze_ambiguity node."""

    terms: list[TermToDefine] = Field(
        default_factory=list,
        description="Terms that need a definition. Empty when the question is fully specified.",
    )
    defined_by_context: list[str] = Field(
        default_factory=list,
        description="Terms in the question that the business description already defines.",
    )

    @property
    def is_ambiguous(self) -> bool:
        return bool(self.terms)


# --- Entity extraction + retrieval (system-design §4.1, §4.2, §5.2) -------------------------

EntityKind = Literal["db_value", "business_term", "metric"]
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
            "metric: a named measure whose formula lives in the glossary ('profit', 'AOV')."
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
            "Every phrase in the question that needs a lookup, one item per phrase. A question "
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


class GlossaryHit(IndexedItem):
    """A Qdrant hit from the glossary collection."""

    query: str  # the entity text that retrieved this hit
    section: GlossarySection


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
        description="A SQL fragment (expression or predicate) over schema columns. Empty if infeasible.",
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
