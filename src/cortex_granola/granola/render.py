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
    event = note.get("calendar_event") or {}
    for value in (event.get("scheduled_start_time"), note.get("created_at")):
        parsed = _parse_ts(value)
        if parsed:
            return parsed
    return None


def note_date(note: JsonDict) -> dt.datetime:
    """Prefer the scheduled meeting time; fall back to now for a filename.

    The fallback exists because a file needs a name even when a note has no
    timestamp. It must not reach the database -- see parse._meeting_date, which
    stores NULL for exactly this case rather than a value that changes on every
    re-parse.
    """
    return note_timestamp(note) or dt.datetime.now(dt.UTC)


def relative_path(note: JsonDict) -> str:
    """Path within either output tree; both use the same shape."""
    when = note_date(note)
    slug = slugify(note.get("title") or "untitled")
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
        _render_transcript(note.get("transcript") or []),
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
    event = note.get("calendar_event") or {}
    folders = [f.get("name") for f in note.get("folder_membership") or [] if f.get("name")]
    spaces = [s.get("name") for s in note.get("space_membership") or [] if s.get("name")]
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


def _render_transcript(items: list[JsonDict]) -> str:
    lines: list[str] = []
    last_speaker = None
    for item in items:
        speaker = _speaker_label(item.get("speaker") or {})
        text = (item.get("text") or "").strip()
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
    for invitee in (note.get("calendar_event") or {}).get("invitees") or []:
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


def _yaml_list(key: str, values: list[str]) -> str:
    if not values:
        return f"{key}: []"
    return "\n".join([f"{key}:"] + [f"  - {_yaml_str(v)}" for v in values])


def _yaml_str(value: str) -> str:
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    escaped = escaped.replace("\n", " ").replace("\r", " ")
    return f'"{escaped}"'


def _parse_ts(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
