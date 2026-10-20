"""Render a Granola note as markdown with YAML frontmatter.

Each meeting produces two discrete files: the AI summary under `notes/` and the
raw transcript under `transcripts/`, cross-linked by relative path. Keeping them
apart means a verbatim transcript never lands in a summary diff, and the two can
be reviewed, grepped, or pruned independently.
"""

from __future__ import annotations

import datetime as dt
import re
import unicodedata

from cortex_granola.granola.api import JsonDict

MAX_SLUG = 60


class MalformedNote(ValueError):
    """A note payload whose shape makes a faithful rendering impossible.

    The rule this file follows, and the reason this exists: an ABSENT field is
    ordinary -- the API documents nullable titles, and a note can have no
    folders -- and gets a documented fallback. A field of the WRONG TYPE is a
    payload nobody can render honestly, and it must stop the export.

    Silently dropping it is the one thing that must not happen. render is
    upstream of export._record, which upserts whatever it is handed with
    written_at = NOW(); a header-only document written over a good file reports
    success while destroying the content it replaced. The crash this class
    replaces was ugly but safe, because it left the old file and a traceback.

    This closes render's half. Export's half -- deciding which files it wants
    by type rather than truthiness -- is export._has_transcript, and the export
    loop no longer counts an error as progress. What remains open is that
    export writes the summary before rendering the transcript (Cortex-d2n).
    """


# Speaker source when nothing better identifies who was talking. `microphone`
# is the note-taker's own audio stream; `speaker` is everyone else's.
_SOURCE_LABELS = {"microphone": "Me", "speaker": "Them"}


def note_timestamp(note: JsonDict) -> dt.datetime | None:
    """The meeting's instant, or None when the note carries no usable one.

    The single definition of that precedence. It had two: this one and
    parse.py's, which meant a future change to the order would move where a
    note is FILED without moving the meeting_date it is filed UNDER -- the same
    note under two different dates depending on which code path you asked.
    """
    return scheduled_start(note) or _parse_ts(note.get("created_at"))


def scheduled_start(note: JsonDict) -> dt.datetime | None:
    """The calendar event's start, or None when it is missing or unparseable.

    Public so parse.py can tell a calendar start from the created_at fallback:
    a scheduled end only belongs beside a scheduled start.
    """
    return _parse_ts(calendar_event(note).get("scheduled_start_time"))


def calendar_event(note: JsonDict) -> JsonDict:
    """The calendar event, or {} when there is none. A wrong type raises.

    Truthiness here decided where a note is FILED: note_date falls back to
    created_at when the event has no start time, and relative_path is built
    from that date. A falsy wrong-typed event -- [], "", 0 -- read as "no
    event", the date moved, the path moved, and export wrote the file under a
    new name and removed the old. Content intact, so a move rather than a loss,
    but the same asymmetry as the transcript deletion: the truthy wrong types
    already raised, the falsy ones acted.
    """
    event = note.get("calendar_event")
    if event is None:
        return {}
    if not isinstance(event, dict):
        raise MalformedNote(f"calendar_event must be an object, got {type(event).__name__}")
    return event


def note_date(note: JsonDict) -> dt.datetime:
    """Prefer the scheduled meeting time; fall back to now for a filename.

    The fallback exists because a file needs a name even when a note has no
    timestamp.

    It does NOT stay out of the database, and an earlier version of this
    docstring said it did. parse._meeting_date is clean -- it calls
    note_timestamp() and stores NULL for a timestamp-less note. But
    relative_path() also feeds export._wanted_paths(), and export._record()
    writes that %Y/%Y-%m-%d path into granola_exported_files.relative_path in
    the same statement that sets written_at from the SERVER clock. Two clocks,
    one row. Worse, export deletes by comparing old path to new, so a re-export
    that crosses a client-clock UTC boundary unlinks the old file and walks the
    note into a different directory while meeting_date stays NULL.

    Tracked as Cortex-px6. Fixing it means export deciding the fallback, not
    this function.
    """
    return note_timestamp(note) or dt.datetime.now(dt.UTC)


def relative_path(note: JsonDict) -> str:
    """Path within either output tree; both use the same shape."""
    when = note_date(note)
    slug = slugify(_text(note, "title") or "untitled")
    # The note ID keeps the name stable and unique when two meetings on the
    # same day share a title.
    return f"{when:%Y}/{when:%Y-%m-%d}-{slug}-{note['id']}.md"


