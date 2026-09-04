"""The poller's watermark, against a real Postgres.

Every rule here has the same failure mode: the watermark moves too far, and the
notes it stepped over are never fetched again. No error, no retry, no row to
notice missing. That is why these are live tests -- the rules ARE the SQL, and a
mock would return whatever the code asked for while the database did something
else.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

import psycopg2
import pytest

from cortex_granola.config import GranolaSyncConfig
from cortex_granola.services.sync import GranolaSync

SOURCE = "pytest"


class FakeClient:
    """Serves a canned note list; `fail_on` makes one note raise."""

    def __init__(self, notes: list[dict[str, Any]], fail_on: str | None = None) -> None:
        self.notes = notes
        self.fail_on = fail_on
        self.updated_after_seen: list[str | None] = []
        self.note_updated_at = "2026-09-01T10:00:00Z"

    # The real API rejects anything that is not RFC3339 seconds-precision UTC.
    # The first version of this fake accepted any string, which is why every
    # incremental poll failed in production against a suite that was green:
    # datetime.isoformat() produces "2026-09-04T19:33:35.250175+00:00" and the
    # API answers VALIDATION_ERROR / "Invalid date". A fake looser than the
    # service it stands in for tests the fake.
    _RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def list_notes(self, **kwargs: Any) -> list[dict[str, Any]]:
        since = kwargs.get("updated_after")
        if since is not None and not self._RFC3339.match(since):
            raise AssertionError(
                f"granola rejects updated_after={since!r}: "
                "it must be RFC3339 UTC to the second, e.g. 2026-09-04T19:33:35Z"
            )
        self.updated_after_seen.append(since)
        return self.notes

    def list_folders(self) -> list[dict[str, Any]]:
        return []

    def get_note(self, note_id: str, *, include_transcript: bool = False) -> dict[str, Any]:
        if note_id == self.fail_on:
            raise RuntimeError("granola exploded")
        return {
            "id": note_id,
            "title": note_id,
            "updated_at": self.note_updated_at,
            "summary_markdown": "x",
        }


def _note(note_id: str) -> dict[str, Any]:
    return {"id": note_id, "updated_at": "2026-09-01T10:00:00Z"}


@pytest.fixture
def sync(clean: psycopg2.extensions.connection, dsn: str) -> GranolaSync:
    cfg = GranolaSyncConfig(api_key="grn_test", dsn=dsn, source=SOURCE, overlap_minutes=60)
    service = GranolaSync(cfg, client=FakeClient([]))
    with clean.cursor() as cur:
        cur.execute("DELETE FROM granola_sync_state WHERE source = %s", (SOURCE,))
        cur.execute(
            "INSERT INTO granola_sync_state (source, overlap_minutes) VALUES (%s, %s)",
            (SOURCE, 60),
        )
    clean.commit()
    return service


def _watermark(conn: psycopg2.extensions.connection) -> Any:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT last_synced_at, last_error FROM granola_sync_state WHERE source = %s", (SOURCE,)
        )
        return cur.fetchone()


@pytest.mark.live
def test_a_clean_run_advances_the_watermark(
    sync: GranolaSync, clean: psycopg2.extensions.connection
) -> None:
    sync.client = FakeClient([_note("not_a"), _note("not_b")])
    stats = sync.poll()
    assert stats.clean and stats.stored == 2

    last_synced_at, last_error = _watermark(clean)
    assert last_synced_at is not None
    assert last_error is None


@pytest.mark.live
def test_a_run_with_one_failure_does_not_advance_the_watermark(
    sync: GranolaSync, clean: psycopg2.extensions.connection
) -> None:
    """The rule the whole watermark-integrity charter exists for.

    The failed note is inside the old window and outside a newer one, so
    advancing past it loses it permanently.
    """
    sync.client = FakeClient([_note("not_a"), _note("not_b")], fail_on="not_b")
    stats = sync.poll()
    assert not stats.clean and stats.errors == 1

    last_synced_at, last_error = _watermark(clean)
    assert last_synced_at is None, "a failed run must not move the watermark"
    assert last_error is not None and "not_b" in last_error


@pytest.mark.live
def test_a_failure_still_records_that_the_poll_ran(
    sync: GranolaSync, clean: psycopg2.extensions.connection
) -> None:
    """Holding the watermark must not also hide the failure."""
    sync.client = FakeClient([_note("not_a")], fail_on="not_a")
    sync.poll()
    with clean.cursor() as cur:
        cur.execute("SELECT last_poll_at FROM granola_sync_state WHERE source = %s", (SOURCE,))
        assert cur.fetchone()[0] is not None


@pytest.mark.live
def test_the_overlap_is_applied_on_read_and_never_stored(
    sync: GranolaSync, clean: psycopg2.extensions.connection
) -> None:
    """Storing (now - overlap) compounds it: a 60-minute overlap applied 24
    times has walked the watermark a day backwards."""
    client = FakeClient([_note("not_a")])
    sync.client = client

    sync.poll()
    first_watermark = _watermark(clean)[0]

    sync.poll()
    second_watermark = _watermark(clean)[0]

    # The stored value moved forward, not backward by the overlap.
    assert second_watermark >= first_watermark

    # And the window sent to the API is the stored value MINUS the overlap.
    since = client.updated_after_seen[-1]
    assert since is not None
    assert abs((first_watermark - timedelta(minutes=60)) - _parse(since)) < timedelta(seconds=2)


def _parse(value: str) -> Any:
    from datetime import datetime

    return datetime.fromisoformat(value)


@pytest.mark.live
def test_the_first_poll_scans_everything(sync: GranolaSync) -> None:
    """A NULL watermark means no updated_after bound at all, not 'since now'."""
    client = FakeClient([_note("not_a")])
    sync.client = client
    sync.poll()
    assert client.updated_after_seen[0] is None


@pytest.mark.live
def test_seeding_does_not_overwrite_an_existing_overlap(
    sync: GranolaSync, clean: psycopg2.extensions.connection
) -> None:
    """The DB column is the single source of truth once the row exists.

    Re-seeding from config on every boot would make a per-source override last
    until the next restart, which is worse than not supporting one at all
    because it appears to work.
    """
    with clean.cursor() as cur:
        cur.execute(
            "UPDATE granola_sync_state SET overlap_minutes = 5 WHERE source = %s", (SOURCE,)
        )
    clean.commit()

    sync._seed_sync_state(clean)
    clean.commit()

    with clean.cursor() as cur:
        cur.execute("SELECT overlap_minutes FROM granola_sync_state WHERE source = %s", (SOURCE,))
        assert cur.fetchone()[0] == 5


# --- backfill -------------------------------------------------------------


def _job(conn: psycopg2.extensions.connection, status: str = "pending") -> str:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO granola_backfill_jobs (status, after_date, before_date) "
            "VALUES (%s, '2024-01-01', '2024-12-31') RETURNING id",
            (status,),
        )
        job_id: str = cur.fetchone()[0]
    conn.commit()
    return job_id


@pytest.mark.live
def test_backfill_claims_and_completes_a_job(
    sync: GranolaSync, clean: psycopg2.extensions.connection
) -> None:
    job_id = _job(clean)
    sync.client = FakeClient([_note("not_a")])

    assert sync.check_backfill_queue() is True

    with clean.cursor() as cur:
        cur.execute(
            "SELECT status, processed, completed_at FROM granola_backfill_jobs WHERE id = %s",
            (job_id,),
        )
        status, processed, completed_at = cur.fetchone()
    assert status == "completed"
    assert processed == 1
    assert completed_at is not None


@pytest.mark.live
def test_an_empty_queue_is_not_an_error(sync: GranolaSync) -> None:
    assert sync.check_backfill_queue() is False


@pytest.mark.live
def test_a_failing_backfill_is_recorded_as_failed(
    sync: GranolaSync, clean: psycopg2.extensions.connection
) -> None:
    """A crash must leave a record of how far it got, not a job stuck at
    'running' forever."""
    job_id = _job(clean)

    class Exploding(FakeClient):
        def list_notes(self, **kwargs: Any) -> list[dict[str, Any]]:
            raise RuntimeError("granola exploded")

    sync.client = Exploding([])
    assert sync.check_backfill_queue() is True

    with clean.cursor() as cur:
        cur.execute("SELECT status, error FROM granola_backfill_jobs WHERE id = %s", (job_id,))
        status, error = cur.fetchone()
    assert status == "failed"
    assert error and "exploded" in error


@pytest.mark.live
def test_finishing_does_not_clobber_a_cancel(
    sync: GranolaSync, clean: psycopg2.extensions.connection
) -> None:
    """A job cancelled mid-scan stays cancelled.

    _finish_backfill guards on status = 'running' precisely so a scan that was
    stopped by an operator cannot report 'completed' over their decision.
    """
    job_id = _job(clean)
    with clean.cursor() as cur:
        cur.execute(
            "UPDATE granola_backfill_jobs SET status = 'cancelled' WHERE id = %s", (job_id,)
        )
    clean.commit()

    sync._finish_backfill(job_id, "completed")

    with clean.cursor() as cur:
        cur.execute("SELECT status FROM granola_backfill_jobs WHERE id = %s", (job_id,))
        assert cur.fetchone()[0] == "cancelled"


@pytest.mark.live
def test_backfill_does_not_touch_the_watermark(
    sync: GranolaSync, clean: psycopg2.extensions.connection
) -> None:
    """The range is explicit, so a backfill failure moves nothing the
    incremental poll depends on."""
    _job(clean)
    sync.client = FakeClient([_note("not_a")])
    sync.check_backfill_queue()
    assert _watermark(clean)[0] is None


# --- paths every earlier test left dead -----------------------------------


class FolderClient(FakeClient):
    """Serves one note that lives in a folder the deny list can name."""

    def __init__(self, notes: list[dict[str, Any]]) -> None:
        super().__init__(notes)
        self.fetched: list[str] = []

    def list_folders(self) -> list[dict[str, Any]]:
        return [
            {
                "id": "fol_x",
                "name": "Bus Dev",
                "parent_folder_id": None,
                "space_id": "spa_1",
                "space_name": "Team",
            }
        ]

    def list_notes(self, **kwargs: Any) -> list[dict[str, Any]]:
        if kwargs.get("folder_id"):
            return self.notes
        return super().list_notes(**kwargs)

    def get_note(self, note_id: str, *, include_transcript: bool = False) -> dict[str, Any]:
        self.fetched.append(note_id)
        return super().get_note(note_id, include_transcript=include_transcript)


def _denying_sync(dsn: str, client: FolderClient) -> GranolaSync:
    cfg = GranolaSyncConfig(api_key="grn_test", dsn=dsn, source=SOURCE, deny_folders=["Bus Dev"])
    return GranolaSync(cfg, client=client)


@pytest.mark.live
def test_a_denied_note_is_never_fetched(
    sync: GranolaSync, dsn: str, clean: psycopg2.extensions.connection
) -> None:
    """Exclusions resolve BEFORE the content fetch, which is the whole point:
    an excluded meeting's summary and transcript never leave Granola.

    A note that was never stored has nothing to tombstone -- there are no files
    to remove -- so the correct outcome is that nothing happens at all.
    """
    client = FolderClient([_note("not_secret")])
    stats = _denying_sync(dsn, client).poll()

    assert client.fetched == [], "an excluded note's body must never be downloaded"
    assert stats.tombstoned == 0, "nothing was stored, so there is nothing to tombstone"


@pytest.mark.live
def test_a_stored_note_that_becomes_denied_is_tombstoned(
    sync: GranolaSync, dsn: str, clean: psycopg2.extensions.connection
) -> None:
    """The tombstone path CLAUDE.md names as this repo's top risk.

    A note moved into an excluded folder after it was already synced must be
    marked deleted so the export worker knows to remove its files -- the row is
    kept precisely because a vanished row could not tell it which files those
    were.
    """
    sync.client = FakeClient([_note("not_secret")])
    sync.poll()
    with clean.cursor() as cur:
        cur.execute("SELECT count(*) FROM granola_notes_raw WHERE granola_id = 'not_secret'")
        assert cur.fetchone()[0] == 1

    stats = _denying_sync(dsn, FolderClient([_note("not_secret")])).poll()

    assert stats.tombstoned == 1
    with clean.cursor() as cur:
        cur.execute(
            "SELECT deleted_at IS NOT NULL FROM granola_notes_raw WHERE granola_id = 'not_secret'"
        )
        assert cur.fetchone()[0] is True


@pytest.mark.live
def test_a_note_whose_updated_at_moves_is_reingested(
    sync: GranolaSync, clean: psycopg2.extensions.connection
) -> None:
    """The UPDATED branch, which no test reached because none changed a note
    between two polls."""
    client = FakeClient([_note("not_a")])
    sync.client = client
    sync.poll()

    client.notes = [{"id": "not_a", "updated_at": "2026-09-05T10:00:00Z"}]
    client.note_updated_at = "2026-09-05T10:00:00Z"
    stats = sync.poll()

    assert stats.updated == 1, "a moved updated_at must re-ingest, not be seen as unchanged"


@pytest.mark.live
def test_a_note_with_no_id_holds_the_watermark(
    sync: GranolaSync, clean: psycopg2.extensions.connection
) -> None:
    """Regression: a bare `continue` here skipped the note without counting it,
    so stats.clean stayed True and the watermark advanced past a note that was
    never ingested."""
    sync.client = FakeClient([{"updated_at": "2026-09-01T10:00:00Z"}])
    stats = sync.poll()

    assert stats.errors == 1
    assert not stats.clean
    assert _watermark(clean)[0] is None, "a note with no id must not be silently skipped"


@pytest.mark.live
def test_ensure_schema_creates_everything_the_service_needs(dsn: str) -> None:
    """ensure_schema() was never called by a test -- conftest duplicated its
    steps instead, so the method the container actually runs at boot was
    unexercised."""
    cfg = GranolaSyncConfig(api_key="grn_test", dsn=dsn, source="ensure-schema-test")
    GranolaSync(cfg, client=FakeClient([])).ensure_schema()

    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            for table in ("queue", "granola_notes_raw", "granola_sync_state"):
                cur.execute("SELECT to_regclass(%s)", (table,))
                assert cur.fetchone()[0] is not None, f"{table} missing"
            cur.execute(
                "SELECT overlap_minutes FROM granola_sync_state WHERE source = %s",
                ("ensure-schema-test",),
            )
            assert cur.fetchone()[0] == 60
            cur.execute("DELETE FROM granola_sync_state WHERE source = %s", ("ensure-schema-test",))
        conn.commit()
    finally:
        conn.close()


@pytest.mark.live
def test_run_once_survives_a_crash_without_advancing(
    sync: GranolaSync, clean: psycopg2.extensions.connection
) -> None:
    """run()'s crash-recovery branch is unreachable from a test because of its
    `while True`; run_once() exists so it is not."""

    class Exploding(FakeClient):
        def list_notes(self, **kwargs: Any) -> list[dict[str, Any]]:
            raise RuntimeError("granola exploded")

    sync.client = Exploding([])
    assert sync.run_once() is None, "a crashed pass reports None rather than raising"
    assert _watermark(clean)[0] is None


@pytest.mark.live
def test_the_incremental_window_is_sent_as_rfc3339(
    sync: GranolaSync, clean: psycopg2.extensions.connection
) -> None:
    """The bug this pins broke every incremental poll in production while the
    suite stayed green.

    datetime.isoformat() gives '2026-09-04T19:33:35.250175+00:00'; the API
    answers VALIDATION_ERROR / 'Invalid date'. The first full scan succeeded
    because it passes updated_after=None and never formats anything -- so the
    backfill working looked like proof the poller worked.
    """
    client = FakeClient([_note("not_a")])
    sync.client = client

    sync.poll()  # first pass: no watermark, so no updated_after
    assert client.updated_after_seen == [None]

    sync.poll()  # second pass sends the window; FakeClient now rejects bad formats
    since = client.updated_after_seen[-1]
    assert since is not None
    assert since.endswith("Z") and "." not in since, since
