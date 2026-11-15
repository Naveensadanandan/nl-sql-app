"""extract_entities node (fast model): pull db values, business terms and metrics from the question.

Output feeds two parallel lookups (see graph.py):
- db_value entities                 -> resolve_values    (Elasticsearch)
- business_term / metric entities   -> retrieve_glossary (Qdrant)
"""

from functools import lru_cache
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openrouter import ChatOpenRouter

from nlsql.agent.schemas import EntityExtraction
from nlsql.agent.state import AgentState
from nlsql.core.config import get_settings

_PROMPT = (Path(__file__).parent.parent / "prompts" / "extract_entities.md").read_text(
    encoding="utf-8"
)


@lru_cache
def _extract_llm() -> Any:
    s = get_settings()
    llm = ChatOpenRouter(model=s.fast_model, temperature=0, api_key=s.openrouter_api_key)
    return llm.with_structured_output(EntityExtraction)


async def extract_entities(state: AgentState) -> dict[str, Any]:
    question = state["question"].replace("</question>", "")  # untrusted: keep delimiter intact
    result: EntityExtraction = await _extract_llm().ainvoke(
        [
            SystemMessage(content=_PROMPT),
            HumanMessage(content=f"<question>\n{question}\n</question>"),
        ]
    )
    return {"entities": result.entities}
