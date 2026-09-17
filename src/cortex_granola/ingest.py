"""The single ingest boundary every arrival mechanism goes through.

Cortex learned this shape from postmark, where `store_raw_message()` is called
by exactly two things -- the Pub/Sub push handler and the backfill -- and
nothing downstream knows which one it was. This module is that function for
Granola, with one difference: postmark's polling adapter was left a stub
(`fallback_poll()` is `pass`), so push became load-bearing by default. Here
polling is the primary adapter and push is the addition.

Three adapters are planned, in this order:

    poller      services/sync.py     -- phase 1, primary
    backfill    services/sync.py     -- phase 1, bounded historical scan
    pubsub      services/pubsub.py   -- phase 2, a public shim verifies the
                                        Granola webhook signature and republishes
                                        to Pub/Sub; this host pulls, so it never
                                        accepts inbound traffic

All three call `ingest_one()`. An adapter decides only WHICH note ids to ingest
and WHEN.

This module deliberately does no logging. Every function returns an
IngestResult naming the note and what happened to it, and the adapter has the
context worth logging -- which poll, which backfill job, how far through the
window. Logging here too would double every line at a worse altitude.

Nothing below this line varies by arrival, which is what keeps the second and
third adapters cheap to add.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import psycopg2
from cortex_utils.queue import enqueue
from psycopg2.extras import Json

from cortex_granola.granola.api import GranolaClient, TranscriptTooLarge

PARSE_QUEUE = "granola_parse"


class Outcome(StrEnum):
    STORED = "stored"  # first time we have seen this note
    UPDATED = "updated"  # api_updated_at moved
    UNCHANGED = "unchanged"  # already current; no work enqueued
    DELETED = "deleted"  # tombstoned; export must remove its files


@dataclass(frozen=True)
class IngestResult:
    """What one ingest did, and the job it enqueued if it enqueued one.

    UNCHANGED never carries a job_id: nothing was written, so nothing was
    enqueued. IngestResult(id, UNCHANGED, job_id=7) was previously
    constructible and meaningless.

    The converse is deliberately NOT asserted -- see __post_init__.
    """

    granola_id: str
    outcome: Outcome
    job_id: int | None = None

    def __post_init__(self) -> None:
        # Only one direction is an invariant. The other looks like one and is
        # not: enqueue(dedup_key=...) returns None when an identical job is
        # already pending, which is success -- "the work is already covered".
        # An UPDATED note whose parse job is still queued from the previous
        # change lands here legitimately with job_id None, so asserting
        # "non-UNCHANGED implies a job_id" would raise on a correct path.
        if self.outcome is Outcome.UNCHANGED and self.job_id is not None:
            raise ValueError(f"UNCHANGED must not carry a job_id: {self.granola_id}")


def ingest_one(
    conn: psycopg2.extensions.connection,
    client: GranolaClient,
    granola_id: str,
    *,
    api_updated_at: str | None = None,
    priority: int = 0,
) -> IngestResult:
    """Fetch a note, store the raw response, enqueue parsing. Idempotent.

    `api_updated_at`, when the caller already has it from a list response, skips
    the fetch for a note we are already current on. That is the difference
    between a poll costing one request and costing one request per note in the
    overlap window -- and the overlap window is re-scanned on every run by
    design, so most notes in it are unchanged.

    Does not commit. The caller owns the transaction so that the raw row and the
    parse job land together or not at all; a committed row with no job is a note
    that never gets parsed and that nothing will retry, because the next poll
    sees it as unchanged.
    """
    if api_updated_at is not None and _is_current(conn, granola_id, api_updated_at):
        return IngestResult(granola_id, Outcome.UNCHANGED)

    note = _fetch_with_transcript(client, granola_id)
    updated_at = note.get("updated_at")
    if not updated_at:
        # Every note the API returns carries updated_at; its absence means the
        # response shape changed under us. Failing here is better than storing a
        # row the watermark can never advance past.
        raise ValueError(f"note {granola_id} has no updated_at")

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO granola_notes_raw (granola_id, raw, api_updated_at)
            VALUES (%s, %s, %s)
            ON CONFLICT (granola_id) DO UPDATE
               SET raw = EXCLUDED.raw,
                   api_updated_at = EXCLUDED.api_updated_at,
                   fetched_at = NOW(),
                   -- A note that comes back after a tombstone is live again:
                   -- access was regranted, or it was moved out of an excluded
                   -- folder. Leaving deleted_at set would keep the export
                   -- deleting files the note now wants.
                   deleted_at = NULL
             WHERE granola_notes_raw.api_updated_at IS DISTINCT FROM EXCLUDED.api_updated_at
                OR granola_notes_raw.deleted_at IS NOT NULL
            RETURNING (xmax = 0) AS inserted
            """,
            (granola_id, Json(note), updated_at),
        )
        row = cur.fetchone()

    if row is None:
        # The WHERE on DO UPDATE suppressed the write: same api_updated_at, not
        # tombstoned. Nothing to parse.
        return IngestResult(granola_id, Outcome.UNCHANGED)

    outcome = Outcome.STORED if row[0] else Outcome.UPDATED
    job_id = enqueue(
        conn,
        PARSE_QUEUE,
        {"granola_id": granola_id},
        priority=priority,
        dedup_key="granola_id",
        commit=False,
    )
    return IngestResult(granola_id, outcome, job_id)


def tombstone(
    conn: psycopg2.extensions.connection, granola_id: str, *, priority: int = 0
) -> IngestResult:
    """Mark a note gone and enqueue parsing so the export removes its files.

    Reached from /v1/audit (deleted, access revoked) and from the poller when a
    note lands in an excluded folder. The row is kept: the export worker cannot
    delete files for a note that has vanished from the database, because it has
    nothing left to tell it which files those were.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE granola_notes_raw SET deleted_at = NOW() "
            "WHERE granola_id = %s AND deleted_at IS NULL",
            (granola_id,),
        )
        if cur.rowcount == 0:
            return IngestResult(granola_id, Outcome.UNCHANGED)

    job_id = enqueue(
        conn,
        PARSE_QUEUE,
        {"granola_id": granola_id, "deleted": True},
        priority=priority,
        dedup_key="granola_id",
        commit=False,
    )
    return IngestResult(granola_id, Outcome.DELETED, job_id)


def _fetch_with_transcript(client: GranolaClient, granola_id: str) -> dict[str, object]:
    """The note with its transcript, by whichever route the API allows.

    The inline form has a size limit, and the API answers 413 above it. Nothing
    caught that: TranscriptTooLarge escaped ingest_one, sync counted the run
    unclean, the watermark stayed put, and the next poll re-issued the identical
    request against the identical window -- every fifteen minutes, forever,
    with the container reporting Up. The paged endpoint the docs say to use
    instead had zero call sites. One long meeting was all it would have taken;
    the M1 soak passed because no note had crossed the limit yet.

    The paged segments are spliced in under `transcript`, so the stored raw row
    has the same shape either way and parse.py does not know which route it
    came by.
    """
    try:
        return client.get_note(granola_id, include_transcript=True)
    except TranscriptTooLarge:
        note = client.get_note(granola_id, include_transcript=False)
        note["transcript"] = client.get_transcript(granola_id)
        return note


def _is_current(conn: psycopg2.extensions.connection, granola_id: str, api_updated_at: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM granola_notes_raw "
            "WHERE granola_id = %s AND api_updated_at = %s AND deleted_at IS NULL",
            (granola_id, api_updated_at),
        )
        return cur.fetchone() is not None
