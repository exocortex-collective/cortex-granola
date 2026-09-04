"""The guard on CORTEX_TEST_DSN.

Round 2 replaced a substring check with a segment match, and named the exact
names the old one wrongly admitted -- but nothing exercised the guard, so the
suite would not have caught the bug coming back. The only DSN the tests ever
drive it with is `cortex_test`, which passes identically under both the buggy
and the fixed version.
"""

from __future__ import annotations

import pytest
from conftest import _is_disposable, _refuse_non_test_database


@pytest.mark.parametrize(
    "dbname", ["cortex_test", "test", "postgres", "granola-test-1", "test_cortex"]
)
def test_disposable_names_are_allowed(dbname: str) -> None:
    assert _is_disposable(dbname)


@pytest.mark.parametrize(
    "dbname", ["protest", "greatest", "attestation", "postgres_prod", "cortex"]
)
def test_names_the_substring_check_wrongly_admitted(dbname: str) -> None:
    """Each of these passed the old `"test" in dbname` check.

    `cortex` is the real production database this whole guard exists for.
    """
    assert not _is_disposable(dbname)


def test_the_guard_raises_on_a_production_dsn() -> None:
    with pytest.raises(RuntimeError, match="refusing to run destructive fixtures"):
        _refuse_non_test_database("host=127.0.0.1 port=5432 user=cortex dbname=cortex")


def test_a_dsn_without_a_dbname_is_refused() -> None:
    """Falling through to the server default would be the production database."""
    with pytest.raises(RuntimeError, match="names no dbname"):
        _refuse_non_test_database("host=127.0.0.1 port=5432 user=cortex")


def test_the_guard_accepts_a_scratch_dsn() -> None:
    _refuse_non_test_database("host=127.0.0.1 port=5432 user=cortex dbname=cortex_test")
