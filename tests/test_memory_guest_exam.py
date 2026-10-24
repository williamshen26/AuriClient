"""Guest memory (Guest Settings, seeds, the guest schema) and the memory exam's question file and scoring."""
from __future__ import annotations

from datetime import datetime
import zoneinfo

import pytest

pytest.importorskip("homeassistant")

from homeassistant.util import dt as dt_util  # noqa: E402

from custom_components.hey_auri_client.memory import guest  # noqa: E402
from custom_components.hey_auri_client.memory.db import GUEST_MEMORY_SCHEMA, MEMORY_SCHEMA  # noqa: E402
from custom_components.hey_auri_client.memory.exam import parse_exam, score  # noqa: E402
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
