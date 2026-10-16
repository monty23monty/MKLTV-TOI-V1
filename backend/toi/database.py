"""SQLAlchemy storage. A compare-and-swap protects each match aggregate."""

import os
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import URL
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


def database_url() -> str:
    explicit_url = os.getenv("TOI_DATABASE_URL")
    if explicit_url is not None:
        return explicit_url
    postgres_fields = ("HOST", "PORT", "DB", "USER", "PASSWORD")
    if not any(f"TOI_POSTGRES_{field}" in os.environ for field in postgres_fields):
        return "sqlite:///./data/toi.db"
    settings = {field: os.getenv(f"TOI_POSTGRES_{field}", "") for field in postgres_fields}
    missing = [f"TOI_POSTGRES_{field}" for field in ("HOST", "DB", "USER", "PASSWORD")
               if not settings[field].strip()]
    if missing:
        raise ValueError(f"PostgreSQL configuration requires {', '.join(missing)}")
    try:
        port = int(os.getenv("TOI_POSTGRES_PORT", "5432"))
    except ValueError:
        raise ValueError("TOI_POSTGRES_PORT must be an integer between 1 and 65535") from None
    if not 1 <= port <= 65535:
        raise ValueError("TOI_POSTGRES_PORT must be an integer between 1 and 65535")
    return URL.create(
        "postgresql+psycopg", username=settings["USER"], password=settings["PASSWORD"],
        host=settings["HOST"], port=port, database=settings["DB"],
    ).render_as_string(hide_password=False)


def make_engine(url: str):
    options = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        options["connect_args"] = {"check_same_thread": False, "timeout": 15}
        if url.endswith(":memory:"):
            options["poolclass"] = StaticPool
        elif url.startswith("sqlite:///"):
            Path(url.removeprefix("sqlite:///")).resolve().parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url, **options)
    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def sqlite_pragmas(connection, _record):
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA journal_mode=WAL")
    return engine


def sessions(engine):
    return sessionmaker(engine, expire_on_commit=False)
