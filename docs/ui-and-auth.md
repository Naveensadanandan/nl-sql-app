# UI, logins and running everything together

This page explains three things, kept simple for v0.1:

1. How logins work (sign-up, log-in, what keeps one user's data away from another's).
2. How a user's data gets in: a Postgres connection or a CSV upload, and how it is secured.
3. How to run the UI, the API and the services together.

---

## 1. The big picture

```
 Browser
    │  http://localhost:8501
    ▼
┌──────────────────┐   HTTP + "Authorization: Bearer <token>"   ┌──────────────────────────┐
│ Streamlit UI     │ ─────────────────────────────────────────▶ │ FastAPI  (port 8000)     │
│ ui/app.py        │                                            │ /auth  /sources  /ask    │
│ (port 8501)      │ ◀───────────────────────────────────────── │ /questions/{id}          │
└──────────────────┘          JSON (answer, SQL, rows)          └────────────┬─────────────┘
  holds only the login token,                                                │
  never a database URL                                                       │ LangGraph agent
                                                                             ▼
     ┌───────────────────────────── our own server ───────────────────────────────────┐
     │ Postgres (app DB, :5433)   users, sessions, data_sources, query_log,            │
     │                            uploaded CSVs in ws_<organisation> schemas           │
     │ Elasticsearch (:9200)      distinct values per organisation (values_<org>)     │
     │ Qdrant (:6333)             glossary / business definitions (filtered by org)    │
     │ OpenRouter                 LLM calls                                            │
     └─────────────────────────────────────────────────────────────────────────────────┘
                                                                             │ read-only SQL
                                                                             ▼
                                                           ┌──────────────────────────────┐
                                                           │ User's own Postgres server   │
                                                           │ (connected with a read-only  │
                                                           │  role, URL stored encrypted) │
                                                           └──────────────────────────────┘
```

The UI is only a front end. Everything (logins, data, SQL) goes through the API.

---

## 2. How logins work

### Words used here

| Word | Meaning |
|---|---|
| **User** | One login (username + password). |
| **Organisation** | A workspace (tenant). It owns the data sources, the glossary, the indexed values and the question history. In v0.1 every new user creates their own organisation at sign-up. |
| **Token** | A long random string the API gives you at log-in. Every later call sends it as `Authorization: Bearer <token>`. |
| **Session** | The server's record of a token: whose it is and when it expires. |

### Sign-up (`POST /auth/signup`)

1. The user enters a username, a password (8+ characters) and a new organisation id
   (for example `acme_sales`, using `a-z 0-9 _`).
2. The API hashes the password with **scrypt** and a random salt. Only the hash is stored
   (`nlsql_app.users.password_hash`). Nobody, not even an admin, can read the password back.
3. The organisation and the user are created in one transaction. If the username or the
   organisation is already taken, nothing is created and the API answers `409`. An organisation
   id is also "taken" if it already has data in Elasticsearch or Qdrant (for example, loaded
   by the admin server), so a stranger can't sign up as `acme` and inherit Acme's glossary.

### Log-in (`POST /auth/login`)

1. The API looks up the username and checks the password against the stored hash.
   A wrong username and a wrong password give the same message and take the same time, so a
   stranger cannot find out which usernames exist.
2. On success it creates a random token (`secrets.token_urlsafe(32)`) and stores only
   **SHA-256(token)** in `nlsql_app.sessions`, with an expiry (`NLSQL_SESSION_TTL_HOURS`, default
   12 h). A copy of the sessions table is useless to an attacker.
3. The token goes back to the UI. Streamlit keeps it in its **server-side session state**. It is
   not in a cookie or in the URL.

### Each request after that

```
UI ──"Authorization: Bearer abc…"──▶ API: sha256(abc…) in sessions and not expired?
                                       ├─ no  → 401 → the UI shows the log-in page again
                                       └─ yes → user = (user_id, username, organisation_id)
```

`src/nlsql/api/deps.py` (`CurrentUser`) does this check on every data endpoint.
**The organisation always comes from the session, never from the request body.** That is what
keeps tenants apart:

| What | How it is limited to your organisation |
|---|---|
| Data sources | `data_sources.organisation_id` must match; another org's `source_id` returns 404 |
| Uploaded CSVs | Stored in your own schema `ws_<organisation_id>`. Only the role `ws_<organisation_id>_reader` may read it |
| SQL | The validator allows only the selected source's tables. Then Postgres enforces it: `search_path` is the source's schema only, and CSV questions run under `SET LOCAL ROLE ws_<org>_reader`, so even SQL that got past the validator can't read another workspace or the app tables |
| Value matches (Elasticsearch) | One index per organisation: `values_<organisation_id>` |
| Glossary (Qdrant) | Every search is filtered on `organisation_id` |
| Answers (`GET /questions/{id}`) | Returned only to users of the organisation that asked them |

