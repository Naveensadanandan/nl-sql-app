"""Lookup nodes that run in parallel after extract_entities (no LLM).

- resolve_values:    db_value entities -> exact stored values via Elasticsearch (BM25, then a
                     kNN fallback for words that mean a value but are spelled differently)
- retrieve_glossary: business_term / metric entities -> approved or proposed definitions (Qdrant)

Both are safe to run before the servers exist: if the URL setting or organisation_id is
missing they return "nothing found" results instead of failing. Questions on the shared demo
data look up the demo organisation's knowledge (registry.knowledge_organisation), never the
asker's, so a user's own glossary cannot change demo answers. Value lookups run
concurrently; glossary lookups go to Qdrant as one batch (one embedding pass, one round trip).
"""

import asyncio
from typing import Any

from nlsql.agent.schemas import ValueMatch
from nlsql.agent.state import AgentState
from nlsql.core.config import get_settings
from nlsql.retrieval.glossary_search import search_glossary_batch
from nlsql.retrieval.value_search import (
    decide,
    decide_semantic,
    search_values,
    search_values_semantic,
)
from nlsql.sources.registry import knowledge_organisation, target_for


async def resolve_values(state: AgentState) -> dict[str, Any]:
    values = [e for e in state.get("entities", []) if e.kind == "db_value"]
    org = knowledge_organisation(state.get("source_id"), state.get("organisation_id"))
    if not values:
        return {"resolved_values": []}
    if not get_settings().es_url or not org:
        return {"resolved_values": [ValueMatch(text=e.text, status="skipped") for e in values]}
    # Only values from the tables this question can query (the selected data source, or the
    # shared demo tables), so stray values in the index never reach the answer.
    tables = target_for(state.get("source_id"), state.get("organisation_id")).tables
    matches = await asyncio.gather(*(_resolve(e.text, org, tables) for e in values))
    return {"resolved_values": list(matches)}


async def _resolve(text: str, org: str, tables: list[str]) -> ValueMatch:
    """Lexical (BM25) first; semantic (kNN) only when nothing matched by spelling."""
    match = decide(text, await search_values(text, org, tables=tables))
    if match.status != "not_found" or not get_settings().value_semantic_enabled:
        return match
    return decide_semantic(text, await search_values_semantic(text, org, tables=tables))


async def retrieve_glossary(state: AgentState) -> dict[str, Any]:
    terms = [e for e in state.get("entities", []) if e.kind in ("business_term", "metric")]
    source_id = state.get("source_id")
    org = knowledge_organisation(source_id, state.get("organisation_id"))
    if not terms or not get_settings().qdrant_url or not org:
        return {"glossary_hits": []}
    # Demo knowledge is org-wide only: no private entries of the asker apply to it.
    user_id = state.get("user_id") if source_id else None
    results = await search_glossary_batch([t.text for t in terms], org, user_id)
    # A confirmed definition wins: drop proposed hits for a term that also has an approved one.
    results = [
        [h for h in hits if h.status == "approved"]
        if any(h.status == "approved" for h in hits)
        else hits
        for hits in results
    ]
    # Same entry can match several terms; keep its best-scoring hit.
    best: dict[str, Any] = {}
    for hit in (h for hits in results for h in hits):
        if hit.id not in best or hit.score > best[hit.id].score:
            best[hit.id] = hit
    return {"glossary_hits": sorted(best.values(), key=lambda h: h.score, reverse=True)}
