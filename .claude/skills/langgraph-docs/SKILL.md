---
name: langgraph-docs
description: Fetch current LangGraph / LangChain / LangSmith API docs through the Context7 MCP server. Use before writing or changing graph, node, state, reducer, checkpointer, interrupt/Command, streaming, tool-calling, structured-output, or LangSmith evaluation code in this repo.
---

# LangGraph docs via Context7

These libraries change quickly, so don't write against remembered APIs. Check them first.

1. Resolve the library ID with `mcp__context7__resolve-library-id`. Use the Python library,
   not the JS one:
   - `langgraph`: graphs, `StateGraph`, `interrupt`, `Command`, `PostgresSaver`, streaming
   - `langchain`: chat models, `with_structured_output`, messages, retrievers
   - `langchain-anthropic`: `ChatAnthropic` options
   - `langsmith`: `evaluate`, datasets, evaluators, feedback
   - `qdrant-client`, `elasticsearch-py`, `sqlglot` when touching retrieval/validation code
2. Fetch docs with the Context7 docs tool, using a narrow `topic`. For example: `interrupt resume Command`,
   `PostgresSaver async setup`, `astream stream_mode messages`, `Send fan-out parallel nodes`,
   `langsmith evaluate summary_evaluators`.
3. Write the code from what the docs say. If they conflict with existing code in `src/nlsql/agent/`,
   point out the conflict instead of quietly mixing both styles.
4. If Context7 is unavailable (the server isn't connected or the call is rate-limited), say so and fall back to the
   official docs at https://langchain-ai.github.io/langgraph/ (WebFetch). Don't guess.

Repo-specific reminders:
- Graph entry point: `src/nlsql/agent/graph.py`. `langgraph.json` exposes it as `nlsql`.
- Use the async checkpointer (`AsyncPostgresSaver`). Thread IDs come from the API layer.
- Every LLM call uses structured output with a Pydantic model from `src/nlsql/agent/schemas.py`.
