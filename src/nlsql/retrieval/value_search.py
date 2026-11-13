"""Value resolution against Elasticsearch (system-design §4.1).

Turns a vague literal from the question ("coconut oil 5l") into the exact value stored in
the database ("5000 ml Coconut Oil"), so SQL can use an equality / IN filter.

Index layout (built later by the indexer, `python -m nlsql.worker.reindex`)
--------------------------------------------------------------------------
One index per organisation: ``values_<organisation_id>`` (tenant isolation). One document
per distinct value of an indexed text column::

    {
      "id": "products.product_name:5000 ml Coconut Oil",
      "name": "5000 ml Coconut Oil",          # the literal exactly as stored -> goes into SQL
      "description": "Coconut oil, 5 litre can",
      "synonyms": ["5l coconut oil", "coco oil 5 ltr"],
      "table": "products",
      "column": "product_name"
    }

Search: one fuzzy ``multi_match`` over name (x3), synonyms (x2) and description.

Decision (``decide``):
- no hit with score >= ``value_min_score``          -> ``not_found``
- top hit clearly ahead of the 2nd (ratio check)     -> ``resolved``
- several hits close to the top                      -> ``ambiguous`` (candidates for the user)

Not done yet (see docs/entity-extraction.md): unit normalisation with pint (5l == 5000 ml),
edge-ngram / synonym_graph analyzers. Until then, put unit variants in ``synonyms``.
"""

from functools import lru_cache
from typing import Any

from elasticsearch import AsyncElasticsearch, NotFoundError

from nlsql.agent.schemas import ValueItem, ValueMatch
from nlsql.core.config import get_settings
from nlsql.core.tenant import normalize_organisation_id

_FIELDS = ["name^3", "synonyms^2", "description"]


def index_name(organisation_id: str) -> str:
    """ES index for one organisation. Rejects ids that could address another index."""
    return f"values_{normalize_organisation_id(organisation_id)}"


@lru_cache
def _client() -> AsyncElasticsearch:
    return AsyncElasticsearch(get_settings().es_url)


async def search_values(
    text: str, organisation_id: str, size: int = 5, tables: list[str] | None = None
) -> list[ValueItem]:
    """Top `size` indexed values for `text`, best first. Empty if the org has no index yet.

    `tables` restricts the search to values from those tables (the user's selected tables).
    The filter does not affect scoring.
    """
    match = {"multi_match": {"query": text, "fields": _FIELDS, "fuzziness": "AUTO"}}
    query: dict[str, Any] = (
        {"bool": {"must": match, "filter": [{"terms": {"table": tables}}]}} if tables else match
    )
    try:
        resp = await _client().search(index=index_name(organisation_id), query=query, size=size)
    except NotFoundError:
        return []
    return [
        ValueItem.model_validate({"id": h["_id"], **h["_source"], "score": h["_score"]})
        for h in resp["hits"]["hits"]
    ]


def decide(text: str, hits: list[ValueItem]) -> ValueMatch:
    """Apply the accept / ambiguous / not-found rule to ranked hits."""
    s = get_settings()
    hits = [h for h in hits if h.score >= s.value_min_score]
    if not hits:
        return ValueMatch(text=text, status="not_found")
    close = [h for h in hits if h.score >= hits[0].score * s.value_ambiguity_ratio]
    if len(close) == 1:
        return ValueMatch(text=text, status="resolved", match=hits[0], candidates=[hits[0]])
    return ValueMatch(text=text, status="ambiguous", candidates=close)
