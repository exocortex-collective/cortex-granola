"""Shared fixtures.

Live tests take a real Postgres from CORTEX_TEST_DSN rather than a mock. The
defects this suite exists to catch -- a watermark that advances after a failure,
an ON CONFLICT that fires when it should not, a dedup_key that does not dedup --
are all invisible to a mock, which hands back whatever the code asked for.
"""

from __future__ import annotations

import os
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
