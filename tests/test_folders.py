"""Deny-list resolution.

This is the privacy gate: it decides which meetings never reach Postgres or the
Obsidian vault. It had no tests at all, and that is how three separate defects
in `_match_space_terms` survived the port -- a miscased space id excluded
nothing, a well-formed but nonexistent id was accepted as matched, and a space
NAME (the documented primary usage) never matched at all. All three failed
open, and two failed silently.

The cases below are the regression suite for that, plus the subtree and
provenance behaviour the module promises.
"""

from __future__ import annotations

from typing import Any

import pytest

from cortex_granola.granola.folders import DenyResolution, resolve_deny_list

SPACE = "spa_abc"
SPACE_NAME = "Valid Eval team"


def _folder(
    fid: str, name: str, parent: str | None = None, space: str | None = SPACE
) -> dict[str, Any]:
    return {
        "id": fid,
        "name": name,
        "parent_folder_id": parent,
        "space_id": space,
        "space_name": SPACE_NAME if space else None,
    }


@pytest.fixture
def folders() -> list[dict[str, Any]]:
    """Two roots, one nested child, and a folder in no space."""
    return [
        _folder("fol_bus", "Bus Dev"),
        _folder("fol_nested", "Leads", parent="fol_bus"),
        _folder("fol_deep", "Q3", parent="fol_nested"),
        _folder("fol_notes", "Notes"),
        _folder("fol_orphan", "Unfiled", space=None),
    ]


# --- folder terms ---------------------------------------------------------


def test_matches_a_folder_by_name(folders: list[dict[str, Any]]) -> None:
    r = resolve_deny_list(folders, ["Bus Dev"], [])
    assert "fol_bus" in r.folder_ids


def test_folder_name_match_is_case_insensitive(folders: list[dict[str, Any]]) -> None:
    assert (
        resolve_deny_list(folders, ["BUS DEV"], []).folder_ids
        == resolve_deny_list(folders, ["Bus Dev"], []).folder_ids
    )


def test_matches_a_folder_by_id(folders: list[dict[str, Any]]) -> None:
    assert "fol_bus" in resolve_deny_list(folders, ["fol_bus"], []).folder_ids


def test_excluding_a_folder_excludes_its_whole_subtree(folders: list[dict[str, Any]]) -> None:
    """Nesting is the point: a note filed two levels down is still excluded."""
    r = resolve_deny_list(folders, ["Bus Dev"], [])
    assert {"fol_bus", "fol_nested", "fol_deep"} <= set(r.folder_ids)
    assert "fol_notes" not in r.folder_ids


def test_an_unmatched_folder_term_is_reported(folders: list[dict[str, Any]]) -> None:
    r = resolve_deny_list(folders, ["Does Not Exist"], [])
    assert r.unmatched_folders == ("Does Not Exist",)
    assert not r.folder_ids


# --- space terms: the three regressions -----------------------------------


def test_matches_a_space_by_id(folders: list[dict[str, Any]]) -> None:
    r = resolve_deny_list(folders, [], [SPACE])
    assert {"fol_bus", "fol_nested", "fol_deep", "fol_notes"} <= set(r.folder_ids)
    assert "fol_orphan" not in r.folder_ids, "a folder in no space is not in this space"


def test_space_id_match_is_case_insensitive(folders: list[dict[str, Any]]) -> None:
    """Regression: SPA_ABC excluded nothing and reported nothing."""
    r = resolve_deny_list(folders, [], [SPACE.upper()])
    assert r.folder_ids == resolve_deny_list(folders, [], [SPACE]).folder_ids
    assert not r.unmatched_spaces


def test_matches_a_space_by_name(folders: list[dict[str, Any]]) -> None:
    """Regression: the index mapped id->id and never read the name, so the
    documented primary usage always fell through to unmatched."""
    r = resolve_deny_list(folders, [], [SPACE_NAME])
    assert r.folder_ids == resolve_deny_list(folders, [], [SPACE]).folder_ids


