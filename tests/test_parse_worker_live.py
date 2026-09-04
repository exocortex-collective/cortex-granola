"""The parse worker's queue contract.

parse.py is tested directly; this file is only about what the worker does
around it -- claiming, completing, and the three ways a job can fail. Those are
the paths that decide whether a note quietly stops being parsed.
"""

from __future__ import annotations

from typing import Any

import psycopg2
import pytest
from cortex_utils.queue import enqueue
from psycopg2.extras import Json

from cortex_granola.ingest import PARSE_QUEUE
from cortex_granola.services.parse_worker import ParseWorker

NOTE = "not_worker0001"


@pytest.fixture
def worker(dsn: str, clean: psycopg2.extensions.connection) -> ParseWorker:
    return ParseWorker(dsn, batch_size=5)


def _store_raw(conn: psycopg2.extensions.connection, note_id: str = NOTE) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO granola_notes_raw (granola_id, raw, api_updated_at) VALUES (%s, %s, %s) "
            "ON CONFLICT (granola_id) DO NOTHING",
            (
                note_id,
                Json(
                    {
                        "id": note_id,
                        "title": "T",
                        "updated_at": "2026-09-01T10:00:00Z",
                        "created_at": "2026-09-01T09:00:00Z",
                    }
                ),
                "2026-09-01T10:00:00Z",
            ),
        )
    conn.commit()


def _enqueue(conn: psycopg2.extensions.connection, payload: dict[str, Any]) -> None:
    enqueue(conn, PARSE_QUEUE, payload, commit=False)
    conn.commit()


def _job_status(conn: psycopg2.extensions.connection) -> list[tuple[str, int]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT status, attempts FROM queue WHERE queue_name = %s ORDER BY id", (PARSE_QUEUE,)
        )
        return list(cur.fetchall())


@pytest.mark.live
def test_a_claimed_job_is_parsed_and_completed(
    worker: ParseWorker, clean: psycopg2.extensions.connection
) -> None:
    _store_raw(clean)
    _enqueue(clean, {"granola_id": NOTE})

    stats = worker.run_once()

    assert stats.parsed == 1 and stats.failed == 0
    assert _job_status(clean) == [("completed", 0)]
    with clean.cursor() as cur:
        cur.execute("SELECT count(*) FROM granola_notes WHERE granola_id = %s", (NOTE,))
        assert cur.fetchone()[0] == 1


@pytest.mark.live
def test_an_empty_queue_does_nothing(worker: ParseWorker) -> None:
    stats = worker.run_once()
    assert (stats.parsed, stats.tombstoned, stats.failed) == (0, 0, 0)


@pytest.mark.live
def test_a_job_with_no_granola_id_fails_rather_than_retrying_forever(
    worker: ParseWorker, clean: psycopg2.extensions.connection
) -> None:
    """It can never succeed, so retrying only burns attempts to reach the same
    conclusion."""
    _enqueue(clean, {"wrong_key": "x"})

    stats = worker.run_once()

    assert stats.failed == 1
    # Pinned exactly: max_attempts is 3, so one failure leaves the job pending
    # with a spent attempt. `status in {"pending", "failed"}` accepted either
    # and so could not catch a change in retry policy.
    assert _job_status(clean)[0] == ("pending", 1)


@pytest.mark.live
def test_a_job_naming_a_missing_raw_row_fails(
    worker: ParseWorker, clean: psycopg2.extensions.connection
) -> None:
    """Enqueue happens in the same transaction as the raw upsert, so the row
    cannot be legitimately absent. Waiting will not bring it back."""
    _enqueue(clean, {"granola_id": "not_never_stored"})

    stats = worker.run_once()

    assert stats.failed == 1
    with clean.cursor() as cur:
        cur.execute("SELECT last_error FROM queue WHERE queue_name = %s", (PARSE_QUEUE,))
        assert "raw row missing" in cur.fetchone()[0]


@pytest.mark.live
def test_the_derived_rows_and_the_completion_land_together(
    worker: ParseWorker, clean: psycopg2.extensions.connection
) -> None:
    """A completed job with half-written tables is the state nothing fixes: the
    next poll sees the note as unchanged and never re-enqueues it."""
    _store_raw(clean)
    _enqueue(clean, {"granola_id": NOTE})
    worker.run_once()

    with clean.cursor() as cur:
        cur.execute(
            "SELECT (SELECT count(*) FROM granola_notes WHERE granola_id = %s), "
            "(SELECT count(*) FROM queue WHERE queue_name = %s AND status = 'completed')",
            (NOTE, PARSE_QUEUE),
        )
        notes, completed = cur.fetchone()
    assert (notes, completed) == (1, 1), "both or neither"


