"""Web UI (Streamlit).  Run from the repo root:  streamlit run ui/app.py
(the API must be on NLSQL_API_URL; the theme and font come from .streamlit/config.toml).

Screens
- Welcome: log in / create account (no token yet)
- Sidebar navigation after log-in:
  1. Get started: a tour of the shared demo data (tables, sample rows), sample questions, and an
     ask box that always runs on the demo data.
  2. Workspace: pick or add a data source (Postgres / CSV), ask, and see the answer, SQL, result
     table and how the question was read (glossary terms, value matches, assumptions).

See docs/ui-and-auth.md.
"""

import contextlib
import os
import time
from typing import Any
from urllib.parse import quote, unquote, urlsplit

import pandas as pd
import streamlit as st
from api_client import API_URL, ASK_TIMEOUT, ApiError, call

APP_NAME = os.environ.get("NLSQL_UI_APP_NAME", "Meridian")
TAGLINE = "The analytics engine that answers in plain English and shows its working."

st.set_page_config(page_title=APP_NAME, page_icon=":material/insights:", layout="wide")

SHARED = "__shared__"
GRANT_SQL = """-- Run as the database owner. Replace mydb / public / the password.
CREATE ROLE nlsql_reader LOGIN PASSWORD 'choose-a-strong-password';
GRANT CONNECT ON DATABASE mydb TO nlsql_reader;
GRANT USAGE ON SCHEMA public TO nlsql_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO nlsql_reader;
-- tables created later:
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO nlsql_reader;"""

# Curated questions for the default demo table; other demo tables get generic ones (see below).
DEMO_QUESTIONS = {
    "retail_sales": [
        ("Revenue by product category", "Business term: revenue"),
        (
            "How much did women spend on cosmetics?",
            "Value matching: women → Female, cosmetics → Beauty",
        ),
        ("Who are our best customers?", "Business term: best customers"),
        ("AOV of electronic items by gender", "Both: AOV + electronic items → Electronics"),
    ]
}

st.html(
    """<style>
    .brand-hero { padding: 2.5rem 0 1rem; }
    .brand-eyebrow { font-size: .8rem; font-weight: 600; letter-spacing: .14em;
        text-transform: uppercase; color: #1E3A8A; margin-bottom: .5rem; }
    .brand-title { font-size: 2.4rem; font-weight: 700; line-height: 1.15; color: #0F172A;
        margin: 0 0 .75rem; }
    .brand-sub { font-size: 1.05rem; color: #475569; margin: 0 0 1.5rem; }
    .brand-card { border: 1px solid #E2E8F0; border-radius: .6rem; padding: 1rem 1.1rem;
        background: #F8FAFC; height: 100%; }
    .brand-card h4 { margin: 0 0 .35rem; font-size: .95rem; font-weight: 600; color: #0F172A; }
    .brand-card p { margin: 0; font-size: .9rem; color: #475569; }
    .step-num { display: inline-block; width: 1.6rem; height: 1.6rem; border-radius: 50%;
        background: #1E3A8A; color: #fff; text-align: center; line-height: 1.6rem;
        font-size: .85rem; font-weight: 600; margin-right: .5rem; }
    </style>"""
)

ss = st.session_state
ss.setdefault("token", None)
ss.setdefault("user", None)
ss.setdefault("history", [])  # Workspace answers, newest first: {"ask": ..., "stored": ...}
ss.setdefault("demo_history", [])  # Get started answers (always the shared demo data)
ss.setdefault("source", SHARED)


def reset_session() -> None:
    ss.token, ss.user = None, None
    ss.history, ss.demo_history = [], []
    ss.pop("demo_preview", None)
    ss.pop("demo_knowledge", None)


def api(method: str, path: str, **kwargs: Any) -> Any:
    """Authenticated call. A 401 means the session ended: drop the token and show log-in."""
    try:
        return call(method, path, token=ss.token, **kwargs)
    except ApiError as e:
        if e.status == 401 and ss.token:
            reset_session()
            st.warning("Your session expired. Log in again.")
            st.rerun()
        raise


# ------------------------------------------------------------------------------ welcome page


def card(title: str, body: str) -> str:
    return f'<div class="brand-card"><h4>{title}</h4><p>{body}</p></div>'


