"""Fixtures for tests that need a real PostgreSQL built by the migrations."""

import socket
from collections.abc import Generator
from urllib.parse import urlparse, urlunparse

import psycopg
import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from app.core.config import settings
from sqlalchemy.orm import Session, sessionmaker


def _test_db_url() -> str:
    url = settings.TEST_DATABASE_URL
    parsed = urlparse(url)
    if parsed.port == 5432 and parsed.hostname in ("localhost", "127.0.0.1", ""):
        raise RuntimeError(
            "Safety error: Refusing to connect to port 5432 on localhost "
            "(reserved on host machine)."
        )
    return url


def _admin_url(test_db_url: str) -> str:
    parsed = urlparse(test_db_url)
    return urlunparse(parsed._replace(scheme="postgresql", path="/postgres"))


def _reachable(admin_url: str) -> bool:
    parsed = urlparse(admin_url)
    try:
        socket.create_connection(
            (parsed.hostname or "127.0.0.1", parsed.port or 15432), timeout=1
        ).close()
        with psycopg.connect(admin_url, connect_timeout=3, autocommit=True) as conn:
            conn.execute("SELECT 1")
        return True
    except (OSError, psycopg.Error):
        return False


def _recreate_database(admin_url: str) -> None:
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute("DROP DATABASE IF EXISTS sma_test WITH (FORCE)")
        conn.execute("CREATE DATABASE sma_test OWNER sma")


@pytest.fixture(scope="module")
def migrated_engine() -> Generator[sa.Engine, None, None]:
    """A fresh ``sma_test`` database upgraded to ``head`` by Alembic.

    Skipped when PostgreSQL is not reachable. The database is recreated empty
    afterwards, like the other integration tests leave it.
    """
    url = _test_db_url()
    admin = _admin_url(url)
    if not _reachable(admin):
        pytest.skip("PostgreSQL is not reachable; skipping integration test.")

    _recreate_database(admin)
    engine = sa.create_engine(url, poolclass=sa.pool.NullPool)
    try:
        cfg = Config("alembic.ini")
        cfg.set_main_option("sqlalchemy.url", url)
        command.upgrade(cfg, "head")
        yield engine
    finally:
        engine.dispose()
        _recreate_database(admin)


@pytest.fixture
def pg(migrated_engine: sa.Engine) -> Generator[Session, None, None]:
    """A session on the migrated database; run tables are emptied afterwards."""
    session = sessionmaker(bind=migrated_engine, autoflush=False)()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        with migrated_engine.begin() as conn:
            conn.execute(sa.text("TRUNCATE workflow_runs, agent_steps CASCADE"))
