# NL-SQL App

## WHY
Business users can't write SQL, and generic text-to-SQL breaks on real data in two ways.
It can't match vague values ("5l coconut oil" vs the stored `5000 ml Coconut Oil`), and it
makes up business terms ("best customers", "super routes"). This MVP answers natural-language
questions over uploaded CSVs or a connected Postgres database. It returns a SQL result table
and shows how it read the question, using only definitions it can trace to a source.

## WHAT
- **Agent**: LangGraph state graph (`src/nlsql/agent/graph.py`) on LangChain. Every LLM call goes
  through OpenRouter (`ChatOpenRouter`), built only by `core/llm.py` (`fast_llm` / `strong_llm`)
- **API**: FastAPI (`src/nlsql/api`). `POST /ask` returns a summary and a `question_id`; the
  frontend reads the full answer (SQL, rows, `interpretation`) from the query store with
  `GET /questions/{id}`, polling while `rows_status` is `saving`. **UI**: Streamlit (`ui/`)
- **App DB**: Postgres 16. It holds metadata, the glossary (source of truth), the query log, the LangGraph checkpointer, and the CSV uploads, which go into one `ws_<id>` schema per workspace
- **Value resolution**: Elasticsearch 8. It indexes distinct values of text columns, normalized for units and synonyms
- **Knowledge/RAG**: Qdrant. Collections: `glossary`, `schema_docs`, `query_examples`. Embeddings come from fastembed
- **SQL safety**: `sqlglot` AST validation, read-only transactions, statement timeout, row cap
- **Evals/observability**: LangSmith for traces, datasets, and evaluators (`evals/`), plus structlog and Prometheus
- **Background jobs**: v0.1 uses in-process tasks (`core/background.spawn`) for result rows and
  saved definitions. Planned: a real queue (arq + Redis, or Celery) behind the same call

Layout: `src/nlsql/{api,agent,retrieval,sources,sql,knowledge,suggestions,core,worker}`,
`evals/`, `tests/{unit,integration}`, `ui/`, `docs/`. The full design is in `docs/system-design.md`,
and the phased roadmap is in `docs/development-plan.md`.

## HOW
```bash
source myenv/Scripts/activate        # PowerShell: myenv\Scripts\Activate.ps1
pip install -e ".[dev]"              # install app + dev tools (".[later]" = not-yet-used deps)
cp .env.example .env                 # then fill NLSQL_OPENROUTER_API_KEY, LANGSMITH_API_KEY
docker compose up -d                 # postgres, elasticsearch, qdrant, redis
uvicorn nlsql.api.main:app --reload  # API on :8000 (login, sources, ask)
streamlit run ui/app.py              # UI on :8501 (NLSQL_API_URL points it at the API)
langgraph dev                        # LangGraph Studio for graph debugging (langgraph.json)
```

No new test suites before version 1. Evals (`evals/datasets/`) run at deployment time, against
the exact models and prompts being deployed (see docs/v0.1-release-review.md §5.4).

## Conventions (non-style)
- **Read-only always**: every generated SQL statement goes through `nlsql.sql.validator` before
  execution. Never run model output directly, and never add write or DDL paths to user databases.
- **Consistent, traceable definitions**: every business term or metric in an answer comes from
  a source the user can see in `interpretation.definitions`:
  1. an `approved` glossary entry (above the similarity threshold), else
  2. a `proposed` glossary entry saved from an earlier question, else
  3. a new draft from `define_terms`, grounded in real schema columns (any column it cites must
     exist, or the draft is dropped).
  After a successful answer, new drafts are saved as `proposed` (`remember_definitions`), so the
  same term gives the same answer on every later question. Users change a definition by
  uploading the same name and section (it replaces the draft) and confirm it by setting
  `approved`. Never overwrite an existing entry with a draft. If the schema can't support a
  term, the answer asks the user for its definition instead of guessing.
- **Every answer is explained**: `respond` builds an `Interpretation` (no LLM) with the SQL that
  ran, the definitions given and used, value matches, unclear terms and the reading chosen,
  assumptions, and every debug repair. It is stored in `query_log.interpretation`.
- **Unresolved values** may be matched loosely (`ILIKE`) and must be listed in `assumptions`.
- **Source of truth**: the target is Postgres for the glossary and examples, with Qdrant and ES
  as derived indexes rebuildable with `python -m nlsql.worker.reindex`. In v0.1 the glossary
  lives only in Qdrant (admin CSV upload + saved drafts), so back up the Qdrant collection.
- **Tenant isolation**: every Qdrant query filters on `organisation_id` (`build_filter`), ES uses
  one index per organisation. In v0.1, the data tables come from config (`NLSQL_DATA_SCHEMA`,
  `NLSQL_DATA_TABLES`) and are shared; per-tenant `ws_<id>` schemas are planned.
- **Nodes** are small functions `(state) -> partial state update`. Prompts live in
  `src/nlsql/agent/prompts/` and load through `agent/prompting.load_prompt`. Untrusted text goes
  into prompts only through `prompting.block` / `prompting.line`. Shared prompt facts live in
  `agent/grounding.py`; nodes never import each other. Use structured output for every LLM call.
- **Latency**: nothing slow sits on the response path. The first four nodes start in parallel,
  schema reflection is cached, and result rows and saved definitions are written in the
  background. A node that calls `spawn` must be `async`.
- **Model tiering**: use the fast model for routing, extraction, and suggestions, and the strong model for
  SQL generation and correction. Model IDs come only from `core/config.py`.
- **Evals ship with features**: a new node or prompt change needs cases added to `evals/datasets/`
  (run at deployment, not per commit, until v1).
- **Auth**: every data endpoint takes `CurrentUser` (`api/deps.py`); `organisation_id` / `user_id`
  come from the session, never the request body. Agent state carries only `source_id`; nodes
  resolve the connection with `sources.registry.target_for` (see docs/ui-and-auth.md).
- **Secrets**: DB credentials are Fernet-encrypted at rest. Never log connection strings or
  raw result rows. Never read `.env`.

## Working with Claude here
- Before writing or changing LangGraph/LangChain/LangSmith code, use the `langgraph-docs` skill,
  which pulls current API docs from the Context7 MCP server. Don't rely on memory, because these APIs change often.
- Use the `sql-safety-reviewer` agent for any diff touching `sql/`, `sources/`, or executor code.
- Build in roadmap phase order (`docs/development-plan.md`) and mark exit criteria as you go.
