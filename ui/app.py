"""NL-SQL web UI (Streamlit).  Run:  streamlit run ui/app.py   (API must be on NLSQL_API_URL)

Screens
- Log in / Create account (no token yet)
- Sidebar: who is logged in, data source picker, connect Postgres / upload CSV, log out
- Main: ask box + answer + SQL + result table (left), and on the right expandable panels with
  the glossary terms (Qdrant) and value matches (Elasticsearch) used for the answer.

See docs/ui-and-auth.md.
"""

import contextlib
import time
from typing import Any

import pandas as pd
import streamlit as st
from api_client import API_URL, ASK_TIMEOUT, ApiError, call

st.set_page_config(page_title="NL-SQL", page_icon="🔎", layout="wide")

SHARED = "__shared__"
GRANT_SQL = """-- Run as the database owner. Replace mydb / public / the password.
CREATE ROLE nlsql_reader LOGIN PASSWORD 'choose-a-strong-password';
GRANT CONNECT ON DATABASE mydb TO nlsql_reader;
GRANT USAGE ON SCHEMA public TO nlsql_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO nlsql_reader;
-- tables created later:
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO nlsql_reader;"""

ss = st.session_state
ss.setdefault("token", None)
ss.setdefault("user", None)
ss.setdefault("history", [])  # newest first: {"ask": /ask response, "stored": /questions/{id}}
ss.setdefault("source", SHARED)


def api(method: str, path: str, **kwargs: Any) -> Any:
    """Authenticated call. A 401 means the session ended: drop the token and show log-in."""
    try:
        return call(method, path, token=ss.token, **kwargs)
    except ApiError as e:
        if e.status == 401 and ss.token:
            ss.token, ss.user, ss.history = None, None, []
            st.warning("Your session expired. Log in again.")
            st.rerun()
        raise


# ------------------------------------------------------------------------------- log-in page


def login_page() -> None:
    _, mid, _ = st.columns([1, 1.3, 1])
    with mid:
        st.title("🔎 NL-SQL")
        st.caption("Ask questions about your data in plain English.")
        login_tab, signup_tab = st.tabs(["Log in", "Create account"])
        with login_tab, st.form("login"):
            username = st.text_input("Username")
            password = st.text_input("Password", type="password")
            if st.form_submit_button("Log in", type="primary", width="stretch"):
                try:
                    out = call(
                        "POST", "/auth/login", json={"username": username, "password": password}
                    )
                except ApiError as e:
                    st.error(str(e))
                else:
                    ss.token, ss.user = out["access_token"], out["user"]
                    st.rerun()
        with signup_tab, st.form("signup"):
            new_user = st.text_input("Username", help="3-40 chars: a-z, 0-9, _ . -")
            org = st.text_input(
                "Organisation id",
                help="A new workspace for your data and glossary: 2-40 chars of a-z, 0-9, _",
            )
            pw1 = st.text_input("Password", type="password", help="At least 8 characters")
            pw2 = st.text_input("Repeat password", type="password")
            if st.form_submit_button("Create account", width="stretch"):
                if pw1 != pw2:
                    st.error("The passwords do not match.")
                else:
                    try:
                        call(
                            "POST",
                            "/auth/signup",
                            json={"username": new_user, "password": pw1, "organisation": org},
                        )
                    except ApiError as e:
                        st.error(str(e))
                    else:
                        st.success("Account created. Log in on the first tab.")
        st.caption(f"API: {API_URL}")


# ----------------------------------------------------------------------------------- sidebar


