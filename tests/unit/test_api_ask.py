import pytest
from fastapi.testclient import TestClient

from nlsql.agent.nodes import extract_entities as ext_module
from nlsql.agent.nodes import fetch_schema as schema_module
from nlsql.agent.nodes import guard as guard_module
from nlsql.agent.schemas import EntityExtraction, GuardDecision
from nlsql.api.deps import current_user
from nlsql.api.main import app
from nlsql.core.accounts import User

app.dependency_overrides[current_user] = lambda: User("u_test", "tester", "acme")
client = TestClient(app)


class _FakeExtract:
    async def ainvoke(self, _messages):
        return EntityExtraction()


@pytest.fixture(autouse=True)
def _fake_llms(monkeypatch):
    monkeypatch.setattr(ext_module, "_extract_llm", lambda: _FakeExtract())
    monkeypatch.setattr(schema_module, "get_table_schema", lambda table, *a, **k: {"table": table})


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


def test_ask_requires_login():
    app.dependency_overrides.pop(current_user)
    try:
        assert client.post("/ask", json={"question": "total sales"}).status_code == 401
    finally:
        app.dependency_overrides[current_user] = lambda: User("u_test", "tester", "acme")
