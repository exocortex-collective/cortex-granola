"""Service configuration, from the environment.

The file-based syncer read config.toml plus .env because it ran from cron with
no virtualenv. In a container the environment is the config surface, so the toml
is gone.

docs/original-config.toml is kept verbatim as the record of what the file-based
syncer accepted -- it explains those settings, not this port's choices. Four of
its fields have no equivalent here and are deliberate omissions:

  state_file            replaced by granola_sync_state; Postgres is the record.
  sync_unfiled_notes    default-allow already covers a note in no folder.
  include_private_notes always null for workspace keys and shared notes, so the
                        flag only ever did anything for the key owner's own notes.
  initial_backfill_days granola_backfill_jobs takes explicit dates instead.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

from cortex_utils.config import PostgresConfig


def dsn_from_env() -> str:
    """The Postgres DSN, without requiring anything Granola-specific.

    Its own function because the parse worker needs a database and no
    credential. Reaching for GranolaSyncConfig.from_env() there would have
    forced an API key on a service that never calls the API, and the compose
    file would then have mounted the secret into a container with no use for
    it.
    """
    # Built by cortex_utils.PostgresConfig rather than a local format string:
    # CLAUDE.md's "shared code goes in cortex-utils" applies, and a second copy
    # of the DSN shape is how the family ends up with services that disagree
    # about it. Only the env reading is local, because PostgresConfig takes
    # explicit fields and has no from_env of its own.
    dsn: str = PostgresConfig(
        host=os.environ.get("POSTGRES_HOST", "localhost"),
        port=int(os.environ.get("POSTGRES_PORT", "5432")),
        database=os.environ.get("POSTGRES_DB", "cortex"),
        user=os.environ.get("POSTGRES_USER", "cortex"),
        password=os.environ.get("POSTGRES_PASSWORD", "cortex"),
    ).dsn
    return dsn


def _api_key() -> str:
    """The Granola API key, from the environment or a mounted file.

    GRANOLA_API_KEY_FILE is how the containers get it, matching the rest of the
    family: ~/cortex-secrets mounted read-only and the path passed in, the same
    shape as GMAIL_TOKEN_PATH. Putting the key itself in compose env would put
    it in `docker inspect` output.
    """
    key = os.environ.get("GRANOLA_API_KEY", "").strip()
    if key:
        return key

    path = os.environ.get("GRANOLA_API_KEY_FILE", "").strip()
    if not path:
        raise ValueError("GRANOLA_API_KEY or GRANOLA_API_KEY_FILE is required")
    return _read_key_file(Path(path))


_ASSIGNMENT = re.compile(r"^(?:export\s+)?GRANOLA_API_KEY\s*=\s*(.*)$")


def _read_key_file(path: Path) -> str:
    """Read a key from a file that is either bare or env-file shaped.

    The first version sniffed for "=" in the first line and split on it. That
    guessed wrong in two ways, silently, which is worse than not supporting the
    format at all:

      * a bare key that happens to contain "=" (base64 padding, for one) was
        truncated to whatever followed the first "=",
      * a file whose first line was a comment or blank never matched the sniff,
        so the WHOLE multi-line blob was returned and sent as the bearer token.

    Neither raised. Both produce a 401 that reads like a bad key rather than a
    bad parse. So: look for a real GRANOLA_API_KEY assignment on any line, and
    otherwise require the file to be exactly one non-comment line. Anything
    else is ambiguous and says so.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"GRANOLA_API_KEY_FILE {str(path)!r} is unreadable: {exc}") from exc

    lines = [line.strip() for line in text.splitlines()]
    meaningful = [line for line in lines if line and not line.startswith("#")]

    for line in meaningful:
        match = _ASSIGNMENT.match(line)
        if match:
            return match.group(1).strip().strip('"').strip("'")

    if not meaningful:
        raise ValueError(f"GRANOLA_API_KEY_FILE {str(path)!r} is empty")
    if len(meaningful) == 1:
        return meaningful[0]
    raise ValueError(
        f"GRANOLA_API_KEY_FILE {str(path)!r} has {len(meaningful)} non-comment lines and no "
        "GRANOLA_API_KEY= assignment; cannot tell which line is the key"
    )


