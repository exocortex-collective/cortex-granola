"""Thin client for the Granola public API (https://public-api.granola.ai).

Stdlib only. That began as a constraint -- the original syncer ran from cron
with no virtualenv -- and survives the move into a uv-managed container because
the client has no need urllib does not meet, not because anything still requires
it.
"""

from __future__ import annotations

import json
import logging
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator, Mapping
from typing import Any

log = logging.getLogger(__name__)

# Granola's payloads are JSON objects whose shape the API owns, so the client
# carries them as plain dicts and lets parse.py impose meaning. The alias names
# that decision; parse.py and export.py spell the same type out longhand.
JsonDict = dict[str, Any]

# Query-string values, which are not payloads and must not borrow JsonDict:
# every call site builds one of these literally and exhaustively.
QueryParams = Mapping[str, str | int | None]

BASE_URL = "https://public-api.granola.ai"

# Documented limits: 25 request burst, 5 requests/second sustained.
RATE_PER_SEC = 5.0
BURST = 25

# Ceiling for both the exponential backoff and an honoured Retry-After.
MAX_BACKOFF = 60.0

# Page size ceilings from the OpenAPI spec.
NOTES_PAGE_SIZE = 30
FOLDERS_PAGE_SIZE = 30
TRANSCRIPT_PAGE_SIZE = 100


class GranolaError(RuntimeError):
    def __init__(self, status: int, message: str, code: str | None = None) -> None:
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.code = code


class TranscriptTooLarge(GranolaError):
    """Get Note refused to inline the transcript; fetch it page by page."""