def sidebar() -> list[dict[str, Any]]:
    user = ss.user
    with st.sidebar:
        st.markdown(f"**{user['username']}** · org `{user['organisation_id']}`")
        if st.button("Log out", width="stretch"):
            with contextlib.suppress(ApiError):
                api("POST", "/auth/logout")
            ss.token, ss.user, ss.history = None, None, []
            st.rerun()

        st.divider()
        st.subheader("Data source")
        try:
            info = api("GET", "/sources")
        except ApiError as e:
            st.error(str(e))
            return []
        sources = info["sources"]
        options = ([SHARED] if info["shared_data_enabled"] else []) + [
            s["source_id"] for s in sources
        ]
        labels = {SHARED: "Shared demo data"} | {
            s["source_id"]: f"{'🐘' if s['kind'] == 'postgres' else '📄'} {s['name']}"
            for s in sources
        }
        if not options:
            st.info("Connect a database or upload a CSV to start.")
        else:
            # A source added below takes effect on the next run (the picker is drawn first).
            if "next_source" in ss:
                ss.source = ss.pop("next_source")
            if ss.source not in options:
                ss.source = options[0]
            st.selectbox("Ask questions about", options, key="source", format_func=labels.get)
            current = next((s for s in sources if s["source_id"] == ss.source), None)
            if current:
                st.caption(
                    f"schema `{current['schema_name']}` · tables: "
                    + ", ".join(f"`{t}`" for t in current["tables"])
                    + f" · value index: {current['values_status']}"
                )
                if st.button("Delete this source", type="secondary"):
                    try:
                        api("DELETE", f"/sources/{current['source_id']}")
                        st.rerun()
                    except ApiError as e:
                        st.error(str(e))

        with st.expander("➕ Add data source", expanded=not options):
            pg_tab, csv_tab = st.tabs(["Connect Postgres", "Upload CSV"])
            with pg_tab:
                if not info["postgres_enabled"]:
                    reason = info.get("postgres_disabled_reason") or "NLSQL_FERNET_KEY is not set"
                    st.error(
                        f"Database connections are turned off on the API server: {reason}. "
                        "Add a key to .env and restart the API (see docs/ui-and-auth.md)."
                    )
                    st.code(
                        'python -c "from cryptography.fernet import Fernet; '
                        'print(Fernet.generate_key().decode())"',
                        language="bash",
                    )
                st.caption(info["read_only_help"])
                with st.popover("Show the GRANT script"):
                    st.code(GRANT_SQL, language="sql")
                with st.form("pg", clear_on_submit=True):
                    name = st.text_input("Name", placeholder="Sales DB")
                    url = st.text_input(
                        "Connection URL",
                        type="password",
                        placeholder="postgresql://nlsql_reader:***@host:5432/mydb",
                        help="Stored encrypted. Never shown again.",
                    )
                    schema = st.text_input("Schema", value="public")
                    tables = st.text_input("Tables (optional)", placeholder="orders, customers")
                    if st.form_submit_button(
                        "Test & connect", disabled=not info["postgres_enabled"]
                    ):
                        body = {
                            "name": name or "My database",
                            "url": url,
                            "schema_name": schema or "public",
                            "tables": [t.strip() for t in tables.split(",") if t.strip()] or None,
                        }
                        try:
                            with st.spinner("Checking the connection is read-only…"):
                                src = api("POST", "/sources/postgres", json=body)
                        except ApiError as e:
                            st.error(str(e))
                        else:
                            ss["next_source"] = src["source_id"]
                            st.rerun()
            with csv_tab, st.form("csv", clear_on_submit=True):
                file = st.file_uploader("CSV file (UTF-8, header row)", type=["csv"])
                table = st.text_input("Table name (optional)", placeholder="from the file name")
                if st.form_submit_button("Upload") and file is not None:
                    try:
                        with st.spinner("Loading…"):
                            src = api(
                                "POST",
                                "/sources/csv",
                                files={"file": (file.name, file.getvalue(), "text/csv")},
                                data={"table": table} if table else {},
                                timeout=120.0,
                            )
                    except ApiError as e:
                        st.error(str(e))
                    else:
                        ss["next_source"] = src["source_id"]
                        st.rerun()
    return sources


# ------------------------------------------------------------------------------------ asking


def ask(question: str, context: str) -> None:
    body: dict[str, Any] = {"question": question}
    if context.strip():
        body["business_context"] = context
    if ss.source != SHARED:
        body["source_id"] = ss.source
    with st.spinner("Reading the question, matching terms and values, writing SQL…"):
        out = api("POST", "/ask", json=body, timeout=ASK_TIMEOUT)
    stored = None
    if out.get("question_id"):
        # Result rows are saved in the background: poll while rows_status is 'saving'.
        for _ in range(20):
            try:
                stored = api("GET", f"/questions/{out['question_id']}")
            except ApiError:
                stored = None
                break
            if stored.get("rows_status") != "saving":
                break
            time.sleep(0.5)
    ss.history.insert(0, {"ask": out, "stored": stored})


OUTCOME = {
    "success": ("✅", "Answered"),
    "needs_definition": ("❓", "Needs a definition"),
    "no_sql": ("⚠️", "No SQL"),
    "failed": ("❌", "Query failed"),
    "rejected": ("🚫", "Not a data question"),
}


def show_answer(item: dict[str, Any]) -> None:
    out, stored = item["ask"], item["stored"] or {}
    icon, label = OUTCOME.get(out.get("outcome") or "rejected", ("ℹ️", out.get("outcome")))
    st.markdown(f"#### {icon} {label}")
    st.write(out.get("answer") or out.get("reason"))
    if stored.get("sql"):
        with st.expander("SQL that ran", expanded=False):
            st.code(stored["sql"], language="sql")
    if stored.get("error"):
        st.error(stored["error"])
    cols, rows = stored.get("result_columns") or [], stored.get("rows") or []
    if cols:
        if stored.get("rows_status") == "saving":
            st.info("Rows are still being saved; ask again in a moment to refresh.")
        st.dataframe(pd.DataFrame(rows, columns=cols), width="stretch", hide_index=True)
        note = f"{stored.get('row_count', len(rows))} row(s)"
        if stored.get("truncated"):
            note += " — truncated at the row cap"
        st.caption(note)