def login_page() -> None:
    pitch, _, auth = st.columns([1.25, 0.1, 1])
    with pitch:
        st.html(
            f"""<div class="brand-hero">
            <div class="brand-eyebrow">{APP_NAME} · Analytics engine</div>
            <div class="brand-title">Welcome. Ask your data anything.</div>
            <p class="brand-sub">{TAGLINE} Connect a database or upload a CSV, ask a question
            the way you would ask a colleague, and get a table you can trust.</p></div>"""
        )
        a, b = st.columns(2)
        a.html(
            card(
                "For business leaders",
                "Plain-English questions, clear answers. No SQL, no waiting on a report queue.",
            )
        )
        b.html(
            card(
                "For analysts",
                "See the exact SQL, the business definitions used, and how every value was "
                "matched, so you can check the answer.",
            )
        )
        st.html("<div style='height:.75rem'></div>")
        c, d = st.columns(2)
        c.html(
            card(
                "Understands messy values",
                "“5l coconut oil” finds the stored “5000 ml Coconut Oil”.",
            )
        )
        d.html(
            card(
                "Read-only by design",
                "Every query is checked before it runs and can never change your data.",
            )
        )
    with auth:
        st.html("<div style='height:2.5rem'></div>")
        with st.container(border=True):
            st.subheader("Sign in to continue")
            login_tab, signup_tab = st.tabs(["Log in", "Create account"])
            with login_tab, st.form("login"):
                username = st.text_input("Username")
                password = st.text_input("Password", type="password")
                if st.form_submit_button("Log in", type="primary", width="stretch"):
                    try:
                        out = call(
                            "POST",
                            "/auth/login",
                            json={"username": username, "password": password},
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
        st.caption(f"Connected to {API_URL}")


# --------------------------------------------------------------------------- sidebar (shared)


def account_box() -> None:
    user = ss.user
    with st.sidebar:
        st.divider()
        st.markdown(f"**{user['username']}**  \norganisation `{user['organisation_id']}`")
        if st.button("Log out", width="stretch", icon=":material/logout:"):
            with contextlib.suppress(ApiError):
                api("POST", "/auth/logout")
            reset_session()
            st.rerun()


def build_pg_url(host: str, port: str, database: str, user: str, password: str) -> str:
    """postgresql:// URL from separate fields (user and password percent-encoded)."""
    auth = f"{quote(user.strip(), safe='')}:{quote(password, safe='')}"
    return f"postgresql://{auth}@{host.strip()}:{port.strip() or '5432'}/{database.strip()}"


def database_name(url: str) -> str:
    with contextlib.suppress(ValueError):
        return unquote(urlsplit(url).path.lstrip("/")) or ""
    return ""


def pg_connect_step1(info: dict[str, Any]) -> None:
    """Connection details -> POST /sources/postgres/discover (nothing is saved yet)."""
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
    with st.expander("Show the GRANT script for a read-only role"):
        st.code(GRANT_SQL, language="sql")
    mode = st.segmented_control(
        "Connect with", ["Details", "Connection URL"], default="Details", key="pg_mode"
    )
    with st.form("pg_discover"):
        if mode == "Connection URL":
            url = st.text_input(
                "Connection URL",
                type="password",
                placeholder="postgresql://nlsql_reader:***@host:5432/mydb",
                help="Stored encrypted once you connect. Never shown again.",
            )
        else:
            a, b = st.columns([3, 1])
            host = a.text_input("Host", placeholder="db.example.com")
            port = b.text_input("Port", value="5432")
            database = st.text_input("Database", placeholder="mydb")
            c, d = st.columns(2)
            user = c.text_input("User", placeholder="nlsql_reader")
            password = d.text_input("Password", type="password")
            url = build_pg_url(host, port, database, user, password) if host else ""
        go = st.form_submit_button(
            "Test connection",
            type="primary",
            icon=":material/cable:",
            disabled=not info["postgres_enabled"],
        )
    if go:
        if not url:
            st.error("Fill in the connection details first.")
            return
        try:
            with st.spinner("Checking the role is read-only and listing its tables…"):
                out = api("POST", "/sources/postgres/discover", json={"url": url})
        except ApiError as e:
            st.error(str(e))
            return
        # Held in server-side session state only until Connect or Start over.
        ss.pg_discover = {"url": url, **out}
        st.rerun(scope="fragment")


def pg_connect_step2() -> None:
    """Pick a schema and tables from what the role can read -> POST /sources/postgres."""
    found = ss.pg_discover
    usable = [x for x in found["schemas"] if not x["blocked"]]
    st.success(
        f"Connected read-only. {sum(len(x['tables']) for x in usable)} readable table(s) "
        f"in {len(usable)} schema(s).",
        icon=":material/verified_user:",
    )
    for x in found["schemas"]:
        if x["blocked"]:
            st.warning(f"Schema `{x['schema_name']}` can't be used: {x['blocked']}.")
    if not usable:
        if st.button("Start over"):
            ss.pop("pg_discover", None)
            st.rerun(scope="fragment")
        return
    names = [x["schema_name"] for x in usable]
    name = st.text_input("Name", value=database_name(found["url"]) or "My database")
    schema = st.selectbox(
        "Schema",
        names,
        index=names.index("public") if "public" in names else 0,
        format_func=lambda n: (
            f"{n}  ({len(next(x for x in usable if x['schema_name'] == n)['tables'])} tables)"
        ),
    )
    tables = next(x["tables"] for x in usable if x["schema_name"] == schema)
    cap = found["max_tables"]
    picked = st.multiselect(
        "Tables to ask about",
        tables,
        default=tables[:cap],
        max_selections=cap,
        key=f"pg_tables_{schema}",
        help=f"Up to {cap}. You can add or remove tables later with Manage tables.",
    )
    if len(tables) > cap:
        st.caption(f"{len(tables)} tables in this schema; the first {cap} are picked.")
    left, right = st.columns(2)
    if left.button(
        "Connect", type="primary", width="stretch", disabled=not picked, icon=":material/link:"
    ):
        body = {
            "name": name.strip() or "My database",
            "url": found["url"],
            "schema_name": schema,
            "tables": picked,
        }
        try:
            with st.spinner("Saving the connection…"):
                src = api("POST", "/sources/postgres", json=body)
        except ApiError as e:
            st.error(str(e))
        else:
            ss.pop("pg_discover", None)
            ss["next_source"] = src["source_id"]
            st.rerun()
    if right.button("Start over", width="stretch"):
        ss.pop("pg_discover", None)
        st.rerun(scope="fragment")


@st.dialog("Add a data source", width="large")
def add_source_dialog(info: dict[str, Any]) -> None:
    pg_tab, csv_tab = st.tabs(["Connect Postgres", "Upload CSV"])
    with pg_tab:
        if "pg_discover" in ss:
            pg_connect_step2()
        else:
            pg_connect_step1(info)
    with csv_tab, st.form("csv", clear_on_submit=True):
        file = st.file_uploader("CSV file (UTF-8, header row)", type=["csv"])
        table = st.text_input("Table name (optional)", placeholder="from the file name")
        st.caption("To join several files, upload one, then add the rest with Manage tables.")
        if st.form_submit_button("Upload", type="primary") and file is not None:
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


@st.dialog("Manage tables", width="large")
def manage_tables_dialog(src: dict[str, Any]) -> None:
    """Add or remove tables of a saved source without entering the connection again."""
    key = f"catalog_{src['source_id']}"
    if key not in ss:
        try:
            with st.spinner("Re-checking the connection and listing tables…"):
                ss[key] = api("GET", f"/sources/{src['source_id']}/tables")
        except ApiError as e:
            st.error(str(e))
            return
    cat = ss[key]
    st.markdown(f"**{src['name']}** · schema `{cat['schema_name']}`")
    if cat["kind"] == "postgres":
        st.caption(
            f"{len(cat['available'])} table(s) readable by the saved role. "
            "Changes re-check that the role is still read-only."
        )
        new_ones = [t for t in cat["available"] if t not in cat["selected"]]
        if new_ones:
            st.info(
                f"{len(new_ones)} table(s) not added yet: "
                + ", ".join(new_ones[:10])
                + (" …" if len(new_ones) > 10 else "")
            )
    else:
        st.caption("Uploaded tables. A table you remove is deleted from this app.")
    with st.form(f"tables_{src['source_id']}"):
        picked = st.multiselect(
            "Tables to ask about",
            cat["available"],
            default=[t for t in cat["selected"] if t in cat["available"]],
            max_selections=cat["max_tables"],
        )
        saved = st.form_submit_button("Save tables", type="primary", icon=":material/save:")
    if saved:
        if not picked:
            st.error("Keep at least one table, or delete the source.")
        else:
            try:
                with st.spinner("Saving…"):
                    api("PUT", f"/sources/{src['source_id']}/tables", json={"tables": picked})
            except ApiError as e:
                st.error(str(e))
            else:
                ss.pop(key, None)
                st.rerun()
    if cat["kind"] == "csv":
        st.divider()
        st.markdown("**Add a table from another CSV**")
        with st.form(f"csv_add_{src['source_id']}", clear_on_submit=True):
            file = st.file_uploader("CSV file (UTF-8, header row)", type=["csv"])
            table = st.text_input("Table name (optional)", placeholder="from the file name")
            if st.form_submit_button("Add table", icon=":material/add:") and file is not None:
                try:
                    with st.spinner("Loading…"):
                        api(
                            "POST",
                            f"/sources/{src['source_id']}/csv",
                            files={"file": (file.name, file.getvalue(), "text/csv")},
                            data={"table": table} if table else {},
                            timeout=120.0,
                        )
                except ApiError as e:
                    st.error(str(e))
                else:
                    ss.pop(key, None)
                    st.rerun()


def source_sidebar() -> list[dict[str, Any]]:
    with st.sidebar:
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
            # A source added in a dialog takes effect on the next run (the picker is drawn first).
            if "next_source" in ss:
                ss.source = ss.pop("next_source")
            if ss.source not in options:
                ss.source = options[0]
            st.selectbox("Ask questions about", options, key="source", format_func=labels.get)
            current = next((s for s in sources if s["source_id"] == ss.source), None)
            if current:
                st.caption(
                    f"schema `{current['schema_name']}` · {len(current['tables'])} table(s): "
                    + ", ".join(f"`{t}`" for t in current["tables"][:8])
                    + (" …" if len(current["tables"]) > 8 else "")
                    + f" · value index: {current['values_status']}"
                )
                a, b = st.columns(2)
                if a.button("Manage tables", icon=":material/table_edit:", width="stretch"):
                    ss.pop(f"catalog_{current['source_id']}", None)
                    manage_tables_dialog(current)
                if b.button("Delete", icon=":material/delete:", width="stretch"):
                    try:
                        api("DELETE", f"/sources/{current['source_id']}")
                        st.rerun()
                    except ApiError as e:
                        st.error(str(e))

        if st.button(
            "Add data source",
            icon=":material/add:",
            type="secondary" if options else "primary",
            width="stretch",
        ):
            ss.pop("pg_discover", None)
            add_source_dialog(info)
    return sources


# ------------------------------------------------------------------------------------ asking


def ask(question: str, context: str, source_id: str | None, history: list[dict[str, Any]]) -> None:
    """Ask on `source_id` (None = shared demo data) and put the answer first in `history`."""
    body: dict[str, Any] = {"question": question}
    if context.strip():
        body["business_context"] = context
    if source_id:
        body["source_id"] = source_id
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
    history.insert(0, {"ask": out, "stored": stored})


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


def definition_badges(d: dict[str, Any]) -> list[str]:
    """Coloured Markdown badges for one definition: status, origin, used, match score."""
    badges = [
        ":green-badge[:material/verified: Approved]"
        if d["status"] == "approved"
        else ":orange-badge[:material/pending: Proposed]"
    ]
    if d["origin"] == "drafted_now":
        badges.append(":violet-badge[:material/edit_note: Drafted for this question]")
    else:
        badges.append(":blue-badge[:material/menu_book: From glossary]")
    if d.get("used") is not None:
        badges.append(
            ":green-badge[:material/check_circle: Used]"
            if d["used"]
            else ":gray-badge[:material/do_not_disturb_on: Not used]"
        )
    if d.get("score") is not None:
        score = float(d["score"])
        color = "green" if score >= 0.8 else "blue" if score >= 0.65 else "gray"
        badges.append(f":{color}-badge[:material/target: Match {score:.2f}]")
    return badges


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
            with st.container(border=True):
                st.markdown(f"**{d['term']}**")
                st.markdown(" ".join(definition_badges(d)))
                st.markdown(d["definition"])
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
            "method": v.get("method", "lexical"),
        }
        for v in out.get("resolved_values", [])
    ]
    with st.expander(f"🔎 Value matches ({len(values)})", expanded=bool(values)):
        if not values:
            st.caption("No specific values (products, regions, …) in this question.")
        for v in values:
            if v["status"] == "resolved":
                by_meaning = " _(matched by meaning)_" if v.get("method") == "semantic" else ""
                st.markdown(
                    f"“{v['phrase']}” → `{v['table']}.{v['column']}` = **{v['value']}**{by_meaning}"
                )
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


