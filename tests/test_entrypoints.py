"""The container entrypoints.

Glue, but glue with a history: the metrics server was added because compose
mapped ports 8097/8098 with nothing listening, and a main() that forgets to
start it puts that back without failing anything. These assert the wiring, not
the loop.
"""

from __future__ import annotations

import pytest

from cortex_granola.services import parse_worker as pw
from cortex_granola.services import sync as sync_mod


@pytest.fixture
def _pg_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POSTGRES_HOST", "127.0.0.1")
    monkeypatch.setenv("GRANOLA_API_KEY", "grn_test")


def test_parse_worker_main_serves_metrics_and_runs(
    monkeypatch: pytest.MonkeyPatch, _pg_env: None
) -> None:
    served: list[int] = []
    ran: list[bool] = []
    monkeypatch.setattr(pw, "start_metrics_server", lambda port: served.append(port))
    monkeypatch.setattr(pw.ParseWorker, "run", lambda self: ran.append(True))
    monkeypatch.setenv("METRICS_PORT", "8098")

    pw.main()

    assert served == [8098], "the mapped port must actually be served"
    assert ran == [True]


def test_sync_main_serves_metrics_and_runs(monkeypatch: pytest.MonkeyPatch, _pg_env: None) -> None:
    served: list[int] = []
    ran: list[bool] = []
    monkeypatch.setattr(sync_mod, "start_metrics_server", lambda port: served.append(port))
    monkeypatch.setattr(sync_mod.GranolaSync, "run", lambda self: ran.append(True))
    monkeypatch.setenv("METRICS_PORT", "8097")

    sync_mod.main()

    assert served == [8097]
    assert ran == [True]


def test_the_parse_worker_needs_no_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reason it takes a DSN rather than a config: compose must not have to
    mount a secret into a container that never calls the API."""
    monkeypatch.delenv("GRANOLA_API_KEY", raising=False)
    monkeypatch.delenv("GRANOLA_API_KEY_FILE", raising=False)
    served: list[int] = []
    ran: list[bool] = []
    monkeypatch.setattr(pw, "start_metrics_server", lambda port: served.append(port))
    monkeypatch.setattr(pw.ParseWorker, "run", lambda self: ran.append(True))

    pw.main()  # must not raise

    assert ran == [True]


def test_the_poller_does_need_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GRANOLA_API_KEY", raising=False)
    monkeypatch.delenv("GRANOLA_API_KEY_FILE", raising=False)
    monkeypatch.setattr(sync_mod, "start_metrics_server", lambda port: None)
    with pytest.raises(ValueError, match="GRANOLA_API_KEY"):
        sync_mod.main()