def slugify(title: str) -> str:
    normalized = unicodedata.normalize("NFKD", title)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_only).strip("-").lower()
    return (slug[:MAX_SLUG].rstrip("-")) or "untitled"


def render_note(note: JsonDict, *, include_private_notes: bool, transcript_link: str | None) -> str:
    """The AI summary document."""
    body: list[str] = [
        _frontmatter(
            note,
            kind="summary",
            extra=[("transcript_file", transcript_link)] if transcript_link else [],
        ),
        "",
        f"# {note.get('title') or 'Untitled'}",
        "",
    ]

    summary = note.get("summary_markdown") or note.get("summary_text")
    body += [summary.strip(), ""] if summary else ["_No summary available._", ""]

    private = note.get("private_notes_markdown") or note.get("private_notes_text")
    if include_private_notes and private:
        body += ["## Private notes", "", private.strip(), ""]

    if transcript_link:
        body += [f"[Full transcript]({transcript_link})", ""]

    return "\n".join(body).rstrip() + "\n"


def render_transcript(note: JsonDict, *, note_link: str) -> str:
    """The verbatim transcript document."""
    body: list[str] = [
        _frontmatter(note, kind="transcript", extra=[("summary_file", note_link)]),
        "",
        f"# {note.get('title') or 'Untitled'} — transcript",
        "",
        f"[Summary]({note_link})",
        "",
        _render_transcript(note.get("transcript")),
        "",
    ]
    return "\n".join(body).rstrip() + "\n"


def _frontmatter(note: JsonDict, *, kind: str, extra: list[tuple[str, str]]) -> str:
    """The YAML block both renderers share.

    `extra` is appended verbatim so each renderer can cross-link to the other
    (summary <-> transcript) without this helper knowing which is which.
    Calendar-event fields appear only when the note has an event; a note
    created outside a meeting has neither start nor end.
    """
    event = calendar_event(note)
    folders = _member_names(note.get("folder_membership"), "folder_membership")
    spaces = _member_names(note.get("space_membership"), "space_membership")
    owner = note.get("owner") or {}

    lines: list[str] = [
        "---",
        f"granola_id: {note['id']}",
        f"kind: {kind}",
        f"title: {_yaml_str(note.get('title') or 'Untitled')}",
        f"date: {note_date(note):%Y-%m-%d}",
    ]
    if event.get("scheduled_start_time"):
        lines.append(f"start_time: {_yaml_str(event['scheduled_start_time'])}")
        if event.get("scheduled_end_time"):
            lines.append(f"end_time: {_yaml_str(event['scheduled_end_time'])}")
    lines += [
        f"created_at: {_yaml_str(note.get('created_at') or '')}",
        f"updated_at: {_yaml_str(note.get('updated_at') or '')}",
        f"owner: {_yaml_str(owner.get('email') or owner.get('name') or '')}",
        _yaml_list("folders", folders),
        _yaml_list("spaces", spaces),
        _yaml_list("attendees", _attendee_lines(note)),
    ]
    if note.get("web_url"):
        lines.append(f"web_url: {_yaml_str(note['web_url'])}")
    lines += [f"{key}: {_yaml_str(value)}" for key, value in extra]
    lines.append("---")
    return "\n".join(lines)


def _render_transcript(items: object) -> str:
    """Render transcript segments, or raise if the payload is not renderable.

    Typed `object`, not list[JsonDict]: the caller reads this straight from a
    payload. Annotating it as a list of dicts is what let a non-dict segment
    reach .strip() -- mypy believed the annotation and stopped checking.

    A missing or empty text field skips that segment, which is ordinary. A
    wrong-TYPED one raises: returning "" here would hand export a header-only
    transcript to write over a complete one.

    The None check is the only one -- the caller passes note.get("transcript")
    straight through rather than collapsing it with `or []`, which would leave
    this branch unreachable and untestable.
    """
    if items is None:
        return ""
    if not isinstance(items, list):
        raise MalformedNote(f"transcript must be a list, got {type(items).__name__}")
    lines: list[str] = []
    last_speaker = None
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise MalformedNote(
                f"transcript segment {index} must be an object, got {type(item).__name__}"
            )
        raw_speaker = item.get("speaker")
        if raw_speaker is not None and not isinstance(raw_speaker, dict):
            raise MalformedNote(
                f"transcript segment {index} speaker must be an object, "
                f"got {type(raw_speaker).__name__}"
            )
        speaker = _speaker_label(raw_speaker or {})
        raw_text = item.get("text")
        if raw_text is not None and not isinstance(raw_text, str):
            raise MalformedNote(
                f"transcript segment {index} text must be a string, got {type(raw_text).__name__}"
            )
        text = (raw_text or "").strip()
        if not text:
            continue
        if speaker != last_speaker:
            if lines:
                lines.append("")
            lines.append(f"**{speaker}**")
            last_speaker = speaker
        lines.append(text)
    return "\n".join(lines)


