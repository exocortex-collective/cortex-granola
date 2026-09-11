"""Tests for markdown rendering.

These functions decide what a file is CALLED, and the export worker deletes by
the names they produce. A slug that changes for the same note orphans the old
file; one that collides merges two meetings into one. That is why naming is
tested here at least as heavily as the document bodies.
"""

from __future__ import annotations

import datetime as dt

import pytest

from cortex_granola.granola.render import (
    MAX_SLUG,
    _attendee_lines,
    _parse_ts,
    _speaker_label,
    _yaml_str,
    note_date,
    note_timestamp,
    relative_path,
    render_note,
    render_transcript,
    slugify,
)


def note(**overrides: object) -> dict:
    base: dict = {
        "id": "not_1d3tmYTlCICgjy",
        "title": "Quarterly yoghurt budget review",
        "created_at": "2026-01-27T15:30:00Z",
        "updated_at": "2026-01-27T16:45:00Z",
        "owner": {"name": "Oat Benson", "email": "oat@granola.ai"},
        "attendees": [],
        "folder_membership": [],
        "space_membership": [],
        "calendar_event": None,
        "summary_markdown": "## Notes\n\n- Spent money",
        "transcript": [],
    }
    base.update(overrides)
    return base


# -- slugify ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Quarterly yoghurt budget review", "quarterly-yoghurt-budget-review"),
        ("UI/UX - Dev Discussion", "ui-ux-dev-discussion"),
        ("Valid Eval/UI\\UX design Review", "valid-eval-ui-ux-design-review"),
        ("ValidEval<>Unicorns Sync", "valideval-unicorns-sync"),
        ("Ctrl+Lunch | Tech talks", "ctrl-lunch-tech-talks"),
        ("  leading and trailing  ", "leading-and-trailing"),
        ("Café déjà vu", "cafe-deja-vu"),
        ("!!!", "untitled"),
        ("", "untitled"),
        ("日本語", "untitled"),
    ],
)
def test_slugify(title: str, expected: str) -> None:
    assert slugify(title) == expected


def test_slugify_truncates_without_a_trailing_hyphen() -> None:
    # Cutting mid-separator would leave "...word-" and a filename that reads as
    # truncated garbage; the rstrip is the thing under test.
    slug = slugify("a" * 20 + " " + "b" * 80)
    assert len(slug) <= MAX_SLUG
    assert not slug.endswith("-")


def test_slugify_is_stable_for_the_same_title() -> None:
    # The export worker deletes by previously-rendered path. An unstable slug
    # would orphan a file on every run.
    assert slugify("All Hands Weekly") == slugify("All Hands Weekly")


# -- timestamps and paths --------------------------------------------------


def test_note_timestamp_prefers_the_scheduled_start() -> None:
    result = note_timestamp(
        note(
            created_at="2026-01-27T15:30:00Z",
            calendar_event={"scheduled_start_time": "2026-01-20T09:00:00Z"},
        )
    )
    assert result == dt.datetime(2026, 1, 20, 9, 0, tzinfo=dt.UTC)


def test_note_timestamp_falls_back_to_created_at() -> None:
    result = note_timestamp(note(calendar_event=None))
    assert result == dt.datetime(2026, 1, 27, 15, 30, tzinfo=dt.UTC)


def test_note_timestamp_skips_an_unparseable_scheduled_start() -> None:
    result = note_timestamp(
        note(
            calendar_event={"scheduled_start_time": "not a date"}, created_at="2026-01-27T15:30:00Z"
        )
    )
    assert result == dt.datetime(2026, 1, 27, 15, 30, tzinfo=dt.UTC)


def test_note_timestamp_is_none_when_nothing_is_usable() -> None:
    # parse._meeting_date stores NULL for this rather than a value that moves
    # on every re-parse, which only works because this returns None.
    assert note_timestamp(note(created_at=None, calendar_event=None)) is None


def test_note_date_falls_back_to_now_for_a_filename() -> None:
    before = dt.datetime.now(dt.UTC)
    result = note_date(note(created_at=None, calendar_event=None))
    assert before <= result <= dt.datetime.now(dt.UTC)


def test_relative_path_shape() -> None:
    assert relative_path(note()) == (
        "2026/2026-01-27-quarterly-yoghurt-budget-review-not_1d3tmYTlCICgjy.md"
    )


