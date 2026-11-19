"""Value resolution against Elasticsearch (system-design §4.1).

Turns a vague literal from the question ("coconut oil 5l", "women") into the exact value stored
in the database ("5000 ml Coconut Oil", "Female"), so SQL can use an equality / IN filter.

Index layout (built by `worker.indexing.index_values`)
-----------------------------------------------------
One index per organisation: ``values_<organisation_id>`` (tenant isolation). One document
per distinct value of an indexed text column, taken as-is from the database (no hand-written
synonyms, so onboarding a large database needs no manual work)::

    {
      "id": "products.product_name:5000 ml Coconut Oil",
      "name": "5000 ml Coconut Oil",     # the literal exactly as stored -> goes into SQL
      "description": "",
      "table": "products",
      "column": "product_name",
      "name_vector": [...]               # fastembed embedding of name (semantic fallback)
    }

Search is two-stage (run by the ``resolve_values`` node):
1. Lexical: fuzzy BM25 ``multi_match`` over name (x3) and description. Spelling, typos and
   word overlap ("coconut oil 5l" -> "5000 ml Coconut Oil"). Decided by ``decide``:
   - no hit with score >= ``value_min_score``        -> ``not_found``
   - top hit clearly ahead of the 2nd (ratio check)   -> ``resolved``
   - several hits close to the top                    -> ``ambiguous``
2. Semantic, only when stage 1 is ``not_found``: kNN on ``name_vector`` for words that mean
   the value without sharing its spelling ("women" -> "Female"). Decided by
   ``decide_semantic`` (cosine floor + margin over the 2nd hit). These matches carry
   ``method="semantic"`` and are listed as assumptions in the answer.

Not done yet (see docs/entity-extraction.md): unit normalisation with pint (5l == 5000 ml),
edge-ngram analyzers.
"""

import asyncio
from functools import lru_cache
from typing import Any

from elasticsearch import AsyncElasticsearch, NotFoundError

from nlsql.agent.schemas import ValueItem, ValueMatch
from nlsql.core.config import get_settings
from nlsql.core.tenant import normalize_organisation_id
from nlsql.retrieval import embeddings

_FIELDS = ["name^3", "description"]
VECTOR_FIELD = "name_vector"


def index_name(organisation_id: str) -> str:
    """ES index for one organisation. Rejects ids that could address another index."""
    return f"values_{normalize_organisation_id(organisation_id)}"


@lru_cache
def _client() -> AsyncElasticsearch:
    return AsyncElasticsearch(get_settings().es_url)


def client() -> AsyncElasticsearch:
    """The process-wide ES client (also used to index a new data source's values)."""
    return _client()


def _items(resp: Any, semantic: bool = False) -> list[ValueItem]:
    # kNN _score for cosine is (1 + cos) / 2; report the cosine so thresholds read naturally.
    return [
        ValueItem.model_validate(
            {
                "id": h["_id"],
                **h["_source"],
                "score": 2 * h["_score"] - 1 if semantic else h["_score"],
            }
        )
        for h in resp["hits"]["hits"]
    ]


async def search_values(
    text: str, organisation_id: str, size: int = 5, tables: list[str] | None = None
) -> list[ValueItem]:
    """Lexical (BM25, fuzzy) top `size` values for `text`, best first. Empty if no index yet.

    `tables` restricts the search to values from those tables (the user's selected tables).
    The filter does not affect scoring.
    """
    match = {"multi_match": {"query": text, "fields": _FIELDS, "fuzziness": "AUTO"}}
    query: dict[str, Any] = (
        {"bool": {"must": match, "filter": [{"terms": {"table": tables}}]}} if tables else match
    )
    try:
        resp = await _client().search(
            index=index_name(organisation_id),
            query=query,
            size=size,
            source_excludes=[VECTOR_FIELD],
        )
    except NotFoundError:
        return []
    return _items(resp)


async def search_values_semantic(
    text: str, organisation_id: str, size: int = 5, tables: list[str] | None = None
) -> list[ValueItem]:
    """kNN top `size` values whose meaning is close to `text`; `score` is the cosine.

    Values indexed before vectors existed have no `name_vector` and are never returned.
    """
    s = get_settings()
    index = index_name(organisation_id)  # validate the tenant before embedding anything
    [vector] = await asyncio.to_thread(embeddings.embed, [text])
    knn: dict[str, Any] = {
        "field": VECTOR_FIELD,
        "query_vector": vector,
        "k": size,
        "num_candidates": max(50, size * 10),
        "similarity": s.value_semantic_min_score,  # raw cosine floor, applied by ES
    }
    if tables:
        knn["filter"] = {"terms": {"table": tables}}
    try:
        resp = await _client().search(
            index=index, knn=knn, size=size, source_excludes=[VECTOR_FIELD]
        )
    except NotFoundError:
        return []
    return _items(resp, semantic=True)


async def summarize_values(
    organisation_id: str, tables: list[str], per_column: int = 6
) -> list[dict[str, Any]]:
    """Indexed columns of `tables` with their value count and a few example values.

    Empty if the organisation has no index yet, or `tables` is empty. Examples are the first
    values by name.
    """
    if not tables:
        return []
    query: dict[str, Any] = {"terms": {"table": tables}}
    aggs = {
        "cols": {
            "multi_terms": {"terms": [{"field": "table"}, {"field": "column"}], "size": 50},
            "aggs": {
                "examples": {
                    "top_hits": {
                        "size": per_column,
                        "sort": [{"name.raw": "asc"}],
                        "_source": ["name"],
                    }
                }
            },
        }
    }
    try:
        resp = await _client().search(
            index=index_name(organisation_id), query=query, aggs=aggs, size=0
        )
    except NotFoundError:
        return []
    out = []
    for b in resp["aggregations"]["cols"]["buckets"]:
        table, column = b["key"]
        out.append(
            {
                "table": table,
                "column": column,
                "count": b["doc_count"],
                "examples": [h["_source"] for h in b["examples"]["hits"]["hits"]],
            }
        )
    return sorted(out, key=lambda c: (c["count"], c["table"], c["column"]))


def decide(text: str, hits: list[ValueItem]) -> ValueMatch:
    """Lexical rule: accept / ambiguous / not-found on BM25-ranked hits."""
    s = get_settings()
    hits = [h for h in hits if h.score >= s.value_min_score]
    if not hits:
        return ValueMatch(text=text, status="not_found")
    close = [h for h in hits if h.score >= hits[0].score * s.value_ambiguity_ratio]
    if len(close) == 1:
        return ValueMatch(text=text, status="resolved", match=hits[0], candidates=[hits[0]])
    return ValueMatch(text=text, status="ambiguous", candidates=close)


def decide_semantic(text: str, hits: list[ValueItem]) -> ValueMatch:
    """Semantic rule on cosine-ranked hits.

    Cosines of short strings bunch together, so a ratio test like `decide` would accept almost
    anything. Instead the top hit must clear the floor and beat the 2nd by a fixed margin.
    """
    s = get_settings()
    hits = [h for h in hits if h.score >= s.value_semantic_min_score]
    if not hits:
        return ValueMatch(text=text, status="not_found", method="semantic")
    close = [h for h in hits if h.score >= hits[0].score - s.value_semantic_margin]
    if len(close) == 1:
        return ValueMatch(
            text=text, status="resolved", match=hits[0], candidates=[hits[0]], method="semantic"
        )
    return ValueMatch(text=text, status="ambiguous", candidates=close, method="semantic")
