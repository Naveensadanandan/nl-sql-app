# NL-SQL MVP: System Design

Status: Draft v1 · 2026-11-02 · Branch `dev/system-design`

## 1. Goals and scope

| # | Feature | MVP acceptance |
|---|---------|----------------|
| F1 | Ask questions over uploaded CSVs or a connected Postgres DB and get a SQL result table | ≥ 80% execution accuracy on the internal golden set |
| F2 | Resolve vague values ("5l coconut oil" → `5000 ml Coconut Oil`) with Elasticsearch | ≥ 90% top-1 value-resolution accuracy |
| F3 | Understand business terms ("best customers", "super routes") from a self-expanding glossary in Qdrant, with no invented definitions | 0 answers that use an unapproved or unsourced definition |
| F4 | Recommend similar or follow-up questions | ≥ 3 runnable suggestions per answer, all valid against the schema |
| F5 | Turn vague questions into clear candidate questions | Ambiguity detection F1 ≥ 0.8 |

**Not in the MVP:** data-modifying SQL against user data (`INSERT`/`UPDATE`/`DELETE`/DDL). The agent generates and runs read-only `SELECT` queries only. Also out: databases other than Postgres, charts, SSO, fine-tuned models.

## 2. High-level architecture

```
          ┌──────────────┐  SSE  ┌──────────────────────────────────────────────┐
 User ───▶│ Streamlit UI │──────▶│ FastAPI (stateless)                          │
          └──────────────┘       │  /sources /chat /glossary /feedback          │
                                 │        │                                     │
                                 │        ▼                                     │
                                 │  LangGraph agent ──▶ LLM (Claude, tiered)    │
                                 └───┬───────┬────────┬────────┬────────────────┘
                                     │       │        │        │ enqueue
               read-only, timeout    │       │        │        ▼
        ┌──────────────────────┐     │       │        │   ┌─────────┐   ┌──────────────┐
        │ User Postgres DBs    │◀────┘       │        │   │ Redis   │──▶│ arq workers  │
        │ + ws_<id> CSV schemas│             │        │   └─────────┘   │ index/learn/ │
        └──────────────────────┘             │        │                 │ evals        │
        ┌──────────────────────┐             │        │                 └──────┬───────┘
        │ App Postgres         │◀────────────┘        │                        │
        │ metadata, glossary,  │  checkpointer        │                        │
        │ query_log, feedback  │                      ▼                        │
        └──────────────────────┘   ┌───────────────┐ ┌─────────────────┐       │
                                   │ Elasticsearch │ │ Qdrant          │◀──────┘
                                   │ values_<ws>   │ │ glossary        │
                                   └───────────────┘ │ schema_docs     │
                                                     │ query_examples  │
                                   LangSmith ◀── traces, evals, feedback
```

**Principles**
1. **Deterministic guardrails around an LLM core.** The LLM proposes SQL. Code (sqlglot, read-only transactions, limits) decides whether it runs.
2. **Ground every interpretation.** Each value, term, and table the agent uses traces back to an ES hit, a glossary entry, or the schema, and the answer shows this.
3. **Postgres is the source of truth.** ES and Qdrant are rebuildable indexes.
4. **Ask instead of guessing** when confidence is low. The agent uses LangGraph `interrupt`.

## 3. Data onboarding

### 3.1 Sources
- **CSV upload.** Infer types with pandas, sampling the first 10k rows. Sanitize column names. Load with `COPY` into the app Postgres under schema `ws_<workspace_id>`. Every source then speaks one SQL dialect, so the generator, validator, and executor are shared.
- **Postgres connection.** The user provides a DSN for a **read-only role** (the UI shows the `GRANT` script). The DSN is stored Fernet-encrypted. On connect, the app tests `SELECT 1` and confirms the role has no write privileges.

