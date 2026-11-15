"""SQL safety validation (system-design §7, steps 1-4).

Every model-generated statement goes through `validate` before it can reach a database.
Execution-time protection (read-only transaction, timeout, row cap, cost guard) is in
`nlsql.sql.executor`; the two layers are deliberately redundant.
"""

from dataclasses import dataclass
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

_FORBIDDEN_NODE_NAMES = (
    "Insert", "Update", "Delete", "Drop", "Create", "Alter", "AlterTable", "Command", "Copy",
    "Into", "Merge", "TruncateTable", "Lock", "Set", "Use", "Pragma", "Transaction", "Commit",
    "Rollback", "Grant", "Revoke",
)  # fmt: skip
_FORBIDDEN_NODES = tuple(getattr(exp, n) for n in _FORBIDDEN_NODE_NAMES if hasattr(exp, n))

_BLOCKED_FUNC_PREFIXES = ("pg_", "dblink", "lo_", "has_", "inet_", "txid_")
_BLOCKED_FUNCS = {
    "set_config", "current_setting", "query_to_xml", "table_to_xml",
    "database_to_xml", "schema_to_xml", "cursor_to_xml",
    # server / session introspection: harmless to data, but leaks environment details
    "version", "current_version", "current_user", "session_user", "current_role",
    "current_database", "current_catalog", "current_schema", "current_schemas",
}  # fmt: skip


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    sql: str | None = None  # normalised SQL with LIMIT applied, set when ok
    error: str | None = None  # message written for the debugger / model, set when not ok


def allowed_from_schema(schema: Any) -> dict[str, set[str]]:
    """{table: {columns}} from fetch_schema output (a dict or list of dicts).

    Table names are lower-cased; column names are kept as stored.
    """
    items = schema if isinstance(schema, list) else [schema]
    return {
        t["table"].lower(): {c["name"] for c in t.get("columns", [])}
        for t in items
        if t and t.get("table")
    }


def _func_name(f: exp.Func) -> str:
    return (f.name if isinstance(f, exp.Anonymous) else f.sql_name()).lower()


def validate(
    sql: str,
    allowed: dict[str, set[str]],
    *,
    row_cap: int,
    schema_name: str = "public",
) -> ValidationResult:
    def fail(msg: str) -> ValidationResult:
        return ValidationResult(ok=False, error=msg)

    if not sql or not sql.strip():
        return fail("Empty SQL.")
    try:
        statements = [s for s in sqlglot.parse(sql, read="postgres") if s is not None]
    except SqlglotError as e:
        return fail(f"SQL syntax error: {str(e)[:300]}")
    if len(statements) != 1:
        return fail("Exactly one SQL statement is allowed.")
    tree = statements[0]
    if not isinstance(tree, exp.Query):
        return fail("Only a single SELECT or WITH ... SELECT statement is allowed.")

    bad = tree.find(*_FORBIDDEN_NODES)
    if bad is not None:
        return fail(f"Forbidden construct: {type(bad).__name__}. The query must be read-only.")

    for f in tree.find_all(exp.Func):
        name = _func_name(f)
        if name in _BLOCKED_FUNCS or name.startswith(_BLOCKED_FUNC_PREFIXES):
            return fail(f"Function not allowed: {name}.")

    # Table allowlist. CTE names are legal table references inside the same query.
    # A CTE must not reuse a real table or catalog name: the inner reference of a non-recursive
    # CTE resolves to the real relation (e.g. WITH pg_roles AS (SELECT ... FROM pg_roles)).
    cte_names = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    for n in cte_names:
        if n in allowed or n.startswith("pg_"):
            return fail(f"CTE name '{n}' is reserved. Choose a different name for the CTE.")
    used_tables: set[str] = set()
    for t in tree.find_all(exp.Table):
        if isinstance(t.this, exp.Func):  # table function, e.g. generate_series; name-checked above
            continue
        name = t.name.lower()
        if name.startswith("pg_"):
            return fail("System catalog tables are not allowed.")
        if not t.db and name in cte_names:
            continue
        if t.db and t.db.lower() != schema_name.lower():
            return fail(f"Schema '{t.db}' is not allowed. Use only tables: {sorted(allowed)}.")
        if name not in allowed:
            return fail(f"Table '{t.name}' does not exist. Available tables: {sorted(allowed)}.")
        used_tables.add(name)

    # Column allowlist. Aliases (select aliases, CTE / subquery column lists) are legal names too.
    known = {c.lower() for cols in allowed.values() for c in cols}
    known |= {a.alias.lower() for a in tree.find_all(exp.Alias) if a.alias}
    for ta in tree.find_all(exp.TableAlias):
        known |= {c.name.lower() for c in ta.columns}
    unknown = sorted(
        {c.name for c in tree.find_all(exp.Column) if c.name != "*" and c.name.lower() not in known}
    )
    if unknown:
        avail = {t: sorted(allowed[t]) for t in sorted(used_tables)} or {
            t: sorted(c) for t, c in sorted(allowed.items())
        }
        return fail(f"Unknown column(s): {unknown}. Available columns by table: {avail}.")

    # Clamp or add LIMIT on the outermost query.
    # Anything other than a plain integer literal <= cap (LIMIT ALL, expressions, FETCH FIRST)
    # is replaced, so the cap cannot be bypassed.
    limit = tree.args.get("limit")
    value = limit.args.get("expression") if limit is not None else None
    within_cap = isinstance(value, exp.Literal) and value.is_int and int(value.this) <= row_cap
    if not within_cap:
        tree = tree.copy()
        tree.set("fetch", None)
        tree = tree.limit(row_cap)

    return ValidationResult(ok=True, sql=tree.sql(dialect="postgres"))
