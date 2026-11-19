"""Knowledge for the shared demo data, kept under one reserved organisation.

Questions on the shared demo tables (no source_id) look up glossary terms in Qdrant and values
in Elasticsearch under `settings.demo_organisation_id`, not the asker's organisation (see
sources.registry.knowledge_organisation). Every user's demo then gets the same definitions and
value matches, and nobody's own glossary changes demo answers.

`seed_demo_knowledge()` runs in the background at API start (and as
`python -m nlsql.knowledge.demo`):
- ES: re-indexes the distinct category values of the shared tables into
  values_<demo organisation>, adding the synonyms below so "women" finds "Female".
- Qdrant: adds the demo glossary entries that are missing. Existing entries are never
  overwritten, so an admin upload to the demo organisation (same name and section) wins.

The glossary below is written for the default demo table `retail_sales` (transaction_id, date,
customer_id, gender, age, product_category, quantity, price_per_unit, total_amount). For other
NLSQL_DATA_TABLES, upload a glossary to the demo organisation instead.
"""

import asyncio
import logging

from nlsql.core.config import get_settings
from nlsql.worker.indexing import GlossaryRow

log = logging.getLogger(__name__)

DEMO_GLOSSARY: list[GlossaryRow] = [
    GlossaryRow(
        name="revenue",
        description="Total sales value: the sum of total_amount over the transactions asked about.",
        section="metric",
        synonyms=["sales", "total sales", "turnover"],
        sql_expression="SUM(total_amount)",
    ),
    GlossaryRow(
        name="average order value",
        description="Average value of one transaction: revenue divided by the number of "
        "transactions.",
        section="metric",
        synonyms=["AOV", "avg order value", "basket size"],
        sql_expression="AVG(total_amount)",
    ),
    GlossaryRow(
        name="units sold",
        description="Total number of items sold: the sum of quantity.",
        section="metric",
        synonyms=["items sold", "quantity sold", "volume"],
        sql_expression="SUM(quantity)",
    ),
    GlossaryRow(
        name="best customers",
        description="The 5 customers (customer_id) with the highest revenue, sum of "
        "total_amount, in the period asked about.",
        section="business_definition",
        synonyms=["top customers", "biggest spenders", "key accounts"],
        sql_expression="customer_id ranked by SUM(total_amount) DESC, LIMIT 5",
    ),
    GlossaryRow(
        name="high value order",
        description="A transaction whose total_amount is 1000 or more.",
        section="business_definition",
        synonyms=["big order", "large order", "big ticket purchase"],
        sql_expression="total_amount >= 1000",
    ),
    GlossaryRow(
        name="young shoppers",
        description="Customers younger than 30 (age < 30).",
        section="business_definition",
        synonyms=["young customers", "youth", "gen z"],
        sql_expression="age < 30",
    ),
    GlossaryRow(
        name="premium products",
        description="Items sold at a price_per_unit of 300 or more.",
        section="business_definition",
        synonyms=["premium items", "luxury items", "high-end products"],
        sql_expression="price_per_unit >= 300",
    ),
]

# Extra words people use for stored demo values (lower-cased stored value -> synonyms).
DEMO_VALUE_SYNONYMS: dict[str, list[str]] = {
    "female": ["women", "woman", "ladies", "female customers"],
    "male": ["men", "man", "gents", "male customers"],
    "electronics": ["electronic items", "gadgets", "devices", "tech"],
    "clothing": ["clothes", "apparel", "fashion", "garments"],
    "beauty": ["cosmetics", "makeup", "skincare", "beauty products"],
}


async def seed_values() -> int:
    """Re-index the shared tables' category values under the demo organisation."""
    from nlsql.retrieval.value_search import client
    from nlsql.worker import indexing

    s = get_settings()
    tables = list(s.data_tables)
    if not tables:  # an empty list would scan every table in the schema: index nothing
        return 0
    docs, _skipped, _scanned = await asyncio.to_thread(
        indexing.collect_values, s.data_schema, tables, s.value_max_distinct
    )
    for d in docs:
        d["synonyms"] = DEMO_VALUE_SYNONYMS.get(d["name"].lower(), [])
    return await indexing.index_values(client(), s.demo_organisation_id, docs, tables)


async def seed_glossary() -> int:
    """Add the demo glossary entries that do not exist yet. Returns how many were added."""
    from nlsql.retrieval.glossary_search import client
    from nlsql.worker import indexing

    s = get_settings()
    qdrant = client()
    await indexing.ensure_glossary_collection(qdrant)
    ids = [indexing.point_id(s.demo_organisation_id, r) for r in DEMO_GLOSSARY]
    found = await qdrant.retrieve(s.glossary_collection, ids=ids, with_payload=False)
    existing = {str(p.id) for p in found}
    new = [r for r, pid in zip(DEMO_GLOSSARY, ids, strict=True) if pid not in existing]
    return await indexing.index_glossary(qdrant, s.demo_organisation_id, new) if new else 0


def _demo_org_is_taken() -> bool:
    """True if a real organisation (signed up before the id was reserved) owns the demo id."""
    from nlsql.core import app_db

    with app_db.connection() as conn, conn.cursor() as cur:
        cur.execute(
            app_db.q("SELECT 1 FROM {s}.organisations WHERE organisation_id = %s"),
            (get_settings().demo_organisation_id,),
        )
        return cur.fetchone() is not None


async def seed_demo_knowledge() -> None:
    """Seed ES values and the Qdrant glossary for the demo. Logs failures, never raises."""
    s = get_settings()
    if not s.shared_data_enabled:
        return
    try:
        taken = await asyncio.to_thread(_demo_org_is_taken)
    except Exception as e:  # noqa: BLE001 - app DB down: seeding below may still work
        log.warning("could not check the demo organisation: %s", type(e).__name__)
        taken = False
    if taken:
        log.warning(
            "organisation %r has users: its own data would mix into the demo. Set "
            "NLSQL_DEMO_ORGANISATION_ID to an unused id.",
            s.demo_organisation_id,
        )
    if s.es_url:
        try:
            n = await seed_values()
            log.info("demo values indexed: %d", n)
        except Exception as e:  # noqa: BLE001 - the demo still answers, values show not_found
            log.warning("demo value indexing failed: %s", type(e).__name__)
    if s.qdrant_url:
        try:
            n = await seed_glossary()
            log.info("demo glossary entries added: %d", n)
        except Exception as e:  # noqa: BLE001
            log.warning("demo glossary seeding failed: %s", type(e).__name__)


async def _main() -> None:
    from nlsql.retrieval import glossary_search, value_search

    try:
        await seed_demo_knowledge()
    finally:
        s = get_settings()
        if s.es_url:
            await value_search.client().close()
        if s.qdrant_url:
            await glossary_search.client().close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())