def test_space_name_match_is_case_insensitive(folders: list[dict[str, Any]]) -> None:
    r = resolve_deny_list(folders, [], [SPACE_NAME.upper()])
    assert r.folder_ids == resolve_deny_list(folders, [], [SPACE]).folder_ids


def test_a_wellformed_but_unknown_space_id_is_reported(folders: list[dict[str, Any]]) -> None:
    """Regression, and the worst of the three: it looked like an id, so it was
    accepted as matched, excluded nothing, and warned about nothing."""
    r = resolve_deny_list(folders, [], ["spa_does_not_exist"])
    assert not r.folder_ids
    assert r.unmatched_spaces == ("spa_does_not_exist",)


def test_unmatched_keeps_folder_and_space_provenance(folders: list[dict[str, Any]]) -> None:
    r = resolve_deny_list(folders, ["nope_folder"], ["nope_space"])
    assert r.unmatched_folders == ("nope_folder",)
    assert r.unmatched_spaces == ("nope_space",)
    assert set(r.unmatched) == {"nope_folder", "nope_space"}


def test_unmatched_terms_are_logged(
    folders: list[dict[str, Any]], caplog: pytest.LogCaptureFixture
) -> None:
    """Returning `unmatched` made the no-silent-typo promise depend on a caller
    remembering to look at it."""
    with caplog.at_level("WARNING"):
        resolve_deny_list(folders, ["nope_folder"], ["nope_space"])
    assert "nope_folder" in caplog.text
    assert "nope_space" in caplog.text


# --- shape ----------------------------------------------------------------


def test_resolution_is_immutable(folders: list[dict[str, Any]]) -> None:
    """A resolved exclusion a caller can widen after the fact is not a gate."""
    r = resolve_deny_list(folders, ["Bus Dev"], [])
    with pytest.raises(AttributeError):
        r.folder_ids = frozenset()  # type: ignore[misc]


def test_an_empty_deny_list_excludes_nothing(folders: list[dict[str, Any]]) -> None:
    r = resolve_deny_list(folders, [], [])
    assert r == DenyResolution(
        folder_ids=frozenset(),
        space_ids=frozenset(),
        matched_folder_names=(),
        matched_space_names=(),
        unmatched_folders=(),
        unmatched_spaces=(),
    )


def test_terms_are_stripped(folders: list[dict[str, Any]]) -> None:
    """Deny lists come from a comma-separated env var, so whitespace is normal."""
    assert resolve_deny_list(folders, ["  Bus Dev  "], []).folder_ids


def test_matched_names_keep_folder_and_space_provenance(folders: list[dict[str, Any]]) -> None:
    """The matched_* fields exist so a startup summary can say what a deny term
    actually resolved to. Untested, they were free to record the wrong side."""
    r = resolve_deny_list(folders, ["Bus Dev"], [SPACE_NAME])
    assert r.matched_folder_names == ("Bus Dev",)
    assert r.matched_space_names == (SPACE_NAME,)


def test_a_space_matched_by_id_reports_its_name(folders: list[dict[str, Any]]) -> None:
    """Input an id, expect a NAME -- the assertion the previous version of this
    test could not make, because it used the name as both input and expected
    output and so passed even if the code just echoed the term back.

    It also fixes a real asymmetry: a folder matched by id reported its name
    while a space matched by id reported the raw spa_ id, which is the least
    useful thing to hand an operator hunting for their typo.
    """
    r = resolve_deny_list(folders, [], [SPACE])
    assert r.matched_space_names == (SPACE_NAME,)


def test_a_folder_matched_by_id_reports_its_name(folders: list[dict[str, Any]]) -> None:
    """Matching by id but reporting by name is the point: an operator who denied
    fol_bus wants the warning to say Bus Dev."""
    r = resolve_deny_list(folders, ["fol_bus"], [])
    assert r.matched_folder_names == ("Bus Dev",)
