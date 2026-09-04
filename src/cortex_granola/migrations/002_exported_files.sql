-- What the export worker has written, so it can clean up after itself.
--
-- Without this the worker would have to decide what to delete by diffing the
-- vault against the database, which makes every unrelated file in the user's
-- Obsidian vault a deletion candidate, and every query bug a bulk delete. The
-- rule the export-destructiveness charter states is that deletion is driven by
-- the PRESENCE of a record, never the absence of one -- and a record has to
-- exist for that to be possible.

CREATE TABLE IF NOT EXISTS granola_exported_files (
    granola_id    TEXT NOT NULL
        REFERENCES granola_notes_raw(granola_id) ON DELETE CASCADE,
    -- 'summary' or 'transcript'. Two files per note at most, so the pair is
    -- the natural key: re-exporting a note replaces its own rows and cannot
    -- accumulate orphans.
    kind          TEXT NOT NULL,
    -- Relative to the configured export root, never absolute. The root is a
    -- deployment detail -- a container mount, a laptop path -- and storing it
    -- would make every row wrong the first time it changed.
    relative_path TEXT NOT NULL,
    written_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (granola_id, kind),
    CONSTRAINT granola_exported_kind_valid CHECK (kind IN ('summary', 'transcript'))
);

CREATE INDEX IF NOT EXISTS idx_granola_exported_note
    ON granola_exported_files(granola_id);
