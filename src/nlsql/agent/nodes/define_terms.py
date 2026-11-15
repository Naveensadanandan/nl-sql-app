"""define_terms node (strong model): draft schema-based definitions for terms the glossary missed.

Runs after the lookups join. For each business_term / metric entity with no glossary hit it
asks the model for a definition grounded in the table schema. Drafts are `proposed`. A draft that
cites a column missing from the schema is dropped, so generate_sql reports the term as missing
instead of using a made-up definition.

Drafts that the final answer used are saved to the glossary as `proposed` by
remember_definitions, so later questions reuse the same reading until a user edits it.
"""

from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from nlsql.agent.grounding import known_columns, question_block, schema_block, undefined_terms
from nlsql.agent.prompting import block, line, load_prompt
from nlsql.agent.schemas import DefinitionDrafts, ProposedDefinition
from nlsql.agent.state import AgentState
from nlsql.core.llm import strong_llm


def _grounded(d: ProposedDefinition, known: set[str], wanted: set[str]) -> bool:
    return (
        d.term in wanted
        and d.feasible
        and bool(d.definition.strip() and d.sql_expression.strip())
        and bool(d.columns_used)
        and all(c.lower() in known for c in d.columns_used)
    )


async def define_terms(state: AgentState) -> dict[str, Any]:
    terms = undefined_terms(state)
    if not terms or not state.get("schema"):
        return {"proposed_definitions": []}

    ctx = state.get("business_context")
    listed = "\n".join(f"- {line(t)}" for t in terms)
    human = (
        f"{block('schema', schema_block(state['schema']))}\n"
        f"<undefined_terms>\n{listed}\n</undefined_terms>\n"
        + (f"{block('business_context', ctx)}\n" if ctx else "")
        + question_block(state)
    )
    drafts: DefinitionDrafts = await strong_llm(DefinitionDrafts).ainvoke(
        [SystemMessage(content=load_prompt("define_terms")), HumanMessage(content=human)]
    )
    known, wanted = known_columns(state["schema"]), set(terms)
    return {"proposed_definitions": [d for d in drafts.definitions if _grounded(d, known, wanted)]}
