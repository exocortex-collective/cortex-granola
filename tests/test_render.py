"""Tests for markdown rendering.

These functions decide what a file is CALLED, and the export worker deletes by
the names they produce. A slug that changes for the same note orphans the old
file; one that collides merges two meetings into one. That is why naming is
tested here at least as heavily as the document bodies.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from cortex_granola.granola.render import (
    MAX_SLUG,
    MalformedNote,
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


def note(**overrides: object) -> dict[str, Any]:
    base: dict[str, Any] = {
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


def test_slugify_is_pinned_for_a_real_folder_name() -> None:
    """The export worker deletes by previously-rendered path, so a slug that
    changes orphans the old file and re-writes the note under a new name.

    Pinned to a literal rather than asserting slugify(x) == slugify(x), which is
    true of any pure function -- including one whose body is `return "MUTANT"`.
    """
    assert slugify("All Hands Weekly") == "all-hands-weekly"


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


def test_note_date_falls_back_to_utc_now_for_a_filename() -> None:
    """The fallback must be UTC, not merely the right instant.

    Both consumers format it as %Y-%m-%d. An aware-vs-aware bracket compares
    instants and ignores tzinfo, so it stays green if the fallback becomes
    now(ZoneInfo("America/Denver")) -- same instant, filename off by a day for
    six hours out of every twenty-four.
    """
    result = note_date(note(created_at=None, calendar_event=None))
    assert result.utcoffset() == dt.timedelta(0)
    assert abs((result - dt.datetime.now(dt.UTC)).total_seconds()) < 60


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
        ("not a date", None),
        ("", None),
        (None, None),
        (42, None),
    ],
)
def test_parse_ts(raw: object, expected: dt.datetime | None) -> None:
    result = _parse_ts(raw)
    if expected is None:
        assert result is None
    else:
        assert result == expected


def test_parse_ts_preserves_the_offset_as_written() -> None:
    """The offset must survive, because .date() is taken from this result.

    meeting_date is a DATE column filled from .date() on what this returns, and
    the filename is a %Y-%m-%d strftime of the same value. An earlier version
    asserted through
    .astimezone(UTC), which is a no-op for equality between aware datetimes and
    normalised away the one property under test -- so a trailing
    .astimezone(dt.UTC) inside _parse_ts would have gone unnoticed while moving
    every evening meeting to the next day.
    """
    result = _parse_ts("2026-04-02T20:00:00-06:00")
    assert result is not None
    assert result.utcoffset() == dt.timedelta(hours=-6)
    assert result.date() == dt.date(2026, 4, 2)
    assert result.astimezone(dt.UTC).date() == dt.date(2026, 4, 3)


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
def test_speaker_label_precedence(speaker: dict[str, Any], expected: str) -> None:
    assert _speaker_label(speaker) == expected


@pytest.mark.parametrize("bad", [{"name": None}, {"name": ""}, {"name": 42}])
def test_speaker_label_ignores_a_non_string_name(bad: dict[str, Any]) -> None:
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
    """Pinned to the expected order, not to sorted(result).

    `result == sorted(result)` is satisfied by `return []`, so the only
    ordering test in the file passed with the function deleted.
    """
    result = _attendee_lines(
        note(attendees=[{"email": "zeta@x.com"}, {"email": "alpha@x.com"}, {"email": "mid@x.com"}])
    )
    assert result == ["alpha@x.com", "mid@x.com", "zeta@x.com"]


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


def test_frontmatter_renders_populated_lists() -> None:
    """_yaml_list's non-empty branch, which the empty fixture never reached.

    _attendee_lines is well covered in isolation, but nothing checked that its
    output survives into a document with the right indentation and quoting.
    """
    rendered = render_note(
        note(
            attendees=[{"name": 'Oat "The Boss" Benson', "email": "oat@granola.ai"}],
            folder_membership=[{"id": "fol_x", "name": "Bus Dev"}],
            space_membership=[{"id": "spa_x", "name": "Valid Eval team"}],
        ),
        include_private_notes=False,
        transcript_link=None,
    )
    assert '  - "Oat \\"The Boss\\" Benson <oat@granola.ai>"' in rendered
    assert 'folders:\n  - "Bus Dev"' in rendered
    assert 'spaces:\n  - "Valid Eval team"' in rendered


def test_frontmatter_skips_members_with_no_name() -> None:
    # A member without a name is a real shape, not a malformed one.
    rendered = render_note(
        note(folder_membership=[{"id": "fol_x"}, {"name": None}, {"name": "Real"}]),
        include_private_notes=False,
        transcript_link=None,
    )
    assert 'folders:\n  - "Real"' in rendered


@pytest.mark.parametrize(
    ("membership", "expected"),
    [
        (42, "folder_membership must be a list"),
        ("a string", "folder_membership must be a list"),
        ({"not": "a list"}, "folder_membership must be a list"),
        (["not a dict"], r"folder_membership\[0\] must be an object"),
        ([{"name": "ok"}, "not a dict"], r"folder_membership\[1\] must be an object"),
        ([{"name": 42}], r"folder_membership\[0\]\.name must be a string"),
        ([{"name": ["x"]}], r"folder_membership\[0\]\.name must be a string"),
    ],
)
def test_frontmatter_rejects_malformed_membership(membership: object, expected: str) -> None:
    """Wrong-typed membership must stop the export, not quietly vanish.

    Dropping it silently would write the note into the vault with its folder
    metadata missing -- and since the deny list is expressed in folders, a note
    whose folders disappeared is exactly the note that must not be written.
    """
    with pytest.raises(MalformedNote, match=expected):
        render_note(
            note(folder_membership=membership), include_private_notes=False, transcript_link=None
        )


def test_frontmatter_accepts_absent_membership() -> None:
    rendered = render_note(
        note(folder_membership=None, space_membership=None),
        include_private_notes=False,
        transcript_link=None,
    )
    assert "folders: []" in rendered


def test_render_note_includes_web_url_when_present() -> None:
    rendered = render_note(
        note(web_url="https://notes.granola.ai/d/abc"),
        include_private_notes=False,
        transcript_link=None,
    )
    assert 'web_url: "https://notes.granola.ai/d/abc"' in rendered


def test_render_note_omits_web_url_when_absent() -> None:
    rendered = render_note(note(web_url=None), include_private_notes=False, transcript_link=None)
    assert "web_url:" not in rendered


@pytest.mark.parametrize("absent_title", [None, ""])
def test_relative_path_falls_back_to_untitled_for_an_absent_title(absent_title: object) -> None:
    # The API documents title as nullable, so absence is ordinary.
    assert relative_path(note(title=absent_title)).endswith("untitled-not_1d3tmYTlCICgjy.md")


@pytest.mark.parametrize("bad_title", [42, {"nested": "dict"}, ["list"], 3.5])
def test_relative_path_rejects_a_wrong_typed_title(bad_title: object) -> None:
    """A non-string title reached unicodedata.normalize and raised TypeError.

    It now raises MalformedNote instead -- but it must still RAISE. Falling back
    to "untitled" would file the note at a path derived from nothing, and the
    export worker deletes by previously-rendered path.
    """
    with pytest.raises(MalformedNote, match="title"):
        relative_path(note(title=bad_title))


@pytest.mark.parametrize(
    ("segments", "expected"),
    [
        ("not a list", "transcript must be a list"),
        (42, "transcript must be a list"),
        ({"not": "a list"}, "transcript must be a list"),
        (["not a dict"], "segment 0 must be an object"),
        ([{"text": 42, "speaker": {"source": "microphone"}}], "segment 0 text must be a string"),
        ([{"text": "ok", "speaker": "not a dict"}], "segment 0 speaker must be an object"),
        ([{"text": "fine"}, {"text": ["bad"]}], "segment 1 text must be a string"),
    ],
)
def test_render_transcript_rejects_malformed_segments(segments: object, expected: str) -> None:
    """Malformed transcript data must stop the export, not shorten the file.

    An earlier version of this guard returned "" or skipped the segment. That
    is worse than the AttributeError it replaced: render is upstream of
    export._record, which upserts whatever it is given with written_at = NOW(),
    so a header-only transcript reports success while overwriting a complete
    one. The crash at least left the old file intact.
    """
    with pytest.raises(MalformedNote, match=expected):
        render_transcript(note(transcript=segments), note_link="../../n/x.md")


@pytest.mark.parametrize(
    "segment", [{"speaker": {"source": "microphone"}}, {"text": None}, {"text": "  "}]
)
def test_render_transcript_skips_segments_with_no_text(segment: dict[str, Any]) -> None:
    # Absent text is ordinary; only a wrong TYPE is fatal.
    rendered = render_transcript(note(transcript=[segment]), note_link="../../n/x.md")
    assert "transcript" in rendered


def test_render_transcript_keeps_the_line_when_one_speaker_record_is_absent() -> None:
    """A segment with no speaker must still contribute its text.

    Guarding by skipping the segment would cost a line of transcript over a
    missing label -- and a test that only asserts "no crash" passes when it does.
    """
    rendered = render_transcript(
        note(
            transcript=[
                {"text": "Alice speaking.", "speaker": {"name": "Alice"}},
                {"text": "Said something.", "speaker": None},
            ]
        ),
        note_link="../../n/x.md",
    )
    assert "Said something." in rendered
    # And it must not inherit the previous speaker's name: falling back to
    # `last_speaker` keeps the line but attributes it to Alice, which is worse
    # than losing it. An absent speaker record is Unknown, not the last one.
    assert rendered.index("**Unknown**") > rendered.index("**Alice**")


def test_both_documents_carry_the_granola_id_and_their_kind() -> None:
    summary = render_note(note(), include_private_notes=False, transcript_link=None)
    transcript = render_transcript(note(transcript=[]), note_link="../../n/x.md")
    for doc, kind in ((summary, "summary"), (transcript, "transcript")):
        assert "granola_id: not_1d3tmYTlCICgjy" in doc
        assert f"kind: {kind}" in doc


# -- the `or` fallbacks coverage.py cannot see ------------------------------
#
# coverage.py does not branch on `x or y` inside an expression, so every one of
# these reported as covered while never executing. The shape that reaches them
# is a note created outside a meeting: no title, no calendar event, an owner
# with no name.


def test_untitled_note_renders_untitled_everywhere() -> None:
    payload = note(title=None)
    summary = render_note(payload, include_private_notes=False, transcript_link=None)
    transcript = render_transcript(
        note(title=None, transcript=[{"text": "hi", "speaker": {"source": "microphone"}}]),
        note_link="../../n/x.md",
    )
    assert 'title: "Untitled"' in summary
    assert "# Untitled" in summary
    assert "# Untitled — transcript" in transcript


def test_owner_falls_back_to_name_when_there_is_no_email() -> None:
    rendered = render_note(
        note(owner={"name": "Oat Benson", "email": None}),
        include_private_notes=False,
        transcript_link=None,
    )
    assert 'owner: "Oat Benson"' in rendered


def test_owner_prefers_email_over_name() -> None:
    rendered = render_note(
        note(owner={"name": "Oat Benson", "email": "oat@granola.ai"}),
        include_private_notes=False,
        transcript_link=None,
    )
    assert 'owner: "oat@granola.ai"' in rendered


def test_owner_is_empty_when_neither_is_present() -> None:
    rendered = render_note(note(owner={}), include_private_notes=False, transcript_link=None)
    assert 'owner: ""' in rendered


def test_summary_falls_back_to_plain_text() -> None:
    rendered = render_note(
        note(summary_markdown=None, summary_text="Plain text summary."),
        include_private_notes=False,
        transcript_link=None,
    )
    assert "Plain text summary." in rendered


def test_summary_prefers_markdown_over_plain_text() -> None:
    # Reversing the pair silently replaces every formatted summary in the vault
    # with its flattened twin, and nothing else would notice.
    rendered = render_note(
        note(summary_markdown="## Formatted", summary_text="Flattened"),
        include_private_notes=False,
        transcript_link=None,
    )
    assert "## Formatted" in rendered
    assert "Flattened" not in rendered


def test_private_notes_fall_back_to_plain_text() -> None:
    rendered = render_note(
        note(private_notes_markdown=None, private_notes_text="Plain private note."),
        include_private_notes=True,
        transcript_link=None,
    )
    assert "Plain private note." in rendered


def test_private_notes_prefer_markdown_over_plain_text() -> None:
    rendered = render_note(
        note(private_notes_markdown="- Formatted", private_notes_text="Flattened"),
        include_private_notes=True,
        transcript_link=None,
    )
    assert "- Formatted" in rendered
    assert "Flattened" not in rendered


def test_a_member_with_an_empty_name_is_skipped() -> None:
    # The false arm of `if name:` -- no param supplied {"name": ""}, so removing
    # the guard entirely survived.
    rendered = render_note(
        note(folder_membership=[{"name": ""}, {"name": "Real"}]),
        include_private_notes=False,
        transcript_link=None,
    )
    assert 'folders:\n  - "Real"' in rendered
    assert '- ""' not in rendered


def test_an_absent_transcript_renders_no_body() -> None:
    # The `items is None` branch, now reachable: the caller no longer collapses
    # None with `or []`.
    rendered = render_transcript(note(transcript=None), note_link="../../n/x.md")
    assert rendered.rstrip().endswith("[Summary](../../n/x.md)")


def test_absent_timestamps_render_as_empty_strings() -> None:
    rendered = render_note(
        note(created_at=None, updated_at=None), include_private_notes=False, transcript_link=None
    )
    assert 'created_at: ""' in rendered
    assert 'updated_at: ""' in rendered
