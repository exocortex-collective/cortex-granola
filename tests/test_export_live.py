"""The export worker, which is the only thing in Cortex that deletes files.

It runs inside the user's Obsidian vault, next to notes it did not write. Every
test here is about a way that could go wrong, and several assert that a file
this worker did NOT write is still there afterwards -- which is the property no
amount of "it worked on my machine" establishes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import psycopg2
import pytest
from psycopg2.extras import Json

from cortex_granola.config import ExportConfig
from cortex_granola.export import (
    ExportPathError,
    export_note,
    remove_recorded,
    safe_target,
    write_atomically,
)

NOTE = "not_export0001"


@pytest.fixture
def config(tmp_path: Path) -> ExportConfig:
    return ExportConfig(notes_dir=tmp_path / "notes", transcripts_dir=tmp_path / "transcripts")


def _raw(**overrides: Any) -> dict[str, Any]:
    note: dict[str, Any] = {
        "id": NOTE,
        "title": "Product planning",
        "created_at": "2026-09-01T09:00:00Z",
        "updated_at": "2026-09-01T10:00:00Z",
        "calendar_event": {"scheduled_start_time": "2026-09-01T12:00:00Z"},
        "summary_markdown": "# Product planning\n\n- ship it",
        "transcript": [{"speaker": {"name": "Devon"}, "text": "hello"}],
    }
    note.update(overrides)
    return note


def _store(
    conn: psycopg2.extensions.connection, raw: dict[str, Any], deleted: bool = False
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO granola_notes_raw (granola_id, raw, api_updated_at, deleted_at) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (granola_id) DO UPDATE SET raw = EXCLUDED.raw, "
            "api_updated_at = EXCLUDED.api_updated_at, deleted_at = EXCLUDED.deleted_at",
            (NOTE, Json(raw), raw["updated_at"], "2026-09-02T00:00:00Z" if deleted else None),
        )
    conn.commit()


def _files(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*.md")) if root.exists() else []


# --- the path boundary ----------------------------------------------------


@pytest.mark.parametrize(
    "escape",
    [
        "../outside.md",
        "../../outside.md",
        "2026/../../outside.md",
        "/etc/passwd",
    ],
)
def test_a_path_escaping_the_root_is_refused(tmp_path: Path, escape: str) -> None:
    """Note titles reach filenames and titles come from an external API, so
    this is a boundary rather than a formality. Resolution happens before the
    check: '../x' only looks like an escape once resolved."""
    with pytest.raises(ExportPathError):
        safe_target(tmp_path / "notes", escape)


def test_a_legitimate_nested_path_is_allowed(tmp_path: Path) -> None:
    """Asserts the whole resolved path, not just the parent's name -- the
    latter passes under a subtly wrong safe_target."""
    root = tmp_path / "notes"
    target = safe_target(root, "2026/2026-09-01-x-not_1.md")
    assert target == root.resolve() / "2026" / "2026-09-01-x-not_1.md"


# --- writing --------------------------------------------------------------


def test_a_write_is_atomic(tmp_path: Path) -> None:
    """A half-written note in a synced vault is worse than no note: it looks
    real and it propagates."""
    target = tmp_path / "notes" / "2026" / "a.md"
    write_atomically(target, "hello")
    assert target.read_text() == "hello"
    assert not list(target.parent.glob(".granola-*")), "no temp file left behind"


def test_a_failed_write_leaves_no_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "notes" / "a.md"
    target.parent.mkdir(parents=True)

    def boom(self: Any, other: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(Path, "replace", boom)
    with pytest.raises(OSError):
        write_atomically(target, "hello")
    assert not list(target.parent.glob(".granola-*"))


# --- the destructive paths ------------------------------------------------


@pytest.mark.live
def test_exporting_writes_both_trees(
    clean: psycopg2.extensions.connection, config: ExportConfig
) -> None:
    _store(clean, _raw())
    assert export_note(clean, config, NOTE) == "written"
    clean.commit()

    assert _files(config.notes_dir) == [f"2026/2026-09-01-product-planning-{NOTE}.md"]
    assert _files(config.transcripts_dir) == [f"2026/2026-09-01-product-planning-{NOTE}.md"]


@pytest.mark.live
def test_a_tombstoned_note_has_its_files_removed(
    clean: psycopg2.extensions.connection, config: ExportConfig
) -> None:
    _store(clean, _raw())
    export_note(clean, config, NOTE)
    clean.commit()
    assert _files(config.notes_dir)

    _store(clean, _raw(), deleted=True)
    assert export_note(clean, config, NOTE) == "removed"
    clean.commit()

    assert _files(config.notes_dir) == []
    assert _files(config.transcripts_dir) == []


@pytest.mark.live
def test_a_file_this_worker_did_not_write_is_never_touched(
    clean: psycopg2.extensions.connection, config: ExportConfig
) -> None:
    """The property that matters most. The vault holds the user's own notes,
    and a tombstone must remove this worker's files and nothing else.
    """
    bystander = config.notes_dir / "2026" / "my-own-note.md"
    bystander.parent.mkdir(parents=True)
    bystander.write_text("mine, not yours")

    _store(clean, _raw())
    export_note(clean, config, NOTE)
    clean.commit()

    _store(clean, _raw(), deleted=True)
    export_note(clean, config, NOTE)
    clean.commit()

    assert bystander.read_text() == "mine, not yours"


@pytest.mark.live
def test_a_note_with_no_export_record_deletes_nothing(
    clean: psycopg2.extensions.connection, config: ExportConfig
) -> None:
    """Deletion is driven by the presence of a record, never the absence of a
    row. A tombstoned note this worker never exported has nothing to clean up,
    and must not go looking."""
    bystander = config.notes_dir / "2026" / "unrelated.md"
    bystander.parent.mkdir(parents=True)
    bystander.write_text("keep me")

    _store(clean, _raw(), deleted=True)
    assert export_note(clean, config, NOTE) == "unchanged"
    clean.commit()

    assert bystander.exists()


@pytest.mark.live
def test_a_retitled_note_writes_before_removing(
    clean: psycopg2.extensions.connection, config: ExportConfig
) -> None:
    """A rename must never leave the note in neither place."""
    _store(clean, _raw())
    export_note(clean, config, NOTE)
    clean.commit()
    old = _files(config.notes_dir)[0]

    _store(clean, _raw(title="Renamed entirely"))
    export_note(clean, config, NOTE)
    clean.commit()

    current = _files(config.notes_dir)
    assert len(current) == 1, "exactly one file survives a rename"
    assert current[0] != old
    assert "renamed-entirely" in current[0]


@pytest.mark.live
def test_turning_transcripts_off_removes_the_transcript_file(
    clean: psycopg2.extensions.connection, tmp_path: Path
) -> None:
    on = ExportConfig(notes_dir=tmp_path / "notes", transcripts_dir=tmp_path / "transcripts")
    _store(clean, _raw())
    export_note(clean, on, NOTE)
    clean.commit()
    assert _files(on.transcripts_dir)

    off = ExportConfig(
        notes_dir=tmp_path / "notes",
        transcripts_dir=tmp_path / "transcripts",
        include_transcript=False,
    )
    export_note(clean, off, NOTE)
    clean.commit()

    assert _files(off.transcripts_dir) == []
    assert _files(off.notes_dir), "the summary stays"


@pytest.mark.live
def test_export_does_not_commit(
    clean: psycopg2.extensions.connection, config: ExportConfig
) -> None:
    """The files and the record of them must agree. A committed record for a
    file that was never written makes the worker believe it has cleanup to do
    on a path that does not exist."""
    _store(clean, _raw())
    export_note(clean, config, NOTE)
    clean.rollback()

    with clean.cursor() as cur:
        cur.execute("SELECT count(*) FROM granola_exported_files WHERE granola_id = %s", (NOTE,))
        assert cur.fetchone()[0] == 0


@pytest.mark.live
def test_re_exporting_an_unchanged_note_is_stable(
    clean: psycopg2.extensions.connection, config: ExportConfig
) -> None:
    _store(clean, _raw())
    export_note(clean, config, NOTE)
    clean.commit()
    first = (config.notes_dir / _files(config.notes_dir)[0]).read_text()

    export_note(clean, config, NOTE)
    clean.commit()

    assert len(_files(config.notes_dir)) == 1
    assert (config.notes_dir / _files(config.notes_dir)[0]).read_text() == first


def test_removing_a_file_the_user_already_deleted_is_not_an_error(tmp_path: Path) -> None:
    """Refusing would strand the record forever."""
    assert remove_recorded(tmp_path, "2026/gone.md") is False


# --- the worker's work-finding query ---------------------------------------


@pytest.mark.live
def test_the_worker_finds_a_note_that_has_never_been_exported(
    clean: psycopg2.extensions.connection, config: ExportConfig, dsn: str
) -> None:
    from cortex_granola.parse import parse_note
    from cortex_granola.services.export_worker import ExportWorker

    _store(clean, _raw())
    parse_note(clean, NOTE)
    clean.commit()

    worker = ExportWorker(dsn, config)
    assert NOTE in worker.pending(clean)

    stats = worker.run_once()
    assert stats.written == 1
    assert _files(config.notes_dir)


@pytest.mark.live
def test_the_worker_does_not_re_export_an_unchanged_note(
    clean: psycopg2.extensions.connection, config: ExportConfig, dsn: str
) -> None:
    """Otherwise every poll rewrites all 158 notes, and an Obsidian vault that
    is also a git repo would show 158 changed files every thirty seconds."""
    from cortex_granola.parse import parse_note
    from cortex_granola.services.export_worker import ExportWorker

    _store(clean, _raw())
    parse_note(clean, NOTE)
    clean.commit()

    worker = ExportWorker(dsn, config)
    worker.run_once()
    assert worker.pending(clean) == []


@pytest.mark.live
def test_an_unparsed_note_is_not_exported(
    clean: psycopg2.extensions.connection, config: ExportConfig, dsn: str
) -> None:
    """The raw row arrives before the parse job runs. Exporting then would
    write a note the rest of the system does not yet know about."""
    from cortex_granola.services.export_worker import ExportWorker

    _store(clean, _raw())
    clean.commit()
    assert ExportWorker(dsn, config).pending(clean) == []


@pytest.mark.live
def test_tombstoned_notes_are_handled_before_new_ones(
    clean: psycopg2.extensions.connection, config: ExportConfig, dsn: str
) -> None:
    """Removing a note the user excluded is more urgent than writing one they
    added; a large backfill must not delay it."""
    from cortex_granola.parse import parse_note
    from cortex_granola.services.export_worker import ExportWorker

    _store(clean, _raw())
    parse_note(clean, NOTE)
    clean.commit()
    worker = ExportWorker(dsn, config)
    worker.run_once()

    other = "not_export0002"
    with clean.cursor() as cur:
        cur.execute(
            "INSERT INTO granola_notes_raw (granola_id, raw, api_updated_at) VALUES (%s,%s,%s)",
            (other, Json({**_raw(), "id": other, "title": "Second"}), "2026-09-01T10:00:00Z"),
        )
    clean.commit()
    parse_note(clean, other)
    _store(clean, _raw(), deleted=True)
    clean.commit()

    assert worker.pending(clean)[0] == NOTE, "the tombstone comes first"


# --- the cross-links ------------------------------------------------------


@pytest.mark.live
def test_the_transcript_link_actually_resolves(
    clean: psycopg2.extensions.connection, config: ExportConfig
) -> None:
    """A link that points at nothing is not an error in Obsidian, it is just a
    link that does not go anywhere. The first version of this arithmetic was
    off by one '..' and would have shipped across every note in the vault.
    """
    _store(clean, _raw())
    export_note(clean, config, NOTE)
    clean.commit()

    note = config.notes_dir / _files(config.notes_dir)[0]
    link = next(
        line.split(":", 1)[1].strip().strip('"')
        for line in note.read_text().splitlines()
        if line.startswith("transcript_file:")
    )
    assert (note.parent / link).resolve().exists(), f"{link} does not resolve"


@pytest.mark.live
def test_the_summary_link_actually_resolves(
    clean: psycopg2.extensions.connection, config: ExportConfig
) -> None:
    _store(clean, _raw())
    export_note(clean, config, NOTE)
    clean.commit()

    transcript = config.transcripts_dir / _files(config.transcripts_dir)[0]
    link = next(
        line.split(":", 1)[1].strip().strip('"')
        for line in transcript.read_text().splitlines()
        if line.startswith("summary_file:")
    )
    assert (transcript.parent / link).resolve().exists(), f"{link} does not resolve"


@pytest.mark.live
def test_links_resolve_when_the_trees_are_nested_differently(
    clean: psycopg2.extensions.connection, tmp_path: Path
) -> None:
    """The compose file puts both trees under meetings/; a link computed from
    path-component counting rather than the real paths breaks as soon as the
    two trees are not siblings at the same depth."""
    cfg = ExportConfig(
        notes_dir=tmp_path / "meetings" / "notes",
        transcripts_dir=tmp_path / "archive" / "deep" / "transcripts",
    )
    _store(clean, _raw())
    export_note(clean, cfg, NOTE)
    clean.commit()

    note = cfg.notes_dir / _files(cfg.notes_dir)[0]
    link = next(
        line.split(":", 1)[1].strip().strip('"')
        for line in note.read_text().splitlines()
        if line.startswith("transcript_file:")
    )
    assert (note.parent / link).resolve().exists()


# --- symlinks and permissions ---------------------------------------------


def test_a_symlink_inside_the_root_is_refused(tmp_path: Path) -> None:
    """Containment is not enough. A symlink planted inside the vault points at
    a real file that is ALSO inside the vault, so a containment-only check
    passes and the delete lands on a file this worker never wrote.
    """
    root = tmp_path / "notes"
    (root / "2026").mkdir(parents=True)
    victim = root / "2026" / "victim.md"
    victim.write_text("someone else's note")
    link = root / "2026" / "exported.md"
    link.symlink_to(victim)

    with pytest.raises(ExportPathError, match="symlink"):
        safe_target(root, "2026/exported.md")


def test_a_symlinked_directory_component_is_refused(tmp_path: Path) -> None:
    """The symlink need not be the leaf. A symlinked YEAR directory redirects
    every note filed under it."""
    root = tmp_path / "notes"
    root.mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (root / "2026").symlink_to(elsewhere)

    with pytest.raises(ExportPathError, match="symlink"):
        safe_target(root, "2026/note.md")


def test_a_symlink_is_not_deleted_through(tmp_path: Path) -> None:
    """The regression, stated as the invariant it protects: nothing is deleted
    that this worker did not write."""
    root = tmp_path / "notes"
    (root / "2026").mkdir(parents=True)
    victim = root / "2026" / "victim.md"
    victim.write_text("mine")
    (root / "2026" / "exported.md").symlink_to(victim)

    with pytest.raises(ExportPathError):
        remove_recorded(root, "2026/exported.md")
    assert victim.read_text() == "mine", "the victim must survive"


def test_an_exported_file_is_readable_by_more_than_its_owner(tmp_path: Path) -> None:
    """mkstemp creates 0600 and rename does not adopt the replaced file's mode,
    so without an explicit chmod every export silently narrowed the file to
    owner-only -- which a vault read by sync or backup tooling notices and the
    user does not, until it stops working."""
    target = tmp_path / "notes" / "2026" / "a.md"
    write_atomically(target, "hello")
    mode = target.stat().st_mode & 0o777
    assert mode & 0o044, f"mode {mode:o} is not readable beyond the owner"


def test_a_re_export_does_not_narrow_an_existing_file(tmp_path: Path) -> None:
    target = tmp_path / "notes" / "a.md"
    target.parent.mkdir(parents=True)
    target.write_text("first")
    target.chmod(0o644)

    write_atomically(target, "second")

    assert target.stat().st_mode & 0o777 == 0o644 & ~_umask()


def _umask() -> int:
    import os

    current = os.umask(0)
    os.umask(current)
    return current


# --- the worker's error paths ---------------------------------------------


@pytest.mark.live
def test_an_ordinary_failure_is_counted_and_the_batch_continues(
    clean: psycopg2.extensions.connection,
    config: ExportConfig,
    dsn: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cortex_granola.parse import parse_note
    from cortex_granola.services import export_worker as ew

    _store(clean, _raw())
    parse_note(clean, NOTE)
    clean.commit()

    def boom(*args: Any, **kwargs: Any) -> str:
        raise OSError("disk full")

    monkeypatch.setattr(ew, "export_note", boom)
    stats = ew.ExportWorker(dsn, config).run_once()

    assert stats.errors == 1, "a transient failure is counted, not fatal"


@pytest.mark.live
def test_a_refused_path_stops_the_worker_instead_of_retrying_forever(
    clean: psycopg2.extensions.connection,
    config: ExportConfig,
    dsn: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ExportPathError means something in the vault tried to send a write or a
    delete somewhere this worker had no business going. Counting it as a
    routine error retried it every poll forever, at the same severity as a
    flaky disk -- so the one log line that mattered scrolled past.
    """
    from cortex_granola.export import ExportPathError
    from cortex_granola.parse import parse_note
    from cortex_granola.services import export_worker as ew

    _store(clean, _raw())
    parse_note(clean, NOTE)
    clean.commit()

    def refuse(*args: Any, **kwargs: Any) -> str:
        raise ExportPathError("/etc/passwd is outside the export root")

    monkeypatch.setattr(ew, "export_note", refuse)
    worker = ew.ExportWorker(dsn, config)

    with pytest.raises(ExportPathError):
        worker.run_once()


def test_migrations_are_discovered_not_listed() -> None:
    """A hand-maintained tuple meant a new migration file could be added and
    silently never applied: no error, no failing test, just a column that is
    not there at runtime."""
    from cortex_granola.config import schema_sql

    sql = schema_sql()
    assert "granola_notes_raw" in sql, "001 applied"
    assert "granola_exported_files" in sql, "002 applied"