### Log-out (`POST /auth/logout`)

This deletes the session row, so the token stops working right away. Tokens also stop working
after the TTL. Expired rows are cleaned up at the next log-in.

> v0.1 notes: refreshing the browser tab starts a new Streamlit session, so you log in again.
> There are no invites to an existing organisation yet, and no rate limit on log-in attempts.
> Run behind HTTPS in any shared deployment, because the token and password travel over
> HTTP otherwise.

---

## 3. Bringing your data in (secured)

Pick or add a source in the sidebar, under **Data source**.

### a) Connect your own Postgres (the "user server")

1. Create a **read-only** role on your database (the UI shows this script):

   ```sql
   CREATE ROLE nlsql_reader LOGIN PASSWORD 'choose-a-strong-password';
   GRANT CONNECT ON DATABASE mydb TO nlsql_reader;
   GRANT USAGE ON SCHEMA public TO nlsql_reader;
   GRANT SELECT ON ALL TABLES IN SCHEMA public TO nlsql_reader;
   ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO nlsql_reader;
   ```

2. Paste `postgresql://nlsql_reader:<password>@<host>:5432/mydb`, the schema and, optionally,
   the tables you want to use. The URL must contain a user and a password and name one host.
   Sockets, `service=`, `passfile=` and `options=` are refused (they could borrow the API
   server's own credentials), and so is this app's own database.
3. The API (`sources/postgres.py`) connects with a 5 s timeout inside a read-only
   transaction and **refuses** the role if it is a superuser, can `CREATE` in the schema, or
   can `INSERT/UPDATE/DELETE/TRUNCATE` any table. Error messages never repeat the URL or the
   server's own text.
4. The URL is **encrypted with Fernet** (`NLSQL_FERNET_KEY`) before it is stored
   (`data_sources.dsn_enc`). The API never returns it, never logs it, and never puts it in the
   agent state (which is traced to LangSmith). The state holds only `source_id`.
5. In the background, distinct values of text columns are indexed into Elasticsearch, so
   "5l coconut oil" can match `5000 ml Coconut Oil`. The sidebar shows `value index: indexed`.

Every question then runs on your server with the same protections as before:
`sqlglot` validation, a `READ ONLY` transaction, a statement timeout, an EXPLAIN cost limit and
a row cap. On top of that, the role itself can only read.

### b) Upload a CSV

1. Choose a UTF-8 CSV with a header row (up to `NLSQL_UPLOAD_MAX_MB`, default 20 MB).
2. It becomes the table `ws_<your organisation>.<table>` in the app Postgres. Column names are
   cleaned up, types are inferred (whole numbers, decimals, true/false, dates, timestamps, else
   text), and the rows are loaded with `COPY`. SELECT on it is granted only to the role
   `ws_<your organisation>_reader` (created on first upload; the API's writer needs `CREATEROLE`).
3. Values are indexed into Elasticsearch, as for Postgres.
4. Deleting the source drops that table.

---

## 4. Running it all

### First time

```bash
source myenv/Scripts/activate            # PowerShell: myenv\Scripts\Activate.ps1
pip install -e ".[dev]"                  # now includes streamlit + cryptography
cp .env.example .env                     # fill the values below
docker compose up -d                     # postgres, elasticsearch, qdrant, redis
```

Values in `.env` that matter for the UI and logins:

| Variable | Needed for | Example |
|---|---|---|
| `NLSQL_DATABASE_URL` | app DB (users, sessions, CSVs, answers) | `postgresql://nlsql:…@localhost:5433/nlsql` |
| `NLSQL_OPENROUTER_API_KEY` | the LLM | `sk-or-…` |
| `NLSQL_ES_URL`, `NLSQL_QDRANT_URL` | value matches and glossary panels | `http://localhost:9200`, `http://localhost:6333` |
| `NLSQL_FERNET_KEY` | connecting a Postgres source | output of the command below |
| `NLSQL_SESSION_TTL_HOURS` | optional, log-in lifetime | `12` |
| `NLSQL_SHARED_DATA_ENABLED` | optional; `false` hides the shared demo tables | `true` |
| `NLSQL_QUERY_ROLE` | optional; read-only role to grant uploaded CSV tables to | `nlsql_ro` |

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Keep the Fernet key safe and stable. If it changes, saved connections can no longer be
decrypted, and users have to connect again.

### Every time: two terminals

```bash
# terminal 1: the API (creates its app tables on start-up)
uvicorn nlsql.api.main:app --reload --port 8000

# terminal 2: the UI
streamlit run ui/app.py                  # opens http://localhost:8501
```

The UI finds the API at `NLSQL_API_URL` (default `http://localhost:8000`). Set it to point the
UI at another host:

```bash
NLSQL_API_URL=http://10.0.0.5:8000 streamlit run ui/app.py     # PowerShell: $env:NLSQL_API_URL="..."
```

`NLSQL_UI_ASK_TIMEOUT` (seconds, default 300) is how long the UI waits for one answer.

Optional, a third terminal for the admin server (glossary CSV upload):
`uvicorn nlsql.admin.main:app --port 8001` (needs `NLSQL_ADMIN_API_KEY`).

### Try it

1. Open http://localhost:8501, then go to **Create account**, then **Log in**.
2. In the sidebar, open **Add data source**, then **Upload CSV**, and choose a small sales CSV.
3. Ask *"total amount of coconut oil 5l sold by region"*.
4. The left side shows the answer, the SQL that ran and the result table. The right side has
   expandable panels:
   - **📘 Glossary terms**: definitions from Qdrant (approved or proposed), or drafted for this
     question, with whether the SQL used them.
   - **🔎 Value matches · Elasticsearch**: how phrases were matched to stored values
     (`"coconut oil 5l" → sales.product = 5000 ml Coconut Oil`).
   - **🧭 Assumptions** and **🛠 SQL repairs**, when there are any.

### API endpoints the UI uses

| Method | Path | Auth | What |
|---|---|---|---|
| POST | `/auth/signup` | none | create user + organisation |
| POST | `/auth/login` | none | returns `access_token` |
| POST | `/auth/logout` | token | ends the session |
| GET | `/auth/me` | token | who am I |
| GET | `/sources` | token | your organisation's sources |
| POST | `/sources/postgres` | token | test (read-only) + save an encrypted connection |
| POST | `/sources/csv` | token | upload a CSV (multipart `file`, optional `table`, `name`) |
| DELETE | `/sources/{id}` | token | remove a source (drops an uploaded table) |
| POST | `/ask` | token | `{question, source_id?, business_context?}` |
| GET | `/questions/{id}` | token | stored SQL, rows, interpretation |

Interactive docs: http://localhost:8000/docs (use **Authorize** and paste the token).

### Troubleshooting

| Symptom | Cause |
|---|---|
| UI: "cannot reach the API" | The API isn't running, or `NLSQL_API_URL` is wrong |
| Log-in returns 500 | The app DB is unreachable: check `docker compose ps` and `NLSQL_DATABASE_URL` |
| "database connections are disabled" | `NLSQL_FERNET_KEY` is not set |
| "this role can write to N table(s)" | Use a read-only role (script above) |
| "stored connection cannot be decrypted" | The Fernet key changed: delete the source and connect again |
| Value panel shows `skipped` | `NLSQL_ES_URL` is not set |
| UI: ReadTimeout on Ask | The LLM is slow: raise `NLSQL_UI_ASK_TIMEOUT` or use a faster model |

### Production notes

- At start-up the API warns when `NLSQL_DATABASE_URL` is a superuser (as the compose user is
  in dev). For any shared deployment, use two roles:
  - **Writer** (`NLSQL_RESULTS_DATABASE_URL`): owns `nlsql_app`, `query_results` and the
    `ws_*` schemas, and has `CREATEROLE`.
  - **Reader** (`NLSQL_DATABASE_URL`, name it in `NLSQL_QUERY_ROLE`): read-only, and given
    `SELECT` on the shared demo tables only.
  
  CSV questions are isolated by the per-organisation reader roles either way. The shared demo
  tables rely on the validator and the READ ONLY transaction, so keep them to non-sensitive
  data, or set `NLSQL_SHARED_DATA_ENABLED=false`.
- Serve the UI and the API over HTTPS.

### Known v0.1 limits

- Glossary definitions belong to the organisation, not to one source. A definition saved while
  asking about one source can come up for another source that has similar terms.
- Elasticsearch values are keyed by table name. Two sources in one organisation with the same
  table name share those values.
- A user's Postgres host can be any address the API server can reach. In a shared deployment,
  restrict the API's outbound network access.
