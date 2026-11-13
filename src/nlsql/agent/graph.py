from langgraph.graph import END, START, StateGraph

from nlsql.agent.nodes.analyze_ambiguity import analyze_ambiguity
from nlsql.agent.nodes.extract_entities import extract_entities
from nlsql.agent.nodes.fetch_schema import fetch_schema
from nlsql.agent.nodes.guard import guard, reject, route_after_guard
from nlsql.agent.nodes.retrieve_knowledge import resolve_values, retrieve_glossary
from nlsql.agent.state import AgentState

builder = StateGraph(AgentState)
builder.add_node("guard", guard)
builder.add_node("reject", reject)
builder.add_node("analyze_ambiguity", analyze_ambiguity)
builder.add_node("fetch_schema", fetch_schema)
builder.add_node("extract_entities", extract_entities)
builder.add_node("resolve_values", resolve_values)
builder.add_node("retrieve_glossary", retrieve_glossary)

builder.add_edge(START, "guard")
builder.add_conditional_edges(
    "guard",
    route_after_guard,
    {"proceed": "analyze_ambiguity", "reject": "reject"},
)
builder.add_edge("reject", END)

# Fan-out: schema fetch runs in parallel with entity extraction + lookups.
#   analyze_ambiguity -+- fetch_schema -----------------------------> END
#                      +- extract_entities -+- resolve_values -----> END  (ES)
#                                           +- retrieve_glossary --> END  (Qdrant)
# Each branch writes its own state key, so no reducer is needed. When generate_sql
# exists, replace the END edges with a join:
#   add_edge(["fetch_schema", "resolve_values", "retrieve_glossary"], "generate_sql")
builder.add_edge("analyze_ambiguity", "fetch_schema")
builder.add_edge("analyze_ambiguity", "extract_entities")
builder.add_edge("extract_entities", "resolve_values")
builder.add_edge("extract_entities", "retrieve_glossary")
builder.add_edge("fetch_schema", END)
builder.add_edge("resolve_values", END)
builder.add_edge("retrieve_glossary", END)

graph = builder.compile()
