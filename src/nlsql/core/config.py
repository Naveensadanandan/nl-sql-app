from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NLSQL_", env_file=".env", extra="ignore")

    fast_model: str = "nvidia/nemotron-3-ultra-550b-a55b:free"
    strong_model: str = "nvidia/nemotron-3-ultra-550b-a55b:free"
    database_url: str

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

    # Admin server (src/nlsql/admin). Every admin request needs the X-Admin-Key header;
    # with no key set the admin API refuses all requests.
    admin_api_key: str | None = None
    value_max_distinct: int = 5000  # a text column with more distinct values is not a category
    admin_max_upload_mb: int = 5


@lru_cache
def get_settings() -> Settings:
    return Settings()
