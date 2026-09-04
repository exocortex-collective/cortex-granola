"""granola-export-worker: keeps the vault in line with the database.

Pulls its work from a query rather than a queue. A note needs exporting when
its raw row is newer than what was written for it, or when it is tombstoned and
files remain -- both of which the database can answer directly. That makes the
worker self-healing: delete the vault and it rebuilds, change the renderer and
a re-export follows from the timestamps, with no queue to drain or replay.

The export itself lives in cortex_granola.export, which is where the rules
about deleting files are written down and tested.
"""

from __future__ import annotations

import os
import signal
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from types import FrameType

import psycopg2
import structlog
from cortex_utils.metrics import start_metrics_server

from cortex_granola.config import ExportConfig, apply_schema, dsn_from_env
from cortex_granola.export import ExportPathError, ExportStats, export_note

log = structlog.get_logger(__name__)

DEFAULT_BATCH = 50
DEFAULT_POLL_INTERVAL_S = 30

# Tombstoned notes first: removing a note the user excluded is more urgent than
# writing one they added, and a large backfill must not delay it.
#
# This leans on one assumption worth naming, because it is the kind that fails
# silently: fetched_at must advance whenever export-relevant content changes.
# ingest_one() bumps it only when api_updated_at moves or a tombstone clears,
# so if Granola ever returns changed content under an unchanged updated_at, the
# export for that note never fires and nothing reports it -- the same shape the
# watermark charter warns about. Tracked as Cortex-b4o.14.
_PENDING = """
    SELECT r.granola_id
      FROM granola_notes_raw r
      LEFT JOIN granola_exported_files f ON f.granola_id = r.granola_id
     WHERE (r.deleted_at IS NOT NULL AND f.granola_id IS NOT NULL)
        OR (r.deleted_at IS NULL
            AND EXISTS (SELECT 1 FROM granola_notes n WHERE n.granola_id = r.granola_id)
            AND (f.granola_id IS NULL OR f.written_at < r.fetched_at))
     GROUP BY r.granola_id, r.deleted_at, r.fetched_at
     ORDER BY (r.deleted_at IS NOT NULL) DESC, r.fetched_at
     LIMIT %s
"""


class ExportWorker:
    def __init__(
        self,
        dsn: str,
        config: ExportConfig,
        batch_size: int = DEFAULT_BATCH,
        poll_interval_s: int = DEFAULT_POLL_INTERVAL_S,
    ) -> None:
        self.dsn = dsn
        self.config = config
        self.batch_size = batch_size
        self.poll_interval_s = poll_interval_s
        self._stop = threading.Event()

    def request_stop(self, signum: int, _frame: FrameType | None = None) -> None:
        log.info("granola-export-worker stopping", signal=signum)
        self._stop.set()

    @contextmanager
    def _conn(self) -> Iterator[psycopg2.extensions.connection]:
        conn = psycopg2.connect(self.dsn)
        try:
            yield conn
        finally:
            conn.close()

    def ensure_schema(self) -> None:
        with self._conn() as conn:
            apply_schema(conn)

    def pending(self, conn: psycopg2.extensions.connection) -> list[str]:
        with conn.cursor() as cur:
            cur.execute(_PENDING, (self.batch_size,))
            return [row[0] for row in cur.fetchall()]

    def run_once(self) -> ExportStats:
        stats = ExportStats()
        with self._conn() as conn:
            for granola_id in self.pending(conn):
                if self._stop.is_set():
                    break
                self._export(conn, granola_id, stats)
        return stats

    def _export(
        self, conn: psycopg2.extensions.connection, granola_id: str, stats: ExportStats
    ) -> None:
        """One note per transaction.

        The files and their record commit together: a record without its file
        makes the worker think it has cleanup to do on a path that is not
        there, and a file without its record is one it will never clean up.
        """
        try:
            outcome = export_note(conn, self.config, granola_id)
            conn.commit()
        except ExportPathError:
            # Deliberately NOT caught with the rest. This means something in
            # the vault tried to send a write or a delete somewhere this worker
            # had no business going -- a symlink, or a title that escaped the
            # slug. Counting it as a routine error would retry it every poll
            # forever, at the same severity as a flaky disk, and the log line
            # saying so would scroll past.
            conn.rollback()
            raise
        except Exception as exc:
            conn.rollback()
            stats.errors += 1
            log.error("granola export failed", granola_id=granola_id, error=str(exc), exc_info=True)
            return

        if outcome == "written":
            stats.written += 1
        elif outcome == "removed":
            stats.removed += 1
        else:
            stats.unchanged += 1

    def run(self) -> None:
        self.ensure_schema()
        signal.signal(signal.SIGTERM, self.request_stop)
        signal.signal(signal.SIGINT, self.request_stop)
        log.info(
            "granola-export-worker started",
            notes=str(self.config.notes_dir),
            transcripts=str(self.config.transcripts_dir),
        )
        while not self._stop.is_set():
            try:
                stats = self.run_once()
            except ExportPathError:
                # Stop, rather than crash-loop or retry. Restarting would hit
                # the same path next poll, and a container flapping on
                # restart: unless-stopped hides the reason. Stopping cleanly
                # leaves the process up, metrics still served, and one CRITICAL
                # line saying exactly which path it refused.
                log.critical(
                    "granola export refused a path and stopped; "
                    "the vault holds something this worker will not touch",
                    exc_info=True,
                )
                self._stop.set()
                break
            except Exception:
                log.error("granola export worker crashed", exc_info=True)
                self._stop.wait(self.poll_interval_s)
                continue

            if stats.written or stats.removed or stats.errors:
                log.info(
                    "granola export batch",
                    written=stats.written,
                    removed=stats.removed,
                    errors=stats.errors,
                )
            else:
                self._stop.wait(self.poll_interval_s)

        log.info("granola-export-worker stopped")


def main() -> None:
    start_metrics_server(port=int(os.environ.get("METRICS_PORT", "8000")))
    ExportWorker(dsn_from_env(), ExportConfig.from_env()).run()


if __name__ == "__main__":
    main()
