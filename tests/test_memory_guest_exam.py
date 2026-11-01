"""Guest memory (Guest Settings, seeds, the guest schema), and the memory exam's file, scoring and test memory."""
from __future__ import annotations

from datetime import date, datetime, timedelta
import zoneinfo

import pytest

pytest.importorskip("homeassistant")

from homeassistant.util import dt as dt_util  # noqa: E402

import voluptuous as vol  # noqa: E402

from custom_components.hey_auri_client.conversation.services import RUN_MEMORY_EXAM_SCHEMA  # noqa: E402
from custom_components.hey_auri_client.memory import guest  # noqa: E402
from custom_components.hey_auri_client.memory.db import GUEST_MEMORY_SCHEMA, MEMORY_SCHEMA  # noqa: E402
from custom_components.hey_auri_client.memory.exam import (  # noqa: E402
    find_memory,
    memory_words,
    parse_exam,
    recall_summary,
    score,
)
from custom_components.hey_auri_client.memory.exam_memory import SeedMemory, exam_seeds, parse_exam_memory  # noqa: E402
from custom_components.hey_auri_client.memory.recall import RecallRow  # noqa: E402
from custom_components.hey_auri_client.memory.migrations import apply_migrations, drop_schema, reset_schema  # noqa: E402
from custom_components.hey_auri_client.memory.store import GUEST_SCOPE, MemoryStore, NewMemory  # noqa: E402

VANCOUVER = zoneinfo.ZoneInfo("America/Vancouver")
OPTIONS = {
    "booking_id": "B-1",
    "guest_name": "Sarah Lee",
    "guest_count": 3,
    "check_in": "2026-10-20 15:00:00",
    "check_out": "2026-10-25 11:00:00",
    "notes": "Celebrating their anniversary\n\nDoor code is 4821\nTV: {{ states('media_player.tv') }}\nTravelling with a dog",
}


@pytest.fixture(autouse=True)
def local_time_zone():
    previous = dt_util.get_default_time_zone()
    dt_util.set_default_time_zone(VANCOUVER)
    yield
    dt_util.set_default_time_zone(previous)


def test_stay_times_are_read_in_local_time():
    assert guest.parse_stay_time("2026-10-20 11:00:00") == datetime(2026, 10, 20, 11, tzinfo=VANCOUVER)
    assert guest.parse_stay_time("") is None


def test_a_stay_ends_an_hour_after_check_out():
    assert guest.guest_erase_time(OPTIONS) == datetime(2026, 10, 25, 12, tzinfo=VANCOUVER)
    assert guest.guest_erase_time({**OPTIONS, "check_out": ""}) is None
    # Guests leaving a little late still have their memory.
    assert not guest.stay_over(OPTIONS, datetime(2026, 10, 25, 11, 59, tzinfo=VANCOUVER))
    assert guest.stay_over(OPTIONS, datetime(2026, 10, 25, 12, tzinfo=VANCOUVER))
    assert not guest.stay_over({**OPTIONS, "check_out": ""}, datetime(2099, 1, 1, tzinfo=VANCOUVER))
    assert guest.parse_stay_time("not a date") is None


def test_a_stay_is_its_booking_id_or_else_its_check_in():
    assert guest.stay_key(OPTIONS) == "booking:B-1"
    assert guest.stay_key({**OPTIONS, "booking_id": " "}) == "check_in:2026-10-20 15:00:00"
    assert guest.stay_marker({**OPTIONS, "guest_memory_reset_at": "2026-10-21T08:00:00+00:00"}) == {
        "stay": "booking:B-1",
        "reset_at": "2026-10-21T08:00:00+00:00",
    }
    assert guest.guest_memory_enabled({}) is False and guest.guest_memory_enabled({"guest_memory_enabled": True})


def test_a_stay_starts_from_the_guest_details():
    seeds = guest.seed_memories(OPTIONS)
    assert [memory.content for memory, _ in seeds] == [
        "Sarah Lee booked this stay.",
        "3 guests are staying.",
        "The stay runs from Tue 2026-10-20 15:00 to Sun 2026-10-25 11:00.",
        "The host's note about this stay: Celebrating their anniversary",
        "The host's note about this stay: Travelling with a dog",
    ]
    assert seeds[0][1] == [("Sarah Lee", "person")]
    assert all(subjects == [("household", "household")] for _, subjects in seeds[1:])
    assert all(memory.source == "import" and memory.kind == "fact" for memory, _ in seeds)
    # Nothing to start from: no seeds.
    assert guest.seed_memories({}) == []


