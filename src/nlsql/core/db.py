"""Shared SQLAlchemy engines: one pooled engine per URL per process.

Never create an engine per request; that opens a new pool (and connections) each time.
"""

from functools import lru_cache

from sqlalchemy import Engine, create_engine


@lru_cache
def engine(url: str) -> Engine:
    return create_engine(url, pool_pre_ping=True)