class _TokenBucket:
    """Leaky bucket over an injectable clock.

    The clock is a constructor argument rather than a module import so tests can
    drive it without monkeypatching `time`, which is a process-wide singleton:
    patching it means any background thread's sleep lands in the fake, and the
    exact-sequence assertions in the suite become order-dependent.
    """

    def __init__(
        self,
        rate: float,
        burst: int,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.rate = rate
        self.capacity = burst
        self.tokens = float(burst)
        self._monotonic = monotonic
        self._sleep = sleep
        self.updated = monotonic()

    def take(self) -> None:
        now = self._monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
        self.updated = now
        if self.tokens < 1.0:
            self._sleep((1.0 - self.tokens) / self.rate)
            self.tokens = 0.0
            self.updated = self._monotonic()
        else:
            self.tokens -= 1.0


class GranolaClient:
    def __init__(
        self,
        api_key: str,
        timeout: int = 60,
        max_retries: int = 5,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not api_key:
            raise ValueError("GRANOLA_API_KEY is empty")
        if max_retries < 0:
            # Otherwise the retry loop body never runs and _get falls through to
            # the "unreachable" assertion at the bottom, which is neither
            # unreachable nor a GranolaError.
            raise ValueError(f"max_retries must be >= 0, got {max_retries}")
        self._key = api_key
        self._timeout = timeout
        self._max_retries = max_retries
        self._sleep = sleep
        self._bucket = _TokenBucket(RATE_PER_SEC, BURST, monotonic=monotonic, sleep=sleep)

    # -- transport ---------------------------------------------------------

    def _get(self, path: str, params: QueryParams | None = None) -> JsonDict:
        url = _build_url(path, params)

        for attempt in range(self._max_retries + 1):
            self._bucket.take()
            req = urllib.request.Request(
                url,
                headers={
                    "Authorization": f"Bearer {self._key}",
                    "Accept": "application/json",
                    "User-Agent": "valideval-granola-sync/1.0",
                },
            )
            try:
                with urllib.request.urlopen(  # nosec B310 - https asserted above
                    req, timeout=self._timeout
                ) as resp:
                    return _decode(resp, path)
            except urllib.error.HTTPError as exc:
                body = _error_body(exc)
                code = _error_code(body, allow_substring=exc.code in (400, 413))
                if exc.code == 413 or code == "TRANSCRIPT_TOO_LARGE":
                    raise TranscriptTooLarge(exc.code, body, code) from exc
                retryable = exc.code == 429 or exc.code >= 500
                if not retryable or attempt == self._max_retries:
                    raise GranolaError(exc.code, body, code) from exc
                delay = _retry_after(exc) or min(MAX_BACKOFF, 2.0**attempt)
                log.warning("HTTP %s on %s; retrying in %.1fs", exc.code, path, delay)
                self._sleep(delay)
            except urllib.error.URLError as exc:
                if attempt == self._max_retries:
                    raise
                delay = min(MAX_BACKOFF, 2.0**attempt)
                log.warning("network error on %s (%s); retrying in %.1fs", path, exc.reason, delay)
                self._sleep(delay)

        raise AssertionError("unreachable")

    def _paginate(self, path: str, key: str, params: QueryParams) -> Iterator[JsonDict]:
        cursor: str | None = None
        while True:
            page = self._get(path, {**params, "cursor": cursor})
            if key not in page:
                # The likelier failure than a wrong type: a renamed or dropped
                # collection key reads as an empty page, the poll reports clean,
                # and the watermark advances over notes nothing will rescan.
                raise GranolaError(0, f"{path} returned no {key!r} key: {sorted(page)}")
            items = page[key]
            if items is None:
                items = []
            if not isinstance(items, list):
                raise GranolaError(
                    0, f"expected a list at {key!r} from {path}, got {type(items).__name__}"
                )
            for item in items:
                if not isinstance(item, dict):
                    raise GranolaError(0, f"expected objects in {key!r}, got {type(item).__name__}")
                yield item
            # The API echoes hasMore=true on the final page in some cases,
            # so a null/absent cursor is the authoritative stop signal.
            next_cursor = page.get("cursor")
            cursor = next_cursor if isinstance(next_cursor, str) and next_cursor else None
            if not page.get("hasMore") or not cursor:
                return

    # -- endpoints ---------------------------------------------------------

    def list_folders(self) -> list[JsonDict]:
        return list(self._paginate("/v1/folders", "folders", {"page_size": FOLDERS_PAGE_SIZE}))

    def list_notes(
        self,
        *,
        folder_id: str | None = None,
        created_after: str | None = None,
        created_before: str | None = None,
        updated_after: str | None = None,
    ) -> Iterator[JsonDict]:
        return self._paginate(
            "/v1/notes",
            "notes",
            {
                "page_size": NOTES_PAGE_SIZE,
                "folder_id": folder_id,
                "created_after": created_after,
                "created_before": created_before,
                "updated_after": updated_after,
            },
        )

    def get_note(self, note_id: str, *, include_transcript: bool = False) -> JsonDict:
        return self._get(
            f"/v1/notes/{_path_segment(note_id)}",
            {"include": "transcript"} if include_transcript else None,
        )

    def get_transcript(self, note_id: str) -> list[JsonDict]:
        return list(
            self._paginate(
                f"/v1/notes/{_path_segment(note_id)}/transcript",
                "transcript",
                {"page_size": TRANSCRIPT_PAGE_SIZE},
            )
        )


def _build_url(path: str, params: QueryParams | None) -> str:
    """The absolute request URL, with the scheme asserted.

    Note ids reach the path (`/v1/notes/{id}`), and the base URL is the only
    thing fixing the scheme. urlopen honours file:// and custom schemes, so an
    id that escaped the path would read local files rather than fail. The
    per-segment quoting in _path_segment closes the `?`/`#`/`/` cases; this
    closes the scheme.
    """
    query = {k: v for k, v in (params or {}).items() if v is not None}
    url = f"{BASE_URL}{path}"
    if query:
        url += "?" + urllib.parse.urlencode(query)
    if not url.startswith("https://"):
        raise GranolaError(0, f"refusing a non-https request URL: {url!r}")
    return url


def _decode(resp: Any, path: str) -> JsonDict:
    """The response body as a JSON object, or a GranolaError explaining why not.

    `resp` is Any because json.load's stub is typed against _typeshed's private
    SupportsRead, which has no runtime home. Restating that protocol here would
    trade one honest Any for an unused `length` parameter that vulture correctly
    reports as dead.
    """
    try:
        payload = json.load(resp)
    except ValueError as exc:
        # A 200 carrying HTML -- a proxy's error page, a captive portal -- dies
        # here, before the isinstance check below ever sees it. Unwrapped, it
        # escapes as a bare JSONDecodeError that no `except GranolaError` catches.
        raise GranolaError(0, f"{path} returned a body that is not JSON: {exc}") from exc
    # A bare JSON array would otherwise fail much later, as an AttributeError
    # inside a caller.
    if not isinstance(payload, dict):
        raise GranolaError(0, f"expected a JSON object from {path}, got {type(payload).__name__}")
    return payload


def _error_body(exc: urllib.error.HTTPError) -> str:
    """The error response body, or "" if reading it fails.

    The body arrives over the same socket that just failed, so read() can raise
    -- and an exception escaping here would replace a retryable 429/503 with an
    unrelated OSError that skips the retry path entirely. The status is the part
    that decides control flow; the body is only ever diagnostic.
    """
    try:
        with exc:
            return exc.read().decode("utf-8", "replace")
    except OSError as read_error:
        log.warning("could not read error body for HTTP %s: %s", exc.code, read_error)
        return ""


def _error_code(body: str, *, allow_substring: bool = True) -> str | None:
    """The API's own error code, or None.

    `allow_substring` gates the non-JSON fallback. A gateway's 502 page can
    quote any string, including this one, and matching it there would convert a
    retryable outage into a permanent TranscriptTooLarge that nothing retries.
    """
    try:
        parsed = json.loads(body)
    except ValueError:
        if allow_substring and "TRANSCRIPT_TOO_LARGE" in body:
            return "TRANSCRIPT_TOO_LARGE"
        return None
    if isinstance(parsed, dict):
        for field in ("code", "error", "error_code", "message"):
            value = parsed.get(field)
            if isinstance(value, str) and value:
                return value
    return None


def _retry_after(exc: urllib.error.HTTPError) -> float | None:
    """Seconds from a Retry-After header, clamped, or None to use backoff.

    `inf`, `nan` and negatives all parse and are all truthy, so without the
    isfinite check they reach time.sleep and raise OverflowError or ValueError
    from inside an exception handler. The ceiling matches the exponential path's:
    an honest `Retry-After: 86400` would otherwise park a 15-minute poller for a
    day.
    """
    raw = exc.headers.get("Retry-After") if exc.headers else None
    if not raw:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        # The HTTP-date form is legal and unparsed here; backoff covers it.
        return None
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return min(seconds, MAX_BACKOFF)


def _path_segment(value: str) -> str:
    """Percent-encode a caller-supplied path segment.

    Note ids are the only caller-controlled part of any URL here. The https
    assertion in _get catches a rewritten scheme; it does not catch a `?` or `#`,
    which would silently turn the rest of the path into a query or fragment and
    request a different note than the one asked for.
    """
    return urllib.parse.quote(value, safe="")
