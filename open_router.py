import os
from langgraph.graph import StateGraph, MessagesState, START, END
from langchain_openrouter import ChatOpenRouter
from langchain_core.messages import HumanMessage

try:
  from dotenv import load_dotenv
  load_dotenv()
except ImportError:
  pass

# 1. Initialize the OpenRouter model (e.g., Claude 3.5 Sonnet or a free model)
# ChatOpenRouter looks for OPENROUTER_API_KEY; our env var is OPENROUTER_API
# and includes the "Bearer " prefix, which the client adds itself.
llm = ChatOpenRouter(
    model="xingchen-agi/xing4.0-29b-a4b:free",
    temperature=0.3,
    max_retries=5,  # free models often 429 on the shared upstream pool
    api_key=os.environ["OPENROUTER_API"],
)

# If the primary free model stays rate-limited, fall back to other free models.
llm = ChatOpenRouter(model="nvidia/nemotron-3-ultra-550b-a55b:free", 
                   temperature=0.3, 
                   max_retries=2,
                   api_key=os.environ["OPENROUTER_API"])

# llm = llm.with_fallbacks(_fallbacks)

# 2. Define a simple LangGraph node function
def call_agent(state: MessagesState):
    messages = state["messages"]
    response = llm.invoke(messages)
    return {"messages": [response]}

# 3. Build your LangGraph state workflow
builder = StateGraph(MessagesState)
builder.add_node("agent", call_agent)
builder.add_edge(START, "agent")
builder.add_edge("agent", END)

# Compile the graph
graph = builder.compile()

# 4. Invoke your OpenRouter-powered graph
inputs = {"messages": [HumanMessage(content="Explain LangGraph in one sentence.")]}
final_state = graph.invoke(inputs)

print(final_state["messages"][-1].content)