def _speaker_label(speaker: JsonDict) -> str:
    """Best available name for whoever spoke, most specific first.

    A resolved name beats a diarization label, which beats the me/them
    attribution, which beats the raw audio source. Every rung can be absent:
    iOS notes carry only a `diarization_label`, and older ones only `source`.
    """
    for field in ("name", "diarization_label"):
        value = speaker.get(field)
        if isinstance(value, str) and value:
            return value

    attribution = speaker.get("attribution")
    if attribution == "me":
        return "Me"
    if attribution == "them":
        return "Them"

    source = speaker.get("source")
    if isinstance(source, str):
        return _SOURCE_LABELS.get(source, "Unknown")
    return "Unknown"


def _attendee_lines(note: JsonDict) -> list[str]:
    seen: dict[str, str] = {}
    sources = list(note.get("attendees") or [])
    for invitee in calendar_event(note).get("invitees") or []:
        sources.append(invitee)
    for person in sources:
        email = (person.get("email") or "").strip()
        name = (person.get("name") or "").strip()
        key = email.lower() or name.lower()
        if not key:
            continue
        label = f"{name} <{email}>" if name and email else (email or name)
        # A calendar invitee carries only an email; a matching attendee record
        # with a real name should win.
        if key not in seen or len(label) > len(seen[key]):
            seen[key] = label
    return sorted(seen.values())


def _member_names(members: object, field: str) -> list[str]:
    """Names from a folder/space membership list, or raise if it is not one.

    Typed `object` rather than list[JsonDict] because the caller reads it from a
    payload: the annotation would be a claim, and this function's job is to stop
    making that claim on the export path.

    A member with no name is skipped -- that is a real shape. A non-list, or a
    member that is not an object, raises: these names are the note's only record
    in the vault of where it lived, and a reader grepping for a folder would get
    a wrong answer rather than an error.

    Not a deny-list concern, despite an earlier version of this docstring saying
    so: sync skips denied notes before render ever sees them.
    """
    if members is None:
        return []
    if not isinstance(members, list):
        raise MalformedNote(f"{field} must be a list, got {type(members).__name__}")
    names = []
    for index, member in enumerate(members):
        if not isinstance(member, dict):
            raise MalformedNote(f"{field}[{index}] must be an object, got {type(member).__name__}")
        name = member.get("name")
        if name is None:
            continue
        if not isinstance(name, str):
            raise MalformedNote(
                f"{field}[{index}].name must be a string, got {type(name).__name__}"
            )
        if name:
            names.append(name)
    return names


def _text(note: JsonDict, field: str) -> str | None:
    """A string field from a payload: None when absent, raise when wrong-typed."""
    value = note.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise MalformedNote(f"{field} must be a string, got {type(value).__name__}")
    return value


def _yaml_list(key: str, values: list[str]) -> str:
    if not values:
        return f"{key}: []"
    return "\n".join([f"{key}:"] + [f"  - {_yaml_str(v)}" for v in values])


def _yaml_str(value: object) -> str:
    """Quote and escape a scalar for YAML. Accepts object: callers pass values
    read from a payload, and the str() here was already doing the coercion the
    old `value: str` annotation denied."""
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    escaped = escaped.replace("\n", " ").replace("\r", " ")
    return f'"{escaped}"'


def _parse_ts(value: object) -> dt.datetime | None:
    """Parse an RFC3339 timestamp, preserving the offset as written.

    Deliberately does NOT normalise to UTC. meeting_date is a DATE column filled
    from .date() on this result (parse.py), and the filename is a %Y-%m-%d
    strftime of it (relative_path) -- different mechanisms, same exposure:
    converting here would move a note across a day boundary for any offset whose
    local date differs from its UTC date.

    Annotated `object` because callers pass raw payload values; the str() below
    was already coercing whatever arrived.

    No `Z` -> `+00:00` substitution: fromisoformat has accepted the designator
    natively since 3.11, and pyproject requires >=3.11. The old replace() read
    as a live 3.10 shim and was a provably equivalent mutant.
    """
    if not value or not isinstance(value, str):
        return None
    try:
        return dt.datetime.fromisoformat(value)
    except ValueError:
        return None
