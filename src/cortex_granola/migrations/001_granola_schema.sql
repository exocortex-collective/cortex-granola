-- cortex-granola schema.
--
-- Mirrors postmark's raw-then-parsed split: granola_notes_raw is the immutable
-- API response, everything else is derived and can be rebuilt from it. A parser
-- change is a re-parse, not a re-fetch -- which matters because Granola only
-- returns notes that already have a generated summary, so a note we failed to
-- parse today may be unfetchable in the shape we saw it.

-- ---------------------------------------------------------------- raw ------

CREATE TABLE IF NOT EXISTS granola_notes_raw (
    granola_id      TEXT PRIMARY KEY,
    raw             JSONB NOT NULL,
    -- The API's own updated_at. The incremental watermark compares against
    -- this, and the parse worker skips a note whose value has not moved.
    api_updated_at  TIMESTAMPTZ NOT NULL,
    fetched_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- Set when /v1/audit reports the note deleted or access revoked. Rows are
    -- tombstoned rather than deleted: the export worker needs to know a file
    -- must be removed, and a vanished row cannot tell it that.
    deleted_at      TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_granola_raw_updated
    ON granola_notes_raw(api_updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_granola_raw_live
    ON granola_notes_raw(api_updated_at DESC) WHERE deleted_at IS NULL;

-- ------------------------------------------------------------- parsed ------

CREATE TABLE IF NOT EXISTS granola_notes (
    granola_id       TEXT PRIMARY KEY
        REFERENCES granola_notes_raw(granola_id) ON DELETE CASCADE,
    title            TEXT,
    owner_email      TEXT,
    web_url          TEXT,
    -- Meeting time comes from the calendar event when there is one, falling
    -- back to created_at -- the same precedence render.py already applies.
    started_at       TIMESTAMPTZ,
    ended_at         TIMESTAMPTZ,
    meeting_date     DATE,
    created_at       TIMESTAMPTZ,
    api_updated_at   TIMESTAMPTZ NOT NULL,
    summary_markdown TEXT,
    summary_text     TEXT,
    -- Only ever populated for notes created by the key's owner; always null
    -- for workspace keys and shared notes.
    private_markdown TEXT,
    has_transcript   BOOLEAN NOT NULL DEFAULT FALSE,
    parsed_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_granola_notes_date  ON granola_notes(meeting_date DESC);
CREATE INDEX IF NOT EXISTS idx_granola_notes_owner ON granola_notes(owner_email);

CREATE TABLE IF NOT EXISTS granola_transcript_segments (
    granola_id   TEXT NOT NULL
        REFERENCES granola_notes_raw(granola_id) ON DELETE CASCADE,
    seq          INTEGER NOT NULL,
    speaker_label TEXT,
    speaker       JSONB,
    text         TEXT NOT NULL,
    PRIMARY KEY (granola_id, seq)
);

-- Segments are the unit extraction reads, so full-text search lives here
-- rather than on a reassembled blob.
CREATE INDEX IF NOT EXISTS idx_granola_segments_fts
    ON granola_transcript_segments USING GIN (to_tsvector('english', text));

CREATE TABLE IF NOT EXISTS granola_attendees (
    granola_id  TEXT NOT NULL
        REFERENCES granola_notes_raw(granola_id) ON DELETE CASCADE,
    email       TEXT NOT NULL,
    name        TEXT,
    PRIMARY KEY (granola_id, email)
);

CREATE INDEX IF NOT EXISTS idx_granola_attendees_email ON granola_attendees(email);

CREATE TABLE IF NOT EXISTS granola_folders (
    folder_id   TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    parent_id   TEXT REFERENCES granola_folders(folder_id),
    space_id    TEXT,
    space_name  TEXT,
    -- Denormalised from the deny list at sync time so a query can tell why a
    -- note is absent without re-resolving the folder subtree.
    excluded    BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS granola_note_folders (
    granola_id  TEXT NOT NULL
        REFERENCES granola_notes_raw(granola_id) ON DELETE CASCADE,
    folder_id   TEXT NOT NULL,
    PRIMARY KEY (granola_id, folder_id)
);

-- ---------------------------------------------------------- ingestion ------

-- One row per logical source. Keyed by name rather than a singleton so a
-- second API key (a workspace key alongside the personal one) gets its own
-- watermark instead of fighting over a shared row.
CREATE TABLE IF NOT EXISTS granola_sync_state (
    source           TEXT PRIMARY KEY,
    last_synced_at   TIMESTAMPTZ,
    -- Re-scan window applied to last_synced_at WHEN THE WINDOW IS READ, never
    -- subtracted before the write -- storing (now - overlap) compounds it on
    -- every run and walks the watermark backwards without bound.
    --
    -- THIS COLUMN IS THE SINGLE SOURCE OF TRUTH once the row exists.
    -- GranolaSyncConfig.overlap_minutes seeds it on first insert only, so a
    -- per-source override survives restarts and redeploys. Two live values for
    -- one window is how an overlap gets applied twice, or an override gets
    -- silently ignored.
    overlap_minutes  INTEGER NOT NULL DEFAULT 60,
    last_poll_at     TIMESTAMPTZ,
    last_error       TEXT
);

CREATE TABLE IF NOT EXISTS granola_backfill_jobs (
    id            TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    status        TEXT NOT NULL DEFAULT 'pending',
    after_date    DATE,
    before_date   DATE,
    folder_id     TEXT,
    processed     INTEGER DEFAULT 0,
    stored        INTEGER DEFAULT 0,
    updated       INTEGER DEFAULT 0,
    error         TEXT,
    created_at    TIMESTAMPTZ DEFAULT NOW(),
    started_at    TIMESTAMPTZ,
    completed_at  TIMESTAMPTZ,
    CONSTRAINT granola_backfill_status_valid
        CHECK (status IN ('pending', 'running', 'completed', 'failed', 'cancelled'))
);

CREATE INDEX IF NOT EXISTS idx_granola_backfill_pending
    ON granola_backfill_jobs(status, created_at)
    WHERE status IN ('pending', 'running');
