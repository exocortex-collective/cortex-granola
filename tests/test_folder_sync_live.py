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

    def __init__(
        self,
        folders: list[dict[str, Any]],
        notes: list[dict[str, Any]] | None = None,
        failing: set[str] | None = None,
    ) -> None:
        self.folders = folders
        self.notes = notes or []
        self.failing = failing or set()
        self.list_folders_calls = 0

    def list_folders(self) -> list[dict[str, Any]]:
        self.list_folders_calls += 1
        return self.folders

    def list_notes(self, **kwargs: Any) -> list[dict[str, Any]]:
        return self.notes

    def get_note(self, note_id: str, *, include_transcript: bool = False) -> dict[str, Any]:
        if note_id in self.failing:
            raise RuntimeError(f"granola exploded on {note_id}")
        return {
            "id": note_id,
            "title": note_id,
            "updated_at": "2026-09-01T10:00:00Z",
            "summary_markdown": "x",
        }


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


def _is_acyclic(rows: dict[str, tuple[Any, ...]]) -> bool:
    """No folder reaches itself by following parent_id."""
    for start in rows:
        seen = set()
        current: Any = start
        while current is not None:
            if current in seen:
                return False
            seen.add(current)
            current = rows[current][1] if current in rows else None
    return True


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
    """Placed on the first pass, not rescued by the no-progress fallback.

    Asserting only the output list cannot tell those apart: deleting
    `or parent not in by_id` leaves the orphan falling through to the fallback
    and landing in the same position. The parent-before-child ordering of the
    OTHER folders is what distinguishes them.
    """
    ordered = _parents_first(
        [
            folder("fol_orphan", "Orphan", "fol_missing"),
            folder("fol_child", "Child", "fol_parent"),
            folder("fol_parent", "Parent"),
        ]
    )
    ids = [f["id"] for f in ordered]
    assert set(ids) == {"fol_orphan", "fol_child", "fol_parent"}
    assert ids.index("fol_parent") < ids.index("fol_child")
    # Without the guard the orphan is deferred to the fallback and lands last.
    assert ids.index("fol_orphan") < ids.index("fol_child")


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


# -- durability ------------------------------------------------------------
#
# Every test above drives poll() with an EMPTY note list, which is the one
# ordering where the folder writes happen to reach disk. The two orderings that
# lose them were unreachable from this suite, and both were real: the folder
# writes shared the poll's transaction, so the first note's rollback discarded
# the tree, and backfill has no terminal commit.


@pytest.mark.live
def test_folders_survive_a_failing_first_note(
    sync: GranolaSync, clean: psycopg2.extensions.connection
) -> None:
    """The regression that made this PR pointless in the case it most matters.

    _ingest_one_note rolls back on failure. With the folder writes in the same
    transaction, one 404 on the FIRST note threw away the whole tree -- so
    `excluded`, the column this write exists to populate, stayed false forever
    on any workspace with a deterministically-failing note. Later notes'
    commits do not bring it back.
    """
    sync.client = FolderClient(
        [folder("fol_x", "Product", space="spa_1")],
        notes=[
            {"id": "not_bad", "updated_at": "2026-09-01T10:00:00Z"},
            {"id": "not_ok", "updated_at": "2026-09-01T10:00:00Z"},
        ],
        failing={"not_bad"},
    )

    stats = sync.poll()

    assert stats.errors == 1
    assert not stats.clean
    assert "fol_x" in _rows(clean)


@pytest.mark.live
def test_folders_survive_a_poll_where_every_note_fails(
    sync: GranolaSync, clean: psycopg2.extensions.connection
) -> None:
    sync.client = FolderClient(
        [folder("fol_x", "Product")],
        notes=[{"id": "not_bad", "updated_at": "2026-09-01T10:00:00Z"}],
        failing={"not_bad"},
    )
    sync.poll()
    assert "fol_x" in _rows(clean)


@pytest.mark.live
def test_backfill_over_an_empty_range_still_persists_folders(
    sync: GranolaSync, clean: psycopg2.extensions.connection
) -> None:
    """backfill() has no terminal commit of its own.

    Its durability comes incidentally from the progress writes inside the note
    loop, so a range with no notes executed the folder INSERTs and then dropped
    them on close -- while the job was marked completed.
    """
    sync.client = FolderClient([folder("fol_x", "Product")])
    sync.backfill("job-1", None, None)
    assert "fol_x" in _rows(clean)


# -- the self-referencing FK -----------------------------------------------


@pytest.mark.live
def test_a_cycle_does_not_kill_the_poll(
    sync: GranolaSync, clean: psycopg2.extensions.connection
) -> None:
    """_parents_first terminating is not the same as _store_folders surviving.

    The old guard nulled a parent that was not "known" -- i.e. not mentioned by
    the API. In a cycle every member IS mentioned, so nothing was nulled and the
    insert died on the FK, taking the whole poll with it. Tracking rows actually
    written is what makes the docstring's promise true.
    """
    sync.client = FolderClient([folder("fol_a", "A", "fol_b"), folder("fol_b", "B", "fol_a")])

    sync.poll()

    rows = _rows(clean)
    assert set(rows) == {"fol_a", "fol_b"}
    # Exactly one edge is broken, not both: the first folder written has no
    # parent row to point at yet and gets NULL, the second can legitimately
    # point at the first. What must hold is that the STORED hierarchy is
    # acyclic, which is what the FK can express and the API's cycle cannot.
    assert _is_acyclic(rows)
    assert sum(1 for row in rows.values() if row[1] is None) == 1


