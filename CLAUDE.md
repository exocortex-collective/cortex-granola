# cortex-granola

Granola meeting-note ingest for Cortex. Part of the Cortex family
(`Valid-Eval/cortex` is the parent; `postmark`, `triage`, `cortex-utils`,
`cortex-gateway` are siblings). Project-wide goal statement:
`../docs/granola-slack-goal.md`.

## What this service does

Pulls meeting notes and transcripts from the Granola public API into Postgres,
then renders markdown back out into the Obsidian vault. Postgres is the system
of record; the markdown is derived output and is rebuildable.

Archive-only — no triage rule chains. Entity extraction (people, orgs,
commitments, action items) runs over the archived text.

## The one architectural rule

**Everything goes through `ingest_one()`.** Polling, backfill, and (later) the
Pub/Sub adapter differ only in which note ids they choose and when. Nothing
below that seam knows how a record arrived.

```
poller          ┐
backfill job    ├──► ingest_one(id) ──► granola_notes_raw ──► queue.enqueue(parse)
pubsub adapter  ┘
```

Postmark learned this the expensive way: its `store_raw_message()` has the same
shape, but its polling adapter (`fallback_poll()`) was left as `pass`, so push
became load-bearing with no fallback. Here polling is primary.

Push arrives later as a public shim that verifies Granola's HMAC-SHA256 webhook
and republishes to Pub/Sub; this host **pulls** the subscription and never
accepts inbound traffic.

## Containers

| Container | Port | Role |
|-----------|------|------|
| granola-sync | 8096 | Poller + backfill |
| granola-parse-worker | 8097 | Raw JSONB → structured tables |
| granola-export-worker | 8098 | Postgres → markdown in the vault |

## Two things that will bite

**The watermark.** `granola_sync_state.last_synced_at` advances *only* on a
clean run, and the overlap window is applied when the window is **read**, never
subtracted before the write. Storing `now - overlap` compounds it every run. A
watermark that moves too far fails by not fetching something — no error, no
retry, no row to notice missing. See `watermark-integrity-reviewer`.

**The export worker deletes files**, inside the user's Obsidian vault, next to
notes it did not create. Deletion is driven by the *presence of a tombstone*
(`granola_notes_raw.deleted_at`), never by the absence of a row — an empty
result set is indistinguishable from a broken query. This is why `tombstone()`
sets a column rather than deleting the row. See `export-destructiveness-reviewer`.

## Queue

Uses the shared `queue` table via `cortex_utils.queue`, queue name
`granola_parse`. Both `claim` **and** `enqueue` go through the library —
`enqueue(dedup_key="granola_id")` includes an advisory lock that a hand-written
`WHERE NOT EXISTS` does not. Raw `INSERT INTO queue` is a review finding here.

Boot calls `ensure_queue_schema()`, the single entry point; calling the
individual migration pieces is what it exists to prevent.

## Secrets

`~/cortex-secrets/granola.env`, mounted read-only, holding `GRANOLA_API_KEY`.
The folder/space deny lists are policy, not secrets — they live in compose env.

## Not in this repo

The 313 markdown files from `~/ValidEval/meetings`. Its git history carries
meeting summaries and verbatim transcripts; only the source modules were ported,
with fresh history. Export output is gitignored.

## Development

```bash
uv sync --all-extras --dev
uv run pytest -v                  # live tests need CORTEX_TEST_DSN
uv run ruff check . && uv run ruff format --check .
uv run mypy src
```

Every PR goes through `pr-review-loop` to convergence. Commits and pushes use
`scripts/commit-and-push.sh` from that skill, never raw `git commit`/`push`.
That script runs a bare `git push`, so a fresh branch needs
`git config push.autoSetupRemote true` in this clone or the push fails after the
commit lands.
