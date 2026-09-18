"""The ingest seam, against a real Postgres.

Every adapter -- poller, backfill, and later the Pub/Sub pull -- goes through
`ingest_one()`, so its contract is the one thing in this repo that must not
drift. These are live tests because the contract IS the SQL: an ON CONFLICT
whose WHERE clause suppresses a write, a dedup_key that must not double-enqueue,
a tombstone that must survive. A mock hands back whatever the code asked for and
would pass every one of these while the database did the opposite.
"""

from __future__ import annotations

from typing import Any

import psycopg2
import pytest

from cortex_granola.granola.api import GranolaError, TranscriptTooLarge
from cortex_granola.ingest import Outcome, ingest_one, tombstone

NOTE_ID = "not_test0000000001"


class FakeClient:
    """A Granola client that returns a canned note and counts fetches.

    The fetch count is an assertion target, not bookkeeping: skipping the fetch
    for an unchanged note is the difference between a poll costing one request
    and one costing a request per note in the overlap window, and the overlap
    window is re-scanned on every run by design.
    """

    def __init__(self, note: dict[str, Any]) -> None:
        self.note = note
        self.fetches = 0

    def get_note(self, note_id: str, *, include_transcript: bool = False) -> dict[str, Any]:
        self.fetches += 1
        return dict(self.note, id=note_id)


def _note(updated_at: str = "2026-09-01T10:00:00Z") -> dict[str, Any]:
    return {
        "id": NOTE_ID,
        "title": "Planning",
        "updated_at": updated_at,
        "created_at": "2026-09-01T09:00:00Z",
        "summary_markdown": "# Planning\n\n- ship it",
    }


def _queue_depth(conn: psycopg2.extensions.connection) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM queue WHERE queue_name = 'granola_parse' "
            "AND payload->>'granola_id' = %s AND status IN ('pending','processing')",
            (NOTE_ID,),
        )
        return int(cur.fetchone()[0])


@pytest.mark.live
def test_first_ingest_stores_and_enqueues(clean: psycopg2.extensions.connection) -> None:
    client = FakeClient(_note())
    result = ingest_one(clean, client, NOTE_ID)
    clean.commit()

    assert result.outcome is Outcome.STORED
    assert result.job_id is not None
    assert _queue_depth(clean) == 1

    with clean.cursor() as cur:
        cur.execute(
            "SELECT raw->>'title', deleted_at FROM granola_notes_raw WHERE granola_id = %s",
            (NOTE_ID,),
        )
        title, deleted_at = cur.fetchone()
    assert title == "Planning"
    assert deleted_at is None


@pytest.mark.live
def test_reingest_at_same_updated_at_is_a_no_op(clean: psycopg2.extensions.connection) -> None:
    """The overlap window re-presents notes that have not changed.

    Neither a second raw write nor a second parse job may result -- the first
    would churn the row on every poll, the second would re-parse the whole
    overlap window every 15 minutes.
    """
    client = FakeClient(_note())
    ingest_one(clean, client, NOTE_ID)
    clean.commit()

    again = ingest_one(clean, client, NOTE_ID)
    clean.commit()

    assert again.outcome is Outcome.UNCHANGED
    assert again.job_id is None
    assert _queue_depth(clean) == 1


@pytest.mark.live
def test_known_updated_at_skips_the_fetch(clean: psycopg2.extensions.connection) -> None:
    """A note the list response says we are current on is never fetched."""
    client = FakeClient(_note())
    ingest_one(clean, client, NOTE_ID)
    clean.commit()
    after_first = client.fetches

    result = ingest_one(clean, client, NOTE_ID, api_updated_at="2026-09-01T10:00:00Z")

    assert result.outcome is Outcome.UNCHANGED
    assert client.fetches == after_first, "an unchanged note must not be re-fetched"


@pytest.mark.live
def test_a_moved_updated_at_restores_and_re_enqueues(clean: psycopg2.extensions.connection) -> None:
    client = FakeClient(_note())
    ingest_one(clean, client, NOTE_ID)
    clean.commit()

    client.note = _note("2026-09-02T12:00:00Z")
    result = ingest_one(clean, client, NOTE_ID)
    clean.commit()

    assert result.outcome is Outcome.UPDATED
    with clean.cursor() as cur:
        cur.execute(
            "SELECT api_updated_at FROM granola_notes_raw WHERE granola_id = %s", (NOTE_ID,)
        )
        assert cur.fetchone()[0].isoformat().startswith("2026-09-02")


@pytest.mark.live
def test_tombstone_marks_and_enqueues_once(clean: psycopg2.extensions.connection) -> None:
    """A tombstone keeps the row: the export cannot remove files for a note it
    can no longer see."""
    client = FakeClient(_note())
    ingest_one(clean, client, NOTE_ID)
    clean.commit()

    first = tombstone(clean, NOTE_ID)
    clean.commit()
    assert first.outcome is Outcome.DELETED

    second = tombstone(clean, NOTE_ID)
    clean.commit()
    assert second.outcome is Outcome.UNCHANGED, "tombstoning twice must not re-enqueue"

    with clean.cursor() as cur:
        cur.execute(
            "SELECT deleted_at IS NOT NULL FROM granola_notes_raw WHERE granola_id = %s", (NOTE_ID,)
        )
        assert cur.fetchone()[0] is True


