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
