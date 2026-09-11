"""Persisting the folder tree from /v1/folders, against a real Postgres.

granola_folders.parent_id references granola_folders, so ordering and FK
behaviour are the whole subject here and a mock would assert nothing. Before
this, parse.py was the only writer and it wrote parent_id as a literal NULL:
the table held 13 of this workspace's 17 folders -- only those reachable from
an ingested note -- which also left `excluded` false on every row, because a
denied folder never got a row to flag.
"""

from __future__ import annotations

from typing import Any

import psycopg2
import pytest

from cortex_granola.config import GranolaSyncConfig
from cortex_granola.services.sync import GranolaSync, _parents_first

SOURCE = "pytest-folders"


class FolderClient:
    """Serves a canned folder list and no notes."""

    def __init__(self, folders: list[dict[str, Any]]) -> None:
        self.folders = folders
        self.list_folders_calls = 0

    def list_folders(self) -> list[dict[str, Any]]:
        self.list_folders_calls += 1
        return self.folders

    def list_notes(self, **kwargs: Any) -> list[dict[str, Any]]:
        return []

    def get_note(self, note_id: str, *, include_transcript: bool = False) -> dict[str, Any]:
        raise AssertionError("no notes in this fixture")


def folder(
    fid: str, name: str, parent: str | None = None, space: str | None = None
) -> dict[str, Any]:
    return {
        "object": "folder",
        "id": fid,
        "name": name,
        "parent_folder_id": parent,
        "space_id": space,
    }


@pytest.fixture
def sync(clean: psycopg2.extensions.connection, dsn: str) -> GranolaSync:
    cfg = GranolaSyncConfig(api_key="grn_test", dsn=dsn, source=SOURCE, overlap_minutes=60)
    service = GranolaSync(cfg, client=FolderClient([]))
    with clean.cursor() as cur:
        cur.execute("DELETE FROM granola_folders")
        cur.execute("DELETE FROM granola_sync_state WHERE source = %s", (SOURCE,))
        cur.execute(
            "INSERT INTO granola_sync_state (source, overlap_minutes) VALUES (%s, %s)",
            (SOURCE, 60),
        )
    clean.commit()
    return service


def _rows(conn: psycopg2.extensions.connection) -> dict[str, tuple[Any, ...]]:
    with conn.cursor() as cur:
        cur.execute("SELECT folder_id, name, parent_id, space_id, excluded FROM granola_folders")
        return {row[0]: row[1:] for row in cur.fetchall()}


# -- ordering --------------------------------------------------------------


def test_parents_first_orders_a_parent_before_its_child() -> None:
    ordered = _parents_first(
        [folder("fol_child", "Child", "fol_parent"), folder("fol_parent", "Parent")]
    )
    assert [f["id"] for f in ordered] == ["fol_parent", "fol_child"]


def test_parents_first_handles_a_three_level_chain() -> None:
    ordered = _parents_first(
        [
            folder("fol_c", "C", "fol_b"),
            folder("fol_a", "A"),
            folder("fol_b", "B", "fol_a"),
        ]
    )
    assert [f["id"] for f in ordered] == ["fol_a", "fol_b", "fol_c"]


def test_parents_first_does_not_hang_on_a_cycle() -> None:
    # The API should never return one; an unbounded loop inside the poller is
    # a worse answer than storing a null parent.
    ordered = _parents_first([folder("fol_a", "A", "fol_b"), folder("fol_b", "B", "fol_a")])
    assert len(ordered) == 2


def test_parents_first_keeps_a_folder_whose_parent_is_absent() -> None:
    ordered = _parents_first([folder("fol_orphan", "Orphan", "fol_missing")])
    assert [f["id"] for f in ordered] == ["fol_orphan"]


def test_parents_first_ignores_entries_with_no_id() -> None:
    assert _parents_first([{"name": "nameless"}]) == []


# -- persistence -----------------------------------------------------------


@pytest.mark.live
def test_every_folder_is_stored_not_only_those_with_notes(
    sync: GranolaSync, clean: psycopg2.extensions.connection
) -> None:
    sync.client = FolderClient(
        [folder("fol_used", "Used", space="spa_x"), folder("fol_empty", "Empty", space="spa_x")]
    )
    sync.poll()

    rows = _rows(clean)
    # fol_empty has no notes at all; parse.py would never have created it.
    assert set(rows) == {"fol_used", "fol_empty"}


@pytest.mark.live
def test_hierarchy_is_persisted(sync: GranolaSync, clean: psycopg2.extensions.connection) -> None:
    sync.client = FolderClient(
        [folder("fol_child", "Child", "fol_parent"), folder("fol_parent", "Parent")]
    )
    sync.poll()

    rows = _rows(clean)
    assert rows["fol_child"][1] == "fol_parent"
    assert rows["fol_parent"][1] is None


@pytest.mark.live
def test_child_first_input_still_satisfies_the_self_referencing_fk(
    sync: GranolaSync, clean: psycopg2.extensions.connection
) -> None:
    # The API gives no ordering guarantee. Inserting the child first raises a
    # ForeignKeyViolation and fails the whole poll.
    sync.client = FolderClient(
        [folder("fol_child", "Child", "fol_parent"), folder("fol_parent", "Parent")]
    )
    sync.poll()
    assert _rows(clean)["fol_child"][1] == "fol_parent"


