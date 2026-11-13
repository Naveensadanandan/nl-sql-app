# Entity extraction, value resolution and business definitions

Status: first cut. Elasticsearch and Qdrant are **not** running yet. The new nodes are wired
into the graph and skip their lookups until those servers are configured.

Design references: `docs/system-design.md` §4.1 (value resolution), §4.2 (glossary), §5.2 (graph).

## What changed

### Graph (`src/nlsql/agent/graph.py`)

```
guard ─▶ analyze_ambiguity ─┬─▶ fetch_schema ─────────────────────────────────▶ END
                            └─▶ extract_entities ─┬─▶ resolve_values ─────────▶ END   (Elasticsearch)
                                                  └─▶ retrieve_glossary ──────▶ END   (Qdrant)
```

- `fetch_schema` runs **in parallel** with the entity branch, in the same LangGraph superstep.
- `resolve_values` and `retrieve_glossary` run in parallel after `extract_entities`.
- Each branch writes its own state key, so no reducer is needed.
- When `generate_sql` exists, replace the three `-> END` edges with a join:
  `builder.add_edge(["fetch_schema", "resolve_values", "retrieve_glossary"], "generate_sql")`.
- `ambiguity_graph` (behind `POST /analyze-ambiguity`) is unchanged.

### New nodes

| Node | File | LLM | Reads | Writes |
|---|---|---|---|---|
| `extract_entities` | `agent/nodes/extract_entities.py` | fast | `question` | `entities` |
| `resolve_values` | `agent/nodes/retrieve_knowledge.py` | none | `entities`, `organisation_id` | `resolved_values` |
| `retrieve_glossary` | `agent/nodes/retrieve_knowledge.py` | none | `entities`, `organisation_id`, `user_id` | `glossary_hits` |

`extract_entities` uses structured output (`EntityExtraction`). The prompt is in
`agent/prompts/extract_entities.md`. Each entity has a `kind`:

- `db_value`: a literal that should match a stored value ("5l coconut oil", "Kerala"). It goes to Elasticsearch.
- `business_term`: jargon or a coined label ("best customers", "super routes"). It goes to Qdrant.
- `metric`: a named measure whose formula is not a plain column ("profit", "AOV"). It goes to Qdrant.

### New search helpers (`src/nlsql/retrieval/`)

- `value_search.py`: `search_values()` (ES fuzzy `multi_match`) and `decide()` (accept / ambiguous / not found).
- `glossary_search.py`: `search_glossary()` (Qdrant dense search) and `build_filter()` (the tenant filter).

### Other changes

