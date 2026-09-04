"""granola-parse-worker: claims granola_parse jobs and parses them.

Owns the loop and the queue contract; the parsing itself lives in
cortex_granola.parse. The split is deliberate -- parse_note() is a pure
function of (connection, granola_id) and is tested directly, without a queue.

Claims through cortex_utils.queue, never raw SQL: claim() sets the claim token
that lets complete() and fail_or_retry() refuse a row somebody else has since
taken, and retires a row whose attempts are spent instead of cycling it
forever.
"""

from __future__ import annotations

import os
import signal
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from types import FrameType
from typing import Any

import psycopg2
import structlog
from cortex_utils.metrics import start_metrics_server
from cortex_utils.queue import (
    claim,
    complete,
    ensure_queue_schema,
    fail_or_retry,
    release,
    worker_identity,
)

from cortex_granola.config import dsn_from_env, schema_sql
from cortex_granola.ingest import PARSE_QUEUE
from cortex_granola.parse import RawNoteMissingError, parse_note

log = structlog.get_logger(__name__)

DEFAULT_BATCH = 10
DEFAULT_POLL_INTERVAL_S = 5


@dataclass
class WorkerStats:
    parsed: int = 0
    tombstoned: int = 0
    failed: int = 0
    released: int = 0


class ParseWorker:
    def __init__(
        self,
        dsn: str,
        batch_size: int = DEFAULT_BATCH,
        poll_interval_s: int = DEFAULT_POLL_INTERVAL_S,
    ) -> None:
        """Takes a DSN, not a GranolaSyncConfig.

        This worker reads raw rows out of Postgres and never talks to Granola.
        Accepting a config that carries an API key would have made the compose
        file mount the secret into a container with no use for it -- exposure
        bought by nothing but a type signature.
        """
        self.dsn = dsn
        self.batch_size = batch_size
        self.poll_interval_s = poll_interval_s
        # Stable for the life of the process: a worker that regenerated its
        # token would have every later complete() bounce.
        self.worker = worker_identity("granola-parse-worker")
        # An Event rather than a bool: _wait() returns the instant it is set,
        # so a SIGTERM during an idle interval is answered immediately instead
        # of after up to poll_interval_s. The bool version also had to be
        # polled in one-second slices, which mypy correctly read as dead code
        # -- inside `while not self._stopping` it cannot see the signal handler
        # mutating the attribute.
        self._stop = threading.Event()

    # -- lifecycle ---------------------------------------------------------

    def request_stop(self, signum: int, _frame: FrameType | None = None) -> None:
        """Finish the job in hand, then stop.

        Killing mid-job would leave the row claimed until the visibility
        timeout expires, which is a delay for no reason -- the job is
        idempotent and about to finish.
        """
        log.info("granola-parse-worker stopping", signal=signum)
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
            ensure_queue_schema(conn)
            with conn.cursor() as cur:
                cur.execute(schema_sql())
            conn.commit()

    # -- work --------------------------------------------------------------

    def run_once(self) -> WorkerStats:
        """Claim a batch and process it. Returns what happened."""
        stats = WorkerStats()
        with self._conn() as conn:
            jobs = claim(conn, PARSE_QUEUE, self.worker, limit=self.batch_size)
            for index, job in enumerate(jobs):
                if self._stop.is_set():
                    # Hand back what we claimed and will not do. Simply breaking
                    # left these rows in 'processing' until the visibility
                    # timeout expired -- up to 30 minutes of nothing happening,
                    # which defeats the fast shutdown the Event exists for.
                    # release() does not consume an attempt: the work never
                    # started, so charging it for our shutdown would be wrong.
                    self._release_unclaimed(conn, jobs[index:], stats)
                    break
                self._process(conn, job, stats)
        return stats

    def _release_unclaimed(
        self,
        conn: psycopg2.extensions.connection,
        jobs: list[dict[str, Any]],
        stats: WorkerStats,
    ) -> None:
        for job in jobs:
            if release(conn, job["id"], 0, self.worker):
                stats.released += 1
        log.info("granola parse: released unstarted jobs", count=stats.released)

    def _process(
        self, conn: psycopg2.extensions.connection, job: dict[str, Any], stats: WorkerStats
    ) -> None:
        job_id = job["id"]
        granola_id = job["payload"].get("granola_id")
        if not granola_id:
            # A job with no id can never succeed, so retrying it burns attempts
            # to reach the same conclusion. Fail it once, with the reason.
            self._fail(conn, job_id, "payload has no granola_id", stats)
            return

        try:
            outcome = parse_note(conn, granola_id)
        except RawNoteMissingError:
            # Enqueue happens inside the same transaction as the raw upsert, so
            # a job can only exist if its row did. Waiting will not bring it
            # back; fail it rather than cycling attempts against a row that is
            # gone for good.
            conn.rollback()
            log.error("granola parse: raw row missing", granola_id=granola_id, job_id=job_id)
            self._fail(conn, job_id, f"raw row missing for {granola_id}", stats)
            return
        except Exception as exc:
            conn.rollback()
            log.error(
                "granola parse failed",
                granola_id=granola_id,
                job_id=job_id,
                error=str(exc),
                exc_info=True,
            )
            self._fail(conn, job_id, str(exc), stats)
            return

        # complete(commit=False) so the derived rows and the completion land in
        # one transaction. A completed job with half-written tables is the state
        # nothing would ever fix: the next poll sees the note as unchanged.
        held = complete(conn, job_id, self.worker, commit=False)
        if not held:
            # Somebody else claimed this row while we worked. Our writes must
            # not stand alongside their completion.
            conn.rollback()
            log.warning("granola parse: lost the claim", granola_id=granola_id, job_id=job_id)
            stats.failed += 1
            return

        conn.commit()
        if outcome == "tombstoned":
            stats.tombstoned += 1
        else:
            stats.parsed += 1

    def _fail(
        self, conn: psycopg2.extensions.connection, job_id: int, reason: str, stats: WorkerStats
    ) -> None:
        """Report a failure, counting what actually happened.

        fail_or_retry returns which of three things it did, and counting a
        'stale' -- the claim was already lost to someone else -- as our failure
        would attribute another worker's row to this one. The database said what
        it did; reading the answer costs nothing.
        """
        outcome = fail_or_retry(conn, job_id, reason, self.worker)
        if outcome == "stale":
            log.warning("granola parse: lost the claim before failing", job_id=job_id)
            return
        stats.failed += 1

    def run(self) -> None:
        self.ensure_schema()
        signal.signal(signal.SIGTERM, self.request_stop)
        signal.signal(signal.SIGINT, self.request_stop)
        log.info("granola-parse-worker started", worker=self.worker, batch=self.batch_size)

        while not self._stop.is_set():
            try:
                stats = self.run_once()
            except Exception:
                log.error("granola parse worker crashed", exc_info=True)
                self._stop.wait(self.poll_interval_s)
                continue

            if stats.parsed or stats.tombstoned or stats.failed:
                log.info(
                    "granola parse batch",
                    parsed=stats.parsed,
                    tombstoned=stats.tombstoned,
                    failed=stats.failed,
                )
            else:
                # Nothing claimable. Waiting on the Event means a SIGTERM cuts
                # the idle interval short instead of being noticed after it.
                self._stop.wait(self.poll_interval_s)

        log.info("granola-parse-worker stopped")


def main() -> None:
    # Serves the port compose maps. Without it the mapping is decorative, which
    # is worse than absent: `docker compose ps` shows a port and nothing
    # answers on it.
    start_metrics_server(port=int(os.environ.get("METRICS_PORT", "8000")))
    ParseWorker(dsn_from_env()).run()


if __name__ == "__main__":
    main()
