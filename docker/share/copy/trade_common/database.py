from __future__ import annotations

import os
from functools import lru_cache
from urllib.parse import quote_plus

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from .config import read_secret


def database_url() -> str:
    explicit = os.getenv("DATABASE_URL")
    if explicit:
        return explicit
    password = read_secret(os.getenv("DATABASE_PASSWORD_FILE", "/run/secrets/postgres_password"))
    user = os.getenv("POSTGRES_USER", "trade")
    host = os.getenv("POSTGRES_HOST", "postgres")
    port = os.getenv("POSTGRES_PORT", "5432")
    database = os.getenv("POSTGRES_DB", "trade")
    return f"postgresql+psycopg://{quote_plus(user)}:{quote_plus(password or '')}@{host}:{port}/{database}"


@lru_cache(maxsize=1)
def engine():
    return create_engine(database_url(), pool_pre_ping=True, future=True)


@lru_cache(maxsize=1)
def session_factory():
    return sessionmaker(bind=engine(), expire_on_commit=False, future=True)