@pytest.mark.live
def test_a_parent_the_api_did_not_return_is_stored_as_null(
    sync: GranolaSync, clean: psycopg2.extensions.connection
) -> None:
    # Losing one folder's ancestry beats failing the poll and stalling ingest.
    sync.client = FolderClient([folder("fol_orphan", "Orphan", "fol_missing")])
    sync.poll()

    assert _rows(clean)["fol_orphan"][1] is None


@pytest.mark.live
def test_denied_folders_are_stored_and_flagged(
    clean: psycopg2.extensions.connection, dsn: str
) -> None:
    """The `excluded` column's whole purpose, which nothing populated before.

    A denied folder was previously absent from the table entirely, so a query
    asking "why is there no note from Bus Dev?" had nothing to read.
    """
    cfg = GranolaSyncConfig(
        api_key="grn_test", dsn=dsn, source=SOURCE, overlap_minutes=60, deny_folders=["Bus Dev"]
    )
    service = GranolaSync(
        cfg, client=FolderClient([folder("fol_deny", "Bus Dev"), folder("fol_ok", "Product")])
    )
    with clean.cursor() as cur:
        cur.execute("DELETE FROM granola_folders")
        cur.execute("DELETE FROM granola_sync_state WHERE source = %s", (SOURCE,))
        cur.execute(
            "INSERT INTO granola_sync_state (source, overlap_minutes) VALUES (%s, %s)", (SOURCE, 60)
        )
    clean.commit()

    service.poll()

    rows = _rows(clean)
    assert rows["fol_deny"][3] is True
    assert rows["fol_ok"][3] is False


@pytest.mark.live
def test_a_folder_that_stops_being_denied_is_unflagged(
    sync: GranolaSync, clean: psycopg2.extensions.connection, dsn: str
) -> None:
    folders = [folder("fol_x", "Bus Dev")]
    denied_cfg = GranolaSyncConfig(
        api_key="grn_test", dsn=dsn, source=SOURCE, overlap_minutes=60, deny_folders=["Bus Dev"]
    )
    GranolaSync(denied_cfg, client=FolderClient(folders)).poll()
    assert _rows(clean)["fol_x"][3] is True

    sync.client = FolderClient(folders)
    sync.poll()
    assert _rows(clean)["fol_x"][3] is False


@pytest.mark.live
def test_a_rename_is_picked_up(sync: GranolaSync, clean: psycopg2.extensions.connection) -> None:
    sync.client = FolderClient([folder("fol_x", "Old name")])
    sync.poll()
    sync.client = FolderClient([folder("fol_x", "New name")])
    sync.poll()

    assert _rows(clean)["fol_x"][0] == "New name"


@pytest.mark.live
def test_a_reparent_is_picked_up(sync: GranolaSync, clean: psycopg2.extensions.connection) -> None:
    sync.client = FolderClient([folder("fol_p", "Parent"), folder("fol_c", "Child")])
    sync.poll()
    assert _rows(clean)["fol_c"][1] is None

    sync.client = FolderClient([folder("fol_p", "Parent"), folder("fol_c", "Child", "fol_p")])
    sync.poll()
    assert _rows(clean)["fol_c"][1] == "fol_p"


@pytest.mark.live
def test_space_name_written_by_parse_is_not_clobbered(
    sync: GranolaSync, clean: psycopg2.extensions.connection
) -> None:
    """/v1/folders carries space_id but no space name.

    parse.py fills space_name from a note's space_membership. Writing NULL over
    it on every poll would undo that on a 15-minute cycle.
    """
    with clean.cursor() as cur:
        cur.execute(
            "INSERT INTO granola_folders (folder_id, name, space_id, space_name) "
            "VALUES ('fol_x', 'X', 'spa_1', 'Valid Eval team')"
        )
    clean.commit()

    sync.client = FolderClient([folder("fol_x", "X", space="spa_1")])
    sync.poll()

    with clean.cursor() as cur:
        cur.execute("SELECT space_name FROM granola_folders WHERE folder_id = 'fol_x'")
        assert cur.fetchone()[0] == "Valid Eval team"


@pytest.mark.live
def test_folders_are_fetched_once_per_poll(clean: psycopg2.extensions.connection, dsn: str) -> None:
    # Persisting the tree must not cost a second call: _sync_folders serves
    # both the deny resolution and the write from one fetch.
    cfg = GranolaSyncConfig(
        api_key="grn_test", dsn=dsn, source=SOURCE, overlap_minutes=60, deny_folders=["Bus Dev"]
    )
    client = FolderClient([folder("fol_x", "Bus Dev")])
    service = GranolaSync(cfg, client=client)
    with clean.cursor() as cur:
        cur.execute("DELETE FROM granola_sync_state WHERE source = %s", (SOURCE,))
        cur.execute(
            "INSERT INTO granola_sync_state (source, overlap_minutes) VALUES (%s, %s)", (SOURCE, 60)
        )
    clean.commit()

    service.poll()
    assert client.list_folders_calls == 1
