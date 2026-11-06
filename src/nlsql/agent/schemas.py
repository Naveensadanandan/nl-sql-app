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
