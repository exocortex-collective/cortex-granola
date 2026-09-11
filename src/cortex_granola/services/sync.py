"""granola-sync: the polling and backfill adapters over ingest_one().

This service owns only WHICH notes to ingest and WHEN. It holds no parsing, no
rendering, and no knowledge of what happens after a job is enqueued -- see
cortex_granola.ingest for why that boundary is where it is.

Two adapters live here; a third (Pub/Sub pull, phase 2) will sit beside them
without touching either:

  poll()      Incremental. Lists notes updated since the watermark minus an
              overlap window, ingests each. The primary path.
  backfill()  Bounded historical scan driven by granola_backfill_jobs, the same
              shape postmark uses for Gmail.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import psycopg2
import structlog
from cortex_utils.metrics import start_metrics_server
from cortex_utils.queue import ensure_queue_schema

from cortex_granola.config import GranolaSyncConfig, apply_schema
from cortex_granola.granola.api import GranolaClient
from cortex_granola.granola.folders import collect_denied_note_ids, resolve_deny_list
from cortex_granola.ingest import Outcome, ingest_one, tombstone

log = structlog.get_logger(__name__)


@dataclass
class PollStats:
    seen: int = 0
    stored: int = 0
    updated: int = 0
    unchanged: int = 0
    tombstoned: int = 0
    errors: int = 0
    first_error: str | None = None
    failed_ids: list[str] = field(default_factory=list)

    @property
    def changed(self) -> int:
        return self.stored + self.updated

    @property
    def clean(self) -> bool:
        """A run with no errors. The watermark moves only on this."""
        return self.errors == 0


def _rfc3339(when: datetime) -> str:
    """Format an instant the way the Granola API accepts it.

    NOT datetime.isoformat(). That produces microseconds and a "+00:00" offset:

        2026-09-04T19:33:35.250175+00:00

    which the API rejects with VALIDATION_ERROR / "Invalid date". Every
    incremental poll failed on it while the first full scan succeeded, because
    a full scan passes updated_after=None and never formats anything -- so the
    backfill looked like proof the poller worked.

    Nothing in the test suite could have caught it either: the fake client
    accepts any string. Only a real API rejects this one.
    """
    if when.tzinfo is None:
        # astimezone() on a naive datetime silently assumes LOCAL time, which
        # would shift the window by the machine's offset and skip notes. It is
        # tz-aware today because last_synced_at is a TIMESTAMPTZ, but that is
        # an invariant somewhere else, so say so here rather than inherit it.
        raise ValueError(f"refusing to format a naive datetime as UTC: {when!r}")
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class GranolaSync:
    def __init__(self, config: GranolaSyncConfig, client: Any | None = None) -> None:
        """`client` is injectable so tests do not have to reach past the
        constructor and assign a private attribute -- which they did, and which
        breaks silently on a rename."""
        self.config = config
        self.client = client if client is not None else GranolaClient(config.api_key)

    # -- connections -------------------------------------------------------

    @contextmanager
    def _conn(self) -> Iterator[psycopg2.extensions.connection]:
        conn = psycopg2.connect(self.config.dsn)
        try:
            yield conn
        finally:
            conn.close()

    def ensure_schema(self) -> None:
        """Create the queue schema and this service's tables.

        ensure_queue_schema() is the one entry point cortex_utils.queue offers
        for boot: it sequences the DDL, the migrations, the dead-letter table
        and the first partitions. Calling the individual pieces is what it
        exists to prevent.
        """
        with self._conn() as conn:
            ensure_queue_schema(conn)
            apply_schema(conn)
            self._seed_sync_state(conn)
            conn.commit()

    # -- watermark ---------------------------------------------------------

    def _seed_sync_state(self, conn: psycopg2.extensions.connection) -> None:
        """Create this source's row, seeding overlap_minutes from config.

        ON CONFLICT DO NOTHING, deliberately: the DB column is the single source
        of truth once the row exists, and config seeds it on first insert only.
        Re-seeding on every boot would make a per-source override last until the
        next restart, which is the worst of both -- it appears to work.
        """
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO granola_sync_state (source, overlap_minutes) VALUES (%s, %s) "
                "ON CONFLICT (source) DO NOTHING",
                (self.config.source, self.config.overlap_minutes),
            )

    def _server_now(self, conn: psycopg2.extensions.connection) -> datetime:
        """The server's clock, which is the only one the watermark may use."""
        with conn.cursor() as cur:
            cur.execute("SELECT NOW()")
            row = cur.fetchone()
        if row is None:
            # assert would be the obvious thing and is wrong: python -O strips
            # it, and the next line then raises TypeError on a None subscript
            # rather than saying what happened.
            raise RuntimeError("SELECT NOW() returned no row")
        now: datetime = row[0]
        return now

    def _window_start(self, conn: psycopg2.extensions.connection) -> str | None:
        """The `updated_after` bound for this poll, or None for a full first scan.

        The overlap is subtracted HERE, at read time, and never written back.
        Storing (now - overlap) compounds it on every run: a 60-minute overlap
        applied 24 times has walked the watermark a day backwards, and the scan
        grows without bound.
        """
        with conn.cursor() as cur:
            cur.execute(
                "SELECT last_synced_at, overlap_minutes FROM granola_sync_state WHERE source = %s",
                (self.config.source,),
            )
            row = cur.fetchone()
        if row is None or row[0] is None:
            return None
        last_synced_at, overlap_minutes = row
        window_start: datetime = last_synced_at - timedelta(minutes=overlap_minutes)
        return _rfc3339(window_start)

    def _record_poll(
        self, conn: psycopg2.extensions.connection, scan_started: datetime, stats: PollStats
    ) -> None:
        """Record the poll. The watermark advances only on a clean run.

        A run that hit errors still records last_poll_at and last_error, so the
        failure is visible -- but leaves last_synced_at where it was, because
        the notes that errored are inside the old window and outside a newer
        one. Advancing past them loses them silently: no error, no retry, and no
        row to notice missing.

        It advances to SCAN_STARTED, not to now(). A note edited during the pass
        falls between those two instants, and advancing to now() would step over
        it.
        """
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE granola_sync_state
                   SET last_synced_at = CASE WHEN %(clean)s THEN %(scan_started)s
                                             ELSE last_synced_at END,
                       last_poll_at = NOW(),
                       last_error = %(error)s
                 WHERE source = %(source)s
                """,
                {
                    "clean": stats.clean,
                    "scan_started": scan_started,
                    "error": stats.first_error,
                    "source": self.config.source,
                },
            )

    # -- adapters ----------------------------------------------------------

    def _sync_folders(self, conn: psycopg2.extensions.connection) -> set[str]:
        """Persist the folder tree from /v1/folders, and return the denied notes.

        /v1/folders is the only authoritative source for the hierarchy and for
        folders nobody has filed a note in yet. Letting parse.py be the sole
        writer meant `granola_folders` held only folders reachable from an
        INGESTED note -- measured here as 13 of 17, missing the three the deny
        list excludes and one that is merely empty. That made the `excluded`
        column dead by construction: a denied folder never got a row to flag.

        Folders are fetched once and used for both jobs, so persisting the tree
        costs no extra API call.
        """
        folders = self.client.list_folders()
        resolution = resolve_deny_list(folders, self.config.deny_folders, self.config.deny_spaces)
        # resolve_deny_list already logs unmatched terms; this is the operator
        # summary of what the deny list actually resolved to.
        log.info(
            "deny list resolved",
            folders=len(resolution.folder_ids),
            matched_folders=list(resolution.matched_folder_names),
            matched_spaces=list(resolution.matched_space_names),
            unmatched=list(resolution.unmatched),
        )
        self._store_folders(conn, folders, resolution.folder_ids)

        if not (self.config.deny_folders or self.config.deny_spaces):
            return set()
        return collect_denied_note_ids(self.client, resolution.folder_ids)

    @staticmethod
    def _store_folders(
        conn: psycopg2.extensions.connection,
        folders: list[dict[str, Any]],
        excluded_ids: frozenset[str],
    ) -> None:
        """Upsert every folder the API reports, hierarchy included.

        Ordered parents-first so the self-referencing parent_id FK is satisfied
        on a first run. A parent the API did not return would still violate it,
        so an unresolvable parent is stored as NULL rather than failing the
        whole poll over one folder's missing ancestor.
        """
        known = {f.get("id") for f in folders if f.get("id")}
        with conn.cursor() as cur:
            for folder in _parents_first(folders):
                folder_id = folder.get("id")
                if not folder_id:
                    continue
                parent_id = folder.get("parent_folder_id")
                if parent_id not in known:
                    parent_id = None
                cur.execute(
                    """
                    INSERT INTO granola_folders
                        (folder_id, name, parent_id, space_id, excluded)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (folder_id) DO UPDATE
                       SET name = EXCLUDED.name,
                           parent_id = EXCLUDED.parent_id,
                           space_id = EXCLUDED.space_id,
                           excluded = EXCLUDED.excluded,
                           updated_at = NOW()
                    """,
                    (
                        folder_id,
                        folder.get("name") or "",
                        parent_id,
                        folder.get("space_id"),
                        folder_id in excluded_ids,
                    ),
                )
        # space_name is deliberately not written here: /v1/folders carries a
        # space_id but no space name. parse.py fills it from a note's
        # space_membership, and overwriting it with NULL on every poll would
        # undo that.

    def poll(self) -> PollStats:
        """One incremental pass. Safe to run on any cadence, including overlapping."""
        stats = PollStats()

        with self._conn() as conn:
            # Captured before listing, for the watermark (see _record_poll), and
            # asked of the SERVER rather than computed here. last_synced_at is a
            # Postgres TIMESTAMPTZ compared against values Postgres wrote; a
            # datetime.now(UTC) would be a second clock deciding where the
            # watermark lands, agreeing with the first only by coincidence and
            # only where both machines are UTC. That coincidence holds on this
            # laptop and would not survive a move.
            scan_started = self._server_now(conn)
            since = self._window_start(conn)
            denied = self._sync_folders(conn)
            log.info("granola poll starting", since=since, denied=len(denied))

            for summary in self.client.list_notes(updated_after=since):
                stats.seen += 1
                note_id = summary.get("id")
                if not note_id:
                    # Counted as an error, not skipped. A bare `continue` here
                    # left stats.clean True, so the watermark advanced past a
                    # note that was never ingested -- no error, no retry, and no
                    # row to notice missing. Holding the watermark costs a
                    # re-scan; advancing costs the note.
                    stats.errors += 1
                    if stats.first_error is None:
                        stats.first_error = f"note with no id in listing: {summary!r}"
                    log.error("granola listing returned a note with no id", summary=summary)
                    continue
                self._ingest_one_note(conn, note_id, summary, denied, stats)

            self._record_poll(conn, scan_started, stats)
            conn.commit()

        log.info(
            "granola poll finished",
            seen=stats.seen,
            changed=stats.changed,
            unchanged=stats.unchanged,
            tombstoned=stats.tombstoned,
            errors=stats.errors,
            failed=stats.failed_ids,
            watermark_advanced=stats.clean,
        )
        return stats

    def _ingest_one_note(
        self,
        conn: psycopg2.extensions.connection,
        note_id: str,
        summary: Mapping[str, Any],
        denied: set[str],
        stats: PollStats,
        priority: int = 0,
    ) -> None:
        """Ingest one note in its own transaction, recording the outcome.

        One bad note must not end the poll -- but it must not be forgotten
        either, which is why the failure is counted and named. stats.clean is
        what holds the watermark back.
        """
        try:
            if note_id in denied:
                if tombstone(conn, note_id).outcome is Outcome.DELETED:
                    stats.tombstoned += 1
                conn.commit()
                return

            result = ingest_one(
                conn,
                self.client,
                note_id,
                api_updated_at=summary.get("updated_at"),
                priority=priority,
            )
            conn.commit()
        except Exception as exc:
            conn.rollback()
            stats.errors += 1
            stats.failed_ids.append(note_id)
            if stats.first_error is None:
                stats.first_error = f"{note_id}: {exc}"
            log.error("granola ingest failed", note_id=note_id, error=str(exc), exc_info=True)
            return

        if result.outcome is Outcome.STORED:
            stats.stored += 1
        elif result.outcome is Outcome.UPDATED:
            stats.updated += 1
        else:
            stats.unchanged += 1

    def run_once(self) -> PollStats | None:
        """One supervised pass: drain the backfill queue, then poll.

        Extracted from run() so the crash-recovery branch is reachable from a
        test -- run()'s `while True` is not. Returns None when the pass crashed.

        A crash must not end the service, and it must not advance anything: the
        watermark is only written by a completed poll, so the next pass re-scans
        the same window.
        """
        try:
            while self.check_backfill_queue():
                pass
            return self.poll()
        except Exception:
            log.error("granola poll crashed", exc_info=True)
            return None

    def run(self) -> None:
        """Poll on an interval until the process is stopped."""
        self.ensure_schema()
        log.info("granola-sync started", interval_s=self.config.poll_interval_s)
        while True:
            self.run_once()
            time.sleep(self.config.poll_interval_s)

    # -- backfill ----------------------------------------------------------

    def check_backfill_queue(self) -> bool:
        """Claim and run one pending backfill job. True if one ran.

        Mirrors postmark's shape: a table of jobs rather than a flag, so a
        historical scan is requestable from SQL, resumable, cancellable, and
        visible while it runs.
        """
        with self._conn() as conn:
            job = self._claim_backfill_job(conn)
            if job is None:
                return False
            job_id, after, before = job

        try:
            stats = self.backfill(job_id, after, before)
        except Exception as exc:
            log.error("granola backfill failed", job_id=job_id, error=str(exc), exc_info=True)
            self._finish_backfill(job_id, "failed", error=str(exc))
            return True

        self._finish_backfill(job_id, "completed", stats=stats)
        return True

    def _claim_backfill_job(
        self, conn: psycopg2.extensions.connection
    ) -> tuple[str, date | None, date | None] | None:
        """Claim one pending job, oldest first.

        FOR UPDATE SKIP LOCKED so a second granola-sync process takes a
        different job rather than blocking on this one -- the same claim shape
        cortex_utils.queue uses, for the same reason.
        """
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH claimable AS (
                    SELECT id FROM granola_backfill_jobs
                     WHERE status = 'pending'
                     ORDER BY created_at
                     LIMIT 1
                     FOR UPDATE SKIP LOCKED
                )
                UPDATE granola_backfill_jobs j
                   SET status = 'running', started_at = NOW()
                  FROM claimable c
                 WHERE j.id = c.id
             RETURNING j.id, j.after_date, j.before_date
                """
            )
            row = cur.fetchone()
            conn.commit()
        return (row[0], row[1], row[2]) if row else None

    def backfill(self, job_id: str, after: date | None, before: date | None) -> PollStats:
        """Scan a bounded date range, ingesting every note in it.

        Carries no watermark: the range is explicit, so a failure here does not
        move anything the incremental poll depends on. Progress is written as it
        goes, so a long run is observable and a crash leaves a record of how far
        it got.

        Priority is negative so backfill parse jobs queue behind live ones --
        a historical scan must not delay today's meeting notes.
        """
        stats = PollStats()
        log.info("granola backfill starting", job_id=job_id, after=after, before=before)

        with self._conn() as conn:
            denied = self._sync_folders(conn)
            listing = self.client.list_notes(
                created_after=after.isoformat() if after else None,
                created_before=before.isoformat() if before else None,
            )
            for summary in listing:
                stats.seen += 1
                note_id = summary.get("id")
                if not note_id:
                    stats.errors += 1
                    log.error("granola backfill: note with no id", job_id=job_id, summary=summary)
                    continue
                if self._backfill_cancelled(conn, job_id):
                    log.info("granola backfill cancelled", job_id=job_id, seen=stats.seen)
                    break
                self._ingest_one_note(conn, note_id, summary, denied, stats, priority=-1)
                if stats.seen % 25 == 0:
                    self._update_backfill_progress(conn, job_id, stats)

        log.info(
            "granola backfill finished",
            job_id=job_id,
            seen=stats.seen,
            changed=stats.changed,
            errors=stats.errors,
        )
        return stats

    def _backfill_cancelled(self, conn: psycopg2.extensions.connection, job_id: str) -> bool:
        """Has an operator cancelled this job mid-scan?

        Reuses the scan's connection. The first version opened a fresh one per
        note, on the theory that the scan's transaction could not see a cancel
        committed after it began -- but _ingest_one_note commits or rolls back
        after every note, so under READ COMMITTED this connection starts a new
        transaction for each check and sees a concurrent cancel just as
        reliably. The fresh connection bought nothing and cost a TCP connect
        and auth round trip per note.
        """
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM granola_backfill_jobs WHERE id = %s", (job_id,))
            row = cur.fetchone()
        conn.commit()
        return row is not None and row[0] == "cancelled"

    def _update_backfill_progress(
        self, conn: psycopg2.extensions.connection, job_id: str, stats: PollStats
    ) -> None:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE granola_backfill_jobs SET processed = %s, stored = %s, updated = %s "
                "WHERE id = %s",
                (stats.seen, stats.stored, stats.updated, job_id),
            )
            conn.commit()

    def _finish_backfill(
        self, job_id: str, status: str, stats: PollStats | None = None, error: str | None = None
    ) -> None:
        """Record the terminal state, without clobbering a cancel.

        A job cancelled mid-scan must stay cancelled: writing 'completed' over
        it would report success for work the operator stopped.
        """
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE granola_backfill_jobs
                   SET status = %(status)s,
                       completed_at = NOW(),
                       processed = COALESCE(%(processed)s, processed),
                       stored = COALESCE(%(stored)s, stored),
                       updated = COALESCE(%(updated)s, updated),
                       error = %(error)s
                 WHERE id = %(id)s AND status = 'running'
                """,
                {
                    "status": status,
                    "processed": stats.seen if stats else None,
                    "stored": stats.stored if stats else None,
                    "updated": stats.updated if stats else None,
                    "error": error,
                    "id": job_id,
                },
            )
            conn.commit()


def main() -> None:
    start_metrics_server(port=int(os.environ.get("METRICS_PORT", "8000")))
    GranolaSync(GranolaSyncConfig.from_env()).run()


if __name__ == "__main__":
    main()


def _parents_first(folders: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Folders ordered so a parent is always inserted before its children.

    granola_folders.parent_id references the same table, so a child inserted
    first violates the FK on a fresh database. Granola's folders are one level
    deep today, which is exactly why this is easy to get wrong and not notice.

    A cycle -- which the API should never produce -- would otherwise spin here,
    so anything still unplaced after a pass with no progress is appended as-is
    and lets _store_folders null its parent out.
    """
    by_id = {f["id"]: f for f in folders if f.get("id")}
    ordered: list[dict[str, Any]] = []
    placed: set[str] = set()
    pending = list(by_id.values())

    while pending:
        progressed = False
        remaining: list[dict[str, Any]] = []
        for folder in pending:
            parent = folder.get("parent_folder_id")
            if parent is None or parent in placed or parent not in by_id:
                ordered.append(folder)
                placed.add(folder["id"])
                progressed = True
            else:
                remaining.append(folder)
        if not progressed:
            ordered.extend(remaining)
            break
        pending = remaining

    return ordered
