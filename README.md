# cortex-granola

Granola meeting notes and transcripts, ingested into Cortex.

Postgres is the system of record. Markdown is rendered back out into the
Obsidian vault as derived, rebuildable output — the reverse of the file-based
syncer this replaces.

## Pipeline

```
Granola API
    │
    ├─ poller (incremental, watermark + overlap)   ─┐
    ├─ backfill job (bounded date range)            ├─► ingest_one()
    └─ pubsub adapter (phase 2, pull not push)     ─┘        │
                                                             ▼
                                              granola_notes_raw (immutable)
                                                             │  queue: granola_parse
                                                             ▼
                                    granola_notes, transcript_segments, attendees
                                                             │
                                                             ▼
                                        markdown export → Obsidian vault
```

Adapters choose which notes and when. Nothing downstream varies by how a note
arrived, which is what keeps the phase-2 push adapter cheap to add.

## Services

| Container | Port | Role |
|-----------|------|------|
| `granola-sync` | 8096 | Polling and backfill |
| `granola-parse-worker` | 8097 | Raw JSONB into structured tables |
| `granola-export-worker` | 8098 | Markdown rendering and file lifecycle |

## Configuration

| Variable | Default | Meaning |
|----------|---------|---------|
| `GRANOLA_API_KEY` | — | Required. From `~/cortex-secrets/granola.env`. |
| `GRANOLA_POLL_INTERVAL_S` | `900` | Seconds between incremental polls. |
| `GRANOLA_OVERLAP_MINUTES` | `60` | Re-scan window, applied on read. |
| `GRANOLA_DENY_FOLDERS` | — | Comma-separated folder names or `fol_*` ids. |
| `GRANOLA_DENY_SPACES` | — | Comma-separated space names or `spa_*` ids. |
| `GRANOLA_EXPORT_ROOT` | `/export` | Vault mount point. |
| `GRANOLA_SOURCE` | `default` | Watermark key; a second API key gets its own. |

Policy is **default allow**: every note the key can see is synced unless it
lands in a denied folder or space. Excluded notes are resolved to ids *before*
any content fetch, so their summaries and transcripts are never downloaded.

## What Granola does not expose

Three limits come from the API, not this code:

- Only notes with a generated AI summary and transcript are returned at all.
- A personal API key covers the owner's notes plus workspace-public ones.
  Company-wide coverage needs a workspace key from a Granola admin.
- `private_notes_*` is always null for workspace keys and shared notes.

## Development

```bash
uv sync --all-extras --dev
uv run pytest -v
```

Live tests are marked `live` and need `CORTEX_TEST_DSN`. CI fails if they skip.

Point that DSN at a scratch database, never at the running stack. A dedicated
`cortex_test` database exists alongside `cortex` for exactly this:

```bash
export CORTEX_TEST_DSN="host=127.0.0.1 port=5432 user=cortex password=cortex dbname=cortex_test"
```

The live tests call `ensure_queue_schema()`, which is idempotent -- but "it
happens to be a no-op against production today" is not a property to rely on.

API reference: <https://docs.granola.ai/introduction>. Rate limits are 5 req/s
sustained, 25 burst; the client throttles under both and retries 429/5xx.
