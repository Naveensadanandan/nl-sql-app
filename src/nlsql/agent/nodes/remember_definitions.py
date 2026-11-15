"""remember_definitions node (no LLM): save drafted definitions so answers stay consistent.

After a successful answer, every definition define_terms drafted for this question is saved to
the glossary as `proposed` (org-wide). The next question that uses the term retrieves that
saved definition instead of drafting a new one, so the same term means the same thing every
time until a user edits or approves it (admin glossary upload of the same name and section
replaces the draft). Existing entries are never overwritten. The write runs in the background
(async node: spawn needs the running event loop).
"""

from typing import Any

from nlsql.agent.state import AgentState, outcome_of
from nlsql.core.background import spawn
from nlsql.core.config import get_settings
from nlsql.retrieval.glossary_search import client
from nlsql.worker.indexing import GlossaryRow, save_proposed


async def remember_definitions(state: AgentState) -> dict[str, Any]:
    org = state.get("organisation_id")
    drafts = [d for d in state.get("proposed_definitions", []) if d.feasible]
    if not drafts or not org or not get_settings().qdrant_url or outcome_of(state) != "success":
        return {}
    kinds = {e.text: e.kind for e in state.get("entities", [])}
    rows = [
        GlossaryRow(
            name=d.term,
            description=d.definition,
            section="metric" if kinds.get(d.term) == "metric" else "business_definition",
            sql_expression=d.sql_expression,
            status="proposed",
        )
        for d in drafts
    ]
    spawn(save_proposed(client(), org, rows), name="save_proposed_definitions")
    return {}
