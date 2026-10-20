"""Turning a stored raw note into the tables everything else reads.

The raw table is the record; every table here is derived and can be rebuilt
from it by re-enqueuing. That is the whole reason for the raw/parsed split:
improving this parser is a re-parse, not a re-fetch, and Granola only returns
notes that still have a generated summary -- a note we could not parse today
may be unfetchable in the shape we saw it tomorrow.

Parsing is destructive-by-replacement within one note: the derived rows for a
granola_id are deleted and rewritten in a single transaction, so a note whose
transcript shrank does not keep the segments it no longer has.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import psycopg2
import structlog
from psycopg2 import sql
from psycopg2.extras import Json, execute_values

from cortex_granola.granola.render import calendar_event, note_timestamp

log = structlog.get_logger(__name__)


class RawNoteMissingError(RuntimeError):
    """The queue named a note the raw table does not have.

    Not retryable by waiting: enqueue happens inside the same transaction as
    the raw upsert, so a job can only exist if its row did. A missing row means
    the row was deleted out from under us, and re-running will not conjure it.
    """


def parse_note(conn: psycopg2.extensions.connection, granola_id: str) -> str:
    """Parse one note into the derived tables. Returns what it did.

    Does not commit -- the caller owns the transaction so the derived rows and
    the queue completion land together. Half-parsed derived tables with a
    completed job is the state nothing would ever fix, because the next poll
    sees the note as unchanged.
    """
    raw, deleted_at = _load_raw(conn, granola_id)

    if deleted_at is not None:
        # A tombstoned note keeps its raw row (the export needs to know which
        # files to remove) but loses its derived rows: nothing should surface a
        # note the user has excluded or deleted.
        _clear_derived(conn, granola_id)
        return "tombstoned"

    _clear_derived(conn, granola_id)
    _write_note(conn, granola_id, raw)
    segments = _write_transcript(conn, granola_id, raw)
    attendees = _write_attendees(conn, granola_id, raw)
    _write_folders(conn, granola_id, raw)

    log.info(
        "parsed granola note",
        granola_id=granola_id,
        segments=segments,
        attendees=attendees,
    )
    return "parsed"


def _load_raw(
    conn: psycopg2.extensions.connection, granola_id: str
) -> tuple[dict[str, Any], dt.datetime | None]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT raw, deleted_at FROM granola_notes_raw WHERE granola_id = %s",
            (granola_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise RawNoteMissingError(granola_id)
    raw: dict[str, Any] = row[0]
    return raw, row[1]


def _clear_derived(conn: psycopg2.extensions.connection, granola_id: str) -> None:
    """Delete every derived row for this note.

    Replacement rather than upsert: a re-parse after an edit must not leave
    behind transcript segments, attendees or folder links the note no longer
    has. An upsert keyed on (granola_id, seq) would silently keep the tail of a
    transcript that got shorter.
    """
    # Composed through sql.Identifier rather than an f-string. The table names
    # are a hardcoded tuple so an f-string is safe today, but "safe because of
    # what the caller happens to pass" is the shape that stops being true after
    # a refactor -- and it makes bandit's B608 a suppression instead of a
    # non-finding.
    with conn.cursor() as cur:
        for table in (
            "granola_transcript_segments",
            "granola_attendees",
            "granola_note_folders",
            "granola_notes",
        ):
            cur.execute(
                sql.SQL("DELETE FROM {} WHERE granola_id = %s").format(sql.Identifier(table)),
                (granola_id,),
            )


def _write_note(conn: psycopg2.extensions.connection, granola_id: str, raw: dict[str, Any]) -> None:
    event = calendar_event(raw)
    owner = raw.get("owner") or {}
    when_at = note_timestamp(raw)
    when = _meeting_date(raw)
    transcript = raw.get("transcript") or []

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO granola_notes (
                granola_id, title, owner_email, web_url, started_at, ended_at,
                meeting_date, created_at, api_updated_at, summary_markdown,
                summary_text, private_markdown, has_transcript
            ) VALUES (
                %(id)s, %(title)s, %(owner)s, %(web_url)s, %(start)s, %(end)s,
                %(date)s, %(created)s, %(updated)s, %(summary_md)s,
                %(summary_txt)s, %(private_md)s, %(has_transcript)s
            )
            """,
            {
                "id": granola_id,
                "title": raw.get("title"),
                "owner": (owner.get("email") or "").strip() or None,
                "web_url": raw.get("web_url"),
                # The calendar start, else created_at -- Granola stamps that
                # when recording begins, so an ad hoc meeting's start is not
                # unknown. Same precedence as meeting_date, from one function.
                "start": when_at,
                # No fallback: nothing in a calendar-less note says when it ended.
                "end": event.get("scheduled_end_time"),
                # The date the export worker files this note under, or NULL --
                # see _meeting_date.
                "date": when,
                "created": raw.get("created_at"),
                "updated": raw.get("updated_at"),
                "summary_md": raw.get("summary_markdown"),
                "summary_txt": raw.get("summary_text"),
                "private_md": raw.get("private_notes_markdown"),
                "has_transcript": bool(transcript),
            },
        )


