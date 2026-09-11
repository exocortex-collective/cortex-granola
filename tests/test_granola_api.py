"""Tests for the ported Granola API client.

The module is a verbatim port that ran from cron for a week before it landed
here, which is exactly the reason to test it rather than trust it: the sibling
port, folders.py, carried two real bugs that review caught. Nothing here talks
to the network -- urlopen is replaced, so the retry and rate-limit paths run in
microseconds instead of the minutes their real sleeps would take.
"""

from __future__ import annotations

import io
import json
import urllib.error
from email.message import Message
from typing import Any

import pytest

from cortex_granola.granola import api
from cortex_granola.granola.api import (
    BURST,
    RATE_PER_SEC,
    GranolaClient,
    GranolaError,
    TranscriptTooLarge,
    _retry_after,
    _TokenBucket,
)


class FakeClock:
    """A monotonic clock that only moves when someone sleeps.

    The bucket's refill is a function of elapsed time, so driving time by hand
    is what makes the rate limit assertable at all; with the real clock the
    test would have to actually wait 0.2s per token to observe anything.
    """

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(api.time, "monotonic", fake.monotonic)
    monkeypatch.setattr(api.time, "sleep", fake.sleep)
    return fake


def http_error(
    status: int, body: str = "{}", headers: dict[str, str] | None = None
) -> urllib.error.HTTPError:
    message = Message()
    for key, value in (headers or {}).items():
        message[key] = value
    return urllib.error.HTTPError(
        "https://public-api.granola.ai/v1/notes", status, "err", message, io.BytesIO(body.encode())
    )


class FakeResponse(io.BytesIO):
    """urlopen's context manager over a canned JSON body."""

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def respond_with(monkeypatch: pytest.MonkeyPatch, outcomes: list[Any]) -> list[str]:
    """Serve `outcomes` in order; raise them if they are exceptions.

    Returns the list that accumulates each requested URL, so tests can assert
    on cursors and query parameters without a second seam.
    """
    requested: list[str] = []
    remaining = list(outcomes)

    def fake_urlopen(req: Any, timeout: int | None = None) -> FakeResponse:
        requested.append(req.full_url)
        outcome = remaining.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return FakeResponse(json.dumps(outcome).encode())

    monkeypatch.setattr(api.urllib.request, "urlopen", fake_urlopen)
    return requested


# -- the token bucket ------------------------------------------------------


def test_token_bucket_spends_burst_without_sleeping(clock: FakeClock) -> None:
    bucket = _TokenBucket(RATE_PER_SEC, BURST)
    for _ in range(BURST):
        bucket.take()
    assert clock.slept == []


def test_token_bucket_sleeps_once_burst_is_exhausted(clock: FakeClock) -> None:
    bucket = _TokenBucket(RATE_PER_SEC, BURST)
    for _ in range(BURST):
        bucket.take()

    bucket.take()

    # One token at 5/s is 0.2s. This is the documented sustained rate; a
    # regression to an unthrottled client shows up here as an empty list.
    assert clock.slept == [pytest.approx(1.0 / RATE_PER_SEC)]


def test_token_bucket_refills_over_elapsed_time(clock: FakeClock) -> None:
    bucket = _TokenBucket(RATE_PER_SEC, BURST)
    for _ in range(BURST):
        bucket.take()

    clock.now += 2.0  # 2s at 5/s == 10 tokens back

    for _ in range(10):
        bucket.take()
    assert clock.slept == []


def test_token_bucket_refill_is_capped_at_burst(clock: FakeClock) -> None:
    bucket = _TokenBucket(RATE_PER_SEC, BURST)
    clock.now += 3600.0  # an hour of credit must not become an hour of burst

    for _ in range(BURST):
        bucket.take()
    assert clock.slept == []

    bucket.take()
    assert clock.slept == [pytest.approx(1.0 / RATE_PER_SEC)]


# -- retry and backoff -----------------------------------------------------


def test_retries_429_then_succeeds(clock: FakeClock, monkeypatch: pytest.MonkeyPatch) -> None:
    respond_with(monkeypatch, [http_error(429), {"ok": True}])
    client = GranolaClient("grn_test")

    assert client._get("/v1/notes") == {"ok": True}
    assert clock.slept == [1.0]  # 2**0


