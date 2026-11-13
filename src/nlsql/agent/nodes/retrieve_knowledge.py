"""Lookup nodes that run in parallel after extract_entities (no LLM).

- resolve_values:    db_value entities -> exact stored values via Elasticsearch
- retrieve_glossary: business_term / metric entities -> approved definitions via Qdrant

Both are safe to run before the servers exist: if the URL setting or organisation_id is
missing they return "nothing found" results instead of failing, and the downstream
clarify step (not built yet) asks the user. Lookups for several entities run concurrently.
"""

import asyncio
from typing import Any

from nlsql.agent.schemas import ValueMatch
from nlsql.agent.state import AgentState
from nlsql.core.config import get_settings
from nlsql.retrieval.glossary_search import search_glossary
from nlsql.retrieval.value_search import decide, search_values


async def resolve_values(state: AgentState) -> dict[str, Any]:
    values = [e for e in state.get("entities", []) if e.kind == "db_value"]
    org = state.get("organisation_id")
    if not values:
        return {"resolved_values": []}
    if not get_settings().es_url or not org:
        return {"resolved_values": [ValueMatch(text=e.text, status="skipped") for e in values]}
    hits = await asyncio.gather(*(search_values(e.text, org) for e in values))
    return {"resolved_values": [decide(e.text, h) for e, h in zip(values, hits, strict=True)]}


async def retrieve_glossary(state: AgentState) -> dict[str, Any]:
    terms = [e for e in state.get("entities", []) if e.kind in ("business_term", "metric")]
    org = state.get("organisation_id")
    if not terms or not get_settings().qdrant_url or not org:
        return {"glossary_hits": []}
    user = state.get("user_id")
    results = await asyncio.gather(*(search_glossary(t.text, org, user) for t in terms))
    # Same entry can match several terms; keep its best-scoring hit.
    best: dict[str, Any] = {}
    for hit in (h for hits in results for h in hits):
        if hit.id not in best or hit.score > best[hit.id].score:
            best[hit.id] = hit
    return {"glossary_hits": sorted(best.values(), key=lambda h: h.score, reverse=True)}
