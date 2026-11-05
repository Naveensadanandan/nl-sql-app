import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import Runnable
from langchain_openrouter import ChatOpenRouter

from nlsql.agent.schemas import GuardDecision
from nlsql.agent.state import AgentState
from nlsql.core.config import get_settings

_PROMPT = (Path(__file__).parent.parent / "prompts" / "guard.md").read_text(encoding="utf-8")

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

_REJECTIONS = {
    "off_topic": (
        "I can only answer questions about your data. "
        "Try asking about the tables you uploaded or connected."
    ),
    "unsafe": (
        "I can only read data, not change it or reveal credentials. "
        "Try rephrasing as a question about the data."
    ),
    "unclear": (
        "I'm not sure what you want to know. "
        "Which data, metric, or time period are you interested in?"
    ),
}

@lru_cache
def _guard_llm():
    api_key = os.environ.get("OPENROUTER_API_KEY")
    llm = ChatOpenRouter(model=get_settings().fast_model, temperature=0, api_key=api_key)
    return llm.with_structured_output(GuardDecision)
 

async def guard(state: AgentState) -> dict[str, Any]:
    """Classify the question so the graph can drop anything that is not a data question."""
    decision: GuardDecision = await _guard_llm().ainvoke(
        [SystemMessage(content=_PROMPT), HumanMessage(content=state["question"])]
    )
    return {"guard": decision}


def route_after_guard(state: AgentState) -> Literal["proceed", "reject"]:
    return "proceed" if state["guard"].category == "data_question" else "reject"


def reject(state: AgentState) -> dict[str, Any]:
    """Terminal reply for filtered questions; no SQL is generated."""
    return {"answer": _REJECTIONS[state["guard"].category]}
