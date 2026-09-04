"""Names vulture cannot see used, with the reason each is reachable.

An entry here is a claim that the name IS used and vulture is wrong -- not a
licence to keep dead code. Anything that stops being reachable should be
deleted, and its line removed from this file in the same change.
"""

from cortex_granola.ingest import Outcome

# StrEnum members are compared by identity in the adapters and asserted by name
# in tests; vulture sees the class-body assignments and no reads. Collected in a
# tuple rather than left as bare expression statements, which ruff reads as
# useless (B018) and is right to.
_REACHABLE = (
    Outcome.STORED,
    Outcome.UPDATED,
    Outcome.UNCHANGED,
    Outcome.DELETED,
)
