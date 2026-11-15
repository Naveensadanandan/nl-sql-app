"""define_terms node (strong model): draft schema-based definitions for terms the glossary missed.

Runs after the glossary/value lookups join. For each business_term / metric entity with no
glossary hit it asks the model for a definition grounded in the table schema. Drafts are always
`proposed` (never written to the glossary here); one that cites a column missing from the schema
is dropped, so generate_sql still reports it as missing instead of using a made-up definition.
"""

from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from nlsql.agent.nodes.generate_sql import (
    _schema_block,
    _undefined_terms,
    clean,
    line,
    strong_llm,
)
from nlsql.agent.schemas import DefinitionDrafts, ProposedDefinition
from nlsql.agent.state import AgentState

_PROMPT = (Path(__file__).parent.parent / "prompts" / "define_terms.md").read_text(encoding="utf-8")


def _known_columns(schema: Any) -> set[str]:
    tables = schema if isinstance(schema, list) else [schema]
    return {f"{t['table']}.{c['name']}".lower() for t in tables for c in t.get("columns", [])}


def _grounded(d: ProposedDefinition, known: set[str], wanted: set[str]) -> bool:
    return (
        d.term in wanted
        and d.feasible
        and bool(d.definition.strip() and d.sql_expression.strip())
        and bool(d.columns_used)
        and all(c.lower() in known for c in d.columns_used)
    )


async def define_terms(state: AgentState) -> dict[str, Any]:
    terms = _undefined_terms(state)
    if not terms or not state.get("schema"):
        return {"proposed_definitions": []}

    ctx = state.get("business_context")
    listed = "\n".join(f"- {line(t)}" for t in terms)
    human = (
        f"<schema>\n{clean(_schema_block(state['schema']))}\n</schema>\n"
        f"<undefined_terms>\n{listed}\n</undefined_terms>\n"
        + (f"<business_context>\n{clean(ctx)}\n</business_context>\n" if ctx else "")
        + f"<question>\n{clean(state['question'])}\n</question>"
    )
    drafts: DefinitionDrafts = await strong_llm(DefinitionDrafts).ainvoke(
        [SystemMessage(content=_PROMPT), HumanMessage(content=human)]
    )
    known, wanted = _known_columns(state["schema"]), set(terms)
    return {"proposed_definitions": [d for d in drafts.definitions if _grounded(d, known, wanted)]}