def test_reset_erases_guest_memory_completely_and_never_the_owners(pg_engine):
    owner = MemoryStore(pg_engine)
    owner.insert_memory(NewMemory(kind="fact", content="William is lactose intolerant.", source="chat"))
    apply_migrations(pg_engine, app_version="test", schema=GUEST_MEMORY_SCHEMA)
    guests = MemoryStore(pg_engine, schema=GUEST_MEMORY_SCHEMA, scope=GUEST_SCOPE)
    guests.insert_memory(NewMemory(kind="fact", content="The guests parked in spot 12.", source="chat"))
    guests.insert_messages([{
        "conversation_id": "c", "exchange_id": "00000000-0000-0000-0000-000000000001", "role": "user",
        "content": "We parked in spot 12", "created_at": dt_util.utcnow(),
    }])
    # Each store only sees its own schema.
    assert owner.counts() == {"fact": 1} and guests.counts() == {"fact": 1}

    def seed(conn):
        store = MemoryStore(pg_engine, schema=GUEST_MEMORY_SCHEMA, scope=GUEST_SCOPE)
        for memory, subjects in guest.seed_memories(OPTIONS):
            memory.about_entity_ids = store.resolve_entities(subjects, conn)
            store.insert_memory(memory, conn)
        store.set_meta(guest.STAY_META_KEY, guest.stay_marker(OPTIONS), conn)

    reset_schema(pg_engine, schema=GUEST_MEMORY_SCHEMA, app_version="test", seed=seed)

    assert guests.counts() == {"fact": 5}
    assert "The guests parked in spot 12." not in {row.content for row in guests.current_facts_for_export(include_private=True)}
    assert guests.days_with_messages(since=datetime(2026, 1, 1, tzinfo=VANCOUVER), time_zone="America/Vancouver", boundary_hour=4) == []
    assert guests.get_meta(guest.STAY_META_KEY) == {"stay": "booking:B-1", "reset_at": ""}
    assert owner.counts() == {"fact": 1}

    # A seed that fails leaves the guest schema as it was.
    def broken(conn):
        raise RuntimeError("seed failed")

    with pytest.raises(RuntimeError):
        reset_schema(pg_engine, schema=GUEST_MEMORY_SCHEMA, app_version="test", seed=broken)
    assert guests.counts() == {"fact": 5}

    drop_schema(pg_engine, schema=GUEST_MEMORY_SCHEMA)
    import sqlalchemy

    with pg_engine.connect() as conn:
        assert conn.execute(
            sqlalchemy.text("SELECT count(*) FROM pg_namespace WHERE nspname = :schema"), {"schema": GUEST_MEMORY_SCHEMA}
        ).scalar() == 0
    assert owner.counts() == {"fact": 1}

    # The owner's memory can't be reset or dropped, whatever the caller does.
    with pytest.raises(ValueError):
        reset_schema(pg_engine, schema=MEMORY_SCHEMA, app_version="test")
    with pytest.raises(ValueError):
        drop_schema(pg_engine, schema=MEMORY_SCHEMA)


def test_exam_file_parsing_and_score():
    questions = parse_exam([
        {"q": "What is Mia allergic to?", "expected": "Peanuts."},
        {"question": "  When is the  dentist? ", "answer": "Friday"},
        {"q": "No expected answer"},
        "not a question",
    ])
    assert questions == [
        {"q": "What is Mia allergic to?", "expected": "Peanuts."},
        {"q": "When is the dentist?", "expected": "Friday"},
    ]
    assert parse_exam({"questions": [{"q": "a", "expected": "b"}]}) == [{"q": "a", "expected": "b"}]
    with pytest.raises(ValueError):
        parse_exam({"nothing": True})
    with pytest.raises(ValueError):
        parse_exam([{"q": "only a question"}])

    grades = [{"grade": g} for g in ("correct", "partial", "wrong", "missed")]
    assert score(grades) == (0.375, {"correct": 1, "partial": 1, "wrong": 1, "missed": 1})


def test_exam_questions_can_name_their_memory():
    questions = parse_exam([
        {"q": "When is piano?", "expected": "Thursday", "memory": "  Mia   piano ", "case": "direct"},
        {"q": "Anything else?", "expected": "No", "memory": "?!", "case": " "},  # no words: nothing to match
    ])
    assert questions == [
        {"q": "When is piano?", "expected": "Thursday", "memory": "Mia piano", "case": "direct"},
        {"q": "Anything else?", "expected": "No"},
    ]
    with pytest.raises(ValueError, match="benchmark.yaml"):
        parse_exam({"nothing": True}, "benchmark.yaml")


def test_the_exam_file_is_named_inside_the_config_folder():
    assert RUN_MEMORY_EXAM_SCHEMA({}) == {}
    assert RUN_MEMORY_EXAM_SCHEMA({"file": "hey_auri_memory_benchmark.yaml"}) == {"file": "hey_auri_memory_benchmark.yaml"}
    for outside in ("../secrets.yaml", "exams/benchmark.yaml", "/config/benchmark.yaml", ".benchmark.yaml", "benchmark.txt"):
        with pytest.raises(vol.Invalid):
            RUN_MEMORY_EXAM_SCHEMA({"file": outside})


def _row(memory_id: int, content: str) -> RecallRow:
    return RecallRow(id=memory_id, kind="fact", content=content, at=None)