def schema_sql() -> str:
    """The DDL this service applies at boot, read as package data.

    Not `Path(__file__).parents[2] / "migrations"`. That walked up to a
    repo-root sibling of `src/`, which exists in a source checkout and in
    nothing else: the wheel packages `cortex_granola` only, so an installed
    container -- which is how every service in this family runs -- would raise
    FileNotFoundError at boot, on the first line of work rather than at build.
    """
    return (
        resources.files("cortex_granola.migrations")
        .joinpath("001_granola_schema.sql")
        .read_text(encoding="utf-8")
    )


def _split(value: str | None) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


@dataclass(frozen=True)
class GranolaSyncConfig:
    api_key: str
    dsn: str
    source: str = "default"
    poll_interval_s: int = 900
    # Seeds granola_sync_state.overlap_minutes on the row's FIRST insert and is
    # not consulted afterwards: the DB column is the single source of truth, so
    # a per-source override survives a restart. See the schema comment.
    overlap_minutes: int = 60
    deny_folders: list[str] = field(default_factory=list)
    deny_spaces: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        """Reject a config that is type-valid and operationally nonsense.

        int() accepts "0" and "-5" happily, so without this a misconfigured env
        produces a fully-constructed object whose only symptom is a poll loop
        that spins, or an overlap that walks the scan window backwards. Both
        surface much later and look like watermark bugs.
        """
        if self.poll_interval_s <= 0:
            raise ValueError(
                f"GRANOLA_POLL_INTERVAL_S must be positive, got {self.poll_interval_s}"
            )
        if self.overlap_minutes < 0:
            raise ValueError(
                f"GRANOLA_OVERLAP_MINUTES cannot be negative, got {self.overlap_minutes}"
            )

    @property
    def schema_sql(self) -> str:
        return schema_sql()

    @classmethod
    def from_env(cls) -> GranolaSyncConfig:
        return cls(
            api_key=_api_key(),
            dsn=dsn_from_env(),
            source=os.environ.get("GRANOLA_SOURCE", "default"),
            poll_interval_s=int(os.environ.get("GRANOLA_POLL_INTERVAL_S", "900")),
            overlap_minutes=int(os.environ.get("GRANOLA_OVERLAP_MINUTES", "60")),
            deny_folders=_split(os.environ.get("GRANOLA_DENY_FOLDERS")),
            deny_spaces=_split(os.environ.get("GRANOLA_DENY_SPACES")),
        )


@dataclass(frozen=True)
class ExportConfig:
    """Where the derived markdown lands.

    Postgres is the system of record; these trees are rebuildable output. Kept
    configurable because the destination is undecided -- the existing
    ~/ValidEval/meetings trees or the ~/Cortex Obsidian vault -- and it is a
    path, not a design commitment.
    """

    notes_dir: Path
    transcripts_dir: Path
    include_transcript: bool = True

    def __post_init__(self) -> None:
        """Refuse overlapping export trees.

        The export worker deletes files. If the two trees are the same
        directory, or one nests inside the other, a tombstoned note's cleanup
        can remove a live file from the other tree -- and the worker would be
        operating exactly as designed while doing it.
        """
        notes = self.notes_dir.resolve()
        transcripts = self.transcripts_dir.resolve()
        if notes == transcripts:
            raise ValueError(f"notes_dir and transcripts_dir are the same path: {notes}")
        if notes in transcripts.parents or transcripts in notes.parents:
            raise ValueError(
                f"export trees must not nest: notes_dir={notes}, transcripts_dir={transcripts}"
            )

    @classmethod
    def from_env(cls) -> ExportConfig:
        root = Path(os.environ.get("GRANOLA_EXPORT_ROOT", "/export"))
        return cls(
            notes_dir=root / os.environ.get("GRANOLA_NOTES_DIR", "notes"),
            transcripts_dir=root / os.environ.get("GRANOLA_TRANSCRIPTS_DIR", "transcripts"),
            include_transcript=os.environ.get("GRANOLA_INCLUDE_TRANSCRIPT", "true").lower()
            != "false",
        )
