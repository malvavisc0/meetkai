"""Alembic environment — reads DB URL from CockpitSettings, not alembic.ini.

This keeps the future MySQL switch zero-touch: the URL is resolved at
runtime from ``KAI_COCKPIT_DB`` (default ``sqlite:///data/cockpit.db``),
so the same migrations run against SQLite today and MySQL tomorrow
without ever editing this file or ``alembic.ini``.
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from kai.cockpit.db import Base
from kai.cockpit.models import User  # noqa: F401  — registers tables on Base.metadata
from kai.cockpit.settings import get_cockpit_settings

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", get_cockpit_settings().cockpit_db)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL without a DB connection)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode (apply against a live DB connection)."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