- `agent/schemas.py`: `Entity`, `EntityExtraction`, `IndexedItem`, `ValueItem`, `ValueMatch`, `GlossaryHit`.
- `agent/state.py`: `organisation_id`, `user_id`, `entities`, `resolved_values`, `glossary_hits`.
- `core/config.py`: ES/Qdrant URLs and thresholds (see [Configuration](#configuration)).
- `api/routes/ask.py`: `/ask` takes optional `organisation_id` and `user_id`, and returns
  `entities`, `resolved_values` and `glossary_hits`.
- `pyproject.toml`: `elasticsearch` → `elasticsearch[async]`, because `AsyncElasticsearch` needs aiohttp.
- Tests: `tests/unit/test_entity_retrieval.py` (new). `test_api_ask.py` and `test_guard.py`
  now fake `extract_entities` and `fetch_schema`, so unit tests need no LLM and no DB.
- Evals: `evals/datasets/entities.jsonl` (7 extraction cases).

## Indexed item shape

ES value documents and Qdrant glossary points share one base shape (`IndexedItem`):

| Field | Meaning |
|---|---|
| `id` | Stable identifier |
| `name` | For a value: the literal **exactly as stored** in the DB, which is what goes into SQL. For a term: its canonical name |
| `description` | Free text that helps matching and gets shown to the user |
| `synonyms` | Alternative spellings or phrasings ("5l coconut oil", "top customers") |

### Elasticsearch: values

- Index: one per organisation, `values_<organisation_id>`. The id must match `[a-z0-9][a-z0-9_-]{0,63}`.
  Anything else is rejected, so a crafted id can't address another tenant's index or a wildcard.
- One document per distinct value of an indexed text column:

```json
{
  "id": "products.product_name:5000 ml Coconut Oil",
  "name": "5000 ml Coconut Oil",
  "description": "Coconut oil, 5 litre can",
  "synonyms": ["5l coconut oil", "coco oil 5 ltr"],
  "table": "products",
  "column": "product_name"
}
```

- Query: `multi_match` on `name^3, synonyms^2, description`, `fuzziness: AUTO`, top 5.
- Decision (`decide`):
  - No hit with `score >= value_min_score` → `not_found`.
  - Only the top hit is within `value_ambiguity_ratio` of the top score → `resolved` (`match` is set).
  - Several hits are within that ratio → `ambiguous` (`candidates` become the options to show the user).
  - ES not configured, or no `organisation_id` → `skipped`.
- A missing index (the organisation hasn't been indexed yet) returns no hits, so the status is `not_found`.

### Qdrant: business definitions

- Collection: `glossary` (`NLSQL_GLOSSARY_COLLECTION`). The vector is the fastembed embedding
  (`NLSQL_EMBEDDING_MODEL`, default `BAAI/bge-small-en-v1.5`) of `name + synonyms + description`.
  The indexer must use the same model.
- Payload:

```json
{
  "id": "gl_42",
  "name": "best customers",
  "description": "Top 5 customers by total sales amount over the selected period",
  "synonyms": ["top customers", "highest buyers"],
  "section": "business_definition",
  "organisation_id": "acme",
  "user_id": "u_7",
  "status": "approved"
}
```

- `section` is `metric` or `business_definition`.
- `user_id` is optional. Leave it out for an entry shared with the whole organisation, and set it for a private entry.
- Filter (`build_filter`). Every query goes through it, so callers can't forget it:
  - **must**: `organisation_id == <org>`, `section in [metric, business_definition]`, `status == approved`.
  - **visibility**: with a `user_id`, return entries that have no `user_id` **or** belong to that user.
    Without one, return shared entries only.
- Search: simple dense `query_points` with `score_threshold = glossary_min_score`, top 3 per term.
  When several terms match the same entry, `retrieve_glossary` keeps that entry's best score.
- Create payload indexes on `organisation_id`, `section`, `status` and `user_id` when you create the collection.
- Postgres stays the source of truth (`glossary_terms`). This collection is a rebuildable index.

## Configuration

All settings use the `NLSQL_` prefix and live in `core/config.py`.

| Setting | Default | Notes |
|---|---|---|
| `NLSQL_ES_URL` | unset | Leave unset until ES exists. Values then come back as `skipped` |
| `NLSQL_VALUE_MIN_SCORE` | `1.0` | BM25 score floor. Tune it on real data |
| `NLSQL_VALUE_AMBIGUITY_RATIO` | `0.8` | A 2nd hit at or above `ratio × top score` makes the match ambiguous |
| `NLSQL_QDRANT_URL` | unset | Leave unset until Qdrant exists. Glossary hits then come back empty |
| `NLSQL_GLOSSARY_COLLECTION` | `glossary` | |
| `NLSQL_EMBEDDING_MODEL` | `BAAI/bge-small-en-v1.5` | Needs `qdrant-client[fastembed]` |
| `NLSQL_GLOSSARY_MIN_SCORE` | `0.6` | Cosine similarity floor |

`.env.example` already lists `NLSQL_ES_URL` and `NLSQL_QDRANT_URL`. The elasticsearch and qdrant
services in `docker-compose.yml` are still commented out. Uncomment them to bring the servers up.

## Trying it

```bash
curl -s localhost:8000/ask -H 'content-type: application/json' -d '{
  "question": "total sales of 5l coconut oil for our best customers",
  "organisation_id": "acme", "user_id": "u_7"
}'
```

Without ES or Qdrant you'll still see `entities`, `resolved_values` with status `skipped`, and an empty `glossary_hits`.

## Known gaps / next steps

1. **Indexer**: nothing writes to ES or Qdrant yet. `python -m nlsql.worker.reindex` should:
   - build `values_<org>` from distinct text-column values;
   - mirror approved `glossary_terms` from Postgres into Qdrant.
2. **Unit normalisation** (§4.1): parse `5l` = `5000 ml` with `pint` at index and query time.
   Until then, put unit variants in `synonyms`.
3. **Analyzers**: add edge-ngram and `synonym_graph` analyzers to the ES mapping.
4. **Hybrid search**: dense + sparse (BM25/SPLADE) for the glossary.
5. **Clarify node**: `ambiguous` or `not_found` values, and terms without a glossary hit, should
   `interrupt` and ask the user. Never pass an unresolved literal into SQL.
6. **Auth**: `organisation_id` and `user_id` currently come from the request body. They must come from the authenticated session.
7. **Postgres alternative**: value search could use `pg_trgm` over a `distinct_values` table if
   ES turns out to be overkill. It isn't implemented, because ES was preferred.
8. **Evals**: write an evaluator and runner for `evals/datasets/entities.jsonl`. `evals/run.py` doesn't exist yet.
