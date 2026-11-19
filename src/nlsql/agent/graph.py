from typing import Any

from langgraph.graph import END, START, StateGraph

from nlsql.agent.node_log import logged
from nlsql.agent.nodes.debug_sql import debug_sql
from nlsql.agent.nodes.define_terms import define_terms
from nlsql.agent.nodes.extract_entities import extract_entities
from nlsql.agent.nodes.fetch_schema import fetch_schema
from nlsql.agent.nodes.generate_sql import generate_sql, route_after_generate
from nlsql.agent.nodes.guard import guard, reject, route_after_guard
from nlsql.agent.nodes.remember_definitions import remember_definitions
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


def gather(state: AgentState) -> dict[str, Any]:
    """Join point for the parallel start; routing on the guard decision happens after it."""
    return {}


builder = StateGraph(AgentState)
builder.add_node("guard", logged("guard", guard))
builder.add_node("reject", logged("reject", reject))
builder.add_node("fetch_schema", logged("fetch_schema", fetch_schema))
builder.add_node("extract_entities", logged("extract_entities", extract_entities))
builder.add_node("resolve_values", logged("resolve_values", resolve_values))
builder.add_node("retrieve_glossary", logged("retrieve_glossary", retrieve_glossary))
builder.add_node("gather", logged("gather", gather))
builder.add_node("define_terms", logged("define_terms", define_terms))
builder.add_node("generate_sql", logged("generate_sql", generate_sql))
builder.add_node("validate_sql", logged("validate_sql", validate_sql))
builder.add_node("execute_sql", logged("execute_sql", execute_sql))
builder.add_node("debug_sql", logged("debug_sql", debug_sql))
builder.add_node("respond", logged("respond", respond))
builder.add_node("remember_definitions", logged("remember_definitions", remember_definitions))
builder.add_node("save_result", logged("save_result", save_result))

# Parallel start (one superstep): the guard no longer delays the other LLM calls.
#   START -+- guard ---------------------------------------------+
#          +- fetch_schema (cached) -----------------------------+--> gather -+- reject
#          +- extract_entities -+- resolve_values (ES) ----------+            +- define_terms
#                               +- retrieve_glossary (Qdrant) ---+
# The list-form edge runs gather once, after all four have finished. Every branch always runs,
# so the join cannot stall. A rejected question costs the extra extraction tokens; nothing it
# produces is used, and no SQL is generated for it.
# extract_entities also finds vague periods / cutoffs ("recently", "high"); they reach
# generate_sql as <unclear_terms> (agent/grounding.py).
for first in ("guard", "fetch_schema", "extract_entities"):
    builder.add_edge(START, first)
builder.add_edge("extract_entities", "resolve_values")
builder.add_edge("extract_entities", "retrieve_glossary")
builder.add_edge(["guard", "fetch_schema", "resolve_values", "retrieve_glossary"], "gather")
builder.add_conditional_edges(
    "gather",
    route_after_guard,
    {"proceed": "define_terms", "reject": "reject"},
)
builder.add_edge("reject", "save_result")

# define_terms drafts schema-based definitions for terms the glossary missed (no-op otherwise),
# then the validate/execute/debug loop (max sql_max_debug_attempts debug runs):
#   generate_sql -> validate_sql -> execute_sql -> respond
#                       |               |
#                       +--> debug_sql <+   (debug_sql -> validate_sql)
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

# respond -> save_result (log row now, rows in background) and remember_definitions (saves
# this answer's drafted definitions as proposed, in background), in parallel.
builder.add_edge("respond", "save_result")
builder.add_edge("respond", "remember_definitions")
builder.add_edge("save_result", END)
builder.add_edge("remember_definitions", END)

graph = builder.compile()
