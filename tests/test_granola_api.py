"""Tests for the ported Granola API client.

The module is a verbatim port, which is exactly the reason to test it rather
than trust it: review of the same port found folders.py failing open on the deny
list. Nothing here talks to the network -- urlopen is replaced and the clock is
injected, so the retry and rate-limit paths run in microseconds instead of the
minutes their real sleeps would take.
"""

from __future__ import annotations

import http.client
import io
import json
import ssl
import urllib.error
import urllib.parse
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
def clock() -> FakeClock:
    """A clock to hand to the client, NOT a patch of the time module.

    `api.time` is the stdlib singleton: patching it redirects every sleep in the
    process for the test's duration, so a background thread's sleep would append
    to `clock.slept` and break the exact-sequence assertions nondeterministically.
    """
    return FakeClock()


def client(clock: FakeClock, **kwargs: Any) -> GranolaClient:
    return GranolaClient("grn_test", monotonic=clock.monotonic, sleep=clock.sleep, **kwargs)


def http_error(
    status: int, body: str = "{}", headers: dict[str, str] | None = None
) -> urllib.error.HTTPError:
    message = Message()
    for key, value in (headers or {}).items():
        message[key] = value
    return urllib.error.HTTPError(
        "https://public-api.granola.ai/v1/notes",
        status,
        "err",
        message,
        io.BytesIO(body.encode("utf-8", "surrogateescape")),
    )


class FakeResponse(io.BytesIO):
    """urlopen's context manager over a canned JSON body.

    `raises` makes read() fail the way a truncated or stalled socket does, which
    is the only way to exercise the body-read branches.
    """

    def __init__(self, body: bytes, raises: BaseException | None = None) -> None:
        super().__init__(body)
        self._raises = raises

    def read(self, *args: object) -> bytes:
        if self._raises is not None:
            raise self._raises
        return super().read()

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
    bucket = _TokenBucket(RATE_PER_SEC, BURST, monotonic=clock.monotonic, sleep=clock.sleep)
    for _ in range(BURST):
        bucket.take()
    assert clock.slept == []


def test_token_bucket_sleeps_once_burst_is_exhausted(clock: FakeClock) -> None:
    bucket = _TokenBucket(RATE_PER_SEC, BURST, monotonic=clock.monotonic, sleep=clock.sleep)
    for _ in range(BURST):
        bucket.take()

    bucket.take()

    # One token at 5/s is 0.2s. This is the documented sustained rate; a
    # regression to an unthrottled client shows up here as an empty list.
    assert clock.slept == [pytest.approx(1.0 / RATE_PER_SEC)]


def test_token_bucket_spends_the_token_it_waited_for(clock: FakeClock) -> None:
    """Every other bucket test reaches the sleep branch with tokens == 0.0 exactly.

    That makes the `self.tokens = 0.0` after the sleep a no-op, so deleting it
    left the whole suite green -- while the bucket settled at twice the
    documented sustained rate, waiting out each deficit and never spending the
    token it waited for. Entering the branch with a fraction is what tells
    waiting-then-spending from waiting-and-keeping.
    """
    bucket = _TokenBucket(RATE_PER_SEC, 2, monotonic=clock.monotonic, sleep=clock.sleep)
    bucket.take()
    bucket.take()
    clock.now += 0.1  # half a token back at 5/s

    bucket.take()
    bucket.take()

    assert clock.slept == [pytest.approx(0.1), pytest.approx(1.0 / RATE_PER_SEC)]


def test_token_bucket_refills_over_elapsed_time(clock: FakeClock) -> None:
    bucket = _TokenBucket(RATE_PER_SEC, BURST, monotonic=clock.monotonic, sleep=clock.sleep)
    for _ in range(BURST):
        bucket.take()

    clock.now += 2.0  # 2s at 5/s == 10 tokens back

    for _ in range(10):
        bucket.take()
    assert clock.slept == []


def test_token_bucket_refill_is_capped_at_burst(clock: FakeClock) -> None:
    bucket = _TokenBucket(RATE_PER_SEC, BURST, monotonic=clock.monotonic, sleep=clock.sleep)
    clock.now += 3600.0  # an hour of credit must not become an hour of burst

    for _ in range(BURST):
        bucket.take()
    assert clock.slept == []

    bucket.take()
    assert clock.slept == [pytest.approx(1.0 / RATE_PER_SEC)]


