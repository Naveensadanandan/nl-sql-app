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
      "status": "approved"                  # only approved entries are ever returned
    }

Postgres stays the source of truth; this collection is a rebuildable index.

Every query goes through ``search_glossary``, which always applies the tenant filter, so
callers cannot forget it. Create payload indexes on organisation_id, section, status and
user_id when the collection is created.

Later (see docs/entity-extraction.md): hybrid dense + sparse search.
"""

from functools import lru_cache

from qdrant_client import AsyncQdrantClient, models

from nlsql.agent.schemas import GlossaryHit, GlossarySection
from nlsql.core.config import get_settings
from nlsql.core.tenant import normalize_organisation_id

ALL_SECTIONS: tuple[GlossarySection, ...] = ("metric", "business_definition")


@lru_cache
def _client() -> AsyncQdrantClient:
    return AsyncQdrantClient(url=get_settings().qdrant_url)


def build_filter(
    organisation_id: str,
    user_id: str | None = None,
    sections: tuple[GlossarySection, ...] = ALL_SECTIONS,
) -> models.Filter:
    """Tenant + section + approved filter.

    Visibility: org-wide entries (no user_id) always; private entries only for their owner.
    """
    organisation_id = normalize_organisation_id(organisation_id)  # raises on empty / invalid
    shared = models.IsEmptyCondition(is_empty=models.PayloadField(key="user_id"))
    must: list[models.Condition] = [
        models.FieldCondition(
            key="organisation_id", match=models.MatchValue(value=organisation_id)
        ),
        models.FieldCondition(key="section", match=models.MatchAny(any=[str(x) for x in sections])),
        models.FieldCondition(key="status", match=models.MatchValue(value="approved")),
    ]
    if user_id is None:
        return models.Filter(must=[*must, shared])
    mine = models.FieldCondition(key="user_id", match=models.MatchValue(value=user_id))
    return models.Filter(must=must, should=[shared, mine])  # should = at least one matches


async def search_glossary(
    text: str,
    organisation_id: str,
    user_id: str | None = None,
    sections: tuple[GlossarySection, ...] = ALL_SECTIONS,
    limit: int = 3,
) -> list[GlossaryHit]:
    """Approved definitions similar to `text` with score >= glossary_min_score, best first."""
    s = get_settings()
    resp = await _client().query_points(
        collection_name=s.glossary_collection,
        query=models.Document(text=text, model=s.embedding_model),
        query_filter=build_filter(organisation_id, user_id, sections),
        score_threshold=s.glossary_min_score,
        limit=limit,
        with_payload=True,
    )
    return [
        GlossaryHit.model_validate(
            {"id": str(p.id), **(p.payload or {}), "query": text, "score": p.score}
        )
        for p in resp.points
    ]
