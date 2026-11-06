import pytest

from nlsql.agent.graph import graph
from nlsql.agent.nodes import analyze_ambiguity as amb_module
from nlsql.agent.nodes import guard as guard_module
from nlsql.agent.nodes.guard import reject, route_after_guard
from nlsql.agent.schemas import AmbiguityReport, GuardDecision


class _FakeAmbiguity:
    async def ainvoke(self, _messages):
        return AmbiguityReport()


def _state(category: str) -> dict:
    return {"question": "q", "guard": GuardDecision(category=category, reason="r")}


def test_route_after_guard_proceeds_only_for_data_questions():
    assert route_after_guard(_state("data_question")) == "proceed"
    for category in ("off_topic", "unsafe", "unclear"):
        assert route_after_guard(_state(category)) == "reject"


def test_reject_sets_answer():
    assert reject(_state("unsafe"))["answer"]


class _FakeLLM:
    def __init__(self, category: str):
        self.category = category

    async def ainvoke(self, _messages):
        return GuardDecision(category=self.category, reason="fake")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("category", "expects_answer"),
    [("data_question", False), ("off_topic", True), ("unsafe", True), ("unclear", True)],
)
async def test_graph_filters(monkeypatch, category, expects_answer):
    monkeypatch.setattr(guard_module, "_guard_llm", lambda: _FakeLLM(category))
    monkeypatch.setattr(amb_module, "_ambiguity_llm", lambda: _FakeAmbiguity())
    result = await graph.ainvoke({"question": "anything"})
    assert result["guard"].category == category
    assert ("answer" in result) is expects_answer