def side_panels(item: dict[str, Any] | None) -> None:
    st.markdown("#### How it was read")
    if item is None:
        st.caption("Ask a question to see the glossary terms and value matches it used.")
        return
    out, stored = item["ask"], item["stored"] or {}
    interp = stored.get("interpretation") or {}

    defs = interp.get("definitions") or [
        {
            "term": h["name"],
            "definition": h["description"],
            "status": h["status"],
            "origin": "glossary",
            "used": None,
            "score": h["score"],
            "sql_expression": h.get("sql_expression", ""),
        }
        for h in out.get("glossary_hits", [])
    ]
    with st.expander(f"📘 Glossary terms ({len(defs)})", expanded=bool(defs)):
        if not defs:
            st.caption("No business terms in this question (or none matched the glossary).")
        for d in defs:
            badge = "approved" if d["status"] == "approved" else "proposed"
            origin = (
                "drafted for this question" if d["origin"] == "drafted_now" else "from glossary"
            )
            used = "" if d.get("used") is None else (" · used" if d["used"] else " · not used")
            score = f" · score {d['score']:.2f}" if d.get("score") is not None else ""
            st.markdown(f"**{d['term']}** — `{badge}` · {origin}{used}{score}")
            st.write(d["definition"])
            if d.get("sql_expression"):
                st.code(d["sql_expression"], language="sql")
        for m in interp.get("missing_definitions", []):
            st.warning(f"No definition for **{m}** — add it to the glossary.")

    values = interp.get("values") or [
        {
            "phrase": v["text"],
            "status": v["status"],
            "table": (v.get("match") or {}).get("table"),
            "column": (v.get("match") or {}).get("column"),
            "value": (v.get("match") or {}).get("name"),
            "alternatives": [
                f"{c.get('table')}.{c.get('column')} = '{c.get('name')}'"
                for c in v.get("candidates", [])
            ],
        }
        for v in out.get("resolved_values", [])
    ]
    with st.expander(f"🔎 Value matches · Elasticsearch ({len(values)})", expanded=bool(values)):
        if not values:
            st.caption("No specific values (products, regions, …) in this question.")
        for v in values:
            if v["status"] == "resolved":
                st.markdown(f"“{v['phrase']}” → `{v['table']}.{v['column']}` = **{v['value']}**")
            else:
                st.markdown(f"“{v['phrase']}” — `{v['status']}`")
            if v.get("alternatives"):
                st.caption("Other candidates: " + "; ".join(v["alternatives"]))

    extra = (
        [("Assumption", a) for a in interp.get("assumptions", [])]
        + [("Unclear term", u) for u in interp.get("unclear_terms", [])]
        + [("Ungrounded term", u) for u in interp.get("ungrounded_terms", [])]
    )
    with st.expander(f"🧭 Assumptions ({len(extra)})"):
        if not extra:
            st.caption("None.")
        for kind, text in extra:
            st.markdown(f"- **{kind}:** {text}")
    repairs = interp.get("repairs", [])
    if repairs:
        with st.expander(f"🛠 SQL repairs ({len(repairs)})"):
            for r in repairs:
                st.markdown(f"- {r.get('diagnosis') or r['error']}")


# -------------------------------------------------------------------------------------- main


def main_page() -> None:
    sidebar()
    left, right = st.columns([3, 2], gap="large")
    with left:
        st.title("Ask your data")
        with st.form("ask", clear_on_submit=False):
            question = st.text_area(
                "Question", placeholder="e.g. total sales of 5l coconut oil by region last month"
            )
            with st.expander("Business context (optional)"):
                context = st.text_area(
                    "Anything the assistant should know about your business or data",
                    label_visibility="collapsed",
                )
            submitted = st.form_submit_button("Ask", type="primary")
        if submitted and question.strip():
            try:
                ask(question.strip(), context)
            except ApiError as e:
                st.error(str(e))
        if ss.history:
            show_answer(ss.history[0])
            if len(ss.history) > 1:
                st.divider()
                st.caption("Earlier questions")
                for item in ss.history[1:10]:
                    with st.expander(item["ask"]["question"]):
                        show_answer(item)
    with right:
        side_panels(ss.history[0] if ss.history else None)


if ss.token:
    main_page()
else:
    login_page()
