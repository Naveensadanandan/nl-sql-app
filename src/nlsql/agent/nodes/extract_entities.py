"""extract_entities node (fast model): pull db values, business terms and metrics from the question.

Output feeds two parallel lookups (see graph.py):
- db_value entities                 -> resolve_values    (Elasticsearch)
- business_term / metric entities   -> retrieve_glossary (Qdrant)
"""

from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from nlsql.agent.prompting import block, load_prompt
from nlsql.agent.schemas import EntityExtraction
from nlsql.agent.state import AgentState
from nlsql.core.llm import fast_llm


def _extract_llm() -> Any:
    return fast_llm(EntityExtraction)


async def extract_entities(state: AgentState) -> dict[str, Any]:
    result: EntityExtraction = await _extract_llm().ainvoke(
        [
            SystemMessage(content=load_prompt("extract_entities")),
            HumanMessage(content=block("question", state["question"])),
        ]
    )
    return {"entities": result.entities}
