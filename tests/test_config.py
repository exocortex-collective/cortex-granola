"""Config parsing and validation.

from_env() is the boundary between a deployment's environment and every
invariant this service relies on. Its failures are quiet by nature: an int()
that accepts "0", an export tree that overlaps another, a deny list mangled by
a trailing comma. Each produces a fully-constructed, type-valid object whose
symptom appears much later and looks like a different bug.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cortex_granola.config import ExportConfig, GranolaSyncConfig, schema_sql


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os_environ_names()):
        monkeypatch.delenv(name, raising=False)


def os_environ_names() -> list[str]:
    import os

    return [k for k in os.environ if k.startswith("GRANOLA_") or k.startswith("POSTGRES_")]


def _min_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GRANOLA_API_KEY", "grn_test")


# --- GranolaSyncConfig ----------------------------------------------------


def test_api_key_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="GRANOLA_API_KEY"):
        GranolaSyncConfig.from_env()


def test_defaults_are_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    _min_env(monkeypatch)
    cfg = GranolaSyncConfig.from_env()
    assert cfg.poll_interval_s == 900
    assert cfg.overlap_minutes == 60
    assert cfg.source == "default"
    assert cfg.deny_folders == []


def test_deny_lists_are_split_and_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Comma-separated env vars pick up whitespace and trailing commas in real
    compose files; neither may become an empty deny term, because an empty term
    matches nothing and would be reported as a typo forever."""
    _min_env(monkeypatch)
    monkeypatch.setenv("GRANOLA_DENY_FOLDERS", " Bus Dev , Rubric-writing ,")
    cfg = GranolaSyncConfig.from_env()
    assert cfg.deny_folders == ["Bus Dev", "Rubric-writing"]


def test_an_empty_deny_list_is_empty_not_a_blank_term(monkeypatch: pytest.MonkeyPatch) -> None:
    _min_env(monkeypatch)
    monkeypatch.setenv("GRANOLA_DENY_SPACES", "")
    assert GranolaSyncConfig.from_env().deny_spaces == []


@pytest.mark.parametrize("value", ["0", "-1"])
def test_a_nonpositive_poll_interval_is_rejected(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """Type-valid and operationally nonsense: the poll loop would spin."""
    _min_env(monkeypatch)
    monkeypatch.setenv("GRANOLA_POLL_INTERVAL_S", value)
    with pytest.raises(ValueError, match="GRANOLA_POLL_INTERVAL_S"):
        GranolaSyncConfig.from_env()


def test_a_negative_overlap_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """A negative overlap walks the scan window forward, skipping notes."""
    _min_env(monkeypatch)
    monkeypatch.setenv("GRANOLA_OVERLAP_MINUTES", "-5")
    with pytest.raises(ValueError, match="GRANOLA_OVERLAP_MINUTES"):
        GranolaSyncConfig.from_env()


def test_a_zero_overlap_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Zero is a deliberate choice -- no re-scan -- not a misconfiguration."""
    _min_env(monkeypatch)
    monkeypatch.setenv("GRANOLA_OVERLAP_MINUTES", "0")
    assert GranolaSyncConfig.from_env().overlap_minutes == 0


def test_config_is_immutable(monkeypatch: pytest.MonkeyPatch) -> None:
    _min_env(monkeypatch)
    cfg = GranolaSyncConfig.from_env()
    with pytest.raises(AttributeError):
        cfg.poll_interval_s = 1  # type: ignore[misc]


# --- ExportConfig ---------------------------------------------------------


def test_export_trees_may_not_be_the_same_directory(tmp_path: Path) -> None:
    """The export worker deletes files; one tree's cleanup must not reach the
    other's live notes."""
    with pytest.raises(ValueError, match="same path"):
        ExportConfig(notes_dir=tmp_path / "x", transcripts_dir=tmp_path / "x")


def test_export_trees_may_not_nest(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must not nest"):
        ExportConfig(notes_dir=tmp_path, transcripts_dir=tmp_path / "inside")


def test_sibling_export_trees_are_fine(tmp_path: Path) -> None:
    cfg = ExportConfig(notes_dir=tmp_path / "notes", transcripts_dir=tmp_path / "transcripts")
    assert cfg.include_transcript is True


@pytest.mark.parametrize("value,expected", [("false", False), ("FALSE", False), ("true", True)])
def test_include_transcript_parsing(
    monkeypatch: pytest.MonkeyPatch, value: str, expected: bool
) -> None:
    monkeypatch.setenv("GRANOLA_EXPORT_ROOT", "/tmp/granola-export-test")
    monkeypatch.setenv("GRANOLA_INCLUDE_TRANSCRIPT", value)
    assert ExportConfig.from_env().include_transcript is expected


@pytest.mark.parametrize("value", ["0", "no", "off"])
def test_only_false_disables_transcripts(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """Documented, deliberate, and a foot-gun worth pinning: an operator who
    writes 0 keeps getting transcripts. Locking it in means a future change to
    accept 0/no/off is a visible decision rather than an accident."""
    monkeypatch.setenv("GRANOLA_EXPORT_ROOT", "/tmp/granola-export-test")
    monkeypatch.setenv("GRANOLA_INCLUDE_TRANSCRIPT", value)
    assert ExportConfig.from_env().include_transcript is True


# --- packaging ------------------------------------------------------------


def test_schema_sql_is_readable_as_package_data() -> None:
    """Reads through importlib.resources, not a repo-relative path -- an
    installed container has no repo to be relative to."""
    sql = schema_sql()
    assert "CREATE TABLE IF NOT EXISTS granola_notes_raw" in sql