# -- retry and backoff -----------------------------------------------------


def test_retries_429_then_succeeds(clock: FakeClock, monkeypatch: pytest.MonkeyPatch) -> None:
    respond_with(monkeypatch, [http_error(429), {"ok": True}])
    api_client = client(clock)

    assert api_client._get("/v1/notes") == {"ok": True}
    assert clock.slept == [1.0]  # 2**0


def test_retry_after_header_overrides_exponential_backoff(
    clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    respond_with(monkeypatch, [http_error(429, headers={"Retry-After": "7"}), {"ok": True}])
    api_client = client(clock)

    api_client._get("/v1/notes")
    assert clock.slept == [7.0]


def test_backoff_grows_and_is_capped(clock: FakeClock, monkeypatch: pytest.MonkeyPatch) -> None:
    respond_with(monkeypatch, [http_error(503) for _ in range(8)] + [{"ok": True}])
    api_client = client(clock, max_retries=8)

    api_client._get("/v1/notes")
    # 2**n until the 60s ceiling, which is what keeps a long outage from
    # scheduling a multi-hour sleep inside a 15-minute poll.
    assert clock.slept == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0]


def test_gives_up_after_max_retries(clock: FakeClock, monkeypatch: pytest.MonkeyPatch) -> None:
    respond_with(monkeypatch, [http_error(500) for _ in range(3)])
    api_client = client(clock, max_retries=2)

    with pytest.raises(GranolaError) as caught:
        api_client._get("/v1/notes")
    assert caught.value.status == 500
    assert len(clock.slept) == 2  # retried twice, then raised rather than looping


