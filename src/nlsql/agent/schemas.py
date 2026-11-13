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
    """Structured output of the extract_entities node."""

    entities: list[Entity] = Field(default_factory=list)


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
