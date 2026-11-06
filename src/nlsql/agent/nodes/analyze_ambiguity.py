import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openrouter import ChatOpenRouter

from nlsql.agent.schemas import AmbiguityReport
from nlsql.agent.state import AgentState
from nlsql.core.config import get_settings

_PROMPT = (Path(__file__).parent.parent / "prompts" / "analyze_ambiguity.md").read_text(
    encoding="utf-8"
)

_NO_CONTEXT = "(none provided)"


@lru_cache
def _ambiguity_llm():
    api_key = os.environ.get("OPENROUTER_API_KEY")
    llm = ChatOpenRouter(model=get_settings().fast_model, temperature=0, api_key=api_key)
    return llm.with_structured_output(AmbiguityReport)


def _wrap(tag: str, text: str) -> str:
    # The text is untrusted: stop it from closing our delimiter early.
    safe = text.replace(f"</{tag}>", "")
    return f"<{tag}>\n{safe}\n</{tag}>"


async def analyze_ambiguity(state: AgentState) -> dict[str, Any]:
    """Extract terms that need a clear definition before SQL can be generated."""
    context = (state.get("business_context") or "").strip() or _NO_CONTEXT
    report: AmbiguityReport = await _ambiguity_llm().ainvoke(
        [
            SystemMessage(content=_PROMPT),
            HumanMessage(
                content=f"{_wrap('business_context', context)}\n\n"
                f"{_wrap('question', state['question'])}"
            ),
        ]
    )
    return {"ambiguity": report}
