from langgraph.graph import END, START, StateGraph

from nlsql.agent.nodes.guard import guard, reject, route_after_guard
from nlsql.agent.state import AgentState

builder = StateGraph(AgentState)
builder.add_node("guard", guard)
builder.add_node("reject", reject)

builder.add_edge(START, "guard")
builder.add_conditional_edges(
    "guard",
    route_after_guard,
    # "proceed" ends the graph for now; it will point at the next node (retrieval) later.
    {"proceed": END, "reject": "reject"},
)
builder.add_edge("reject", END)

graph = builder.compile()
