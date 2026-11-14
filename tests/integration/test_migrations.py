"""Integration tests for database migrations against real PostgreSQL."""

import socket
from urllib.parse import urlparse, urlunparse

import psycopg
import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from app.core.config import settings

EXPECTED_TABLES = {
    "users",
    "refresh_tokens",
    "user_limits",
    "workspaces",
    "system_settings",
    "audit_logs",
}


def _get_test_db_url() -> str:
    """Return configured test database URL and verify safety."""
    url = settings.TEST_DATABASE_URL
    parsed = urlparse(url)
    if parsed.port == 5432 and parsed.hostname in ("localhost", "127.0.0.1", ""):
        raise RuntimeError(
            "Safety error: Refusing to connect to port 5432 on localhost "
            "(reserved on host machine)."
        )
    return url


def _get_admin_conn_str(test_db_url: str) -> str:
    """Derive admin postgres connection URL from test database URL."""
    parsed = urlparse(test_db_url)
    return urlunparse(parsed._replace(scheme="postgresql", path="/postgres"))


def _postgres_is_reachable(admin_conn_str: str) -> bool:
    """Check whether local PostgreSQL on published port is accepting connections.

    Uses a low-level socket probe first (returns immediately on Windows even for
    filtered ports) and only proceeds to a full psycopg handshake when the port
    appears open.  This avoids the Windows TCP hang that occurs when
    connect_timeout is ignored for filtered/reserved port ranges.
    """
    parsed = urlparse(admin_conn_str)
    host = parsed.hostname or "localhost"
    port = parsed.port or 5432

    # Fast socket-level probe (1-second timeout)
    try:
        sock = socket.create_connection((host, port), timeout=1)
        sock.close()
    except OSError:
        return False

    # Port is open — do a real authentication handshake
    try:
        with psycopg.connect(
            admin_conn_str,
            connect_timeout=3,
            autocommit=True,
        ) as conn:
            conn.execute("SELECT 1")
            return True
    except (OSError, psycopg.Error):
        return False


@pytest.mark.integration
def test_alembic_upgrade_and_downgrade() -> None:
    """Run migration upgrade and downgrade on an isolated test database."""
    test_db_url = _get_test_db_url()
    admin_conn_str = _get_admin_conn_str(test_db_url)

    if not _postgres_is_reachable(admin_conn_str):
        pytest.skip(
            f"PostgreSQL at {admin_conn_str} is not reachable; "
            "skipping integration test."
        )

    # 1. Recreate clean sma_test database
    with psycopg.connect(admin_conn_str, autocommit=True) as conn:
        conn.execute("DROP DATABASE IF EXISTS sma_test WITH (FORCE)")
        conn.execute("CREATE DATABASE sma_test OWNER sma")

    test_engine = sa.create_engine(test_db_url, poolclass=sa.pool.NullPool)

    try:
        # 2. Configure Alembic to point to the test database
        alembic_cfg = Config("alembic.ini")
        alembic_cfg.set_main_option("sqlalchemy.url", test_db_url)

        # 3. Upgrade to head
        command.upgrade(alembic_cfg, "head")

        with test_engine.connect() as connection:
            inspector = sa.inspect(connection)
            tables = set(inspector.get_table_names())
            assert EXPECTED_TABLES.issubset(tables)

        # 4. Downgrade back to base
        command.downgrade(alembic_cfg, "base")

        with test_engine.connect() as connection:
            inspector = sa.inspect(connection)
            tables = set(inspector.get_table_names())
            assert EXPECTED_TABLES.isdisjoint(tables)

    finally:
        test_engine.dispose()
        # Clean up database
        with psycopg.connect(admin_conn_str, autocommit=True) as conn:
            conn.execute("DROP DATABASE IF EXISTS sma_test WITH (FORCE)")
