"""Rendering notes back out as markdown, and removing the ones that went away.

This is the only component in Cortex that deletes anything, and it operates
inside the user's Obsidian vault alongside notes it did not write. Every rule
below exists because the obvious implementation is dangerous:

  * Deletion is driven by the PRESENCE of a record in granola_exported_files,
    never by the absence of a row somewhere else. "The database has no note for
    this file, therefore remove it" turns every query bug, every mid-migration
    state and every empty result into a bulk delete of the user's notes.
  * Nothing is deleted that this worker did not write. The record is the
    authority on that, which is why it exists.
  * Every path is resolved and checked against the export root before use. Note
    titles reach filenames and titles come from an external API.
  * There is no rmtree. The unit of deletion is one recorded file.
  * A rename writes the new file before removing the old, so the note never
    exists nowhere.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg2
import structlog

from cortex_granola.config import ExportConfig
from cortex_granola.granola.render import relative_path, render_note, render_transcript

log = structlog.get_logger(__name__)

SUMMARY = "summary"
TRANSCRIPT = "transcript"


class ExportPathError(RuntimeError):
    """A path resolved outside the export root.

    Its own exception because it must never be caught alongside ordinary IO
    errors: an IO error means the write failed, this means something tried to
    write somewhere it had no business writing.
    """


@dataclass
class ExportStats:
    written: int = 0
    removed: int = 0
    unchanged: int = 0
    errors: int = 0


def safe_target(root: Path, relative: str) -> Path:
    """Resolve `relative` under `root`, refusing escapes AND symlinks.

    Two separate refusals, because they defeat different attacks:

    * Resolution happens BEFORE the containment check, since '../../etc/passwd'
      only looks like an escape once resolved and a check on the raw string
      passes it. Note titles reach filenames through slugify and titles come
      from an external API.

    * No component may be a symlink. Containment alone is not enough: a symlink
      planted INSIDE the vault -- which Obsidian users do deliberately -- points
      at a real file that is also inside the vault, so the containment check
      passes and the delete lands on a file this worker never wrote. That
      breaks the one invariant this module has. Verified by review: replacing a
      recorded path with a symlink to an unrelated note deleted the unrelated
      note and left the symlink dangling.

    Refusing rather than following is right for both: this worker only ever
    touches paths it recorded writing, and it never wrote a symlink.
    """
    root = root.resolve()
    literal = root / relative
    target = literal.resolve()
    if target != root and root not in target.parents:
        raise ExportPathError(f"{target} is outside the export root {root}")

    # Walk down from the root rather than up from the leaf, so a symlink at any
    # depth is caught even when the leaf itself does not exist yet.
    current = root
    for part in Path(relative).parts:
        current = current / part
        if current.is_symlink():
            raise ExportPathError(f"{current} is a symlink; refusing to follow it into the vault")
    return literal


def write_atomically(target: Path, content: str) -> None:
    """Write via a temp file in the same directory, then rename.

    Same directory because rename is only atomic within a filesystem, and the
    vault may be on a different one from /tmp. A half-written note in an
    Obsidian vault is worse than no note: it syncs, and it looks real.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=".granola-", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        # mkstemp always creates 0600 and rename does not adopt the replaced
        # file's mode, so without this every export -- including a re-export of
        # an existing note -- silently narrowed it to owner-only. A vault read
        # by sync or backup tooling notices; the user does not, until it stops
        # working.
        umask = os.umask(0)
        os.umask(umask)
        tmp.chmod(0o666 & ~umask)
        tmp.replace(target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def remove_recorded(root: Path, relative: str) -> bool:
    """Delete one file this worker recorded writing. True if it was there.

    missing_ok: a file the user already deleted by hand is not an error, and
    refusing to clear the record for it would strand the row forever.
    """
    target = safe_target(root, relative)
    if target.is_symlink():
        # safe_target already refuses these; belt and braces, because this is
        # the line that actually destroys something.
        raise ExportPathError(f"{target} is a symlink; refusing to delete through it")
    existed = target.exists()
    target.unlink(missing_ok=True)
    return existed


def export_note(conn: psycopg2.extensions.connection, config: ExportConfig, granola_id: str) -> str:
    """Bring the vault into line with the database for one note.

    Does not commit: the files and the record of them must agree, and a
    committed record for a file that was never written makes the worker believe
    it has cleanup to do on a path that does not exist.
    """
    raw, deleted_at, title = _load(conn, granola_id)
    recorded = _recorded_files(conn, granola_id)

    if deleted_at is not None:
        for kind, rel in recorded.items():
            remove_recorded(_root_for(config, kind), rel)
        _forget(conn, granola_id)
        if recorded:
            log.info("granola export removed", granola_id=granola_id, files=len(recorded))
            return "removed"
        return "unchanged"

    wanted = _wanted_paths(raw, config)
    for kind, rel in wanted.items():
        content = _render(raw, kind, config, wanted)
        write_atomically(safe_target(_root_for(config, kind), rel), content)

    # Only after the new files exist. The reverse order leaves a window where a
    # renamed note is in neither place, and a crash in that window loses it.
    for kind, old_rel in recorded.items():
        new_rel = wanted.get(kind)
        if new_rel != old_rel:
            remove_recorded(_root_for(config, kind), old_rel)

    _record(conn, granola_id, wanted)
    log.debug("granola export wrote", granola_id=granola_id, title=title, files=len(wanted))
    return "written"


def _root_for(config: ExportConfig, kind: str) -> Path:
    return config.notes_dir if kind == SUMMARY else config.transcripts_dir


def _wanted_paths(raw: dict[str, Any], config: ExportConfig) -> dict[str, str]:
    rel = relative_path(raw)
    wanted = {SUMMARY: rel}
    if config.include_transcript and raw.get("transcript"):
        wanted[TRANSCRIPT] = rel
    return wanted


def _render(raw: dict[str, Any], kind: str, config: ExportConfig, wanted: dict[str, str]) -> str:
    if kind == SUMMARY:
        link = _link(config, wanted) if TRANSCRIPT in wanted else None
        return render_note(raw, include_private_notes=False, transcript_link=link)
    return render_transcript(raw, note_link=_link(config, wanted, to_summary=True))


def _link(config: ExportConfig, wanted: dict[str, str], to_summary: bool = False) -> str:
    """A relative link from one tree's file to the other's.

    relpath between the two REAL paths, rather than counting "..". The first
    version added one ".." per component of the relative path -- including the
    filename -- and then appended relpath's own "..", double-counting by one:

        ../../../transcripts/2026/x.md   (resolves outside the vault)
        ../../transcripts/2026/x.md      (correct)

    Nothing catches that. An Obsidian link that points at nothing is not an
    error, it is just a link that does not go anywhere, so it would have shipped
    across every note in the vault and been noticed by a person clicking one.
    """
    rel = wanted[SUMMARY]
    src_dir = (config.transcripts_dir if to_summary else config.notes_dir) / Path(rel).parent
    dst_file = (config.notes_dir if to_summary else config.transcripts_dir) / rel
    return os.path.relpath(dst_file, src_dir)


def _load(
    conn: psycopg2.extensions.connection, granola_id: str
) -> tuple[dict[str, Any], Any, str | None]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT r.raw, r.deleted_at, n.title FROM granola_notes_raw r "
            "LEFT JOIN granola_notes n USING (granola_id) WHERE r.granola_id = %s",
            (granola_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise RuntimeError(f"no raw row for {granola_id}")
    return row[0], row[1], row[2]


def _recorded_files(conn: psycopg2.extensions.connection, granola_id: str) -> dict[str, str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT kind, relative_path FROM granola_exported_files WHERE granola_id = %s",
            (granola_id,),
        )
        return {kind: rel for kind, rel in cur.fetchall()}


def _record(conn: psycopg2.extensions.connection, granola_id: str, wanted: dict[str, str]) -> None:
    with conn.cursor() as cur:
        # Kinds no longer wanted (transcripts turned off, or a note that lost
        # its transcript) lose their record here; their files were removed above
        # because wanted.get(kind) returned None and so differed from old_rel.
        cur.execute(
            "DELETE FROM granola_exported_files WHERE granola_id = %s AND NOT (kind = ANY(%s))",
            (granola_id, list(wanted)),
        )
        for kind, rel in wanted.items():
            cur.execute(
                "INSERT INTO granola_exported_files (granola_id, kind, relative_path) "
                "VALUES (%s, %s, %s) "
                "ON CONFLICT (granola_id, kind) DO UPDATE "
                "SET relative_path = EXCLUDED.relative_path, written_at = NOW()",
                (granola_id, kind, rel),
            )


def _forget(conn: psycopg2.extensions.connection, granola_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM granola_exported_files WHERE granola_id = %s", (granola_id,))