@pytest.mark.live
def test_a_note_that_returns_after_a_tombstone_goes_live_again(
    clean: psycopg2.extensions.connection,
) -> None:
    """Access regranted, or moved out of an excluded folder.

    Leaving deleted_at set would keep the export deleting files the note now
    wants -- and the API reports no change, so nothing else would clear it.
    """
    client = FakeClient(_note())
    ingest_one(clean, client, NOTE_ID)
    tombstone(clean, NOTE_ID)
    clean.commit()

    result = ingest_one(clean, client, NOTE_ID)
    clean.commit()

    assert result.outcome is Outcome.UPDATED
    with clean.cursor() as cur:
        cur.execute("SELECT deleted_at FROM granola_notes_raw WHERE granola_id = %s", (NOTE_ID,))
        assert cur.fetchone()[0] is None


@pytest.mark.live
def test_a_note_without_updated_at_is_refused(clean: psycopg2.extensions.connection) -> None:
    """Storing it would leave a row the watermark can never advance past."""
    note = _note()
    del note["updated_at"]
    client = FakeClient(note)

    with pytest.raises(ValueError, match="updated_at"):
        ingest_one(clean, client, NOTE_ID)
    clean.rollback()


@pytest.mark.live
def test_ingest_does_not_commit(clean: psycopg2.extensions.connection) -> None:
    """The raw row and the parse job must land together or not at all.

    A committed row with no job is a note nothing will ever retry: the next poll
    sees it as unchanged and skips it forever.
    """
    client = FakeClient(_note())
    ingest_one(clean, client, NOTE_ID)
    clean.rollback()

    with clean.cursor() as cur:
        cur.execute("SELECT count(*) FROM granola_notes_raw WHERE granola_id = %s", (NOTE_ID,))
        assert cur.fetchone()[0] == 0
    assert _queue_depth(clean) == 0


class _OversizedTranscriptClient(FakeClient):
    """The API's behaviour above the inline transcript limit.

    The inline form answers 413; the paged endpoint serves the same transcript
    in pages. Records which routes were taken, because the fallback costing a
    second and third request is the point of the test, not a side effect.
    """

    def __init__(self, note: dict[str, Any], segments: list[dict[str, Any]]) -> None:
        super().__init__(note)
        self.segments = segments
        self.routes: list[str] = []

    def get_note(self, note_id: str, *, include_transcript: bool = False) -> dict[str, Any]:
        self.fetches += 1
        if include_transcript:
            self.routes.append("inline")
            raise TranscriptTooLarge(413, "Payload Too Large", "TRANSCRIPT_TOO_LARGE")
        self.routes.append("note")
        return dict(self.note, id=note_id)

    def get_transcript(self, note_id: str) -> list[dict[str, Any]]:
        self.routes.append("paged")
        return list(self.segments)


@pytest.mark.live
def test_a_transcript_over_the_inline_limit_is_fetched_by_pages(
    clean: psycopg2.extensions.connection,
) -> None:
    """The first long meeting used to wedge the poller for good.

    413 escaped ingest_one, the run counted unclean, the watermark stayed, and
    the next poll re-issued the identical request. The paged endpoint the API
    documents for exactly this had no call sites.
    """
    segments = [{"speaker": {"name": "Devon"}, "text": f"line {i}"} for i in range(3)]
    client = _OversizedTranscriptClient(_note(), segments)

    result = ingest_one(clean, client, NOTE_ID)
    clean.commit()

    assert result.outcome is Outcome.STORED
    assert client.routes == ["inline", "note", "paged"]
    with clean.cursor() as cur:
        cur.execute(
            "SELECT raw->'transcript', raw->>'title' FROM granola_notes_raw WHERE granola_id = %s",
            (NOTE_ID,),
        )
        row = cur.fetchone()
    assert row is not None
    assert row[0] == segments, "the paged transcript was not spliced into the stored note"
    assert row[1] == "Planning", "the note body was lost on the fallback route"


@pytest.mark.live
def test_other_api_errors_still_propagate(clean: psycopg2.extensions.connection) -> None:
    """Only the size limit has a second route; a 500 is still the caller's problem."""

    class _Failing(FakeClient):
        def get_note(self, note_id: str, *, include_transcript: bool = False) -> dict[str, Any]:
            raise GranolaError(500, "upstream", None)

    with pytest.raises(GranolaError):
        ingest_one(clean, _Failing(_note()), NOTE_ID)


@pytest.mark.live
def test_an_empty_paged_transcript_after_a_413_is_refused(
    clean: psycopg2.extensions.connection,
) -> None:
    """The 413 said too large; a paged endpoint returning nothing contradicts it.

    Storing [] would overwrite the record with an empty transcript, and export
    treats [] as a withdrawal and removes the vault file.
    """
    client = _OversizedTranscriptClient(_note(), segments=[])

    with pytest.raises(GranolaError, match="returned nothing"):
        ingest_one(clean, client, NOTE_ID)
    clean.rollback()

    with clean.cursor() as cur:
        cur.execute("SELECT count(*) FROM granola_notes_raw WHERE granola_id = %s", (NOTE_ID,))
        row = cur.fetchone()
    assert row is not None and row[0] == 0, "a contradictory note was stored"
