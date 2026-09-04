"""Resolve the deny list into concrete folder IDs.

Granola exposes no tags, only folders (which nest) and the spaces that hold
them. Excluding a folder therefore has to exclude its whole subtree.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DenyResolution:
    """What the deny list resolved to, and what it did not.

    Frozen with immutable fields: this is a resolved safety-critical fact --
    which meetings are excluded -- and a caller that can mutate it after the
    fact can widen or narrow the exclusion without going through resolution.

    Folder and space provenance is kept separate. A caller warning about a typo
    has to say WHICH list to go and check, and `unmatched` merged the two so
    that it could not.
    """

    folder_ids: frozenset[str]
    space_ids: frozenset[str]
    matched_folder_names: tuple[str, ...]
    matched_space_names: tuple[str, ...]
    unmatched_folders: tuple[str, ...]
    unmatched_spaces: tuple[str, ...]

    @property
    def unmatched(self) -> tuple[str, ...]:
        """Every term that matched nothing, for a caller that does not care which list."""
        return self.unmatched_folders + self.unmatched_spaces


def _index(
    folders: list[Mapping[str, Any]],
) -> tuple[dict[str, Mapping[str, Any]], dict[str | None, list[str]]]:
    """Folder-by-id, and parent-id to child-ids."""
    by_id = {f["id"]: f for f in folders}
    children: dict[str | None, list[str]] = {}
    for folder in folders:
        children.setdefault(folder.get("parent_folder_id"), []).append(folder["id"])
    return by_id, children


def _match_folder_terms(
    folders: list[Mapping[str, Any]],
    by_id: dict[str, Mapping[str, Any]],
    terms: list[str],
) -> tuple[set[str], list[str], list[str]]:
    """Resolve folder names or fol_* ids to seed ids. Returns (seeds, matched, unmatched)."""
    seeds: set[str] = set()
    matched: list[str] = []
    unmatched: list[str] = []
    for term in terms:
        needle = term.strip().lower()
        hits = [
            f["id"]
            for f in folders
            if f["id"].lower() == needle or (f.get("name") or "").lower() == needle
        ]
        if hits:
            seeds.update(hits)
            matched.extend(by_id[h].get("name") or h for h in hits)
        else:
            # A term that matches nothing is reported, never ignored: a typo
            # must not silently sync notes that were meant to be held back.
            unmatched.append(term)
    return seeds, matched, unmatched


def _match_space_terms(
    folders: list[Mapping[str, Any]], terms: list[str]
) -> tuple[set[str], list[str], list[str]]:
    """Resolve space names or spa_* ids to space ids.

    Returns (space_ids, matched_names, unmatched).

    Every term resolves through one normalised index, keyed by BOTH the
    lowercased space id and the lowercased space name. Three separate defects
    lived in the version that special-cased a `spa_` prefix instead:

      * `spa_abc` worked but `SPA_ABC` did not. The prefix test lowercased the
        term, then stored it with the operator's own casing, and the caller
        compared exact-case against the API's value.
      * A `spa_` id that exists nowhere was accepted as matched. The branch
        trusted the prefix rather than checking membership, so a typo excluded
        nothing AND was never reported -- the deny list failing open in silence.
      * A space NAME never matched at all. The index mapped id to id and never
        read the name, so the documented primary usage fell through to
        unmatched every time.

    All three are the same mistake: deciding a term is good from its shape
    instead of asking whether it names something real.
    """
    known: dict[str, str] = {}
    names: dict[str, str] = {}
    for folder in folders:
        space_id = folder.get("space_id")
        if not space_id:
            continue
        known[space_id.lower()] = space_id
        space_name = folder.get("space_name")
        if space_name:
            names[space_name.lower()] = space_id

    space_ids: set[str] = set()
    matched: list[str] = []
    unmatched: list[str] = []
    for term in terms:
        needle = term.strip().lower()
        resolved = known.get(needle) or names.get(needle)
        if resolved is None:
            # Reported, never ignored -- including a well-formed spa_* id that
            # names no space. "Looks like an id" is not "is an id".
            unmatched.append(term)
            continue
        space_ids.add(resolved)
        matched.append(term.strip())
    return space_ids, matched, unmatched


def _expand(seeds: set[str], children: dict[str | None, list[str]]) -> set[str]:
    """Walk down from each seed so nested folders inherit the exclusion."""
    denied: set[str] = set()
    stack = list(seeds)
    while stack:
        current = stack.pop()
        if current in denied:
            continue
        denied.add(current)
        stack.extend(children.get(current, []))
    return denied


def resolve_deny_list(
    folders: list[Mapping[str, Any]], deny_folders: list[str], deny_spaces: list[str]
) -> DenyResolution:
    """Turn deny-list terms into the set of folder ids whose notes are excluded.

    Excluding a folder excludes every folder nested beneath it, and excluding a
    space excludes every folder living in it.

    Terms that match nothing are logged here as well as returned. The module
    contract is that a typo must not silently sync notes meant to be held back,
    and returning `unmatched` only made that contract depend on a caller
    remembering to look.
    """
    by_id, children = _index(folders)
    seeds, matched_folders, unmatched_folders = _match_folder_terms(folders, by_id, deny_folders)
    space_ids, matched_spaces, unmatched_spaces = _match_space_terms(folders, deny_spaces)

    denied = _expand(seeds, children)
    denied.update(f["id"] for f in folders if f.get("space_id") and f["space_id"] in space_ids)

    if unmatched_folders:
        log.warning("deny-list folder terms matched nothing: %s", ", ".join(unmatched_folders))
    if unmatched_spaces:
        log.warning("deny-list space terms matched nothing: %s", ", ".join(unmatched_spaces))

    return DenyResolution(
        folder_ids=frozenset(denied),
        space_ids=frozenset(space_ids),
        matched_folder_names=tuple(sorted(set(matched_folders))),
        matched_space_names=tuple(sorted(set(matched_spaces))),
        unmatched_folders=tuple(unmatched_folders),
        unmatched_spaces=tuple(unmatched_spaces),
    )


def collect_denied_note_ids(client: Any, folder_ids: frozenset[str] | set[str]) -> set[str]:
    """List note IDs inside excluded folders so they are skipped before fetch.

    `folder_id` filtering is transitive on Granola's side, but the deny list is
    already expanded to the full subtree, so this stays correct either way.
    Doing it up front means excluded note bodies and transcripts are never
    downloaded at all, rather than downloaded and then discarded.
    """
    denied: set[str] = set()
    for folder_id in sorted(folder_ids):
        for note in client.list_notes(folder_id=folder_id):
            denied.add(note["id"])
    return denied
