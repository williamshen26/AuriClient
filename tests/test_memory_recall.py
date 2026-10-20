"""Pure recall helpers: terms, date hints, subjects, note formatting."""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from custom_components.hey_auri_client.memory import recall
from custom_components.hey_auri_client.memory.recall import RecallRow, SubjectEntity

TODAY = date(2026, 10, 14)  # a Wednesday


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("yes", False),
        ("no thanks", False),
        ("thank you", False),
        ("what can Mia eat", True),
        ("turn on the lights", True),
        ("never mind", False),
    ],
)
def test_should_auto_recall(text, expected):
    assert recall.should_auto_recall(text) is expected


def test_extract_terms_drops_stopwords_and_possessives():
    assert recall.extract_terms("What's Mia's favourite ice cream flavour again?") == [
        "mia", "favourite", "ice", "cream", "flavour",
    ]
    assert recall.extract_terms("Do you remember where I put the passports?") == ["put", "passports"]
    assert recall.extract_terms("Set it to 21 degrees") == ["set", "21", "degrees"]
    assert len(recall.extract_terms(" ".join(f"word{i}" for i in range(20)))) == 8


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("what did we talk about yesterday", [date(2026, 10, 13)]),
        ("the day before yesterday", [date(2026, 10, 12), date(2026, 10, 13)]),
        ("what happened last tuesday", [date(2026, 10, 13), date(2026, 10, 6)]),
        ("last saturday", [date(2026, 10, 10)]),
        ("on monday", [date(2026, 10, 12)]),
        ("last wednesday", [date(2026, 10, 7)]),
        ("next friday", [date(2026, 10, 16)]),
        ("this friday", [date(2026, 10, 16)]),
        ("on Sep 19", [date(2026, 9, 19)]),
        ("September 19th, 2025", [date(2025, 9, 19)]),
        ("19 September", [date(2026, 9, 19)]),
        ("in December", []),
        ("Dec 30", [date(2025, 12, 30)]),
        ("2026-08-01", [date(2026, 8, 1)]),
        ("what did I plan for the 27th", [date(2026, 9, 27)]),
        ("the 3rd", [date(2026, 10, 3)]),
        ("turn on the lights", []),
    ],
)
def test_parse_date_hints(text, expected):
    assert recall.parse_date_hints(text, TODAY) == expected


def test_last_week_covers_seven_days():
    days = recall.parse_date_hints("what did we decide last week", TODAY)
    assert days == [date(2026, 10, 5 + offset) for offset in range(7)]


def test_adjacent_days():
    assert recall.adjacent_days([date(2026, 10, 13)]) == [date(2026, 10, 12), date(2026, 10, 14)]
    assert recall.adjacent_days([date(2026, 10, 13), date(2026, 10, 14)]) == [date(2026, 10, 12), date(2026, 10, 15)]


def test_match_subjects_by_name_and_alias():
    entities = [
        SubjectEntity(1, "person", "Mia", ("Mimi",)),
        SubjectEntity(2, "pet", "Luna"),
        SubjectEntity(3, "person", "Al"),
        SubjectEntity(4, "place", "Grandma's House"),
    ]
    assert recall.match_subjects("What does Mimi like?", entities) == [1]
    assert recall.match_subjects("Did Luna's vet call? And mia?", entities) == [1, 2]
    assert recall.match_subjects("Alan is here", entities) == []  # short names never match
    assert recall.match_subjects("we went to grandma's house", entities) == [4]
    assert recall.match_subjects("Lunatic", entities) == []


def test_mentions_self():
    assert recall.mentions_self("What do I like?")
    assert recall.mentions_self("my allergies")
    assert not recall.mentions_self("what does Mia like")


def test_content_hash_ignores_case_whitespace_and_trailing_punctuation():
    assert recall.content_hash("Mia is allergic to peanuts.") == recall.content_hash("  mia IS allergic   to peanuts ")
    assert recall.content_hash("Mia is allergic to peanuts") != recall.content_hash("Mia is allergic to cats")