def test_retry_after_header_overrides_exponential_backoff(
    clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    respond_with(monkeypatch, [http_error(429, headers={"Retry-After": "7"}), {"ok": True}])
    client = GranolaClient("grn_test")

    client._get("/v1/notes")
    assert clock.slept == [7.0]


def test_backoff_grows_and_is_capped(clock: FakeClock, monkeypatch: pytest.MonkeyPatch) -> None:
    respond_with(monkeypatch, [http_error(503) for _ in range(8)] + [{"ok": True}])
    client = GranolaClient("grn_test", max_retries=8)

    client._get("/v1/notes")
    # 2**n until the 60s ceiling, which is what keeps a long outage from
    # scheduling a multi-hour sleep inside a 15-minute poll.
    assert clock.slept == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0]


def test_gives_up_after_max_retries(clock: FakeClock, monkeypatch: pytest.MonkeyPatch) -> None:
    respond_with(monkeypatch, [http_error(500) for _ in range(3)])
    client = GranolaClient("grn_test", max_retries=2)

    with pytest.raises(GranolaError) as caught:
        client._get("/v1/notes")
    assert caught.value.status == 500
    assert len(clock.slept) == 2  # retried twice, then raised rather than looping


def test_4xx_other_than_429_does_not_retry(
    clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    requested = respond_with(monkeypatch, [http_error(401, '{"error":"bad key"}')])
    client = GranolaClient("grn_test")

    with pytest.raises(GranolaError) as caught:
        client._get("/v1/notes")
    assert caught.value.status == 401
    assert caught.value.code == "bad key"
    assert len(requested) == 1  # a bad key is not going to fix itself
    assert clock.slept == []


def test_network_error_retries_then_propagates(
    clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    respond_with(monkeypatch, [urllib.error.URLError("no route") for _ in range(3)])
    client = GranolaClient("grn_test", max_retries=2)

    with pytest.raises(urllib.error.URLError):
        client._get("/v1/notes")
    assert clock.slept == [1.0, 2.0]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("7", 7.0), ("0", 0.0), ("", None), ("Wed, 21 Oct 2026 07:28:00 GMT", None), (None, None)],
)
def test_retry_after_parsing(raw: str | None, expected: float | None) -> None:
    headers = {"Retry-After": raw} if raw is not None else {}
    # The HTTP-date form is legal and unparsed; falling back to exponential
    # backoff is correct, crashing on it is not.
    assert _retry_after(http_error(429, headers=headers)) == expected


# -- transcript-too-large --------------------------------------------------


