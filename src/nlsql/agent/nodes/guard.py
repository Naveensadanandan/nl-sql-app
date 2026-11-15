from typing import Any, Literal

from langchain_core.messages import HumanMessage, SystemMessage

from nlsql.agent.prompting import block, load_prompt
from nlsql.agent.schemas import GuardDecision
from nlsql.agent.state import AgentState
from nlsql.core.llm import fast_llm

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


def _guard_llm() -> Any:
    return fast_llm(GuardDecision)


async def guard(state: AgentState) -> dict[str, Any]:
    """Classify the question so the graph can drop anything that is not a data question."""
    decision: GuardDecision = await _guard_llm().ainvoke(
        [
            SystemMessage(content=load_prompt("guard")),
            HumanMessage(content=block("question", state["question"])),
        ]
    )
    return {"guard": decision}


def route_after_guard(state: AgentState) -> Literal["proceed", "reject"]:
    return "proceed" if state["guard"].category == "data_question" else "reject"


def reject(state: AgentState) -> dict[str, Any]:
    """Terminal reply for filtered questions; no SQL is generated."""
    return {"answer": _REJECTIONS[state["guard"].category]}
