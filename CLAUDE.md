# NL-SQL App

## WHY
Business users can't write SQL, and generic text-to-SQL breaks on real data in two ways.
It can't match vague values ("5l coconut oil" vs the stored `5000 ml Coconut Oil`), and it
makes up business terms ("best customers", "super routes"). This MVP answers natural-language
questions over uploaded CSVs or a connected Postgres database. It returns a SQL result table
and shows how it read the question, using only definitions it can trace to a source.

## WHAT
- **Agent**: LangGraph state graph (`src/nlsql/agent/graph.py`) on LangChain, with Claude models through `langchain-anthropic`
- **API**: FastAPI (`src/nlsql/api`), streaming with SSE. **UI**: Streamlit (`ui/`)
- **App DB**: Postgres 16. It holds metadata, the glossary (source of truth), the query log, the LangGraph checkpointer, and the CSV uploads, which go into one `ws_<id>` schema per workspace
- **Value resolution**: Elasticsearch 8. It indexes distinct values of text columns, normalized for units and synonyms
- **Knowledge/RAG**: Qdrant. Collections: `glossary`, `schema_docs`, `query_examples`. Embeddings come from fastembed
- **SQL safety**: `sqlglot` AST validation, read-only transactions, statement timeout, row cap
- **Evals/observability**: LangSmith for traces, datasets, and evaluators (`evals/`), plus structlog and Prometheus
- **Background jobs**: arq + Redis for indexing, glossary learning, and eval runs

Layout: `src/nlsql/{api,agent,retrieval,sources,sql,knowledge,suggestions,core,worker}`,
`evals/`, `tests/{unit,integration}`, `ui/`, `docs/`. The full design is in `docs/system-design.md`,
and the phased roadmap is in `docs/development-plan.md`.

## HOW
```bash
source myenv/Scripts/activate        # PowerShell: myenv\Scripts\Activate.ps1
pip install -e ".[dev]"              # install app + dev tools
cp .env.example .env                 # then fill ANTHROPIC_API_KEY, LANGSMITH_API_KEY
docker compose up -d                 # postgres, elasticsearch, qdrant, redis
uvicorn nlsql.api.main:app --reload  # API on :8000
streamlit run ui/app.py              # UI on :8501
langgraph dev                        # LangGraph Studio for graph debugging (langgraph.json)
```
Verify every change before calling it done:
```bash
ruff check . --fix && ruff format .  # lint + format (style is the linter's job, not this file's)
mypy src                             # type check
pytest -m "not integration"          # fast unit tests, no services needed
pytest -m integration                # needs docker compose up
python -m evals.run --suite smoke    # LangSmith eval suite; required when prompts/nodes change
```

## Conventions (non-style)
- **Read-only always**: every generated SQL statement goes through `nlsql.sql.validator` before
  execution. Never run model output directly, and never add write or DDL paths to user databases.
- **No hallucinated definitions**: business terms come only from glossary hits above the
  similarity threshold. If a term has no definition, the graph asks the user (`interrupt`)
  instead of guessing. New terms enter as `proposed` and become `approved` after confirmation.
- **Postgres is the source of truth** for the glossary and examples. Qdrant and ES are derived
  indexes and must be rebuildable with `python -m nlsql.worker.reindex`.
- **Tenant isolation**: every Qdrant query filters on `workspace_id`, ES uses one index per
  workspace, and CSV data lives in its own `ws_<id>` schema.
- **Nodes** are small functions `(state) -> partial state update`. Prompts live in
  `src/nlsql/agent/prompts/`, not inline. Use structured output (Pydantic) for every LLM call.
- **Model tiering**: use the fast model for routing, extraction, and suggestions, and the strong model for
  SQL generation and correction. Model IDs come only from `core/config.py`.
- **Evals ship with features**: a new node or prompt change needs cases added to `evals/datasets/`.
- **Secrets**: DB credentials are Fernet-encrypted at rest. Never log connection strings or
  raw result rows. Never read `.env`.

## Working with Claude here
- Before writing or changing LangGraph/LangChain/LangSmith code, use the `langgraph-docs` skill,
  which pulls current API docs from the Context7 MCP server. Don't rely on memory, because these APIs change often.
- Use the `sql-safety-reviewer` agent for any diff touching `sql/`, `sources/`, or executor code.
- Build in roadmap phase order (`docs/development-plan.md`) and mark exit criteria as you go.
