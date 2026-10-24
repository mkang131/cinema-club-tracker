"""Alembic reads the same environment configuration as the application."""

from alembic import context

from app import models  # noqa: F401 -- registers the model metadata
from app.config import Settings
from app.db import Base, make_engine

target_metadata = Base.metadata


def run_migrations():
    settings = Settings()
    if context.is_offline_mode():
        context.configure(
            url=settings.database_url,
            target_metadata=target_metadata,
            literal_binds=True,
            dialect_opts={"paramstyle": "named"},
        )
        with context.begin_transaction():
            context.run_migrations()
    else:
        engine = make_engine(settings.database_url)
        with engine.connect() as connection:
            context.configure(connection=connection, target_metadata=target_metadata)
            with context.begin_transaction():
                context.run_migrations()
        engine.dispose()


run_migrations()
