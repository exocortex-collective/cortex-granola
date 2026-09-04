"""Config parsing and validation.

from_env() is the boundary between a deployment's environment and every
invariant this service relies on. Its failures are quiet by nature: an int()
that accepts "0", an export tree that overlaps another, a deny list mangled by
a trailing comma. Each produces a fully-constructed, type-valid object whose
symptom appears much later and looks like a different bug.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from cortex_granola.config import ExportConfig, GranolaSyncConfig, schema_sql


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test from an empty GRANOLA_/POSTGRES_ environment.

    A developer with GRANOLA_DENY_FOLDERS exported would otherwise see
    from_env() tests pass or fail on their shell rather than on the code.
    monkeypatch restores the real values per test.
    """
    for name in [k for k in os.environ if k.startswith(("GRANOLA_", "POSTGRES_"))]:
        monkeypatch.delenv(name, raising=False)


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


# --- secrets from a mounted file ------------------------------------------


def test_the_api_key_can_come_from_a_mounted_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """How the container gets it: ~/cortex-secrets mounted read-only and the
    path passed in, matching GMAIL_TOKEN_PATH. The key never enters compose,
    so it never enters `docker inspect` output."""
    keyfile = tmp_path / "granola.env"
    keyfile.write_text("grn_from_file\n")
    monkeypatch.setenv("GRANOLA_API_KEY_FILE", str(keyfile))
    assert GranolaSyncConfig.from_env().api_key == "grn_from_file"


def test_a_key_file_in_env_format_yields_the_value_not_the_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """~/cortex-secrets/granola.env holds GRANOLA_API_KEY=grn_...

    Reading it whole would send the literal 'GRANOLA_API_KEY=grn_...' as the
    bearer token -- a 401 that reads like a bad key rather than a bad parse.
    """
    keyfile = tmp_path / "granola.env"
    keyfile.write_text("GRANOLA_API_KEY=grn_real_value\n")
    monkeypatch.setenv("GRANOLA_API_KEY_FILE", str(keyfile))
    assert GranolaSyncConfig.from_env().api_key == "grn_real_value"


def test_the_env_var_wins_over_the_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    keyfile = tmp_path / "granola.env"
    keyfile.write_text("grn_from_file\n")
    monkeypatch.setenv("GRANOLA_API_KEY_FILE", str(keyfile))
    monkeypatch.setenv("GRANOLA_API_KEY", "grn_from_env")
    assert GranolaSyncConfig.from_env().api_key == "grn_from_env"


def test_an_unreadable_key_file_is_a_clear_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A missing mount should say so, not surface later as an auth failure."""
    monkeypatch.setenv("GRANOLA_API_KEY_FILE", str(tmp_path / "nope.env"))
    with pytest.raises(ValueError, match="unreadable"):
        GranolaSyncConfig.from_env()


def test_an_empty_key_file_is_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    keyfile = tmp_path / "granola.env"
    keyfile.write_text("\n")
    monkeypatch.setenv("GRANOLA_API_KEY_FILE", str(keyfile))
    with pytest.raises(ValueError, match="empty"):
        GranolaSyncConfig.from_env()


def test_neither_source_is_a_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="GRANOLA_API_KEY_FILE"):
        GranolaSyncConfig.from_env()


def test_dsn_from_env_needs_no_credential() -> None:
    """The parse worker asks for this and nothing else."""
    from cortex_granola.config import dsn_from_env

    assert "dbname=" in dsn_from_env()


@pytest.mark.parametrize(
    "content,expected",
    [
        ("grn_plain\n", "grn_plain"),
        ("GRANOLA_API_KEY=grn_assigned\n", "grn_assigned"),
        ("export GRANOLA_API_KEY=grn_exported\n", "grn_exported"),
        ('GRANOLA_API_KEY="grn_quoted"\n', "grn_quoted"),
        # Regression: a bare key containing "=" was truncated to what followed
        # the first "=", silently, because the parser sniffed for "=" instead of
        # looking for an assignment.
        ("grn_abc=123XYZ==\n", "grn_abc=123XYZ=="),
        # Regression: a leading comment meant the sniff never matched, so the
        # WHOLE multi-line blob was returned and sent as the bearer token.
        ("# cortex secrets\n\nGRANOLA_API_KEY=grn_after_comment\n", "grn_after_comment"),
        ("\n\nGRANOLA_API_KEY=grn_after_blank\n", "grn_after_blank"),
    ],
)
def test_key_file_shapes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, content: str, expected: str
) -> None:
    keyfile = tmp_path / "granola.env"
    keyfile.write_text(content)
    monkeypatch.setenv("GRANOLA_API_KEY_FILE", str(keyfile))
    assert GranolaSyncConfig.from_env().api_key == expected


def test_an_ambiguous_key_file_is_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Several lines and no assignment: guessing which one is the key is how
    the first version shipped a bug. Say so instead."""
    keyfile = tmp_path / "granola.env"
    keyfile.write_text("grn_one\ngrn_two\n")
    monkeypatch.setenv("GRANOLA_API_KEY_FILE", str(keyfile))
    with pytest.raises(ValueError, match="cannot tell which line"):
        GranolaSyncConfig.from_env()


def test_a_comments_only_key_file_is_empty(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    keyfile = tmp_path / "granola.env"
    keyfile.write_text("# nothing here\n\n")
    monkeypatch.setenv("GRANOLA_API_KEY_FILE", str(keyfile))
    with pytest.raises(ValueError, match="empty"):
        GranolaSyncConfig.from_env()


def test_an_assignment_with_no_value_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regression: `GRANOLA_API_KEY=` matched the assignment and returned "",
    so the container booted with an empty bearer token and failed later as an
    opaque 401 -- the same shape as the bug this parser replaced."""
    keyfile = tmp_path / "granola.env"
    keyfile.write_text("GRANOLA_API_KEY=\n")
    monkeypatch.setenv("GRANOLA_API_KEY_FILE", str(keyfile))
    with pytest.raises(ValueError, match="empty value"):
        GranolaSyncConfig.from_env()


@pytest.mark.parametrize(
    "content", ['GRANOLA_API_KEY=""\n', "GRANOLA_API_KEY=''\n", "GRANOLA_API_KEY=   \n"]
)
def test_a_quoted_or_padded_empty_value_is_also_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, content: str
) -> None:
    keyfile = tmp_path / "granola.env"
    keyfile.write_text(content)
    monkeypatch.setenv("GRANOLA_API_KEY_FILE", str(keyfile))
    with pytest.raises(ValueError, match="empty value"):
        GranolaSyncConfig.from_env()


def test_a_value_containing_a_hash_is_preserved(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No inline-comment stripping: a # inside the key is part of the key."""
    keyfile = tmp_path / "granola.env"
    keyfile.write_text("GRANOLA_API_KEY=grn_a#b\n")
    monkeypatch.setenv("GRANOLA_API_KEY_FILE", str(keyfile))
    assert GranolaSyncConfig.from_env().api_key == "grn_a#b"


def test_crlf_line_endings_are_handled(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    keyfile = tmp_path / "granola.env"
    keyfile.write_bytes(b"# comment\r\nGRANOLA_API_KEY=grn_crlf\r\n")
    monkeypatch.setenv("GRANOLA_API_KEY_FILE", str(keyfile))
    assert GranolaSyncConfig.from_env().api_key == "grn_crlf"