### 3.2 Indexing pipeline (arq job, re-run when the schema changes)
1. **Introspect** `information_schema` and `pg_catalog`: tables, columns, types, PK/FK, row estimates. Save a `schema_snapshot` row with a hash.
2. **Profile columns.** Collect null %, distinct count, min/max, and top-k values.
3. **Describe.** The fast LLM writes a one-line description for each table and column from names and sample values. The user can edit these.
4. **Index the schema** into Qdrant `schema_docs`, one point per table and one per column, with payload `{workspace_id, source_id, table, column, fk_neighbors}`.
5. **Index values** into ES `values_<ws>`: text columns with distinct count ≤ 50k (configurable) → one document per distinct value `{table, column, raw, normalized, quantity_value, quantity_unit, tokens}`.
6. **Seed the glossary.** The LLM proposes candidate terms from the schema, for example "active customer" when an `is_active` column exists. These are stored as `proposed` and never used until approved.

## 4. Retrieval subsystems

### 4.1 Value resolution: Elasticsearch (F2)
- **Normalization (`retrieval/units.py`)** runs the same function at index time and query time. It uses regex plus `pint` to parse quantities (`5l`, `5 ltr`, `5000ml`, `5 kg`) into canonical `(value, base_unit)`: volume → ml, mass → g. It also lowercases, folds Unicode, and expands abbreviations from a per-workspace synonym list (`coco → coconut`).
- **Analyzers:** `standard` + `edge_ngram` (prefix), `phonetic` (optional plugin), and a `synonym_graph` filter.
- **Query:** a `bool` query that combines `multi_match` (fuzziness `AUTO`) on the text fields with a `term` filter or boost on `quantity_value`/`quantity_unit` when the question contains a quantity.
- **Decision:** accept the top hit if score ≥ τ_high and the margin to the 2nd hit ≥ δ. If several hits fall in the ambiguous band, return them as **clarification options**. If nothing matches, tell the user "no matching value". Never pass an unresolved literal straight into SQL.
- The resolved literal goes into SQL as an exact equality or `IN` filter. The UI shows the mapping (`"5l coconut oil" → 'Coconut Oil 5000 ml'`).

### 4.2 Business glossary: Qdrant + Postgres (F3)
**Glossary entry** (Postgres `glossary_terms`, mirrored to Qdrant `glossary`):
```json
{ "term": "best customers", "aliases": ["top customers", "highest buyers"],
  "definition": "Top 5 customers by total sales amount over the selected period",
  "sql_fragment": "SELECT customer_id FROM sales GROUP BY customer_id ORDER BY SUM(amount) DESC LIMIT 5",
  "tables": ["sales", "customers"], "params": {"n": 5, "period": "all_time"},
  "status": "approved", "source": "user|learned|seed", "version": 3,
  "workspace_id": "...", "created_by": "...", "approved_at": "..." }
```
- **Retrieval:** hybrid search, dense (fastembed `bge-small`) plus sparse (BM25/SPLADE) on `term + aliases + definition`, filtered by `workspace_id` and `status=approved`, keeping hits with score ≥ τ_g.
- **Grounding rule:** the generator prompt contains only the retrieved approved definitions, with the instruction: *"If the question uses a business term not defined below, output `needs_definition` instead of SQL."*
- **Self-expansion loop (`knowledge/learner.py`):**
  1. *Explicit.* When the agent asks "How do you define 'super routes'?", the user's answer becomes a `proposed` entry. The LLM drafts a `sql_fragment`, the validator checks it, and user confirmation makes it `approved`.
  2. *Implicit.* Thumbs-up answers whose interpretation used ad-hoc logic go into a review queue as `proposed` terms.
  3. *Hygiene.* Dedupe by near-duplicate embedding (merge as aliases). Bump the version on edit. Keep old versions so answers can be audited.
- **Every answer shows** "Interpreted *best customers* as: top 5 by total sales (glossary v3)".

### 4.3 Schema retrieval (Qdrant `schema_docs`)
For each question, retrieve the top-k tables and columns. Expand along FK edges (1 hop) and add glossary-referenced tables. Build a compact **schema card**: DDL subset, column descriptions, and 3 sample values for each low-cardinality column. When the schema has ≤ 30 tables, skip retrieval and include the whole schema.

### 4.4 Query example bank (Qdrant `query_examples`)
Verified `(question, sql, workspace_id, tables, success_count)` pairs come from thumbs-up answers and the seed set. They serve as **few-shot examples** for generation and as the source of **similar-question suggestions** (F4).

