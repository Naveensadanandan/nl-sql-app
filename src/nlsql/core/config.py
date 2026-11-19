import re
from functools import lru_cache

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _with_driver(url: str, driver: str | None) -> str:
    """Rewrite a Postgres URL's scheme: driver="psycopg" for SQLAlchemy, None for raw psycopg.

    Accepts postgres://, postgresql:// or postgresql+<driver>:// so .env may use any of them.
    """
    scheme, sep, rest = url.partition("://")
    if not sep:
        return url
    base = scheme.split("+", 1)[0]
    if base == "postgres":
        base = "postgresql"
    return f"{base}+{driver}://{rest}" if driver else f"{base}://{rest}"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NLSQL_", env_file=".env", extra="ignore")

    # LLM (OpenRouter). SecretStr keeps the key out of reprs and logs.
    openrouter_api_key: SecretStr | None = None
    fast_model: str = "nvidia/nemotron-3-ultra-550b-a55b:free"
    strong_model: str = "nvidia/nemotron-3-ultra-550b-a55b:free"
    llm_timeout_seconds: int = 60  # per LLM request; a hung provider must not hang /ask
    llm_max_retries: int = 2  # provider retries (429 / 5xx) per LLM request

    # App DB. Write it as postgresql://...; use the properties below, never this field directly.
    database_url: str

    # Data the agent may query. v0.1 is single-source: one schema, a fixed list of tables.
    # JSON list in .env, e.g. NLSQL_DATA_TABLES='["retail_sales","customers"]'.
    data_schema: str = "public"
    data_tables: list[str] = ["retail_sales"]
    schema_cache_ttl_seconds: int = 300  # reflected table schema is reused this long

    # Value resolution (Elasticsearch, one index per organisation: values_<organisation_id>).
    # Leave es_url unset until the ES server exists; resolve_values then marks values "skipped".
    es_url: str | None = None
    value_min_score: float = 1.0  # BM25 score below which a hit is ignored
    value_ambiguity_ratio: float = 0.8  # 2nd hit >= ratio * top hit -> ask the user to choose

    # Business definitions (Qdrant). Leave qdrant_url unset until the server exists.
    qdrant_url: str | None = None
    glossary_collection: str = "glossary"
    embedding_model: str = "BAAI/bge-small-en-v1.5"  # fastembed; must match the indexer's model
    glossary_min_score: float = 0.6  # cosine similarity threshold for a glossary hit

    # SQL generation / execution (system-design §7).
    sql_max_debug_attempts: int = 3  # debug_sql loops before the graph gives up
    sql_row_cap: int = 1000  # LIMIT clamp and fetch cap
    sql_timeout_seconds: int = 15  # statement_timeout inside the read-only transaction
    sql_max_cost: float = 1e7  # EXPLAIN total-cost ceiling; above it the query is rejected

    # Query store (src/nlsql/core/query_store.py): every question is logged to
    # <results_schema>.query_log and its result rows to <results_schema>.q_<question_id>.
    # The store WRITES, so point results_database_url at a role that may CREATE in that schema;
    # unset = reuse database_url. Keep the read-only query role separate from this one.
    results_schema: str = "query_results"
    results_database_url: str | None = None
    store_pool_size: int = 5  # max pooled connections the query store keeps open

    # Admin server (src/nlsql/admin). Every admin request needs the X-Admin-Key header;
    # with no key set the admin API refuses all requests.
    admin_api_key: str | None = None
    value_max_distinct: int = 5000  # a text column with more distinct values is not a category
    admin_max_upload_mb: int = 5

    # Users, sessions and data sources (src/nlsql/core/accounts.py, src/nlsql/sources).
    # Their tables live in <app_schema> on the query-store (writer) connection.
    app_schema: str = "nlsql_app"
    session_ttl_hours: int = 12  # a login token stops working after this
    # Fernet key that encrypts user database URLs at rest. Without it, Postgres sources are off.
    fernet_key: SecretStr | None = None
    upload_max_mb: int = 20  # CSV upload size limit
    source_max_tables: int = 50  # tables one data source may expose to the agent
    # Read-only role behind database_url. When it differs from the writer, uploaded CSV tables
    # are granted to it (SELECT only), so the agent can query them.
    query_role: str | None = None
    # Questions without a data source use the configured data_schema / data_tables, shared by
    # every user (demo data). Set false so each user must connect or upload their own data.
    shared_data_enabled: bool = True
    # Knowledge (Qdrant glossary + ES values) for questions on the shared demo data lives under
    # this reserved organisation, not the asker's, so every user's demo sees the same terms and
    # their own glossary never leaks into it. Seeded at API start (nlsql.knowledge.demo).
    demo_organisation_id: str = "demo_organisation"

    @field_validator("demo_organisation_id")
    @classmethod
    def _demo_org(cls, v: str) -> str:
        v = v.strip().lower()  # same rule as a new organisation (core.accounts.NEW_ORGANISATION)
        if not re.fullmatch(r"[a-z0-9][a-z0-9_]{1,39}", v):
            raise ValueError("demo_organisation_id: 2-40 chars of a-z, 0-9, '_'")
        return v

    @property
    def sqlalchemy_url(self) -> str:
        """database_url for create_engine (postgresql+psycopg://)."""
        return _with_driver(self.database_url, "psycopg")

    @property
    def psycopg_url(self) -> str:
        """database_url for psycopg.connect (postgresql://)."""
        return _with_driver(self.database_url, None)

    @property
    def results_psycopg_url(self) -> str:
        """Query-store URL for psycopg.connect; falls back to database_url."""
        return _with_driver(self.results_database_url or self.database_url, None)


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # database_url comes from the environment
