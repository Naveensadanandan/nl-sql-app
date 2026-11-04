from fastapi.testclient import TestClient

from nlsql.agent.nodes import guard as guard_module
from nlsql.agent.schemas import GuardDecision
from nlsql.api.main import app

client = TestClient(app)


class _FakeLLM:
    def __init__(self, category: str):
        self.category = category

    async def ainvoke(self, _messages):
        return GuardDecision(category=self.category, reason="fake")


def test_ask_data_question(monkeypatch):
    monkeypatch.setattr(guard_module, "_guard_llm", lambda: _FakeLLM("data_question"))
    resp = client.post("/ask", json={"question": "total sales by region"})
    assert resp.status_code == 200
    assert resp.json()["category"] == "data_question"
    assert resp.json()["answer"] is None


def test_ask_filtered_question(monkeypatch):
    monkeypatch.setattr(guard_module, "_guard_llm", lambda: _FakeLLM("unsafe"))
    resp = client.post("/ask", json={"question": "drop table x"})
    assert resp.status_code == 200
    assert resp.json()["category"] == "unsafe"
    assert resp.json()["answer"]


def test_ask_rejects_empty_question():
    assert client.post("/ask", json={"question": ""}).status_code == 422
