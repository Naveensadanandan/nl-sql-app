"""CSV upload: one file becomes one table in the organisation's schema `ws_<organisation_id>`.

The table lives in the app Postgres (not a user database), so every source speaks one SQL
dialect and the same validator / read-only executor apply. Column names are sanitised, types
are inferred (bigint, numeric, boolean, date, timestamp, else text), and rows go in with COPY
as plain values. A table name that already exists is refused: delete that source first.

Tenant isolation is enforced by Postgres, not only by the SQL validator: each organisation has a
NOLOGIN role ws_<org>_reader with USAGE on ws_<org> and SELECT on its tables, nothing else.
Model SQL for a CSV source runs under `SET LOCAL ROLE ws_<org>_reader` (sql/executor.py), so
even SQL that slipped past the validator cannot read another workspace or the app tables.
The query role (NLSQL_QUERY_ROLE, else the writer itself) is made a member so it may SET ROLE.
The writer needs CREATEROLE (or superuser) for this.
"""

import csv
import io
import re

from psycopg import sql

from nlsql.core import app_db
from nlsql.core.config import get_settings
from nlsql.core.query_store import safe_column_names
from nlsql.sources.postgres import SourceError, check_identifier

MAX_COLUMNS = 200

_INT = re.compile(r"^[-+]?\d{1,18}$")
_NUM = re.compile(r"^[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d{1,3})?$")
_BOOL = re.compile(r"^(true|false)$", re.IGNORECASE)
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TS = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2}(\.\d{1,6})?)?$")


def workspace_role(organisation_id: str) -> str:
    """ws_<org>_reader: may read ws_<org> and nothing else."""
    return check_identifier(f"ws_{organisation_id}_reader", "workspace role")


def workspace_schema(organisation_id: str) -> str:
    """ws_<org>. Sign-up only allows [a-z0-9_] org ids, so this is a plain identifier."""
    return check_identifier(f"ws_{organisation_id}", "workspace schema")


def table_name(name: str) -> str:
    """'Sales 2024.csv' -> 'sales_2024'."""
    base = re.sub(r"\.csv$", "", name.strip(), flags=re.IGNORECASE)
    clean = re.sub(r"[^a-z0-9_]+", "_", base.lower()).strip("_")[:50] or "upload"
    return clean if not clean[0].isdigit() else f"t_{clean}"


def _infer(values: list[str]) -> str:
    vals = [v for v in values if v != ""]
    if not vals:
        return "text"
    for pg_type, pattern in (
        ("bigint", _INT),
        ("numeric", _NUM),
        ("boolean", _BOOL),
        ("date", _DATE),
        ("timestamp", _TS),
    ):
        if all(pattern.match(v) for v in vals):
            return pg_type
    return "text"


def parse_csv(raw: bytes) -> tuple[list[str], list[str], list[list[str | None]]]:
    """(safe column names, inferred types, rows). Empty cells become NULL."""
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise SourceError("the file must be UTF-8 encoded CSV") from e
    try:
        records = list(csv.reader(io.StringIO(text)))
    except csv.Error as e:
        raise SourceError(f"not a valid CSV file: {e}") from e
    if len(records) < 2:
        raise SourceError("the CSV needs a header row and at least one data row")
    header, body = records[0], [r for r in records[1:] if any(c.strip() for c in r)]
    if not body:
        raise SourceError("the CSV has no data rows")
    if len(header) > MAX_COLUMNS:
        raise SourceError(f"more than {MAX_COLUMNS} columns")
    width = len(header)
    for i, r in enumerate(body, start=2):
        if len(r) != width:
            raise SourceError(f"line {i}: expected {width} values, found {len(r)}")
    columns = safe_column_names([h.strip() for h in header])
    types = [_infer([r[i].strip() for r in body]) for i in range(width)]
    rows = [[(c.strip() or None) for c in r] for r in body]
    return columns, types, rows


def load_csv(organisation_id: str, table: str, raw: bytes) -> tuple[str, int]:
    """Create ws_<org>.<table> and COPY the rows in. Returns (schema, row count)."""
    columns, types, rows = parse_csv(raw)
    schema = workspace_schema(organisation_id)
    check_identifier(table, "table")
    role = get_settings().query_role
    sch, tbl = sql.Identifier(schema), sql.Identifier(table)
    with app_db.connection() as conn, conn.cursor() as cur:
        cur.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sch))
        cur.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_schema = %s AND table_name = %s",
            (schema, table),
        )
        if cur.fetchone():
            raise SourceError(f"table {table!r} already exists; delete that source or rename")
        cur.execute(
            sql.SQL("CREATE TABLE {}.{} ({})").format(
                sch,
                tbl,
                sql.SQL(", ").join(
                    sql.SQL("{} {}").format(sql.Identifier(c), sql.SQL(t))
                    for c, t in zip(columns, types, strict=True)
                ),
            )
        )
        cols = sql.SQL(", ").join(sql.Identifier(c) for c in columns)
        with cur.copy(sql.SQL("COPY {}.{} ({}) FROM STDIN").format(sch, tbl, cols)) as copy:
            for r in rows:
                copy.write_row(r)
        reader = workspace_role(organisation_id)
        r_id = sql.Identifier(reader)
        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (reader,))
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (reader,))
        if not cur.fetchone():
            cur.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(r_id))
        cur.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(sch, r_id))
        cur.execute(sql.SQL("GRANT SELECT ON {}.{} TO {}").format(sch, tbl, r_id))
        member: sql.Composable = sql.SQL("CURRENT_USER")
        if role:
            member = sql.Identifier(check_identifier(role, "query role"))
        cur.execute(sql.SQL("GRANT {} TO {}").format(r_id, member))
    return schema, len(rows)


def drop_csv_table(organisation_id: str, table: str) -> None:
    """Remove an uploaded table (only ever inside the organisation's own ws_ schema)."""
    schema = workspace_schema(organisation_id)
    check_identifier(table, "table")
    with app_db.connection() as conn, conn.cursor() as cur:
        cur.execute(
            sql.SQL("DROP TABLE IF EXISTS {}.{}").format(
                sql.Identifier(schema), sql.Identifier(table)
            )
        )