def test_4xx_other_than_429_does_not_retry(
    clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    requested = respond_with(monkeypatch, [http_error(401, '{"error":"bad key"}')])
    api_client = client(clock)

    with pytest.raises(GranolaError) as caught:
        api_client._get("/v1/notes")
    assert caught.value.status == 401
    assert caught.value.code == "bad key"
    assert len(requested) == 1  # a bad key is not going to fix itself
    assert clock.slept == []


def test_network_error_retries_then_propagates(
    clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    respond_with(monkeypatch, [urllib.error.URLError("no route") for _ in range(3)])
    api_client = client(clock, max_retries=2)

    with pytest.raises(urllib.error.URLError):
        api_client._get("/v1/notes")
    assert clock.slept == [1.0, 2.0]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("7", 7.0),
        ("0", 0.0),
        ("", None),
        ("Wed, 21 Oct 2026 07:28:00 GMT", None),
        (None, None),
        # Each of these parses and is truthy, so before the isfinite/sign check
        # they reached time.sleep and raised from inside an exception handler:
        # OverflowError, ValueError, ValueError.
        ("inf", None),
        ("nan", None),
        ("-5", None),
        # An honest header must not park a 15-minute poller for a day.
        ("86400", 60.0),
    ],
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
    api_client = client(clock)

    api_client._get("/v1/notes")
    assert clock.slept == [1.0]


def test_413_raises_transcript_too_large(clock: FakeClock, monkeypatch: pytest.MonkeyPatch) -> None:
    requested = respond_with(monkeypatch, [http_error(413)])
    api_client = client(clock)

    with pytest.raises(TranscriptTooLarge):
        api_client.get_note("not_1d3tmYTlCICgjy", include_transcript=True)
    assert len(requested) == 1  # never retried: the transcript will not shrink


def test_error_code_in_body_raises_transcript_too_large(
    clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    respond_with(monkeypatch, [http_error(400, '{"code":"TRANSCRIPT_TOO_LARGE"}')])
    api_client = client(clock)

    with pytest.raises(TranscriptTooLarge):
        api_client.get_note("not_1d3tmYTlCICgjy", include_transcript=True)


def test_transcript_too_large_is_a_granola_error() -> None:
    # sync.py catches TranscriptTooLarge to fall back to the paged endpoint;
    # callers that only catch GranolaError must still see it.
    assert issubclass(TranscriptTooLarge, GranolaError)


def test_non_json_body_still_detects_transcript_too_large(
    clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    respond_with(monkeypatch, [http_error(400, "<html>TRANSCRIPT_TOO_LARGE</html>")])
    api_client = client(clock)

    with pytest.raises(TranscriptTooLarge):
        api_client._get("/v1/notes/not_1d3tmYTlCICgjy")


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
    api_client = client(clock)

    assert [n["id"] for n in api_client.list_notes()] == ["not_a", "not_b", "not_c"]
    assert "cursor=c1" in requested[1]
    assert "cursor=c2" in requested[2]


def test_paginate_stops_when_cursor_is_null_despite_has_more(
    clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The `or` is defensive, not evidence-based: nothing in this repo, its
    # history, or the Granola docs substantiates the inherited comment claiming
    # the API "has been seen" echoing hasMore=true on a final page. Treat it as
    # unverified. The guard is still worth keeping and worth testing, because
    # trusting hasMore alone turns one bad page into an infinite loop
    # re-requesting the same cursorless page forever -- a cheap guard against an
    # unbounded failure.
    requested = respond_with(
        monkeypatch, [{"notes": [{"id": "not_a"}], "hasMore": True, "cursor": None}]
    )
    api_client = client(clock)

    assert [n["id"] for n in api_client.list_notes()] == ["not_a"]
    assert len(requested) == 1


def test_paginate_stops_when_has_more_is_false_despite_cursor(
    clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    requested = respond_with(
        monkeypatch, [{"notes": [{"id": "not_a"}], "hasMore": False, "cursor": "c1"}]
    )
    api_client = client(clock)

    assert [n["id"] for n in api_client.list_notes()] == ["not_a"]
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
    api_client = client(clock)

    assert [n["id"] for n in api_client.list_notes()] == ["not_a"]


def test_list_notes_passes_filters_and_omits_unset_ones(
    clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    requested = respond_with(monkeypatch, [{"notes": [], "hasMore": False, "cursor": None}])
    api_client = client(clock)

    list(api_client.list_notes(updated_after="2026-09-01T00:00:00Z", folder_id="fol_abc"))

    url = requested[0]
    assert "updated_after=2026-09-01T00%3A00%3A00Z" in url
    # A None filter must not become the literal string "None" in the query.
    assert "created_after" not in url
    assert "cursor" not in url


# -- response shape guards -------------------------------------------------


def test_non_object_response_is_an_error(clock: FakeClock, monkeypatch: pytest.MonkeyPatch) -> None:
    respond_with(monkeypatch, [["not", "an", "object"]])
    api_client = client(clock)

    with pytest.raises(GranolaError, match="expected a JSON object"):
        api_client._get("/v1/notes")


def test_non_list_collection_is_an_error(clock: FakeClock, monkeypatch: pytest.MonkeyPatch) -> None:
    respond_with(monkeypatch, [{"notes": "nope", "hasMore": False, "cursor": None}])
    api_client = client(clock)

    with pytest.raises(GranolaError, match="expected a list"):
        list(api_client.list_notes())


# -- the https guard -------------------------------------------------------


def test_rejects_a_note_id_that_escapes_the_base_url(
    clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # urlopen honours file://. A note id is the only caller-controlled part of
    # a path, so an id that rewrites the scheme would read local files.
    requested = respond_with(monkeypatch, [{"ok": True}])
    api_client = client(clock)
    monkeypatch.setattr(api, "BASE_URL", "file:///etc")

    with pytest.raises(GranolaError, match="non-https"):
        api_client.get_note("passwd")
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
    GranolaClient("grn_secret", sleep=lambda _: None)._get("/v1/notes")

    assert captured[0]["Authorization"] == "Bearer grn_secret"


# -- collection-key and body guards ----------------------------------------


def test_a_missing_collection_key_is_an_error_not_an_empty_page(
    clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The failure `or []` used to swallow, and the likelier one than a bad type.

    A renamed or dropped key read as an empty page. The poll then reported
    clean, and sync advances the watermark only on a clean poll -- so it would
    step over every note in the window and nothing would rescan them.
    """
    respond_with(monkeypatch, [{"hasMore": False, "cursor": None}])
    api_client = client(clock)

    with pytest.raises(GranolaError, match="no 'notes' key"):
        list(api_client.list_notes())


def test_a_null_collection_is_an_empty_page(
    clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Present-but-null is the API saying "none", unlike an absent key.
    respond_with(monkeypatch, [{"notes": None, "hasMore": False, "cursor": None}])
    assert list(client(clock).list_notes()) == []


def test_a_non_dict_item_is_an_error(clock: FakeClock, monkeypatch: pytest.MonkeyPatch) -> None:
    respond_with(
        monkeypatch, [{"notes": [{"id": "not_a"}, "nope"], "hasMore": False, "cursor": None}]
    )

    with pytest.raises(GranolaError, match="expected objects"):
        list(client(clock).list_notes())


def test_a_200_carrying_html_is_a_granola_error(
    clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A proxy error page dies in json.load, before the isinstance guard.

    Unwrapped it escaped as a bare JSONDecodeError that no `except GranolaError`
    catches -- the shape guard's comment claimed to cover exactly this case and
    did not.
    """

    def fake_urlopen(req: Any, timeout: int | None = None) -> FakeResponse:
        return FakeResponse(b"<html>502 Bad Gateway</html>")

    monkeypatch.setattr(api.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(GranolaError, match="not JSON"):
        client(clock)._get("/v1/notes")


def test_a_gateway_page_quoting_the_constant_is_retried_not_fatal(
    clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_error_code's substring fallback ran on every status, and it is consulted
    BEFORE the retryable branch -- so a 502 HTML page that happens to quote
    TRANSCRIPT_TOO_LARGE was raised as a permanent failure and never retried."""
    respond_with(
        monkeypatch,
        [
            http_error(502, "<html>TRANSCRIPT_TOO_LARGE appears in this outage page</html>"),
            {"ok": True},
        ],
    )

    assert client(clock)._get("/v1/notes") == {"ok": True}


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ('{"code":"A","error":"B","error_code":"C","message":"D"}', "A"),
        ('{"error":"B","message":"D"}', "B"),
        ('{"error_code":"C","message":"D"}', "C"),
        ('{"message":"D"}', "D"),
        ('{"code":"","message":"D"}', "D"),
        ('{"code":42,"message":"D"}', "D"),
        ('["a","list"]', None),
        ("null", None),
    ],
)
def test_error_code_field_precedence(body: str, expected: str | None) -> None:
    assert api._error_code(body, allow_substring=False) == expected


# -- endpoints -------------------------------------------------------------


def test_list_folders_requests_the_right_path_and_returns_every_page(
    clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Untested, this would have failed silently: a wrong collection key yields
    # an empty folder list, which reads as "no folders to exclude".
    requested = respond_with(
        monkeypatch,
        [
            {"folders": [{"id": "fol_a"}], "hasMore": True, "cursor": "c1"},
            {"folders": [{"id": "fol_b"}], "hasMore": False, "cursor": None},
        ],
    )

    assert [f["id"] for f in client(clock).list_folders()] == ["fol_a", "fol_b"]
    assert "/v1/folders" in requested[0]
    assert f"page_size={api.FOLDERS_PAGE_SIZE}" in requested[0]


def test_get_transcript_pages_through_segments(
    clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    requested = respond_with(
        monkeypatch,
        [
            {"transcript": [{"text": "one"}], "hasMore": True, "cursor": "c1"},
            {"transcript": [{"text": "two"}], "hasMore": False, "cursor": None},
        ],
    )

    segments = client(clock).get_transcript("not_1d3tmYTlCICgjy")
    assert [s["text"] for s in segments] == ["one", "two"]
    assert "/v1/notes/not_1d3tmYTlCICgjy/transcript" in requested[0]
    assert f"page_size={api.TRANSCRIPT_PAGE_SIZE}" in requested[0]


@pytest.mark.parametrize("note_id", ["not_a?x=1", "not_a#frag", "not_a/../folders", "not_a b"])
def test_a_note_id_cannot_rewrite_the_request(
    clock: FakeClock, monkeypatch: pytest.MonkeyPatch, note_id: str
) -> None:
    """The real exposure the scheme assertion does not cover.

    BASE_URL is a constant prefix, so no id can change the scheme -- but an
    unencoded `?` or `#` turns the rest of the path into a query or fragment,
    and `..` walks to a different endpoint entirely. Each would request
    something other than the note asked for.
    """
    requested = respond_with(monkeypatch, [{"ok": True}])
    client(clock).get_note(note_id)

    path = requested[0][len(api.BASE_URL) :]
    # The id must stay ONE path segment. Asserting on the absence of ".." would
    # be wrong: `not_a%2F..%2Ffolders` still contains literal dots, but with the
    # slashes encoded there is nothing for them to traverse.
    assert path.split("/") == ["", "v1", "notes", urllib.parse.quote(note_id, safe="")]


def test_network_branch_is_not_satisfied_by_an_http_error(
    clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HTTPError subclasses URLError, so `pytest.raises(URLError)` in the
    network test would still pass if the two handlers were merged. Pin the
    distinction: a 500 must surface as GranolaError, not escape as a URLError."""
    respond_with(monkeypatch, [http_error(500) for _ in range(2)])

    with pytest.raises(GranolaError):
        client(clock, max_retries=1)._get("/v1/notes")


def test_a_negative_max_retries_is_rejected() -> None:
    # Otherwise _get's loop body never runs and it falls through to an
    # AssertionError("unreachable") that is neither.
    with pytest.raises(ValueError, match="max_retries"):
        GranolaClient("grn_test", max_retries=-1)


# -- the cursor type guard -------------------------------------------------


@pytest.mark.parametrize("bad_cursor", [42, {"c": 1}, ["c"], True])
def test_a_non_string_cursor_ends_pagination(
    clock: FakeClock, monkeypatch: pytest.MonkeyPatch, bad_cursor: object
) -> None:
    """The isinstance arm of the cursor coercion, which nothing exercised.

    Every other pagination test supplies `"cursor": None`, which the pre-PR
    `if not cursor` already handled -- so deleting the isinstance check left the
    whole suite green. A non-string cursor going back into urlencode would
    request a page the API cannot parse, on a loop.
    """
    requested = respond_with(
        monkeypatch,
        [
            {"notes": [{"id": "not_a"}], "hasMore": True, "cursor": bad_cursor},
            {"notes": [{"id": "not_b"}], "hasMore": False, "cursor": None},
        ],
    )

    assert [n["id"] for n in client(clock).list_notes()] == ["not_a"]
    assert len(requested) == 1


def test_an_empty_string_cursor_ends_pagination(
    clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    requested = respond_with(
        monkeypatch, [{"notes": [{"id": "not_a"}], "hasMore": True, "cursor": ""}]
    )

    assert [n["id"] for n in client(clock).list_notes()] == ["not_a"]
    assert len(requested) == 1


# -- reading the error body ------------------------------------------------


class ExplodingBody(io.BytesIO):
    """A response body whose read() fails, as a socket that just died would."""

    def __init__(self, error: BaseException) -> None:
        super().__init__(b"")
        self._error = error

    def read(self, *args: object) -> bytes:
        raise self._error


def exploding_http_error(
    status: int, error: BaseException, headers: dict[str, str]
) -> urllib.error.HTTPError:
    message = Message()
    for key, value in headers.items():
        message[key] = value
    return urllib.error.HTTPError(
        "https://public-api.granola.ai/v1/notes", status, "err", message, ExplodingBody(error)
    )


@pytest.mark.parametrize(
    "error",
    [
        http.client.IncompleteRead(b"partial"),
        ConnectionResetError("connection reset"),
        TimeoutError("timed out"),
    ],
)
def test_a_failing_error_body_read_does_not_defeat_the_retry(
    clock: FakeClock, monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    """_error_body's whole reason to exist, previously untested.

    The body arrives over the socket that just failed. An exception escaping the
    read replaced a retryable 503 with an unrelated error that skipped the retry
    branch entirely -- so the poller gave up on a transient outage.

    IncompleteRead is the case the first version of the guard missed: it derives
    from http.client.HTTPException, not OSError.
    """
    respond_with(
        monkeypatch, [exploding_http_error(503, error, {"Retry-After": "1"}), {"ok": True}]
    )

    assert client(clock)._get("/v1/notes") == {"ok": True}
    assert clock.slept == [1.0]


# -- body-read failures on the success path --------------------------------


@pytest.mark.parametrize(
    "error",
    [
        http.client.IncompleteRead(b"partial"),
        TimeoutError("read timed out"),
        # The set below is why this handler is (OSError, HTTPException) and not
        # (HTTPException, TimeoutError): an earlier version caught the latter,
        # and every one of these escaped _get unretried. The error-path test
        # above already parametrized ConnectionResetError -- keeping it out of
        # THIS list is what let the narrow handler look tested.
        ConnectionResetError("connection reset by peer"),
        BrokenPipeError("broken pipe"),
        ssl.SSLError("decryption failed"),
        OSError("generic socket failure"),
    ],
)
def test_a_truncated_success_body_is_retried(
    clock: FakeClock, monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    """Neither of these is a URLError, and neither is a ValueError.

    So both escaped _get's handlers entirely on the success path -- unretried
    and outside the client's error taxonomy, while _decode's docstring promised
    a GranolaError explaining why not. urllib wraps connect-phase failures in
    URLError; a stalled or truncated BODY read happens after that.
    """
    remaining: list[object] = [error, {"ok": True}]

    def fake_urlopen(req: Any, timeout: int | None = None) -> FakeResponse:
        outcome = remaining.pop(0)
        if isinstance(outcome, BaseException):
            return FakeResponse(b"", raises=outcome)
        return FakeResponse(json.dumps(outcome).encode())

    monkeypatch.setattr(api.urllib.request, "urlopen", fake_urlopen)

    assert client(clock)._get("/v1/notes") == {"ok": True}
    assert clock.slept == [1.0]


def test_a_persistent_body_read_failure_becomes_a_granola_error(
    clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_urlopen(req: Any, timeout: int | None = None) -> FakeResponse:
        return FakeResponse(b"", raises=http.client.IncompleteRead(b"partial"))

    monkeypatch.setattr(api.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(GranolaError, match="IncompleteRead"):
        client(clock, max_retries=1)._get("/v1/notes")


def test_a_keyboard_interrupt_during_the_error_body_read_is_not_swallowed(
    clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reason _error_body is not `except BaseException`.

    That widening previously survived mutation: nothing distinguished it, so a
    SIGINT arriving mid-read was swallowed and reported as a clean retry while
    the operator was trying to stop a long-running poller.

    Says BaseException, not Exception: KeyboardInterrupt is not an Exception, so
    this test cannot and does not rule `except Exception` out. That one is
    equivalent here for a different reason -- errors="replace" makes the only
    Exception-not-OSError candidate impossible, which its own test pins.
    """
    respond_with(monkeypatch, [exploding_http_error(503, KeyboardInterrupt(), {})])

    with pytest.raises(KeyboardInterrupt):
        client(clock)._get("/v1/notes")


def test_an_unreadable_error_body_becomes_an_empty_string_and_a_warning(
    clock: FakeClock, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Pins the `return ""` value and the warning, not just the line.

    Both were line-covered and unasserted: replacing the return value, and
    deleting the log.warning entirely, each survived.
    """
    respond_with(monkeypatch, [exploding_http_error(404, ConnectionResetError("gone"), {})])

    with caplog.at_level("WARNING"), pytest.raises(GranolaError) as caught:
        client(clock)._get("/v1/notes")

    assert caught.value.args[0] == "HTTP 404: "
    assert caught.value.code is None
    assert "could not read error body" in caplog.text


def test_an_undecodable_error_body_does_not_defeat_the_retry(
    clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """errors="replace" is load-bearing: a UnicodeDecodeError is a ValueError.

    Neither OSError nor HTTPException catches it, so a strict decode would let
    a 503 whose body is not valid UTF-8 escape _get unretried -- the exact
    failure _error_body exists to prevent, arriving through the decode instead
    of the read.
    """
    respond_with(
        monkeypatch,
        [http_error(503, "\udcff\udcfe upstream connect error"), {"ok": True}],
    )

    assert client(clock)._get("/v1/notes") == {"ok": True}
