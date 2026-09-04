"""Names vulture cannot see used, with the reason each is reachable.

An entry here is a claim that the name IS used and vulture is wrong -- not a
licence to keep dead code. Anything that stops being reachable should be
deleted, and its line removed from this file in the same change.

Currently empty, and that is the honest state. This file previously listed the
Outcome StrEnum members, claiming "vulture sees the class-body assignments and
no reads". That was not true: vulture resolves them through the Outcome.STORED
attribute accesses in ingest.py and the tests, and reports nothing for them at
any confidence level. The entries suppressed findings that were never made,
which is the failure this file's own honesty bar exists to prevent.
"""
