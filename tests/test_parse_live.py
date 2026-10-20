"""Parsing a raw note into the derived tables, against a real Postgres.

The derived tables are rebuildable from the raw row by re-enqueuing, so the
property that matters is that a re-parse REPLACES rather than accumulates: a
note whose transcript shrank must not keep the segments it no longer has. That
is invisible to a mock and obvious to a database.
"""

from __future__ import annotations

from typing import Any

import psycopg2
import pytest
from psycopg2.extras import Json

from cortex_granola.parse import RawNoteMissingError, parse_note

NOTE = "not_parse0001"


def _raw(**overrides: Any) -> dict[str, Any]:
    note: dict[str, Any] = {
        "id": NOTE,
        "title": "Product planning",
        "created_at": "2026-09-01T09:00:00Z",
        "updated_at": "2026-09-01T10:00:00Z",
        "web_url": "https://notes.granola.ai/d/abc",
        "owner": {"email": "devon@valideval.com", "name": "Devon"},
        "calendar_event": {
            "scheduled_start_time": "2026-09-01T12:00:00Z",
            "scheduled_end_time": "2026-09-01T12:45:00Z",
            "invitees": [{"email": "adam@valideval.com"}],
        },
        "attendees": [{"email": "adam@valideval.com", "name": "Adam Rentschler"}],
        "summary_markdown": "# Product planning\n\n- ship it",
        "transcript": [
            {"speaker": {"name": "Devon"}, "text": "first"},
            {"speaker": {"name": "Adam"}, "text": "second"},
        ],
        "folder_membership": [{"id": "fol_1", "name": "Product", "space_id": "spa_1"}],
        "space_membership": [{"id": "spa_1", "name": "Valid Eval team"}],
    }
    note.update(overrides)
    return note


def _store(
    conn: psycopg2.extensions.connection, raw: dict[str, Any], deleted: bool = False
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO granola_notes_raw (granola_id, raw, api_updated_at, deleted_at) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (granola_id) DO UPDATE SET raw = EXCLUDED.raw, "
            "api_updated_at = EXCLUDED.api_updated_at, deleted_at = EXCLUDED.deleted_at",
            (
                NOTE,
                Json(raw),
                raw["updated_at"],
                "2026-09-02T00:00:00Z" if deleted else None,
            ),
        )
    conn.commit()


def _count(conn: psycopg2.extensions.connection, table: str) -> int:
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {table} WHERE granola_id = %s", (NOTE,))  # noqa: S608
        return int(cur.fetchone()[0])


@pytest.mark.live
def test_parses_a_note_into_every_derived_table(clean: psycopg2.extensions.connection) -> None:
    _store(clean, _raw())
    assert parse_note(clean, NOTE) == "parsed"
    clean.commit()

    with clean.cursor() as cur:
        cur.execute(
            "SELECT title, owner_email, meeting_date, has_transcript, summary_markdown "
            "FROM granola_notes WHERE granola_id = %s",
            (NOTE,),
        )
        title, owner, meeting_date, has_transcript, summary = cur.fetchone()
    assert title == "Product planning"
    assert owner == "devon@valideval.com"
    assert str(meeting_date) == "2026-09-01"
    assert has_transcript is True
    assert "ship it" in summary

    assert _count(clean, "granola_transcript_segments") == 2
    assert _count(clean, "granola_attendees") == 1
    assert _count(clean, "granola_note_folders") == 1


@pytest.mark.live
def test_meeting_date_prefers_the_scheduled_time(clean: psycopg2.extensions.connection) -> None:
    """A note created days after the meeting must file under the MEETING date,
    or the export worker writes it to the wrong place on disk."""
    _store(clean, _raw(created_at="2026-09-20T09:00:00Z"))
    parse_note(clean, NOTE)
    clean.commit()
    with clean.cursor() as cur:
        cur.execute("SELECT meeting_date FROM granola_notes WHERE granola_id = %s", (NOTE,))
        assert str(cur.fetchone()[0]) == "2026-09-01"


def _started_ended(conn: psycopg2.extensions.connection) -> tuple[Any, Any]:
    with conn.cursor() as cur:
        cur.execute("SELECT started_at, ended_at FROM granola_notes WHERE granola_id = %s", (NOTE,))
        return cur.fetchone()