def ask_form(key: str, placeholder: str) -> tuple[bool, str, str]:
    """Question box (text kept in ss[f"{key}_q"]). Returns (submitted, question, context)."""
    with st.form(key, clear_on_submit=False):
        question = st.text_area("Question", key=f"{key}_q", placeholder=placeholder)
        with st.expander("Business context (optional)"):
            context = st.text_area(
                "Anything the assistant should know about your business or data",
                label_visibility="collapsed",
            )
        submitted = st.form_submit_button("Ask", type="primary", icon=":material/send:")
    return submitted, question.strip(), context


def answers(history: list[dict[str, Any]]) -> None:
    left, right = st.columns([3, 2], gap="large")
    with left:
        show_answer(history[0])
        if len(history) > 1:
            st.divider()
            st.caption("Earlier questions")
            for item in history[1:10]:
                with st.expander(item["ask"]["question"]):
                    show_answer(item)
    with right:
        side_panels(history[0])


# ------------------------------------------------------------------------ page: get started


def demo_questions(tables: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Curated questions for known demo tables, else generic ones built from the columns."""
    for t in tables:
        if t["table"] in DEMO_QUESTIONS:
            return DEMO_QUESTIONS[t["table"]]
    out: list[tuple[str, str]] = []
    for t in tables:
        cols = t.get("columns") or []
        text = [c["name"] for c in cols if any(k in c["type"].upper() for k in ("CHAR", "TEXT"))]
        nums = [
            c["name"]
            for c in cols
            if any(k in c["type"].upper() for k in ("INT", "NUMERIC", "DOUBLE", "REAL", "FLOAT"))
        ]
        out.append((f"How many rows are in {t['table']}?", "A simple count"))
        if text and nums:
            out.append((f"Total {nums[0]} by {text[0]}", "A simple aggregate"))
        if text:
            out.append((f"Top 5 {text[0]} values by number of rows", "Ranking and limits"))
        if len(out) >= 4:
            break
    return out[:4]


def demo_knowledge() -> None:
    """Sections 2 and 3: the demo glossary (Qdrant) and the indexed values (Elasticsearch)."""
    if "demo_knowledge" not in ss:
        try:
            ss.demo_knowledge = api("GET", "/sources/knowledge")
        except ApiError as e:
            ss.demo_knowledge = {"error": str(e)}
    k = ss.demo_knowledge

    st.subheader("2 · Business terms it knows")
    st.caption(
        "Words like *revenue* or *best customers* mean different things in every company. "
        f"{APP_NAME} never guesses them: it uses the glossary below, and every answer lists "
        "the definitions it used. These are the demo's terms; your own organisation keeps a "
        "separate glossary."
    )
    if k.get("error"):
        st.warning(f"Could not load the demo glossary: {k['error']}")
    elif not k.get("glossary_enabled"):
        st.info("The glossary (Qdrant) is not configured on the API server.")
    elif k.get("glossary_error"):
        st.warning(k["glossary_error"])
    elif not k.get("glossary"):
        st.info("The demo glossary is empty. Restart the API to seed it.")
    else:
        rows = [
            {
                "Term": g["name"],
                "Kind": "Metric" if g["section"] == "metric" else "Definition",
                "Meaning": g["description"],
                "Also understood as": ", ".join(g.get("synonyms") or []),
                "SQL": g.get("sql_expression") or "",
            }
            for g in k["glossary"]
        ]
        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)

    st.subheader("3 · Values it can match")
    st.caption(
        "People rarely type values exactly as they are stored. The distinct values of each "
        "text column are indexed (Elasticsearch), so *women* finds `Female` and *cosmetics* "
        "finds `Beauty`, even with typos."
    )
    if k.get("error"):
        return
    if not k.get("values_enabled"):
        st.info("Value matching (Elasticsearch) is not configured on the API server.")
    elif k.get("values_error"):
        st.warning(k["values_error"])
    elif not k.get("values"):
        st.info("No demo values are indexed yet. Restart the API to index them.")
    else:
        rows = []
        for c in k["values"]:
            examples = [e["name"] for e in c["examples"]]
            more = c["count"] - len(c["examples"])
            rows.append(
                {
                    "Column": f"{c['table']}.{c['column']}",
                    "Distinct values": c["count"],
                    "Examples (← also matched from)": "; ".join(examples)
                    + (f"; … {more} more" if more > 0 else ""),
                }
            )
        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)


def get_started_page() -> None:
    st.html(
        f"""<div class="brand-hero" style="padding-top:.5rem">
        <div class="brand-eyebrow">Get started</div>
        <div class="brand-title" style="font-size:2rem">See how {APP_NAME} answers a question</div>
        <p class="brand-sub">This page uses a shared demo dataset, so you can try it before
        connecting your own data. Look at the data, pick a sample question or write your own,
        then check how the answer was worked out.</p></div>"""
    )
    s1, s2, s3 = st.columns(3)
    s1.html(
        card(
            '<span class="step-num">1</span>Learn the data',
            "The demo table, its business terms, and the values it understands.",
        )
    )
    s2.html(
        card(
            '<span class="step-num">2</span>Ask a question',
            "Click a sample question or type your own in plain English.",
        )
    )
    s3.html(
        card(
            '<span class="step-num">3</span>Check the working',
            "See the SQL, the definitions used and how values were matched.",
        )
    )

    if "demo_preview" not in ss:
        try:
            ss.demo_preview = api("GET", "/sources/preview", params={"rows": 5})
        except ApiError as e:
            ss.demo_preview = {"error": str(e), "tables": []}
    preview = ss.demo_preview
    if preview.get("error"):
        st.warning(
            f"The demo data is not available: {preview['error']}. "
            "Open **Workspace** to connect a database or upload a CSV."
        )
        return

    st.subheader("1 · The demo data")
    tables = preview["tables"]
    st.caption(
        f"{preview['table_count']} table(s) in schema `{preview['schema_name']}`. "
        "Showing the first 5 rows of each."
    )
    for t in tables:
        with st.container(border=True):
            st.markdown(f"**`{t['table']}`** · {len(t['columns'])} columns")
            if t.get("columns"):
                st.caption(" · ".join(f"`{c['name']}` {c['type'].lower()}" for c in t["columns"]))
            if t.get("error"):
                st.warning(f"Could not load sample rows: {t['error']}")
            elif t["rows"]:
                names = [c["name"] for c in t["columns"]]
                st.dataframe(
                    pd.DataFrame(t["rows"], columns=names), width="stretch", hide_index=True
                )
            else:
                st.caption("This table is empty.")

    demo_knowledge()

    st.subheader("4 · Ask a question")
    samples = demo_questions(tables)
    if samples:
        st.caption("Try one of these. Click to put it in the question box.")
        cols = st.columns(len(samples))
        for col, (q, what) in zip(cols, samples, strict=True):
            with col:
                st.button(
                    q,
                    key=f"sample-{q}",
                    width="stretch",
                    help=what,
                    on_click=lambda q=q: ss.update(demo_ask_q=q),
                )
    submitted, question, context = ask_form(
        "demo_ask",
        placeholder=samples[0][0] if samples else "Ask about the demo data",
    )
    if submitted and question:
        try:
            ask(question, context, None, ss.demo_history)
        except ApiError as e:
            st.error(str(e))

    if ss.demo_history:
        st.subheader("5 · The answer and how it was worked out")
        answers(ss.demo_history)
        st.info(
            "Ready for your own data? Open **Workspace** in the sidebar to connect Postgres or "
            "upload a CSV.",
            icon=":material/arrow_back:",
        )


# -------------------------------------------------------------------------- page: workspace


def workspace_page() -> None:
    source_sidebar()
    st.title("Workspace")
    st.caption("Ask questions about the data source picked in the sidebar.")
    submitted, question, context = ask_form(
        "ask", placeholder="e.g. total sales of 5l coconut oil by region last month"
    )
    if submitted and question:
        try:
            ask(question, context, None if ss.source == SHARED else ss.source, ss.history)
        except ApiError as e:
            st.error(str(e))
    if ss.history:
        answers(ss.history)
    else:
        side_panels(None)


# -------------------------------------------------------------------------------------- main


if ss.token:
    with st.sidebar:
        st.markdown(f"### {APP_NAME}")
    nav = st.navigation(
        [
            st.Page(get_started_page, title="Get started", icon=":material/school:", default=True),
            st.Page(workspace_page, title="Workspace", icon=":material/dashboard:"),
        ]
    )
    nav.run()
    account_box()
else:
    st.navigation([st.Page(login_page, title="Welcome")], position="hidden").run()
