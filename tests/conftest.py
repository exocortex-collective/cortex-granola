"""Shared fixtures.

Live tests take a real Postgres from CORTEX_TEST_DSN rather than a mock. The
defects this suite exists to catch -- a watermark that advances after a failure,
an ON CONFLICT that fires when it should not, a dedup_key that does not dedup --
are all invisible to a mock, which hands back whatever the code asked for.
"""

from __future__ import annotations

import os
import pathlib
from collections.abc import Iterator

import psycopg2
import pytest


@pytest.fixture(scope="session")
def dsn() -> str:
    """The live-Postgres DSN, or a skip -- unless CORTEX_REQUIRE_LIVE forbids it.

    `-p no:skipping` does NOT make this fail. That flag disables the plugin
    handling the `skip`/`skipif` MARKERS; a runtime `pytest.skip()` raised
    inside a fixture goes through the outcome machinery untouched, and the run
    still exits 0. Measured: with the flag and no DSN, "1 skipped", exit 0 --
    a guard against silent skipping that silently skipped.

    So the guard is an explicit contract instead of a plugin trick. CI sets
    CORTEX_REQUIRE_LIVE, and then a missing DSN is a failure rather than a skip.
    """
    value = os.environ.get("CORTEX_TEST_DSN")
    if value:
        return value
    if os.environ.get("CORTEX_REQUIRE_LIVE"):
        pytest.fail(
            "CORTEX_TEST_DSN is unset but CORTEX_REQUIRE_LIVE is set: the live "
            "tests are required here and must not be skipped"
        )
    pytest.skip("CORTEX_TEST_DSN is not set")


@pytest.fixture
def conn(dsn: str) -> Iterator[psycopg2.extensions.connection]:
    """A connection that always rolls back.

    Every live test shares one database, so a test that commits leaks state into
    whichever test runs next -- and the resulting failure lands on the innocent
    one. Schema setup that genuinely needs committing does it explicitly.
    """
    connection = psycopg2.connect(dsn)
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


@pytest.fixture
def schema(conn: psycopg2.extensions.connection) -> psycopg2.extensions.connection:
    """A connection with the queue schema and this service's tables in place.

    Committed, not rolled back: `ensure_queue_schema` creates partitions, which
    needs its own transaction, and every test in the session shares the result.
    Test DATA still rolls back -- only the DDL persists.
    """
    from cortex_utils.queue import ensure_queue_schema

    ensure_queue_schema(conn)
    ddl = pathlib.Path(__file__).resolve().parents[1] / "migrations" / "001_granola_schema.sql"
    with conn.cursor() as cur:
        cur.execute(ddl.read_text())
    conn.commit()
    return conn


@pytest.fixture
def clean(schema: psycopg2.extensions.connection) -> psycopg2.extensions.connection:
    """Schema in place and this service's tables empty.

    The raw table cascades to every derived table, so one TRUNCATE clears the
    lot. The shared `queue` table is filtered by queue_name rather than
    truncated -- other Cortex services use it, and a test that truncates it
    would delete their work.
    """
    with schema.cursor() as cur:
        cur.execute("TRUNCATE granola_notes_raw CASCADE")
        cur.execute("DELETE FROM queue WHERE queue_name = 'granola_parse'")
    schema.commit()
    return schema