@pytest.mark.live
def test_a_self_parented_folder_does_not_kill_the_poll(
    sync: GranolaSync, clean: psycopg2.extensions.connection
) -> None:
    sync.client = FolderClient([folder("fol_a", "A", "fol_a")])
    sync.poll()
    assert _rows(clean)["fol_a"][1] is None


# -- steady state ----------------------------------------------------------


@pytest.mark.live
def test_an_unchanged_folder_is_not_rewritten(
    sync: GranolaSync, clean: psycopg2.extensions.connection
) -> None:
    """This upsert runs ~96 times a day per folder, forever.

    Unguarded, the DO UPDATE wrote a dead tuple every time -- verified by ctid
    moving with every column byte-identical -- and updated_at came to mean
    "last time a poll ran" rather than "last time this folder changed".
    """
    folders = [folder("fol_x", "Product", space="spa_1")]
    sync.client = FolderClient(folders)
    sync.poll()

    with clean.cursor() as cur:
        cur.execute("SELECT ctid, updated_at FROM granola_folders WHERE folder_id = 'fol_x'")
        before = cur.fetchone()

    sync.client = FolderClient(folders)
    sync.poll()

    with clean.cursor() as cur:
        cur.execute("SELECT ctid, updated_at FROM granola_folders WHERE folder_id = 'fol_x'")
        after = cur.fetchone()

    assert before == after


@pytest.mark.live
def test_a_changed_folder_is_rewritten(
    sync: GranolaSync, clean: psycopg2.extensions.connection
) -> None:
    # The guard must not suppress a real change.
    sync.client = FolderClient([folder("fol_x", "Old")])
    sync.poll()
    sync.client = FolderClient([folder("fol_x", "New")])
    sync.poll()
    assert _rows(clean)["fol_x"][0] == "New"


# -- the deny list reaching the column -------------------------------------
#
# resolve_deny_list denies by three routes: a direct name/id seed, subtree
# expansion beneath a seed, and space membership. Only the first was asserted
# against the `excluded` column, so stripping subtree-inherited ids or
# space-denied ids from the set handed to _store_folders passed the whole suite.


def _denying(dsn: str, folders: list[dict[str, Any]], **deny: Any) -> GranolaSync:
    cfg = GranolaSyncConfig(api_key="grn_test", dsn=dsn, source=SOURCE, overlap_minutes=60, **deny)
    return GranolaSync(cfg, client=FolderClient(folders))


@pytest.mark.live
def test_a_subtree_beneath_a_denied_folder_is_flagged(
    sync: GranolaSync, clean: psycopg2.extensions.connection, dsn: str
) -> None:
    """Exclusion is transitive, and the column has to say so.

    A note in a child of a denied folder is excluded, so a query asking why it
    is missing must find the child flagged -- not just its ancestor.
    """
    folders = [
        folder("fol_top", "Bus Dev"),
        folder("fol_mid", "Pipeline", "fol_top"),
        folder("fol_leaf", "Deals", "fol_mid"),
        folder("fol_other", "Product"),
    ]
    _denying(dsn, folders, deny_folders=["Bus Dev"]).poll()

    rows = _rows(clean)
    assert rows["fol_top"][3] is True
    assert rows["fol_mid"][3] is True
    assert rows["fol_leaf"][3] is True
    assert rows["fol_other"][3] is False


@pytest.mark.live
def test_a_folder_in_a_denied_space_is_flagged(
    sync: GranolaSync, clean: psycopg2.extensions.connection, dsn: str
) -> None:
    # Nothing anywhere in the suite configured deny_spaces on GranolaSync.
    folders = [
        folder("fol_in", "Anything", space="spa_denied"),
        folder("fol_out", "Product", space="spa_kept"),
    ]
    _denying(dsn, folders, deny_spaces=["spa_denied"]).poll()

    rows = _rows(clean)
    assert rows["fol_in"][3] is True
    assert rows["fol_out"][3] is False


@pytest.mark.live
def test_backfill_honours_the_deny_list_and_stores_folders(
    clean: psycopg2.extensions.connection, dsn: str
) -> None:
    """The call site this PR moved, which no test reached.

    Replacing `denied = self._sync_folders(conn)` in backfill() with `set()`
    passed the entire suite: backfill neither stored folders nor honoured the
    deny list as far as the tests were concerned.
    """
    service = _denying(
        dsn, [folder("fol_deny", "Bus Dev"), folder("fol_ok", "Product")], deny_folders=["Bus Dev"]
    )
    service.backfill("job-deny", None, None)

    rows = _rows(clean)
    assert rows["fol_deny"][3] is True
    assert rows["fol_ok"][3] is False


@pytest.mark.live
def test_space_id_is_persisted(sync: GranolaSync, clean: psycopg2.extensions.connection) -> None:
    # Writing None for space_id passed everything.
    sync.client = FolderClient([folder("fol_x", "Product", space="spa_CIFEqvKCRx2QG6")])
    sync.poll()
    assert _rows(clean)["fol_x"][2] == "spa_CIFEqvKCRx2QG6"
