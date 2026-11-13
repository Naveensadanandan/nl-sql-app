"""The one definition of a valid organisation id, used by indexing, search and the API.

Ids are case-insensitive and stored lowercase, so "Acme" and "acme" are the same tenant in
Elasticsearch (index name) and in Qdrant (payload filter).
"""

import re

_SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def normalize_organisation_id(organisation_id: str) -> str:
    org = organisation_id.strip().lower()
    if not _SAFE_ID.match(org):
        raise ValueError("organisation_id must be 1-64 chars of [a-z0-9_-]")
    return org
