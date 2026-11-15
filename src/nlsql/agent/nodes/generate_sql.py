"""generate_sql node (strong model): schema + approved definitions + resolved values -> SQL.

Also holds `grounding_context`, shared with debug_sql so both prompts see the same facts.
"""

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openrouter import ChatOpenRouter
from pydantic import BaseModel, SecretStr

from nlsql.agent.schemas import SqlGeneration
from nlsql.agent.state import AgentState
from nlsql.core.config import get_settings

_PROMPT = (Path(__file__).parent.parent / "prompts" / "generate_sql.md").read_text(encoding="utf-8")
_WS = re.compile(r"\s+")


def clean(text: object) -> str:
    """Untrusted text goes inside XML-ish delimiters: escape angle brackets so none can close one."""
    return str(text).replace("<", "&lt;").replace(">", "&gt;")


def line(text: object) -> str:
    """clean() plus single-line, so a stored value cannot fake extra list entries."""
    return _WS.sub(" ", clean(text)).strip()


@lru_cache
def strong_llm(output: type[BaseModel]) -> Any:
    key = os.environ.get("OPENROUTER_API_KEY")
    llm = ChatOpenRouter(
        model=get_settings().strong_model,
        temperature=0,
        api_key=SecretStr(key) if key else None,
    )
    return llm.with_structured_output(output)


def _schema_block(schema: Any) -> str:
    tables = schema if isinstance(schema, list) else [schema]
    lines = []
    for t in tables:
        cols = ", ".join(
            f'"{c["name"]}" {c["type"]}{"" if c.get("nullable", True) else " NOT NULL"}'
            for c in t.get("columns", [])
        )
        lines.append(f"{t['table']}({cols})")
        if t.get("primary_key"):
            lines.append(f"  primary key: {', '.join(t['primary_key'])}")
        for fk in t.get("foreign_keys", []):
            lines.append(
                f"  foreign key: {', '.join(fk['constrained_columns'])} -> "
                f"{fk['referred_table']}({', '.join(fk['referred_columns'])})"
            )
    return "\n".join(lines)


def _undefined_terms(state: AgentState) -> list[str]:
    covered = {h.query for h in state.get("glossary_hits", [])}
    return [
        e.text
        for e in state.get("entities", [])
        if e.kind in ("business_term", "metric") and e.text not in covered
    ]


def grounding_context(state: AgentState) -> str:
    """Everything the model may rely on besides the question, as delimited blocks."""
    defs = [
        f"- {line(h.name)}: {line(h.description)}"
        + (f" (synonyms: {line(', '.join(h.synonyms))})" if h.synonyms else "")
        for h in state.get("glossary_hits", [])
    ]
    resolved, unverified = [], []
    for v in state.get("resolved_values", []):
        if v.status == "resolved" and v.match:
            m = v.match
            where = f"{m.table}.{m.column}" if m.table and m.column else "unknown column"
            resolved.append(f"- \"{line(v.text)}\" -> {line(where)} = '{line(m.name)}'")
        else:
            options = "; ".join(
                f"{line(c.table)}.{line(c.column)} = '{line(c.name)}'" for c in v.candidates[:5]
            )
            unverified.append(
                f'- "{line(v.text)}" ({line(v.status)})'
                + (f" candidates: {options}" if options else "")
            )
    undefined = [f"- {line(t)}" for t in _undefined_terms(state)]
    ctx = state.get("business_context")
    return "\n".join(
        [
            f"<schema>\n{clean(_schema_block(state['schema']))}\n</schema>",
            f"<definitions>\n{chr(10).join(defs) or '(none)'}\n</definitions>",
            f"<resolved_values>\n{chr(10).join(resolved) or '(none)'}\n</resolved_values>",
            f"<unverified_values>\n{chr(10).join(unverified) or '(none)'}\n</unverified_values>",
            f"<undefined_terms>\n{chr(10).join(undefined) or '(none)'}\n</undefined_terms>",
        ]
        + ([f"<business_context>\n{clean(ctx)}\n</business_context>"] if ctx else [])
    )


async def generate_sql(state: AgentState) -> dict[str, Any]:
    human = f"{grounding_context(state)}\n<question>\n{clean(state['question'])}\n</question>"
    gen: SqlGeneration = await strong_llm(SqlGeneration).ainvoke(
        [SystemMessage(content=_PROMPT), HumanMessage(content=human)]
    )
    usable = bool(gen.sql and gen.sql.strip()) and not gen.missing_definitions
    return {
        "generation": gen,
        "sql": gen.sql.strip() if usable and gen.sql else "",  # "" clears any earlier turn's SQL
        "sql_error": None,
        "debug_attempts": 0,
        "sql_history": [],
    }


def route_after_generate(state: AgentState) -> Literal["validate", "respond"]:
    """No SQL means the model asked for a definition: skip straight to the reply."""
    return "validate" if state.get("sql") else "respond"
