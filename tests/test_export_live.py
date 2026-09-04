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
    target = safe_target(tmp_path / "notes", "2026/2026-09-01-x-not_1.md")
    assert target.parent.name == "2026"


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
