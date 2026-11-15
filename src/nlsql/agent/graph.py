from langgraph.graph import END, START, StateGraph

from nlsql.agent.nodes.analyze_ambiguity import analyze_ambiguity
from nlsql.agent.nodes.debug_sql import debug_sql
from nlsql.agent.nodes.define_terms import define_terms
from nlsql.agent.nodes.extract_entities import extract_entities
from nlsql.agent.nodes.fetch_schema import fetch_schema
from nlsql.agent.nodes.generate_sql import generate_sql, route_after_generate
from nlsql.agent.nodes.guard import guard, reject, route_after_guard
from nlsql.agent.nodes.respond import respond
from nlsql.agent.nodes.retrieve_knowledge import resolve_values, retrieve_glossary
from nlsql.agent.nodes.run_sql import (
    execute_sql,
    route_after_execute,
    route_after_validate,
    validate_sql,
)
from nlsql.agent.nodes.save_result import save_result
from nlsql.agent.state import AgentState

builder = StateGraph(AgentState)
builder.add_node("guard", guard)
builder.add_node("reject", reject)
builder.add_node("analyze_ambiguity", analyze_ambiguity)
builder.add_node("fetch_schema", fetch_schema)
builder.add_node("extract_entities", extract_entities)
builder.add_node("resolve_values", resolve_values)
builder.add_node("retrieve_glossary", retrieve_glossary)
builder.add_node("define_terms", define_terms)
builder.add_node("generate_sql", generate_sql)
builder.add_node("validate_sql", validate_sql)
builder.add_node("execute_sql", execute_sql)
builder.add_node("debug_sql", debug_sql)
builder.add_node("respond", respond)
builder.add_node("save_result", save_result)

builder.add_edge(START, "guard")
builder.add_conditional_edges(
    "guard",
    route_after_guard,
    {"proceed": "analyze_ambiguity", "reject": "reject"},
)
builder.add_edge("reject", "save_result")

# Fan-out: schema fetch runs in parallel with entity extraction + lookups.
#   analyze_ambiguity -+- fetch_schema -----------------------------> END
#                      +- extract_entities -+- resolve_values -----> END  (ES)
#                                           +- retrieve_glossary --> END  (Qdrant)
# Each branch writes its own state key, so no reducer is needed. The three branches join
# into generate_sql, which then enters the validate/execute/debug loop (max 3 debug runs).
#   generate_sql -> validate_sql -> execute_sql -> respond
#                       |               |
#                       +--> debug_sql <+   (debug_sql -> validate_sql)
builder.add_edge("analyze_ambiguity", "fetch_schema")
builder.add_edge("analyze_ambiguity", "extract_entities")
builder.add_edge("extract_entities", "resolve_values")
builder.add_edge("extract_entities", "retrieve_glossary")
builder.add_edge(["fetch_schema", "resolve_values", "retrieve_glossary"], "define_terms")
# define_terms drafts schema-based definitions for terms the glossary missed (no-op otherwise).
builder.add_edge("define_terms", "generate_sql")

builder.add_conditional_edges(
    "generate_sql",
    route_after_generate,
    {"validate": "validate_sql", "respond": "respond"},
)
builder.add_conditional_edges(
    "validate_sql",
    route_after_validate,
    {"execute": "execute_sql", "debug": "debug_sql", "respond": "respond"},
)
builder.add_conditional_edges(
    "execute_sql",
    route_after_execute,
    {"debug": "debug_sql", "respond": "respond"},
)
builder.add_edge("debug_sql", "validate_sql")
builder.add_edge("respond", "save_result")
builder.add_edge("save_result", END)

graph = builder.compile()
