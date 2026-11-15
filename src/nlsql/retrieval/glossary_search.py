"""Business-definition lookup in Qdrant (system-design §4.2). Simple dense vector search.

Collection layout (``settings.glossary_collection``, default ``glossary``)
------------------------------------------------------------------------
One point per glossary entry. The vector is the fastembed embedding
(``settings.embedding_model``) of ``name + synonyms + description``. Payload::

    {
      "id": "gl_42",
      "name": "best customers",
      "description": "Top 5 customers by total sales amount over the selected period",
      "synonyms": ["top customers", "highest buyers"],
      "section": "business_definition",     # or "metric"
      "organisation_id": "acme",            # tenant key, ALWAYS filtered
      "user_id": "u_7",                     # optional: absent = shared with the whole org
      "status": "approved",                 # approved, or proposed (drafted by define_terms)
      "sql_expression": "..."               # optional; set for drafted definitions
    }

Postgres stays the source of truth; this collection is a rebuildable index.

Every query goes through ``build_filter``, which always applies the tenant filter, so
callers cannot forget it. Approved and proposed entries are returned
(``GlossaryHit.status`` tells them apart); rejected ones never are. Create payload indexes on
organisation_id, section, status and user_id when the collection is created.

Later (see docs/entity-extraction.md): hybrid dense + sparse search.
"""

import logging
from functools import lru_cache

from qdrant_client import AsyncQdrantClient, models
from qdrant_client.http.exceptions import UnexpectedResponse

from nlsql.agent.schemas import GlossaryHit, GlossarySection
from nlsql.core.config import get_settings
from nlsql.core.tenant import normalize_organisation_id

ALL_SECTIONS: tuple[GlossarySection, ...] = ("metric", "business_definition")
USABLE_STATUSES = ("approved", "proposed")

log = logging.getLogger(__name__)


@lru_cache
def client() -> AsyncQdrantClient:
    """One client per process; it also holds the loaded fastembed model."""
    return AsyncQdrantClient(url=get_settings().qdrant_url)


def build_filter(
    organisation_id: str,
    user_id: str | None = None,
    sections: tuple[GlossarySection, ...] = ALL_SECTIONS,
) -> models.Filter:
    """Tenant + section + usable-status (approved / proposed) filter.

    Visibility: org-wide entries (no user_id) always; private entries only for their owner.
    """
    organisation_id = normalize_organisation_id(organisation_id)  # raises on empty / invalid
    shared = models.IsEmptyCondition(is_empty=models.PayloadField(key="user_id"))
    must: list[models.Condition] = [
        models.FieldCondition(
            key="organisation_id", match=models.MatchValue(value=organisation_id)
        ),
        models.FieldCondition(key="section", match=models.MatchAny(any=[str(x) for x in sections])),
        models.FieldCondition(key="status", match=models.MatchAny(any=list(USABLE_STATUSES))),
    ]
    if user_id is None:
        return models.Filter(must=[*must, shared])
    mine = models.FieldCondition(key="user_id", match=models.MatchValue(value=user_id))
    return models.Filter(must=must, should=[shared, mine])  # should = at least one matches


def _hits(text: str, points: list[models.ScoredPoint]) -> list[GlossaryHit]:
    return [
        GlossaryHit.model_validate(
            {"id": str(p.id), **(p.payload or {}), "query": text, "score": p.score}
        )
        for p in points
    ]


async def search_glossary_batch(
    texts: list[str],
    organisation_id: str,
    user_id: str | None = None,
    sections: tuple[GlossarySection, ...] = ALL_SECTIONS,
    limit: int = 3,
) -> list[list[GlossaryHit]]:
    """One embedding batch + one Qdrant round trip for all texts. Hits >= glossary_min_score.

    A missing collection (nothing indexed yet) means no hits, not an error.
    """
    if not texts:
        return []
    s = get_settings()
    flt = build_filter(organisation_id, user_id, sections)
    requests = [
        models.QueryRequest(
            query=models.Document(text=t, model=s.embedding_model),
            filter=flt,
            score_threshold=s.glossary_min_score,
            limit=limit,
            with_payload=True,
        )
        for t in texts
    ]
    try:
        resps = await client().query_batch_points(s.glossary_collection, requests=requests)
    except UnexpectedResponse as e:
        if e.status_code == 404:
            return [[] for _ in texts]
        raise
    return [_hits(t, r.points) for t, r in zip(texts, resps, strict=True)]


async def search_glossary(
    text: str,
    organisation_id: str,
    user_id: str | None = None,
    sections: tuple[GlossarySection, ...] = ALL_SECTIONS,
    limit: int = 3,
) -> list[GlossaryHit]:
    """Usable definitions similar to `text` with score >= glossary_min_score, best first."""
    return (await search_glossary_batch([text], organisation_id, user_id, sections, limit))[0]


async def warmup() -> None:
    """Load the fastembed model at startup so the first question doesn't pay for it."""
    s = get_settings()
    if not s.qdrant_url:
        return
    try:
        await client().query_points(
            s.glossary_collection,
            query=models.Document(text="warmup", model=s.embedding_model),
            query_filter=build_filter("nlsql-warmup"),  # tenant filter on every query, even this
            limit=1,
        )
    except Exception as e:  # noqa: BLE001 - the model is loaded before the request; that's the point
        log.info("glossary warmup: %s", type(e).__name__)