def test_format_note_and_caps():
    row = RecallRow(
        id=812,
        kind="preference",
        content="Mia prefers oat milk in her coffee.",
        at=datetime(2026, 10, 12, 18, 4, tzinfo=timezone.utc),
        sensitivity="private",
        about=["Mia"],
        replaced=[("Mia prefers almond milk in her coffee.", datetime(2026, 8, 3, tzinfo=timezone.utc))],
    )
    assert recall.format_note(row) == (
        "• [#812 · 2026-10-12 18:04 · preference · about Mia · private] Mia prefers oat milk in her coffee. "
        '(replaced: "Mia prefers almond milk in her coffee.", 2026-08-03)'
    )

    digest = RecallRow(id=1, kind="digest", content="## Summary\n" + "word " * 400, at=None)
    line = recall.format_note(digest)
    assert line.startswith("• [#1 · undated · daily digest] Summary word")
    assert len(line) < 960 and line.endswith("…")

    rows = [RecallRow(id=i, kind="fact", content="x" * 300, at=None) for i in range(30)]
    block = recall.format_notes(rows, total_chars=recall.AUTO_NOTES_TOTAL_CHARS)
    assert len(block) <= recall.AUTO_NOTES_TOTAL_CHARS
    assert block.count("\n") + 1 < 30

    text, shown = recall.format_notes_with_rows(rows, total_chars=recall.AUTO_NOTES_TOTAL_CHARS)
    assert text == block
    # Only the rows that fit are reported as shown.
    assert [row.id for row in shown] == list(range(block.count("\n") + 1))


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Will has a piano lesson every Thursday at 4 pm.", "every Thursday"),
        ("Mia has swimming on Tuesdays.", "on Tuesdays"),
        ("The bins go out each week.", "each week"),
        ("Will takes his vitamins daily.", "daily"),
        ("Mia sees her tutor twice a week.", "twice a week"),
        ("The cleaner comes every other Friday.", "every other Friday"),
        ("Will works from home on weekdays.", "on weekdays"),
        ("Will has a piano lesson on Thursday, 2026-10-15.", None),
        ("Mia's recital is on Saturday.", None),
        ("Will is away the whole week.", None),
    ],
)
def test_repeats(text, expected):
    assert recall.repeats(text) == expected


def test_commitment_notes_show_the_due_date():
    lesson = RecallRow(
        id=58, kind="commitment", content="Will has a piano lesson on Thursday, 2026-10-15.",
        at=datetime(2026, 10, 13, 8, 2, tzinfo=timezone.utc), about=["Will"],
        due=datetime(2026, 10, 15, tzinfo=timezone.utc),
    )
    assert recall.format_note(lesson, today=date(2026, 10, 13)) == (
        "• [#58 · 2026-10-13 08:02 · commitment · due Thu 2026-10-15 · about Will] "
        "Will has a piano lesson on Thursday, 2026-10-15."
    )
    assert "due Thu 2026-10-15 (today) ·" in recall.format_note(lesson, today=date(2026, 10, 15))
    # Two weeks later the old Thursday can't pass for this week's.
    assert "due Thu 2026-10-15 (past) ·" in recall.format_note(lesson, today=date(2026, 10, 27))
    text, _ = recall.format_notes_with_rows([lesson], total_chars=1000, today=date(2026, 10, 27))
    assert "(past)" in text


def test_shown_notes_are_remembered_per_conversation(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(recall, "monotonic", lambda: clock[0])
    cache = recall.ShownNotesCache(ttl_seconds=60, max_conversations=2)
    archive_row = RecallRow(id=7, kind="exchange", content="today", at=None, from_archive=True)

    cache.add("c1", [RecallRow(id=17, kind="fact", content="a", at=None), archive_row])
    cache.add("c1", [RecallRow(id=18, kind="fact", content="b", at=None)])
    assert cache.shown("c1", 17) and cache.shown("c1", 18)
    # Archive rows have no # number the model could see, and other conversations don't count.
    assert not cache.shown("c1", 7)
    assert not cache.shown("c2", 17) and not cache.shown(None, 17)

    clock[0] += 61
    assert not cache.shown("c1", 17)
    cache.add("c1", [RecallRow(id=19, kind="fact", content="c", at=None)])
    assert cache.shown("c1", 19) and not cache.shown("c1", 18)  # expired ids don't come back

    cache.add("c2", [RecallRow(id=1, kind="fact", content="d", at=None)])
    cache.add("c3", [RecallRow(id=2, kind="fact", content="e", at=None)])
    assert not cache.shown("c1", 19)  # oldest conversation dropped past the limit
    cache.clear()
    assert not cache.shown("c3", 2)


def test_client_and_server_redaction_rules_match():
    """The two redaction copies must stay identical below their module docstrings."""
    client = Path(recall.__file__).with_name("redaction.py").read_text(encoding="utf-8")
    server_path = Path(__file__).resolve().parents[2] / "AuriService" / "redaction.py"
    if not server_path.exists():
        pytest.skip("AuriService checkout not found next to AuriClient")
    server = server_path.read_text(encoding="utf-8")

    def body(source: str) -> str:
        return source.split('"""', 2)[2]

    assert body(client) == body(server)
