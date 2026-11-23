"""Index a new data source's category values into Elasticsearch (background, after creation).

Same index and layout as the admin value indexer (values_<organisation_id>), so the agent's
resolve_values finds them. Distinct values are read through the source's own read-only
connection. Progress is visible as data_sources.values_status: pending -> indexed | failed
(| skipped when Elasticsearch is not configured).
"""

import asyncio
import logging

from nlsql.core.config import get_settings
from nlsql.retrieval.value_search import client, index_name
from nlsql.sources import registry
from nlsql.worker import indexing

log = logging.getLogger(__name__)


async def forget_values(organisation_id: str, tables: list[str]) -> None:
    """Drop indexed values of tables a source no longer exposes (best effort)."""
    if not tables or not get_settings().es_url:
        return
    try:
        await client().delete_by_query(
            index=index_name(organisation_id),
            query={"terms": {"table": tables}},
            refresh=True,
            conflicts="proceed",
            ignore_unavailable=True,
        )
    except Exception as e:  # noqa: BLE001 - stale values only cost match quality
        log.warning("could not drop values of removed tables: %s", type(e).__name__)


async def index_source_values(source_id: str, organisation_id: str) -> None:
    s = get_settings()
    if not s.es_url:
        await asyncio.to_thread(registry.set_values_status, source_id, "skipped")
        return
    try:
        target = registry.target_for(source_id, organisation_id)
        docs, _skipped, _scanned = await asyncio.to_thread(
            indexing.collect_values,
            target.schema_name,
            target.tables,
            s.value_max_distinct,
            target.url,
        )
        await indexing.index_values(client(), organisation_id, docs, target.tables)
    except Exception as e:
        # Type only: messages can contain values or connection details.
        log.warning("value indexing failed for %s: %s", source_id, type(e).__name__)
        await asyncio.to_thread(registry.set_values_status, source_id, "failed")
        return
    await asyncio.to_thread(registry.set_values_status, source_id, "indexed")