@pytest.mark.live
def test_started_at_is_the_scheduled_time(clean: psycopg2.extensions.connection) -> None:
    _store(clean, _raw(created_at="2026-09-20T09:00:00Z"))
    parse_note(clean, NOTE)
    clean.commit()
    started, ended = _started_ended(clean)
    assert started.isoformat() == "2026-09-01T12:00:00+00:00"
    assert ended.isoformat() == "2026-09-01T12:45:00+00:00"


@pytest.mark.live
def test_started_at_falls_back_to_created_at_without_a_calendar_event(
    clean: psycopg2.extensions.connection,
) -> None:
    """An ad hoc meeting has no calendar event. A NULL start drops it out of
    every date-ranged query -- the monthly report missed 22 meetings that way."""
    _store(clean, _raw(calendar_event=None))
    parse_note(clean, NOTE)
    clean.commit()
    started, ended = _started_ended(clean)
    assert started.isoformat() == "2026-09-01T09:00:00+00:00"
    assert ended is None


@pytest.mark.live
def test_started_at_and_meeting_date_agree_without_a_calendar_event(
    clean: psycopg2.extensions.connection,
) -> None:
    """Both come from note_timestamp, so they cannot name different days."""
    _store(clean, _raw(calendar_event={}, created_at="2026-09-03T23:30:00Z"))
    parse_note(clean, NOTE)
    clean.commit()
    with clean.cursor() as cur:
        cur.execute(
            "SELECT started_at AT TIME ZONE 'UTC', meeting_date FROM granola_notes "
            "WHERE granola_id = %s",
            (NOTE,),
        )
        started, day = cur.fetchone()
    assert started.date() == day


@pytest.mark.live
def test_an_attendee_appearing_twice_keeps_the_named_version(
    clean: psycopg2.extensions.connection,
) -> None:
    """The same person is in `attendees` with a name and in `invitees` with only
    an email. The primary key would reject the duplicate silently, keeping
    whichever arrived first."""
    _store(clean, _raw())
    parse_note(clean, NOTE)
    clean.commit()
    with clean.cursor() as cur:
        cur.execute("SELECT email, name FROM granola_attendees WHERE granola_id = %s", (NOTE,))
        assert cur.fetchone() == ("adam@valideval.com", "Adam Rentschler")


@pytest.mark.live
def test_an_attendee_without_an_email_is_skipped(clean: psycopg2.extensions.connection) -> None:
    """No email means no stable identity, and this table feeds relationship
    tracking that needs one."""
    _store(clean, _raw(attendees=[{"name": "Nameless"}], calendar_event={}))
    parse_note(clean, NOTE)
    clean.commit()
    assert _count(clean, "granola_attendees") == 0


@pytest.mark.live
def test_empty_transcript_segments_are_dropped_and_seq_stays_dense(
    clean: psycopg2.extensions.connection,
) -> None:
    _store(
        clean,
        _raw(
            transcript=[
                {"speaker": {"name": "A"}, "text": "one"},
                {"speaker": {"name": "B"}, "text": "   "},
                {"speaker": {"name": "C"}, "text": "two"},
            ]
        ),
    )
    parse_note(clean, NOTE)
    clean.commit()
    with clean.cursor() as cur:
        cur.execute(
            "SELECT seq, text FROM granola_transcript_segments WHERE granola_id = %s ORDER BY seq",
            (NOTE,),
        )
        assert cur.fetchall() == [(0, "one"), (1, "two")]


@pytest.mark.live
def test_reparsing_replaces_rather_than_accumulates(
    clean: psycopg2.extensions.connection,
) -> None:
    """The property the whole raw/parsed split exists for.

    An upsert keyed on (granola_id, seq) would keep the tail of a transcript
    that got shorter -- the note would carry segments it no longer has, and
    nothing would notice.
    """
    _store(clean, _raw())
    parse_note(clean, NOTE)
    clean.commit()
    assert _count(clean, "granola_transcript_segments") == 2

    _store(clean, _raw(transcript=[{"speaker": {"name": "Devon"}, "text": "only one now"}]))
    parse_note(clean, NOTE)
    clean.commit()

    assert _count(clean, "granola_transcript_segments") == 1
    assert _count(clean, "granola_notes") == 1, "the note row is replaced, not duplicated"