def test_relative_path_uses_the_scheduled_year_not_the_created_year() -> None:
    path = relative_path(
        note(
            created_at="2026-01-02T00:00:00Z",
            calendar_event={"scheduled_start_time": "2025-12-30T18:00:00Z"},
        )
    )
    assert path.startswith("2025/2025-12-30-")


def test_relative_path_disambiguates_same_day_same_title() -> None:
    # Two "Product planning" meetings on one day must not collide.
    first = relative_path(note(id="not_aaaaaaaaaaaaaa"))
    second = relative_path(note(id="not_bbbbbbbbbbbbbb"))
    assert first != second


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-01-27T15:30:00Z", dt.datetime(2026, 1, 27, 15, 30, tzinfo=dt.UTC)),
        ("2026-01-27T15:30:00+00:00", dt.datetime(2026, 1, 27, 15, 30, tzinfo=dt.UTC)),
        ("2026-04-02T14:00:00-06:00", dt.datetime(2026, 4, 2, 20, 0, tzinfo=dt.UTC)),
        ("not a date", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_ts(raw: str | None, expected: dt.datetime | None) -> None:
    result = _parse_ts(raw)
    if expected is None:
        assert result is None
    else:
        assert result is not None
        assert result.astimezone(dt.UTC) == expected


# -- speaker labels --------------------------------------------------------


@pytest.mark.parametrize(
    ("speaker", "expected"),
    [
        ({"name": "Alice Smith", "source": "speaker"}, "Alice Smith"),
        ({"diarization_label": "Speaker A", "source": "microphone"}, "Speaker A"),
        ({"name": "Alice", "diarization_label": "Speaker A"}, "Alice"),
        ({"attribution": "me", "source": "speaker"}, "Me"),
        ({"attribution": "them", "source": "microphone"}, "Them"),
        ({"source": "microphone"}, "Me"),
        ({"source": "speaker"}, "Them"),
        ({"source": "something-new"}, "Unknown"),
        ({}, "Unknown"),
    ],
)
def test_speaker_label_precedence(speaker: dict, expected: str) -> None:
    assert _speaker_label(speaker) == expected


@pytest.mark.parametrize("bad", [{"name": None}, {"name": ""}, {"name": 42}])
def test_speaker_label_ignores_a_non_string_name(bad: dict) -> None:
    # A null name in the payload must fall through, not become the heading
    # "None" over a block of transcript.
    assert _speaker_label({**bad, "source": "microphone"}) == "Me"


# -- attendees -------------------------------------------------------------


def test_attendee_lines_formats_name_and_email() -> None:
    result = _attendee_lines(note(attendees=[{"name": "Oat Benson", "email": "oat@granola.ai"}]))
    assert result == ["Oat Benson <oat@granola.ai>"]


def test_attendee_lines_merges_invitees_with_attendees() -> None:
    result = _attendee_lines(
        note(
            attendees=[{"name": "Oat Benson", "email": "oat@granola.ai"}],
            calendar_event={"invitees": [{"email": "raisin@granola.ai"}]},
        )
    )
    assert result == ["Oat Benson <oat@granola.ai>", "raisin@granola.ai"]


def test_attendee_lines_prefers_the_named_record_over_the_bare_invitee() -> None:
    # A calendar invitee carries only an email; the attendee record for the
    # same person carries a name and must win the dedup.
    result = _attendee_lines(
        note(
            attendees=[{"name": "Oat Benson", "email": "oat@granola.ai"}],
            calendar_event={"invitees": [{"email": "oat@granola.ai"}]},
        )
    )
    assert result == ["Oat Benson <oat@granola.ai>"]


def test_attendee_lines_dedups_case_insensitively() -> None:
    result = _attendee_lines(
        note(
            attendees=[{"name": "Oat", "email": "Oat@Granola.ai"}],
            calendar_event={"invitees": [{"email": "oat@granola.ai"}]},
        )
    )
    assert len(result) == 1


def test_attendee_lines_drops_records_with_neither_name_nor_email() -> None:
    assert _attendee_lines(note(attendees=[{"name": "", "email": ""}, {}])) == []


def test_attendee_lines_are_sorted() -> None:
    result = _attendee_lines(
        note(attendees=[{"email": "zeta@x.com"}, {"email": "alpha@x.com"}, {"email": "mid@x.com"}])
    )
    assert result == sorted(result)


# -- yaml escaping ---------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("plain", '"plain"'),
        ('has "quotes"', '"has \\"quotes\\""'),
        ("back\\slash", '"back\\\\slash"'),
        ("two\nlines", '"two lines"'),
        ("carriage\rreturn", '"carriage return"'),
    ],
)
def test_yaml_str_escaping(raw: str, expected: str) -> None:
    assert _yaml_str(raw) == expected


