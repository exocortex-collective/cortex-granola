"""Bootstrap checks: the package imports, and the live-Postgres plumbing works.

The second test is not ceremony. CI's anti-skip guard asserts that `live` tests
actually run, and that guard is only meaningful if at least one live test
exists -- `pytest -m live` with no matches exits 5, which would fail the step
for the wrong reason and get "fixed" by weakening the guard.
"""

from __future__ import annotations

import psycopg2
import pytest

import cortex_granola


def test_package_exposes_a_version() -> None:
    assert cortex_granola.__version__


@pytest.mark.live
def test_shared_queue_schema_can_be_ensured(
    conn: psycopg2.extensions.connection,
) -> None:
    """cortex_utils owns the queue schema; this service only calls the entry point.

    Proves the dependency resolves and the DDL applies against a real server.
    ensure_queue_schema() is the single boot entry point -- it sequences the
    DDL, the migrations, the dead-letter table and the first partitions, and
    calling the individual pieces is what it exists to prevent.
    """
    from cortex_utils.queue import ensure_queue_schema

    ensure_queue_schema(conn)
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('queue')")
        row = cur.fetchone()
    assert row is not None and row[0] is not None
