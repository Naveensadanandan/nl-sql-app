"""What the SQL nodes may rely on, and the record of what they did rely on.

- `grounding_context`: the delimited fact blocks shared by generate_sql and debug_sql.
- `undefined_terms`: business terms / metrics with no definition yet (define_terms input).
- `build_interpretation`: the deterministic explanation stored with every answer.
"""

from typing import Any

from nlsql.agent.prompting import block, items, line
from nlsql.agent.schemas import (
    VAGUE_KINDS,
    DefinitionUse,
    Interpretation,
    Repair,
    ValueMapping,
)
from nlsql.agent.state import AgentState, outcome_of


def as_tables(schema: Any) -> list[dict[str, Any]]:
    """fetch_schema output as a list of table dicts (accepts a single dict too)."""
    if not schema:
        return []
    return list(schema) if isinstance(schema, list) else [schema]


def schema_block(schema: Any) -> str:
    lines = []
    for t in as_tables(schema):
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


def known_columns(schema: Any) -> set[str]:
    """{'table.column', ...} lower-cased."""
    return {
        f"{t['table']}.{c['name']}".lower() for t in as_tables(schema) for c in t.get("columns", [])
    }


def undefined_terms(state: AgentState) -> list[str]:
    covered = {h.query for h in state.get("glossary_hits", [])}
    covered |= {d.term for d in state.get("proposed_definitions", []) if d.feasible}
    return [
        e.text
        for e in state.get("entities", [])
        if e.kind in ("business_term", "metric") and e.text not in covered
    ]


def unclear_terms(state: AgentState) -> list[str]:
    """Vague periods / cutoffs found by extract_entities ('recently', 'high')."""
    return [e.text for e in state.get("entities", []) if e.kind in VAGUE_KINDS]


def grounding_context(state: AgentState) -> str:
    """Everything the model may rely on besides the question, as delimited blocks."""
    hits = state.get("glossary_hits", [])
    defs = [
        f"- {line(h.name)}: {line(h.description)}"
        + (f" (synonyms: {line(', '.join(h.synonyms))})" if h.synonyms else "")
        + (f" | SQL: {line(h.sql_expression)}" if h.sql_expression else "")
        for h in hits
        if h.status == "approved"
    ]
    proposed = [
        f"- {line(h.name)}: {line(h.description)}"
        + (f" | SQL: {line(h.sql_expression)}" if h.sql_expression else "")
        for h in hits
        if h.status == "proposed"
    ] + [
        f"- {line(d.term)}: {line(d.definition)} | SQL: {line(d.sql_expression)}"
        for d in state.get("proposed_definitions", [])
        if d.feasible
    ]
    resolved, unverified = [], []
    for v in state.get("resolved_values", []):
        if v.status == "resolved" and v.match:
            m = v.match
            where = f"{m.table}.{m.column}" if m.table and m.column else "unknown column"
            resolved.append(
                f"- \"{line(v.text)}\" -> {line(where)} = '{line(m.name)}'"
                + (" (matched by meaning)" if v.method == "semantic" else "")
            )
        else:
            options = "; ".join(
                f"{line(c.table)}.{line(c.column)} = '{line(c.name)}'" for c in v.candidates[:5]
            )
            unverified.append(
                f'- "{line(v.text)}" ({line(v.status)})'
                + (f" candidates: {options}" if options else "")
            )
    ctx = state.get("business_context")
    return "\n".join(
        [
            block("schema", schema_block(state.get("schema"))),
            items("definitions", defs),
            items("proposed_definitions", proposed),
            items("resolved_values", resolved),
            items("unverified_values", unverified),
            items("undefined_terms", [f"- {line(t)}" for t in undefined_terms(state)]),
            items("unclear_terms", [f"- {line(t)}" for t in unclear_terms(state)]),
        ]
        + ([block("business_context", ctx)] if ctx else [])
    )


def question_block(state: AgentState) -> str:
    return block("question", state["question"])


# --- interpretation ---------------------------------------------------------------------------


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


def build_interpretation(state: AgentState) -> Interpretation:
    gen = state.get("generation")
    used = {_norm(t) for t in (gen.used_terms if gen else [])}

    definitions = [
        DefinitionUse(
            term=h.name,
            definition=h.description,
            sql_expression=h.sql_expression,
            status=h.status,
            origin="glossary",
            used=_norm(h.name) in used or _norm(h.query) in used,
            score=h.score,
            glossary_id=h.id,
        )
        for h in state.get("glossary_hits", [])
    ] + [
        DefinitionUse(
            term=d.term,
            definition=d.definition,
            sql_expression=d.sql_expression,
            status="proposed",
            origin="drafted_now",
            used=_norm(d.term) in used,
        )
        for d in state.get("proposed_definitions", [])
        if d.feasible
    ]
    known = {_norm(d.term) for d in definitions} | {
        _norm(h.query) for h in state.get("glossary_hits", [])
    }
    # A metric that is plainly a schema column may be used without a definition (prompt rule 3).
    columns = {c.split(".", 1)[1] for c in known_columns(state.get("schema"))}
    ungrounded = sorted(t for t in used if t not in known and t.replace(" ", "_") not in columns)

    values = []
    for v in state.get("resolved_values", []):
        m = v.match if v.status == "resolved" else None
        values.append(
            ValueMapping(
                phrase=v.text,
                status=v.status,
                table=m.table if m else None,
                column=m.column if m else None,
                value=m.name if m else None,
                alternatives=[
                    f"{c.table}.{c.column} = '{c.name}'"
                    for c in v.candidates[:5]
                    if m is None or c.id != m.id
                ],
                method=v.method,
            )
        )
    # A semantic match is a guess about meaning, not spelling: say so (CLAUDE.md conventions).
    semantic = [
        f"\"{v.text}\" was read as {v.match.table}.{v.match.column} = '{v.match.name}' "
        "(similar meaning, not the same spelling)"
        for v in state.get("resolved_values", [])
        if v.status == "resolved" and v.method == "semantic" and v.match
    ]

    result = state.get("result")
    outcome = outcome_of(state)
    return Interpretation(
        outcome=outcome,
        sql=state.get("sql") or None,
        definitions=definitions,
        values=values,
        unclear_terms=unclear_terms(state),
        assumptions=semantic + (list(gen.assumptions) if gen else []),
        missing_definitions=list(gen.missing_definitions) if gen else [],
        repairs=[Repair(**h) for h in state.get("sql_history", [])],
        ungrounded_terms=ungrounded if outcome == "success" else [],
        row_count=result.row_count if result else 0,
        truncated=result.truncated if result else False,
    )