## 5. Agent design (LangGraph)

### 5.1 State
```python
class AgentState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    workspace_id: str; source_id: str
    question: str                    # current, possibly rewritten after clarification
    intent: Literal["data_query", "define_term", "meta", "out_of_scope"]
    ambiguity: AmbiguityReport | None
    entities: list[Entity]           # literal values + business terms
    resolved_values: list[ValueMatch]
    glossary_hits: list[GlossaryHit]
    schema_card: str; examples: list[Example]
    sql: str | None; validation: ValidationResult | None
    result: QueryResult | None; attempts: int
    interpretation: list[str]        # human-readable grounding notes
    suggestions: list[str]
```

### 5.2 Graph
```
START → guard_and_route ─┬─ out_of_scope ─────────────────────────────▶ respond
                         ├─ define_term ──▶ glossary_teach (interrupt) ▶ respond
                         └─ data_query ──▶ analyze_ambiguity
                                            │ vague → clarify (interrupt: F5 options) ─┐
                                            ▼ clear                                    │
                                     extract_entities ◀────────────────────────────────┘
                                            │ (fan-out, parallel)
                    ┌───────────────┬───────┴──────┬──────────────────┐
              resolve_values   retrieve_glossary  retrieve_schema  retrieve_examples
                    └───────────────┴───────┬──────┴──────────────────┘
                                            ▼ (join)
                          missing value/term? → clarify (interrupt)
                                            ▼
                                      generate_sql ◀──────────┐
                                            ▼                 │ attempts < 3
                                      validate_sql ── fail ──▶ correct_sql
                                            ▼ ok              ▲
                                       execute_sql ── db error┘
                                            ▼
                                  respond  ║  suggest_questions   (parallel)
                                            ▼
                                     END  → enqueue log_and_learn (async)
```

| Node | Model | Responsibility |
|------|-------|----------------|
| `guard_and_route` | fast | Intent classification and prompt-injection screening |
| `analyze_ambiguity` | fast | Scores vagueness (missing metric, time range, entity, or aggregation). If above threshold, produces 2–4 **clear rewrites** (F5) |
| `extract_entities` | fast | Pulls out literal values and candidate business terms (structured output) |
| `resolve_values` | none | ES lookups (§4.1) |
| `retrieve_glossary` / `retrieve_schema` / `retrieve_examples` | none | Qdrant (§4.2–4.4) |
| `generate_sql` | strong | Postgres SQL from schema card, approved definitions, resolved literals, and few-shots. Output: `{sql, used_terms, used_values, assumptions}` |
| `validate_sql` | none | sqlglot checks (§7) and `EXPLAIN` cost guard |
| `correct_sql` | strong | Repairs the SQL using the validator or DB error, at most 2 retries |
| `execute_sql` | none | Read-only transaction, `statement_timeout`, row cap 1,000, preview capped at 200 |
| `respond` | fast | Table, SQL, interpretation notes, short summary |
| `suggest_questions` | fast | F4: nearest neighbors from `query_examples`, plus LLM follow-ups (drill-down, compare, trend). Every suggestion is checked against the schema and terms by a dry run of `extract_entities` + glossary lookup |

**Persistence:** `PostgresSaver` checkpointer keyed by `thread_id`. This enables multi-turn context ("now only for Kerala") and resuming after `interrupt`.
**Streaming:** `astream(stream_mode=["updates","messages"])` → SSE, so the UI shows progress ("Resolving values…", "Running SQL…").

### 5.3 Clarification UX (F5)
`interrupt({"type": "ambiguous_question", "options": [...]})` sends structured choices to the UI. The user picks one option or types their own. `Command(resume=choice)` continues the graph with a rewritten `question`. Value and term clarifications use the same mechanism with `type: "value_choice" | "define_term"`.