def _meeting_date(raw: dict[str, Any]) -> dt.date | None:
    """The meeting's date, or None when the note carries no usable timestamp.

    render.py's note_date() falls back to datetime.now(UTC) so it always has
    something to build a filename from. That fallback must not reach this
    column. meeting_date is what the export worker files the note under, so a
    re-parse of a timestamp-less note on a different day would silently move it
    on disk -- and the value would be a client clock landing in a DATE column,
    which is the clock-discipline failure this repo has a charter about.

    NULL says "unknown", which is true and stable. The export worker decides
    what to do with an undated note; it is not this function's guess to make.
    """
    when = note_timestamp(raw)
    return when.date() if when else None


def _write_transcript(
    conn: psycopg2.extensions.connection, granola_id: str, raw: dict[str, Any]
) -> int:
    """Store transcript segments, skipping empty ones.

    `seq` is the position in the returned transcript, which is what orders the
    conversation -- segments carry no timestamp this code can rely on, and
    sorting by anything else would scramble it.
    """
    from cortex_granola.granola.render import _speaker_label

    rows = []
    seq = 0
    for item in raw.get("transcript") or []:
        text = (item.get("text") or "").strip()
        if not text:
            continue
        speaker = item.get("speaker") or {}
        rows.append((granola_id, seq, _speaker_label(speaker), Json(speaker), text))
        seq += 1

    if not rows:
        return 0
    with conn.cursor() as cur:
        execute_values(
            cur,
            "INSERT INTO granola_transcript_segments "
            "(granola_id, seq, speaker_label, speaker, text) VALUES %s",
            rows,
        )
    return len(rows)


def _write_attendees(
    conn: psycopg2.extensions.connection, granola_id: str, raw: dict[str, Any]
) -> int:
    """Attendees from the note and from the calendar invitees, merged.

    Keyed by lowercased email because the same person appears in both lists --
    once with a name from the attendee record, once with only an email from the
    invite. The named version wins; the table's primary key would reject the
    duplicate anyway, but silently, and with whichever arrived first.
    """
    people: dict[str, tuple[str, str | None]] = {}
    sources = list(raw.get("attendees") or [])
    sources.extend(calendar_event(raw).get("invitees") or [])

    for person in sources:
        email = (person.get("email") or "").strip()
        if not email:
            # Without an email there is no stable identity to key on, and the
            # relationship-tracking this table feeds needs one.
            continue
        name = (person.get("name") or "").strip() or None
        key = email.lower()
        if key not in people or (name and not people[key][1]):
            people[key] = (email, name)

    if not people:
        return 0
    with conn.cursor() as cur:
        execute_values(
            cur,
            "INSERT INTO granola_attendees (granola_id, email, name) VALUES %s",
            [(granola_id, email, name) for email, name in people.values()],
        )
    return len(people)


def _write_folders(
    conn: psycopg2.extensions.connection, granola_id: str, raw: dict[str, Any]
) -> None:
    """Folder membership, and the folder rows themselves.

    The folders table is upserted rather than replaced: it is shared across
    every note, so a note losing a folder must not delete that folder for the
    others.
    """
    memberships = raw.get("folder_membership") or []
    spaces = {s.get("id"): s.get("name") for s in raw.get("space_membership") or []}
    if not memberships:
        return

    with conn.cursor() as cur:
        for folder in memberships:
            folder_id = folder.get("id")
            if not folder_id:
                continue
            space_id = folder.get("space_id")
            cur.execute(
                """
                INSERT INTO granola_folders (folder_id, name, parent_id, space_id, space_name)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (folder_id) DO UPDATE
                   SET name = EXCLUDED.name,
                       space_id = EXCLUDED.space_id,
                       space_name = EXCLUDED.space_name,
                       updated_at = NOW()
                """,
                (
                    folder_id,
                    folder.get("name"),
                    # NULL, and deliberately absent from the DO UPDATE above so
                    # a re-parse cannot clobber a value someone else set.
                    #
                    # A note's folder_membership carries no parent: only
                    # /v1/folders does, and nothing writes this table from that
                    # endpoint yet. The earlier comment here claimed "the
                    # deny-list walk rebuilds the tree from the API anyway" --
                    # it does not. folders.py reads the tree into memory for
                    # deny resolution and never persists it, so the hierarchy is
                    # simply not populated. Tracked as Cortex-b4o.12.
                    None,
                    space_id,
                    spaces.get(space_id),
                ),
            )
            cur.execute(
                "INSERT INTO granola_note_folders (granola_id, folder_id) VALUES (%s, %s) "
                "ON CONFLICT DO NOTHING",
                (granola_id, folder_id),
            )