def test_retry_after_zero_does_not_become_a_hot_loop(
    clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`Retry-After: 0` must not retry instantly.

    _retry_after returns 0.0 for it, and `_retry_after(exc) or <backoff>` reads
    that as falsy and falls through to exponential backoff. That is the desired
    behaviour, but it rests on 0.0 being falsy -- a refactor to
    `if retry_after is not None` would silently turn a throttled server into a
    spin. This test is what would fail.
    """
    respond_with(monkeypatch, [http_error(429, headers={"Retry-After": "0"}), {"ok": True}])
    client = GranolaClient("grn_test")

    client._get("/v1/notes")
    assert clock.slept == [1.0]


def test_413_raises_transcript_too_large(clock: FakeClock, monkeypatch: pytest.MonkeyPatch) -> None:
    requested = respond_with(monkeypatch, [http_error(413)])
    client = GranolaClient("grn_test")

    with pytest.raises(TranscriptTooLarge):
        client.get_note("not_1d3tmYTlCICgjy", include_transcript=True)
    assert len(requested) == 1  # never retried: the transcript will not shrink


def test_error_code_in_body_raises_transcript_too_large(
    clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    respond_with(monkeypatch, [http_error(400, '{"code":"TRANSCRIPT_TOO_LARGE"}')])
    client = GranolaClient("grn_test")

    with pytest.raises(TranscriptTooLarge):
        client.get_note("not_1d3tmYTlCICgjy", include_transcript=True)


def test_transcript_too_large_is_a_granola_error() -> None:
    # sync.py catches TranscriptTooLarge to fall back to the paged endpoint;
    # callers that only catch GranolaError must still see it.
    assert issubclass(TranscriptTooLarge, GranolaError)


def test_non_json_body_still_detects_transcript_too_large(
    clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    respond_with(monkeypatch, [http_error(400, "<html>TRANSCRIPT_TOO_LARGE</html>")])
    client = GranolaClient("grn_test")

    with pytest.raises(TranscriptTooLarge):
        client._get("/v1/notes/not_1d3tmYTlCICgjy")


# -- pagination ------------------------------------------------------------


def test_paginate_follows_cursor_until_exhausted(
    clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    requested = respond_with(
        monkeypatch,
        [
            {"notes": [{"id": "not_a"}], "hasMore": True, "cursor": "c1"},
            {"notes": [{"id": "not_b"}], "hasMore": True, "cursor": "c2"},
            {"notes": [{"id": "not_c"}], "hasMore": False, "cursor": None},
        ],
    )
    client = GranolaClient("grn_test")

    assert [n["id"] for n in client.list_notes()] == ["not_a", "not_b", "not_c"]
    assert "cursor=c1" in requested[1]
    assert "cursor=c2" in requested[2]


def test_paginate_stops_when_cursor_is_null_despite_has_more(
    clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The documented reason this condition is `or` and not `and`: the API has
    # been seen echoing hasMore=true on a final page. Trusting it alone is an
    # infinite loop re-requesting the same cursorless page forever.
    requested = respond_with(
        monkeypatch, [{"notes": [{"id": "not_a"}], "hasMore": True, "cursor": None}]
    )
    client = GranolaClient("grn_test")

    assert [n["id"] for n in client.list_notes()] == ["not_a"]
    assert len(requested) == 1


def test_paginate_stops_when_has_more_is_false_despite_cursor(
    clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    requested = respond_with(
        monkeypatch, [{"notes": [{"id": "not_a"}], "hasMore": False, "cursor": "c1"}]
    )
    client = GranolaClient("grn_test")

    assert [n["id"] for n in client.list_notes()] == ["not_a"]
    assert len(requested) == 1


def test_paginate_tolerates_an_empty_page(
    clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    respond_with(
        monkeypatch,
        [
            {"notes": [], "hasMore": True, "cursor": "c1"},
            {"notes": [{"id": "not_a"}], "hasMore": False, "cursor": None},
        ],
    )
    client = GranolaClient("grn_test")

    assert [n["id"] for n in client.list_notes()] == ["not_a"]


def test_list_notes_passes_filters_and_omits_unset_ones(
    clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    requested = respond_with(monkeypatch, [{"notes": [], "hasMore": False, "cursor": None}])
    client = GranolaClient("grn_test")

    list(client.list_notes(updated_after="2026-09-01T00:00:00Z", folder_id="fol_abc"))

    url = requested[0]
    assert "updated_after=2026-09-01T00%3A00%3A00Z" in url
    # A None filter must not become the literal string "None" in the query.
    assert "created_after" not in url
    assert "cursor" not in url


# -- response shape guards -------------------------------------------------


def test_non_object_response_is_an_error(clock: FakeClock, monkeypatch: pytest.MonkeyPatch) -> None:
    respond_with(monkeypatch, [["not", "an", "object"]])
    client = GranolaClient("grn_test")

    with pytest.raises(GranolaError, match="expected a JSON object"):
        client._get("/v1/notes")


def test_non_list_collection_is_an_error(clock: FakeClock, monkeypatch: pytest.MonkeyPatch) -> None:
    respond_with(monkeypatch, [{"notes": "nope", "hasMore": False, "cursor": None}])
    client = GranolaClient("grn_test")

    with pytest.raises(GranolaError, match="expected a list"):
        list(client.list_notes())


# -- the https guard -------------------------------------------------------


def test_rejects_a_note_id_that_escapes_the_base_url(
    clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # urlopen honours file://. A note id is the only caller-controlled part of
    # a path, so an id that rewrites the scheme would read local files.
    requested = respond_with(monkeypatch, [{"ok": True}])
    client = GranolaClient("grn_test")
    monkeypatch.setattr(api, "BASE_URL", "file:///etc")

    with pytest.raises(GranolaError, match="non-https"):
        client.get_note("passwd")
    assert requested == []  # refused before the request went out


def test_empty_api_key_is_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="GRANOLA_API_KEY"):
        GranolaClient("")


def test_authorization_header_carries_the_key(
    clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[dict[str, str]] = []

    def fake_urlopen(req: Any, timeout: int | None = None) -> FakeResponse:
        captured.append(dict(req.headers))
        return FakeResponse(b"{}")

    monkeypatch.setattr(api.urllib.request, "urlopen", fake_urlopen)
    GranolaClient("grn_secret")._get("/v1/notes")

    assert captured[0]["Authorization"] == "Bearer grn_secret"