@pytest.mark.live
def test_a_tombstoned_note_loses_its_derived_rows_but_keeps_the_raw(
    clean: psycopg2.extensions.connection,
) -> None:
    """Nothing should surface a note the user excluded, but the raw row stays:
    the export worker needs it to know which files to remove."""
    _store(clean, _raw())
    parse_note(clean, NOTE)
    clean.commit()
    assert _count(clean, "granola_notes") == 1

    _store(clean, _raw(), deleted=True)
    assert parse_note(clean, NOTE) == "tombstoned"
    clean.commit()

    assert _count(clean, "granola_notes") == 0
    assert _count(clean, "granola_transcript_segments") == 0
    with clean.cursor() as cur:
        cur.execute("SELECT count(*) FROM granola_notes_raw WHERE granola_id = %s", (NOTE,))
        assert cur.fetchone()[0] == 1, "the raw row must survive the tombstone"


@pytest.mark.live
def test_a_missing_raw_row_raises_rather_than_writing_nothing_quietly(
    clean: psycopg2.extensions.connection,
) -> None:
    """Enqueue happens in the same transaction as the raw upsert, so a job can
    only exist if its row did. A missing row is a real anomaly, not a retry."""
    with pytest.raises(RawNoteMissingError):
        parse_note(clean, "not_does_not_exist")


@pytest.mark.live
def test_parse_does_not_commit(clean: psycopg2.extensions.connection) -> None:
    """The derived rows and the queue completion must land together."""
    _store(clean, _raw())
    parse_note(clean, NOTE)
    clean.rollback()
    assert _count(clean, "granola_notes") == 0


@pytest.mark.live
def test_a_note_with_no_transcript_is_marked_as_such(
    clean: psycopg2.extensions.connection,
) -> None:
    _store(clean, _raw(transcript=[]))
    parse_note(clean, NOTE)
    clean.commit()
    with clean.cursor() as cur:
        cur.execute("SELECT has_transcript FROM granola_notes WHERE granola_id = %s", (NOTE,))
        assert cur.fetchone()[0] is False


@pytest.mark.live
def test_a_note_with_no_usable_timestamp_gets_a_null_meeting_date(
    clean: psycopg2.extensions.connection,
) -> None:
    """render.py's note_date() falls back to now() so it always has a filename
    to build. That fallback must not reach meeting_date: a re-parse on a
    different day would silently refile the note on disk, and the value would be
    a client clock landing in a DATE column.
    """
    raw = _raw(calendar_event={}, created_at=None)
    _store(clean, {**raw, "updated_at": "2026-09-01T10:00:00Z"})
    parse_note(clean, NOTE)
    clean.commit()

    with clean.cursor() as cur:
        cur.execute("SELECT meeting_date FROM granola_notes WHERE granola_id = %s", (NOTE,))
        assert cur.fetchone()[0] is None, "unknown is true and stable; today is neither"


@pytest.mark.live
def test_reparsing_does_not_clobber_a_folder_parent(
    clean: psycopg2.extensions.connection,
) -> None:
    """parse.py is the only writer of granola_folders, and its ON CONFLICT used
    to reset parent_id to NULL on every re-parse -- so a hierarchy written by
    anything else would be destroyed rather than merely absent.
    """
    _store(clean, _raw())
    parse_note(clean, NOTE)
    clean.commit()

    with clean.cursor() as cur:
        cur.execute(
            "INSERT INTO granola_folders (folder_id, name) VALUES ('fol_parent', 'Parent') "
            "ON CONFLICT DO NOTHING"
        )
        cur.execute("UPDATE granola_folders SET parent_id = 'fol_parent' WHERE folder_id = 'fol_1'")
    clean.commit()

    parse_note(clean, NOTE)
    clean.commit()

    with clean.cursor() as cur:
        cur.execute("SELECT parent_id FROM granola_folders WHERE folder_id = 'fol_1'")
        assert cur.fetchone()[0] == "fol_parent"
