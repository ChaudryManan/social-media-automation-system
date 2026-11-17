"""Alembic environment configuration."""

import logging
from logging.config import fileConfig

import app.models  # noqa: F401 - load all models for target_metadata
from alembic import context
from app.core.config import settings
from app.core.db import Base
from sqlalchemy import engine_from_config, pool

config = context.config

# Apply alembic.ini's logging only when this process has no logging set up yet
# (the `alembic` CLI). When migrations run inside the app or the test suite the
# app's JSON logging is already configured; fileConfig() would otherwise replace
# its handlers and, by default, disable every existing logger (including the
# access log). disable_existing_loggers=False is kept as a second safeguard.
if config.config_file_name is not None and not logging.getLogger().handlers:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

if not config.get_main_option("sqlalchemy.url"):
    config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