def test_frontmatter_survives_a_title_full_of_quotes() -> None:
    # A raw quote here would break every downstream YAML parser reading the vault.
    rendered = render_note(
        note(title='He said "hello" \\ goodbye'), include_private_notes=False, transcript_link=None
    )
    assert '\\"hello\\"' in rendered
    assert rendered.count("---") >= 2


# -- documents -------------------------------------------------------------


def test_render_note_omits_calendar_fields_when_there_is_no_event() -> None:
    rendered = render_note(
        note(calendar_event=None), include_private_notes=False, transcript_link=None
    )
    assert "start_time:" not in rendered
    assert "end_time:" not in rendered


def test_render_note_includes_calendar_fields_when_present() -> None:
    rendered = render_note(
        note(
            calendar_event={
                "scheduled_start_time": "2026-01-27T15:30:00Z",
                "scheduled_end_time": "2026-01-27T16:30:00Z",
                "invitees": [],
            }
        ),
        include_private_notes=False,
        transcript_link=None,
    )
    assert "start_time: " in rendered
    assert "end_time: " in rendered


def test_render_note_omits_end_time_when_only_a_start_is_known() -> None:
    rendered = render_note(
        note(calendar_event={"scheduled_start_time": "2026-01-27T15:30:00Z", "invitees": []}),
        include_private_notes=False,
        transcript_link=None,
    )
    assert "start_time: " in rendered
    assert "end_time: " not in rendered


def test_render_note_hides_private_notes_unless_asked() -> None:
    payload = note(private_notes_markdown="- Push back on the dairy forecast")
    assert "dairy forecast" not in render_note(
        payload, include_private_notes=False, transcript_link=None
    )
    assert "dairy forecast" in render_note(
        payload, include_private_notes=True, transcript_link=None
    )


def test_render_note_says_so_when_there_is_no_summary() -> None:
    rendered = render_note(
        note(summary_markdown=None, summary_text=None),
        include_private_notes=False,
        transcript_link=None,
    )
    assert "_No summary available._" in rendered


def test_render_note_links_the_transcript_only_when_one_exists() -> None:
    linked = render_note(note(), include_private_notes=False, transcript_link="../../t/x.md")
    assert "[Full transcript](../../t/x.md)" in linked
    assert "[Full transcript]" not in render_note(
        note(), include_private_notes=False, transcript_link=None
    )


def test_render_transcript_groups_consecutive_lines_by_speaker() -> None:
    rendered = render_transcript(
        note(
            transcript=[
                {"text": "First.", "speaker": {"source": "microphone"}},
                {"text": "Still me.", "speaker": {"source": "microphone"}},
                {"text": "Their turn.", "speaker": {"source": "speaker"}},
            ]
        ),
        note_link="../../n/x.md",
    )
    assert rendered.count("**Me**") == 1
    assert "First.\nStill me." in rendered
    assert "**Them**" in rendered


def test_render_transcript_skips_empty_segments() -> None:
    rendered = render_transcript(
        note(
            transcript=[
                {"text": "   ", "speaker": {"source": "microphone"}},
                {"text": "Real content.", "speaker": {"source": "microphone"}},
            ]
        ),
        note_link="../../n/x.md",
    )
    # A blank segment must not open its own speaker block.
    assert rendered.count("**Me**") == 1


def test_both_documents_carry_the_granola_id_and_their_kind() -> None:
    summary = render_note(note(), include_private_notes=False, transcript_link=None)
    transcript = render_transcript(note(transcript=[]), note_link="../../n/x.md")
    for doc, kind in ((summary, "summary"), (transcript, "transcript")):
        assert "granola_id: not_1d3tmYTlCICgjy" in doc
        assert f"kind: {kind}" in doc
