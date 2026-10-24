"""SQLite setup shared by the API, Alembic, CLI and tests."""

from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import MetaData, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import StaticPool


def utc_now() -> datetime:
    """SQLite stores UTC without tzinfo; responses add the UTC timezone explicitly."""
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(table_name)s_%(column_0_name)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


def make_engine(database_url: str):
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite":
        raise ValueError("This version supports SQLite databases only")
    if url.database and url.database != ":memory:":
        Path(url.database).parent.mkdir(parents=True, exist_ok=True)
    options = {"connect_args": {"check_same_thread": False}, "hide_parameters": True}
    if not url.database or url.database == ":memory:":
        options["poolclass"] = StaticPool
    engine = create_engine(url, **options)

    @event.listens_for(engine, "connect")
    def configure_sqlite(connection, _record):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    return engine
