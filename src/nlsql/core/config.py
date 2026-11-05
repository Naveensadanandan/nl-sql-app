from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NLSQL_", env_file=".env", extra="ignore")

    fast_model: str = "nvidia/nemotron-3-ultra-550b-a55b:free"
    strong_model: str = "nvidia/nemotron-3-ultra-550b-a55b:free"
    database_url: str


@lru_cache
def get_settings() -> Settings:
    return Settings()