@pytest.mark.live
def test_a_batch_is_processed_in_one_pass(
    worker: ParseWorker, clean: psycopg2.extensions.connection
) -> None:
    for i in range(3):
        note_id = f"not_batch{i}"
        _store_raw(clean, note_id)
        _enqueue(clean, {"granola_id": note_id})

    stats = worker.run_once()

    assert stats.parsed == 3
    assert all(s == "completed" for s, _ in _job_status(clean))


@pytest.mark.live
def test_a_tombstoned_note_completes_as_tombstoned(
    worker: ParseWorker, clean: psycopg2.extensions.connection
) -> None:
    _store_raw(clean)
    with clean.cursor() as cur:
        cur.execute(
            "UPDATE granola_notes_raw SET deleted_at = NOW() WHERE granola_id = %s", (NOTE,)
        )
    clean.commit()
    _enqueue(clean, {"granola_id": NOTE})

    stats = worker.run_once()

    assert stats.tombstoned == 1 and stats.parsed == 0
    assert _job_status(clean) == [("completed", 0)]


@pytest.mark.live
def test_stopping_ends_the_batch_early(
    worker: ParseWorker, clean: psycopg2.extensions.connection
) -> None:
    """SIGTERM finishes the job in hand and stops; it must not abandon claimed
    rows mid-batch to sit until the visibility timeout."""
    for i in range(3):
        note_id = f"not_stop{i}"
        _store_raw(clean, note_id)
        _enqueue(clean, {"granola_id": note_id})

    worker.request_stop(15, None)
    stats = worker.run_once()

    assert stats.parsed == 0, "a worker asked to stop takes no new work from the batch"
    assert stats.released == 3, "and hands back what it claimed"

    # The assertion this test was written for and did not make: rows left in
    # 'processing' sit there until the visibility timeout -- up to 30 minutes --
    # which defeats the point of a fast shutdown.
    with clean.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM queue WHERE queue_name = %s AND status = 'processing'",
            (PARSE_QUEUE,),
        )
        assert cur.fetchone()[0] == 0, "no job may be left claimed by a stopped worker"
        cur.execute(
            "SELECT count(*) FROM queue WHERE queue_name = %s AND status = 'pending' "
            "AND attempts = 0",
            (PARSE_QUEUE,),
        )
        assert cur.fetchone()[0] == 3, "released work is not charged an attempt"


@pytest.mark.live
def test_ensure_schema_is_reachable_from_the_worker(
    worker: ParseWorker, clean: psycopg2.extensions.connection
) -> None:
    """The method the container runs at boot, exercised through the class that
    runs it rather than through a parallel fixture path."""
    worker.ensure_schema()
    with clean.cursor() as cur:
        cur.execute("SELECT to_regclass('granola_notes_raw')")
        assert cur.fetchone()[0] is not None


@pytest.mark.live
def test_concurrent_schema_application_does_not_race(dsn: str) -> None:
    """Cortex-b4o.13: all three services apply migrations on boot, and CREATE
    TABLE IF NOT EXISTS is NOT safe against a concurrent identical CREATE --
    they collide in pg_type and one dies with a UniqueViolation. restart:
    unless-stopped recovered it, which is worse than the crash, because a
    container flapping on boot reads as "Up" and hides why.

    Creates its OWN database. The first version used the shared test DSN and so
    passed with the lock removed: every earlier test in the file had already
    primed those tables through the schema fixture, leaving nothing to race by
    the time this ran. It failed in isolation and passed in CI -- which is the
    worst of both, because CI is where a re-removal of the lock would need
    catching.
    """
    import threading

    from cortex_granola.config import apply_schema

    scratch = "cortex_race_scratch"
    admin = psycopg2.connect(_swap_dbname(dsn, "postgres"))
    admin.autocommit = True
    try:
        with admin.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS {scratch}")
            cur.execute(f"CREATE DATABASE {scratch}")

        errors: list[Exception] = []
        barrier = threading.Barrier(4)

        def apply() -> None:
            conn = psycopg2.connect(_swap_dbname(dsn, scratch))
            try:
                barrier.wait(timeout=15)  # maximise the overlap
                apply_schema(conn)
            except Exception as exc:  # noqa: BLE001 - collected and asserted below
                errors.append(exc)
            finally:
                conn.close()

        threads = [threading.Thread(target=apply) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        assert errors == [], f"concurrent schema application raced: {errors}"
    finally:
        with admin.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS {scratch}")
        admin.close()


def _swap_dbname(dsn: str, dbname: str) -> str:
    parts = [p for p in dsn.split() if not p.startswith("dbname=")]
    return " ".join([*parts, f"dbname={dbname}"])
