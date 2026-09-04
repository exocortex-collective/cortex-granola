"""Resolve the deny list into concrete folder IDs.

Granola exposes no tags, only folders (which nest) and the spaces that hold
them. Excluding a folder therefore has to exclude its whole subtree.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class DenyResolution:
    folder_ids: set[str]
    space_ids: set[str]
    matched_names: list[str]
    unmatched: list[str]


def _index(folders: list[dict]) -> tuple[dict[str, dict], dict[str | None, list[str]]]:
    """Folder-by-id, and parent-id to child-ids."""
    by_id = {f["id"]: f for f in folders}
    children: dict[str | None, list[str]] = {}
    for folder in folders:
        children.setdefault(folder.get("parent_folder_id"), []).append(folder["id"])
    return by_id, children


def _match_folder_terms(
    folders: list[dict], by_id: dict[str, dict], terms: list[str]
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


def _match_space_terms(folders: list[dict], terms: list[str]) -> tuple[set[str], list[str]]:
    """Resolve space names or spa_* ids to space ids. Returns (space_ids, unmatched)."""
    known = {
        (f.get("space_id") or "").lower(): f.get("space_id") for f in folders if f.get("space_id")
    }
    space_ids: set[str] = set()
    unmatched: list[str] = []
    for term in terms:
        needle = term.strip().lower()
        if needle.startswith("spa_"):
            space_ids.add(term.strip())
        elif needle in known:
            space_ids.add(known[needle])
        else:
            unmatched.append(term)
    return space_ids, unmatched


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
    folders: list[dict], deny_folders: list[str], deny_spaces: list[str]
) -> DenyResolution:
    """Turn deny-list terms into the set of folder ids whose notes are excluded.

    Excluding a folder excludes every folder nested beneath it, and excluding a
    space excludes every folder living in it.
    """
    by_id, children = _index(folders)
    seeds, matched, unmatched_folders = _match_folder_terms(folders, by_id, deny_folders)
    space_ids, unmatched_spaces = _match_space_terms(folders, deny_spaces)

    denied = _expand(seeds, children)
    denied.update(f["id"] for f in folders if f.get("space_id") and f["space_id"] in space_ids)

    return DenyResolution(
        denied, space_ids, sorted(set(matched)), unmatched_folders + unmatched_spaces
    )


def collect_denied_note_ids(client, folder_ids: set[str]) -> set[str]:
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
