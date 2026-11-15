"""The one place LLM clients are built (CLAUDE.md: model IDs come only from core/config.py).

    fast_llm(Schema)    routing, entity extraction  (settings.fast_model)
    strong_llm(Schema)  SQL generation, repair, drafting definitions  (settings.strong_model)

Every client gets the configured timeout and retry budget, and always returns the given
Pydantic schema (structured output).
"""

from functools import lru_cache
from typing import Any

from langchain_openrouter import ChatOpenRouter
from pydantic import BaseModel

from nlsql.core.config import get_settings


@lru_cache
def _chat(model: str) -> ChatOpenRouter:
    s = get_settings()
    return ChatOpenRouter(
        model=model,
        temperature=0,
        api_key=s.openrouter_api_key,
        timeout=s.llm_timeout_seconds * 1000,  # ChatOpenRouter takes milliseconds
        max_retries=s.llm_max_retries,
    )


@lru_cache
def fast_llm(output: type[BaseModel]) -> Any:
    return _chat(get_settings().fast_model).with_structured_output(output)


@lru_cache
def strong_llm(output: type[BaseModel]) -> Any:
    return _chat(get_settings().strong_model).with_structured_output(output)