def test_where_an_answers_memory_reached_auri():
    lesson = _row(3, "Mia has PIANO lessons, every Thursday.")
    trace = [
        ("profile", [_row(1, "The recycling goes out on Tuesdays.")]),
        ("notes", [_row(2, "Will had piano lessons in 2024."), lesson]),
        ("recall_memory", [lesson]),
    ]
    assert memory_words("Mia's piano, 16:00") == ["mia's", "piano", "16", "00"]
    assert find_memory(memory_words("mia lesson thursday"), trace) == ("notes", 2)  # the first place it was shown
    assert find_memory(memory_words("Tuesday"), trace) == ("profile", 1)  # a word matches its longer forms
    assert find_memory(memory_words("Mia violin"), trace) is None  # every word is needed
    assert find_memory(memory_words("ano"), trace) is None  # words match from their start
    assert find_memory([], trace) is None


def test_recall_and_mrr():
    def graded(grade: str, found_in: str, rank: int | None = None, case: str | None = None) -> dict:
        return {"grade": grade, "memory_found_in": found_in, "memory_rank": rank, **({"case": case} if case else {})}

    grades = [
        graded("correct", "notes", 1, "direct"),
        graded("wrong", "profile", 2, "direct"),
        graded("partial", "none", case="paraphrase"),
        graded("correct", "recall_memory", 4, "paraphrase"),
        {"grade": "wrong"},  # names no memory: left out
    ]
    assert recall_summary(grades) == {
        "questions": 4,
        "found": 0.75,
        "mrr": 0.4375,  # (1 + 1/2 + 0 + 1/4) / 4
        "found_in": {"profile": 1, "notes": 1, "recall_memory": 1, "none": 1},
        "not_correct": {"memory_shown": 1, "memory_not_shown": 1},
        "by_case": {
            "direct": {"questions": 2, "found": 1.0, "mrr": 0.75},
            "paraphrase": {"questions": 2, "found": 0.5, "mrr": 0.125},
        },
    }
    assert "by_case" not in recall_summary([graded("correct", "notes", 1)])
    assert recall_summary([{"grade": "correct"}]) is None


def test_an_exam_file_can_bring_its_own_memories():
    assert parse_exam_memory([{"q": "a", "expected": "b"}], "exam.yaml") is None
    assert parse_exam_memory({"questions": [{"q": "a", "expected": "b"}]}, "exam.yaml") is None
    memory = parse_exam_memory(
        {
            "household": {"people": ["Will", " Mia ", "will"], "pets": "Biscuit"},
            "memories": [
                "The recycling goes out on Tuesdays.",
                {"text": "Mia has piano every Thursday.", "about": ["Mia", "Lincoln Elementary"], "importance": 4},
                {"text": "Dentist at 10.", "about": "Will", "kind": "Commitment", "due_in_days": "3", "private": True,
                 "days_ago": 2},
            ],
        },
        "exam.yaml",
    )
    assert (memory.people, memory.pets) == (["Will", "Mia"], ["Biscuit"])
    assert memory.memories == [
        SeedMemory(text="The recycling goes out on Tuesdays."),
        SeedMemory(text="Mia has piano every Thursday.", about=["Mia", "Lincoln Elementary"], importance=4),
        SeedMemory(text="Dentist at 10.", about=["Will"], kind="commitment", private=True, days_ago=2, due_in_days=3),
    ]
    for bad, message in [
        ({"memories": []}, "memories must be a list"),
        ({"memories": [{"about": ["Mia"]}]}, "exam.yaml: memory 1 has no text"),
        ({"memories": ["a", {"text": "b", "kind": "secret"}]}, "memory 2 has kind 'secret'"),
        ({"memories": [{"text": "b", "importance": 9}]}, "importance must be from 1 to 5"),
        ({"memories": [{"text": "b", "days_ago": 1.5}]}, "days_ago must be a whole number"),
        ({"memories": [{"text": "b", "private": "yes"}]}, "private must be true or false"),
        ({"memories": ["a"] * 301}, "at most 300 memories"),
    ]:
        with pytest.raises(ValueError, match=message):
            parse_exam_memory(bad, "exam.yaml")


def test_the_test_memory_counts_its_dates_back_from_today():
    memory = parse_exam_memory(
        {
            "household": {"people": ["Will"], "pets": ["Biscuit"]},
            "memories": [
                {"text": "Biscuit can't eat chicken.", "about": ["biscuit", "Chicken"], "importance": 5},
                {"text": "The family went to the beach.", "kind": "digest", "days_ago": 1},
                {"text": "Will has the dentist at 10:00.", "about": ["Will"], "kind": "commitment", "due_in_days": 3},
            ],
        },
        "exam.yaml",
    )
    now = datetime(2026, 10, 31, 9, 30, tzinfo=VANCOUVER)
    (pet, pet_subjects), (digest, digest_subjects), (dentist, dentist_subjects) = exam_seeds(memory, now=now)
    assert pet_subjects == [("biscuit", "pet"), ("Chicken", "topic")] and pet.importance == 5
    assert (pet.source, pet.occurred_at, pet.sensitivity, pet.source_ref) == ("import", now, "normal", None)
    assert digest_subjects == [] and digest.source_ref == date(2026, 10, 30)
    assert digest.occurred_at == now - timedelta(days=1)
    assert dentist_subjects == [("Will", "person")] and dentist.due_at == datetime(2026, 11, 3, tzinfo=VANCOUVER)
