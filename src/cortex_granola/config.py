"""Service configuration, from the environment.

The file-based syncer read config.toml plus .env because it ran from cron with
no virtualenv. In a container the environment is the config surface, so the
toml is gone -- except the deny list, which is policy rather than deployment and
stays reviewable in the repo. See docs/original-config.toml for what was
dropped and why.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

_SCHEMA = Path(__file__).resolve().parents[2] / "migrations" / "001_granola_schema.sql"


def _split(value: str | None) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


@dataclass
class GranolaSyncConfig:
    api_key: str
    dsn: str
    source: str = "default"
    poll_interval_s: int = 900
    overlap_minutes: int = 60
    deny_folders: list[str] = field(default_factory=list)
    deny_spaces: list[str] = field(default_factory=list)

    @property
    def schema_sql(self) -> str:
        return _SCHEMA.read_text(encoding="utf-8")

    @classmethod
    def from_env(cls) -> GranolaSyncConfig:
        api_key = os.environ.get("GRANOLA_API_KEY", "")
        if not api_key:
            raise ValueError("GRANOLA_API_KEY is required")
        return cls(
            api_key=api_key,
            dsn=(
                f"host={os.environ.get('POSTGRES_HOST', 'localhost')} "
                f"port={os.environ.get('POSTGRES_PORT', '5432')} "
                f"dbname={os.environ.get('POSTGRES_DB', 'cortex')} "
                f"user={os.environ.get('POSTGRES_USER', 'cortex')} "
                f"password={os.environ.get('POSTGRES_PASSWORD', 'cortex')}"
            ),
            source=os.environ.get("GRANOLA_SOURCE", "default"),
            poll_interval_s=int(os.environ.get("GRANOLA_POLL_INTERVAL_S", "900")),
            overlap_minutes=int(os.environ.get("GRANOLA_OVERLAP_MINUTES", "60")),
            deny_folders=_split(os.environ.get("GRANOLA_DENY_FOLDERS")),
            deny_spaces=_split(os.environ.get("GRANOLA_DENY_SPACES")),
        )


@dataclass
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

    @classmethod
    def from_env(cls) -> ExportConfig:
        root = Path(os.environ.get("GRANOLA_EXPORT_ROOT", "/export"))
        return cls(
            notes_dir=root / os.environ.get("GRANOLA_NOTES_DIR", "notes"),
            transcripts_dir=root / os.environ.get("GRANOLA_TRANSCRIPTS_DIR", "transcripts"),
            include_transcript=os.environ.get("GRANOLA_INCLUDE_TRANSCRIPT", "true").lower()
            != "false",
        )
