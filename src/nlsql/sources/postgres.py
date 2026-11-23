"""Check a user's Postgres connection before it is saved (system-design §3).

The user must connect with a READ-ONLY role. `inspect_postgres` connects in a read-only
transaction with short timeouts, refuses a role that can write (superuser, CREATE on the
schema, or INSERT/UPDATE/DELETE/TRUNCATE on any table), and lists the tables the role can
SELECT. Error messages never echo the URL, host or server text, so the check cannot be used to
probe networks or leak credentials.

Recommended role (run as the database owner; shown in the UI too):
    CREATE ROLE nlsql_reader LOGIN PASSWORD '...';
    GRANT CONNECT ON DATABASE mydb TO nlsql_reader;
    GRANT USAGE ON SCHEMA public TO nlsql_reader;
    GRANT SELECT ON ALL TABLES IN SCHEMA public TO nlsql_reader;
"""

import re

import psycopg
from psycopg.conninfo import conninfo_to_dict

from nlsql.core.config import _with_driver, get_settings

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")

READ_ONLY_HELP = (
    "Connect with a read-only role: GRANT CONNECT, USAGE on the schema and SELECT on the "
    "tables only (no superuser, no table owner, no INSERT/UPDATE/DELETE/TRUNCATE/CREATE)."
)


class SourceError(ValueError):
    """The source cannot be used. The message is safe to show to the user."""


# Only plain connection settings. No service / passfile / options: they could pull credentials
# or settings from the API server's own environment (~/.pgpass, PG* variables, pg_service.conf).
_ALLOWED_PARAMS = {"host", "port", "dbname", "user", "password", "sslmode", "connect_timeout"}
_LOOPBACK = {"localhost", "127.0.0.1", "::1", ""}


def _endpoint(params: dict[str, str]) -> tuple[str, str, str]:
    host = params.get("host", "").lower()
    return (
        "loopback" if host in _LOOPBACK else host,
        params.get("port", "5432"),
        params.get("dbname", ""),
    )


def normalize_dsn(dsn: str) -> str:
    """Plain postgresql:// URL. Only an explicit user + password login to one host is allowed."""
    dsn = dsn.strip()
    if len(dsn) > 1000 or not re.match(r"^postgres(ql)?(\+\w+)?://", dsn):
        raise SourceError("the URL must look like postgresql://user:password@host:5432/dbname")
    url = _with_driver(dsn, None)
    try:
        params = conninfo_to_dict(url)
    except psycopg.ProgrammingError as e:
        raise SourceError("the URL could not be parsed") from e
    extra = sorted(set(params) - _ALLOWED_PARAMS)
    if extra:
        raise SourceError(f"unsupported URL parameter(s): {', '.join(extra)}")
    host = str(params.get("host", ""))
    if not host or host.startswith("/") or "," in host:
        raise SourceError("give one host name or IP address (no sockets, no host lists)")
    if not params.get("user") or not params.get("password"):
        raise SourceError("the URL must include a user name and a password")
    s = get_settings()
    app_dbs = {
        _endpoint({k: str(v) for k, v in conninfo_to_dict(u).items()})
        for u in (s.psycopg_url, s.results_psycopg_url)
    }
    if _endpoint({k: str(v) for k, v in params.items()}) in app_dbs:
        raise SourceError("that is this app's own database; connect your own database instead")
    return url


def check_identifier(name: str, what: str) -> str:
    if not _IDENT.match(name):
        raise SourceError(f"invalid {what} name: {name[:70]!r}")
    return name


def _connect(dsn: str) -> psycopg.Connection:
    try:
        return psycopg.connect(normalize_dsn(dsn), connect_timeout=5, autocommit=False)
    except psycopg.Error as e:
        raise SourceError(
            "could not connect: check host, port, database, user and password, and that the "
            "server accepts connections from the app"
        ) from e


def _check_role(cur: psycopg.Cursor) -> None:
    """Refuse a superuser or a role that can write to any table (inside a read-only txn)."""
    cur.execute("SET LOCAL statement_timeout = '10s'")
    cur.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
    row = cur.fetchone()
    if row is None or row[0]:
        raise SourceError(f"this role is a superuser. {READ_ONLY_HELP}")
    cur.execute(
        """
        SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind IN ('r', 'p')
          AND n.nspname NOT IN ('pg_catalog', 'information_schema')
          AND n.nspname NOT LIKE 'pg_toast%'
          AND has_table_privilege(c.oid, 'INSERT, UPDATE, DELETE, TRUNCATE')
        """
    )
    writable = (cur.fetchone() or (0,))[0]
    if writable:
        raise SourceError(f"this role can write to {writable} table(s). {READ_ONLY_HELP}")


