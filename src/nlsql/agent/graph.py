from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from nlsql.agent.nodes.analyze_ambiguity import analyze_ambiguity
from nlsql.agent.nodes.fetch_schema import fetch_schema
from nlsql.agent.nodes.guard import guard, reject, route_after_guard
from nlsql.agent.state import AgentState


def _build(after_analysis: str) -> CompiledStateGraph:
    """guard -> analyze_ambiguity -> <after_analysis>, with rejects ending early."""
    builder = StateGraph(AgentState)
    builder.add_node("guard", guard)
    builder.add_node("reject", reject)
    builder.add_node("analyze_ambiguity", analyze_ambiguity)

    builder.add_edge(START, "guard")
    builder.add_conditional_edges(
        "guard",
        route_after_guard,
        {"proceed": "analyze_ambiguity", "reject": "reject"},
    )
    builder.add_edge("reject", END)

    if after_analysis == "fetch_schema":
        builder.add_node("fetch_schema", fetch_schema)
        builder.add_edge("analyze_ambiguity", "fetch_schema")
        builder.add_edge("fetch_schema", END)
    else:
        builder.add_edge("analyze_ambiguity", END)
    return builder.compile()


# Main agent: guard -> analyze_ambiguity -> fetch_schema (retrieval/SQL nodes come later).
graph = _build("fetch_schema")

# Analysis only, no database access. Backs POST /analyze-ambiguity.
ambiguity_graph = _build("end")
