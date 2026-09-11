"""Thin client for the Granola public API (https://public-api.granola.ai).

Stdlib only, so the syncer runs from cron without a virtualenv.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from typing import Any

log = logging.getLogger(__name__)

# Granola's payloads are JSON objects whose shape the API owns, so the client
# carries them as plain dicts and lets parse.py impose meaning. The alias keeps
# that decision legible rather than spelling `dict[str, Any]` out sixteen times.
JsonDict = dict[str, Any]

BASE_URL = "https://public-api.granola.ai"

# Documented limits: 25 request burst, 5 requests/second sustained.
RATE_PER_SEC = 5.0
BURST = 25

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
    def __init__(self, rate: float, burst: int) -> None:
        self.rate = rate
        self.capacity = burst
        self.tokens = float(burst)
        self.updated = time.monotonic()

    def take(self) -> None:
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
        self.updated = now
        if self.tokens < 1.0:
            time.sleep((1.0 - self.tokens) / self.rate)
            self.tokens = 0.0
            self.updated = time.monotonic()
        else:
            self.tokens -= 1.0


class GranolaClient:
    def __init__(self, api_key: str, timeout: int = 60, max_retries: int = 5) -> None:
        if not api_key:
            raise ValueError("GRANOLA_API_KEY is empty")
        self._key = api_key
        self._timeout = timeout
        self._max_retries = max_retries
        self._bucket = _TokenBucket(RATE_PER_SEC, BURST)

    # -- transport ---------------------------------------------------------

    def _get(self, path: str, params: JsonDict | None = None) -> JsonDict:
        query = {k: v for k, v in (params or {}).items() if v is not None}
        url = f"{BASE_URL}{path}"
        if query:
            url += "?" + urllib.parse.urlencode(query)

        # Note ids reach this path (`/v1/notes/{id}`), and the base URL is the
        # only thing fixing the scheme. urlopen honours file:// and custom
        # schemes, so a crafted id that escaped the path would read local files
        # rather than fail. Asserting the scheme costs nothing and closes it.
        if not url.startswith("https://"):
            raise GranolaError(0, f"refusing a non-https request URL: {url!r}")

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
                    payload = json.load(resp)
                # An HTML error page or a bare JSON array would otherwise fail
                # much later, as an AttributeError inside a caller.
                if not isinstance(payload, dict):
                    raise GranolaError(
                        0, f"expected a JSON object from {path}, got {type(payload).__name__}"
                    )
                return payload
            except urllib.error.HTTPError as exc:
                with exc:
                    body = exc.read().decode("utf-8", "replace")
                code = _error_code(body)
                if exc.code == 413 or code == "TRANSCRIPT_TOO_LARGE":
                    raise TranscriptTooLarge(exc.code, body, code) from exc
                retryable = exc.code == 429 or exc.code >= 500
                if not retryable or attempt == self._max_retries:
                    raise GranolaError(exc.code, body, code) from exc
                delay = _retry_after(exc) or min(60.0, 2.0**attempt)
                log.warning("HTTP %s on %s; retrying in %.1fs", exc.code, path, delay)
                time.sleep(delay)
            except urllib.error.URLError as exc:
                if attempt == self._max_retries:
                    raise
                delay = min(60.0, 2.0**attempt)
                log.warning("network error on %s (%s); retrying in %.1fs", path, exc.reason, delay)
                time.sleep(delay)

        raise AssertionError("unreachable")

    def _paginate(self, path: str, key: str, params: JsonDict) -> Iterator[JsonDict]:
        cursor: str | None = None
        while True:
            page = self._get(path, {**params, "cursor": cursor})
            items = page.get(key) or []
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
            f"/v1/notes/{note_id}",
            {"include": "transcript"} if include_transcript else None,
        )

    def get_transcript(self, note_id: str) -> list[JsonDict]:
        return list(
            self._paginate(
                f"/v1/notes/{note_id}/transcript",
                "transcript",
                {"page_size": TRANSCRIPT_PAGE_SIZE},
            )
        )


def _error_code(body: str) -> str | None:
    try:
        parsed = json.loads(body)
    except ValueError:
        return "TRANSCRIPT_TOO_LARGE" if "TRANSCRIPT_TOO_LARGE" in body else None
    if isinstance(parsed, dict):
        for field in ("code", "error", "error_code", "message"):
            value = parsed.get(field)
            if isinstance(value, str) and value:
                return value
    return None


def _retry_after(exc: urllib.error.HTTPError) -> float | None:
    raw = exc.headers.get("Retry-After") if exc.headers else None
    try:
        return float(raw) if raw else None
    except ValueError:
        return None