_READABLE_TABLES = """
    SELECT n.nspname, c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f')
      AND n.nspname NOT IN ('pg_catalog', 'information_schema')
      AND n.nspname NOT LIKE 'pg_toast%%' AND n.nspname NOT LIKE 'pg_temp%%'
      AND has_schema_privilege(n.oid, 'USAGE')
      AND has_table_privilege(c.oid, 'SELECT')
    ORDER BY 1, 2
    LIMIT %s
"""
DISCOVER_MAX_TABLES = 2000  # bound on what a browse returns (the UI shows it in a dropdown)


def discover_postgres(dsn: str) -> list[dict[str, object]]:
    """Check the role is read-only, then list each schema it can read and its readable tables.

    A schema where the role may CREATE is listed with `blocked` set (it cannot be used), so
    the user sees why instead of the schema silently missing.
    """
    conn = _connect(dsn)
    try:
        conn.read_only = True
        with conn.cursor() as cur:
            _check_role(cur)
            cur.execute(_READABLE_TABLES, (DISCOVER_MAX_TABLES,))
            pairs = cur.fetchall()
            schemas = sorted({str(sch) for sch, _ in pairs if _IDENT.match(str(sch))})
            blocked: set[str] = set()
            if schemas:
                cur.execute(
                    "SELECT nspname FROM pg_namespace WHERE nspname = ANY(%s) "
                    "AND has_schema_privilege(oid, 'CREATE')",
                    (schemas,),
                )
                blocked = {r[0] for r in cur.fetchall()}
    except psycopg.Error as e:
        raise SourceError(f"the connection check failed ({type(e).__name__})") from e
    finally:
        conn.close()
    out: list[dict[str, object]] = []
    for sch in schemas:
        tables = [str(t) for s_, t in pairs if s_ == sch and _IDENT.match(str(t))]
        out.append(
            {
                "schema_name": sch,
                "tables": tables,
                "blocked": (
                    f"this role may CREATE in schema {sch!r}; grant USAGE only"
                    if sch in blocked
                    else None
                ),
            }
        )
    if not out:
        raise SourceError(f"this role cannot read any table. {READ_ONLY_HELP}")
    return out


def inspect_postgres(dsn: str, schema: str, tables: list[str] | None) -> list[str]:
    """Validate the connection is read-only; return the tables the agent may query."""
    check_identifier(schema, "schema")
    for t in tables or []:
        check_identifier(t, "table")
    conn = _connect(dsn)
    try:
        conn.read_only = True
        with conn.cursor() as cur:
            _check_role(cur)
            cur.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = %s), "
                "has_schema_privilege(current_user, (SELECT oid FROM pg_namespace "
                "WHERE nspname = %s), 'CREATE')",
                (schema, schema),
            )
            exists, can_create = cur.fetchone() or (False, False)
            if not exists:
                raise SourceError(f"schema {schema!r} not found (or not visible to this role)")
            if can_create:
                raise SourceError(f"this role may CREATE in schema {schema!r}. {READ_ONLY_HELP}")
            cur.execute(
                """
                SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = %s AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
                  AND has_table_privilege(c.oid, 'SELECT')
                ORDER BY 1
                """,
                (schema,),
            )
            readable = [r[0] for r in cur.fetchall()]
    except psycopg.Error as e:
        raise SourceError(f"the connection check failed ({type(e).__name__})") from e
    finally:
        conn.close()

    readable = [t for t in readable if _IDENT.match(t)]  # names the validator can handle
    if tables:
        missing = sorted(set(tables) - set(readable))
        if missing:
            raise SourceError(f"table(s) not found or not readable: {', '.join(missing)}")
        readable = [t for t in readable if t in set(tables)]
    if not readable:
        raise SourceError(f"no readable tables in schema {schema!r}")
    cap = get_settings().source_max_tables
    if len(readable) > cap:
        raise SourceError(f"{len(readable)} tables selected; pick at most {cap}")
    return readable
