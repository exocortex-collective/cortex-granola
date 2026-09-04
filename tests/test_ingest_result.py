"""The IngestResult invariant.

Pure Python, no database: the invariant is about which states are
representable, not about what the SQL does. It went in during round 1 of
review and shipped untested, which is the same gap that let the deny-list bugs
survive the port -- a guard nobody proves fires is a comment.
"""

from __future__ import annotations

import pytest

from cortex_granola.ingest import IngestResult, Outcome


def test_unchanged_may_not_carry_a_job_id() -> None:
    """Nothing was written, so nothing was enqueued. The pairing was previously
    enforced only by both call sites happening to be written correctly."""
    with pytest.raises(ValueError, match="UNCHANGED must not carry a job_id"):
        IngestResult("not_x", Outcome.UNCHANGED, job_id=7)


def test_unchanged_without_a_job_id_is_fine() -> None:
    assert IngestResult("not_x", Outcome.UNCHANGED).job_id is None


@pytest.mark.parametrize("outcome", [Outcome.STORED, Outcome.UPDATED, Outcome.DELETED])
def test_work_producing_outcomes_carry_a_job_id(outcome: Outcome) -> None:
    assert IngestResult("not_x", outcome, job_id=7).job_id == 7


@pytest.mark.parametrize("outcome", [Outcome.STORED, Outcome.UPDATED, Outcome.DELETED])
def test_work_producing_outcomes_may_have_no_job_id(outcome: Outcome) -> None:
    """The converse is deliberately NOT an invariant, and this pins that.

    enqueue(dedup_key=...) returns None when an identical job is already
    queued, which is success -- the work is already covered. An UPDATED note
    whose parse job is still pending from the previous change lands here with
    no job_id, legitimately. The first version of __post_init__ asserted the
    converse and would have raised on that path.
    """
    assert IngestResult("not_x", outcome, job_id=None).job_id is None


def test_result_is_immutable() -> None:
    with pytest.raises(AttributeError):
        IngestResult("not_x", Outcome.STORED, 1).job_id = 2  # type: ignore[misc]