## 6. API

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/workspaces` | Create a workspace |
| POST | `/sources/csv` | Multipart upload → load + enqueue indexing |
| POST | `/sources/postgres` | Register a DSN → validate read-only + enqueue indexing |
| GET | `/sources/{id}/status` | Indexing progress |
| POST | `/chat/{thread_id}` | Ask a question (SSE stream) |
| POST | `/chat/{thread_id}/resume` | Answer a clarification interrupt |
| GET/POST/PATCH | `/glossary` | List, propose, approve, edit terms |
| POST | `/feedback` | Thumbs up/down + comment → LangSmith feedback + learning queue |
| GET | `/suggestions/starter` | Starter questions for a fresh workspace |

## 7. SQL safety

1. Parse with `sqlglot` (dialect `postgres`). Reject anything that is not a single `SELECT`/`WITH … SELECT` statement.
2. Walk the AST. Reject DML/DDL, `COPY`, `pg_*`/`dblink` functions, `SELECT INTO`, writable CTEs, and any `;` beyond one statement.
3. Check every table and column against an **allowlist** from the schema snapshot, so the query cannot reach system catalogs.
4. Add or clamp `LIMIT`.
5. Run `EXPLAIN (FORMAT JSON)` and reject the query if the estimated cost exceeds the threshold.
6. Execute inside `BEGIN READ ONLY; SET LOCAL statement_timeout='15s'`, using a DB role that is also read-only. This is defense in depth.
7. Treat data values in prompts as untrusted. Wrap them in delimiters and never follow instructions found inside them.

## 8. Evaluation

**Datasets** (LangSmith, versioned in `evals/datasets/*.jsonl`):

| Suite | Size (MVP) | Content |
|-------|-----------|---------|
| `smoke` | 20 | Fast regression set covering all features, runs on every PR |
| `nl2sql_core` | 150 | Question → gold SQL on 2 demo schemas (retail sales CSV + Northwind-style PG) |
| `value_resolution` | 100 | Vague literal → gold `(table, column, value)` |
| `glossary` | 60 | Questions using defined terms, plus questions using **undefined** terms (expected: `needs_definition`) |
| `ambiguity` | 80 | Labelled vague/clear, plus acceptable rewrites |
| `suggestions` | 40 | Answers → judge suggestions for relevance and executability |

**Evaluators**
- *Execution accuracy:* run the gold and predicted SQL and compare result sets, ignoring order unless an `ORDER BY` is required. This is the primary metric.
- *Component checks:* tables used, filters used, resolved literal correct, glossary term used correctly.
- *Hallucination check:* `used_terms` ⊆ approved glossary hits. The target is zero violations.
- *Ambiguity:* precision and recall of vague detection. An LLM judge rates rewrite quality.
- *LLM-as-judge:* answer faithfulness (summary vs. table) and suggestion usefulness.
- *Operational:* p50/p95 latency, tokens and cost per question, retry rate, clarification rate.

**Gates:** in CI, `smoke` must pass, and `nl2sql_core` execution accuracy may not drop more than 2 points from the main baseline. Changes to prompts or nodes require a recorded LangSmith experiment link in the PR.

**Online:** sample 10% of production traces into an LLM-judge pass. Thumbs-down answers go into an annotation queue, and reviewed items are promoted into the datasets. This closes the loop with the glossary learner.

## 9. Observability

- **Tracing:** LangSmith (`LANGSMITH_TRACING=true`, project per environment), with every run tagged by `workspace_id`, `source_id`, graph version, and prompt version. If self-hosting is required, Langfuse is a drop-in alternative behind a callback.
- **Metrics (Prometheus):** `nlsql_requests_total{outcome}`, `nlsql_latency_seconds{node}`, `nlsql_sql_retries`, `nlsql_clarifications_total{type}`, `nlsql_llm_tokens_total{model}`, `nlsql_es_resolution_confidence`, indexing job durations.
- **Logs:** structlog JSON with a `trace_id` correlated with the LangSmith run ID. Raw result rows and DSNs are never logged.
- **Dashboards/alerts:** success rate < 85% (1h), p95 > 12s, retry rate > 25%, a validator rejection spike (possible prompt-injection attempt), and indexing failures.

## 10. Deployment

### MVP (single VM / docker compose)
| Service | Image | Notes |
|---------|-------|-------|
| `api` | app image | uvicorn, 2–4 workers |
| `worker` | app image | `arq nlsql.worker.main.WorkerSettings` |
| `ui` | app image | Streamlit |
| `postgres` | postgres:16 | App DB + checkpointer + CSV schemas |
| `elasticsearch` | 8.x single-node | 2 GB heap |
| `qdrant` | qdrant/qdrant | Persistent volume |
| `redis` | redis:7 | Job queue + caches |

A VM with 4 vCPU and 16 GB RAM is enough for the MVP. Caddy in front provides TLS. Secrets come from a `.env` file on the host.

### Production path
- **Containers:** Kubernetes (or ECS). API and UI run as deployments with an HPA on CPU and in-flight requests. Workers run as separate deployments per queue (`indexing`, `learning`, `evals`).
- **Managed data:** RDS or Cloud SQL Postgres, Elastic Cloud, Qdrant Cloud, ElastiCache Redis. Secrets live in a secrets manager or KMS, which replaces Fernet keys on disk.
- **Alternative:** deploy the graph on LangGraph Platform (`langgraph.json` already defines it) and keep FastAPI as a thin BFF.
- **CI/CD:** GitHub Actions runs ruff, mypy, unit tests, integration tests (compose services), and the smoke evals, then builds the image and deploys to staging. Promotion to prod requires the eval gate to pass.

## 11. Scaling

| Concern | Approach |
|---------|----------|
| API throughput | Stateless API, with all state in the Postgres checkpointer, scales horizontally. Fully async I/O |
| LLM cost/latency | Model tiering. Prompt caching for the static system prompt + schema card. A **semantic cache** in Qdrant maps (question embedding, workspace, schema hash) → validated SQL, and re-executes on a hit |
| Result cache | Redis keyed by `(sql_hash, source_id, data_version)` with a short TTL |
| Large schemas (100s of tables) | Schema retrieval + FK expansion + table clustering. Cap the schema card at ~4k tokens |
| User DB load | Per-source connection pool limits, `statement_timeout`, EXPLAIN cost guard, max concurrent queries per workspace |
| ES growth | One index per workspace under an alias. Move to shared indices with routing by `workspace_id` past ~1k tenants. Cap distinct values per column |
| Qdrant growth | Payload-partitioned collections with a `workspace_id` index. Move to sharding or one collection per large tenant |
| Freshness | Scheduled re-introspection. Diffing the schema hash triggers incremental re-indexing of the affected tables only |
| Large results | Server-side cursors, paginated preview, async CSV export |
| Rate limiting | Per-user and per-workspace token buckets in Redis |

## 12. Security and multi-tenancy

- Workspace-scoped auth with JWT. The MVP uses API keys and adds OIDC later.
- Isolation: `ws_<id>` Postgres schemas with per-workspace roles, ES index per workspace, and a mandatory `workspace_id` filter in a single Qdrant access helper.
- Data minimization: only schema, descriptions, and a few sample values reach the LLM. Optional PII column tagging excludes values from prompts and from the ES index.
- Audit: every executed SQL statement is logged with user, glossary versions, and value mappings used.

## 13. App database (core tables)

`workspaces`, `users`, `data_sources(id, ws, kind, dsn_enc, status)`, `schema_snapshots(source, hash, json)`,
`column_profiles`, `glossary_terms(+ glossary_versions)`, `query_examples`, `query_log(thread, question, sql, outcome, latency, trace_id)`,
`feedback(query_log_id, score, comment)`, `value_synonyms(ws, from, to)`. Migrations are managed with Alembic.

## 14. Key risks

| Risk | Mitigation |
|------|------------|
| Wrong-but-plausible SQL | Interpretation notes shown on every answer, execution-accuracy evals, few-shots from verified queries |
| Glossary drift or poisoning | Approval workflow, versioning, role-gated approvals |
| ES unit edge cases ("1/2 kg", "500gm") | Shared normalizer with its own unit-test table, plus clarification fallback |
| Slow user DBs | Cost guard, timeouts, async export |
| LLM provider outage | Model IDs in config. LangChain abstraction allows a fallback model |
