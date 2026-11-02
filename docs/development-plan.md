# Development Plan

Eight weeks of roadmap, one developer. Each phase ends with a demoable increment and an eval gate.
Build in order: every phase depends on the eval harness from Phase 0/1.

## Phase 0: Foundations (week 1)
- [ ] `pyproject.toml` deps installed into `myenv`. Ruff, mypy, and pytest configured
- [ ] `docker compose up` brings up postgres, elasticsearch, qdrant, and redis. Add health checks
- [ ] `core/config.py` (pydantic-settings), `core/db.py` (async SQLAlchemy/psycopg pools), `core/llm.py` (fast/strong model factory)
- [ ] Alembic migrations for the core app tables (system-design §13)
- [ ] LangSmith tracing on. A hello-world graph runs in `langgraph dev`
- [ ] GitHub Actions: lint + type + unit tests
- **Exit:** a CI-green skeleton, and a trace visible in LangSmith.

## Phase 1: Core NL→SQL (weeks 2–3) · F1
- [ ] `sources/`: CSV upload → `ws_<id>` schema. Register a Postgres DSN (encrypted) + read-only check
- [ ] `sources/introspect.py` + column profiling + LLM descriptions. `schema_docs` indexed in Qdrant
- [ ] `sql/validator.py` (sqlglot allowlist, LIMIT, EXPLAIN guard) **with exhaustive unit tests first**
- [ ] `sql/executor.py`: read-only transaction, timeout, row cap
- [ ] Graph v1: route → retrieve_schema → generate → validate → execute → correct loop → respond
- [ ] FastAPI `/sources`, `/chat` (SSE). Streamlit UI showing table + SQL + upload page
- [ ] Two demo datasets (retail sales CSV, Northwind-style PG). Build `nl2sql_core` (150 Qs) + `smoke` eval suites and `evals/run.py`
- **Exit:** ≥ 70% execution accuracy on `nl2sql_core`. Zero validator bypasses in the adversarial tests.

## Phase 2: Value resolution (week 4) · F2
- [ ] `retrieval/units.py` normalizer (pint + regex) with a table-driven test file (≥ 60 cases)
- [ ] ES index template (edge_ngram, synonyms, quantity fields). Value indexing worker job
- [ ] `extract_entities` + `resolve_values` nodes. Confidence bands → `value_choice` interrupt
- [ ] Interpretation notes in the UI. `value_synonyms` admin endpoint
- [ ] `value_resolution` eval suite (100 cases)
- **Exit:** ≥ 90% top-1 resolution. `nl2sql_core` ≥ 75%.

## Phase 3: Business glossary (week 5) · F3
- [ ] `glossary_terms` + versions. `/glossary` CRUD + approve flow. Qdrant hybrid index sync
- [ ] `retrieve_glossary` node. Generator grounding rule (`needs_definition`). `define_term` interrupt
- [ ] `knowledge/learner.py`: explicit teach flow, thumbs-up → proposed terms, dedupe/merge aliases
- [ ] Seed terms from the schema (proposed only). Glossary review screen in the UI
- [ ] `glossary` eval suite, including undefined-term cases. Hallucination evaluator
- **Exit:** zero unsourced-definition violations. ≥ 85% correct usage of defined terms.

## Phase 4: Suggestions and clarification (week 6) · F4, F5
- [ ] `analyze_ambiguity` node + `ambiguous_question` interrupt with 2–4 rewrites
- [ ] `query_examples` bank (fed by thumbs-up). `suggest_questions` node with an executability check
- [ ] Starter questions for a new workspace. Clickable suggestion chips in the UI
- [ ] `ambiguity` + `suggestions` eval suites. Multi-turn follow-ups via the checkpointer
- **Exit:** ambiguity F1 ≥ 0.8. ≥ 90% of suggestions executable.

## Phase 5: Hardening and deploy (weeks 7–8)
- [ ] Semantic cache + result cache. Model tiering tuned with the cost/latency dashboard
- [ ] Prometheus metrics, structlog, alerts. LangSmith online evaluators + annotation queue
- [ ] Security pass: prompt-injection test set, PII column tagging, rate limits, API-key auth
- [ ] Eval gate in CI (smoke + `nl2sql_core` regression ≤ 2 pts)
- [ ] Load test (locust): 20 concurrent users, p95 < 10s
- [ ] Deploy compose stack to a VM with TLS. Backup/restore runbook for Postgres + Qdrant snapshots
- **Exit:** staging deployed. Overall `nl2sql_core` ≥ 80%. All feature gates green.

## Post-MVP backlog
More dialects (MySQL, Snowflake, BigQuery) via sqlglot transpile · charts · OIDC/SSO ·
K8s + managed services · LangGraph Platform deployment · per-tenant fine-tuned rerankers.
