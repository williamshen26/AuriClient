"""Guest scopes (switched off for now) and the memory exam's question file and scoring."""
from __future__ import annotations

from datetime import datetime, timedelta
import zoneinfo

import pytest

pytest.importorskip("homeassistant")

from homeassistant.util import dt as dt_util  # noqa: E402

from custom_components.hey_auri_client.memory import scope  # noqa: E402
from custom_components.hey_auri_client.memory.exam import parse_exam, score  # noqa: E402
from custom_components.hey_auri_client.memory.store import MemoryStore, NewMemory  # noqa: E402

VANCOUVER = zoneinfo.ZoneInfo("America/Vancouver")


def test_scopes_follow_the_mode():
    assert scope.scope_for_turn("owner", "B-1") == "owner"
    # Guest memory is off: guests get none, whatever the booking.
    assert scope.scope_for_turn("guest", "B-1") is None
    assert scope.scope_for_turn(None, None) is None
    assert scope.guest_scope(" B-1/../x ") == "guest:B-1..x"
    assert scope.guest_scope("") is None


def test_scopes_to_purge():
    now = datetime(2026, 10, 14, 12, tzinfo=VANCOUVER)
    scopes = ["owner", "guest:B-1", "guest:B-0"]
    assert scope.scopes_to_purge(scopes, booking_id="B-1", check_out=now + timedelta(days=1), now=now) == ["guest:B-0"]
    assert scope.scopes_to_purge(scopes, booking_id="B-1", check_out=now - timedelta(minutes=1), now=now) == [
        "guest:B-0", "guest:B-1",
    ]
    assert scope.scopes_to_purge(scopes, booking_id=None, check_out=None, now=now) == ["guest:B-0", "guest:B-1"]


def test_check_out_is_read_in_local_time():
    previous = dt_util.get_default_time_zone()
    dt_util.set_default_time_zone(VANCOUVER)
    try:
        assert scope.parse_check_out("2026-10-20 11:00:00") == datetime(2026, 10, 20, 11, tzinfo=VANCOUVER)
        assert scope.parse_check_out("") is None
        assert scope.parse_check_out("not a date") is None
    finally:
        dt_util.set_default_time_zone(previous)


def test_purging_a_guest_scope_leaves_the_owner_alone(pg_engine):
    owner = MemoryStore(pg_engine)
    guest = MemoryStore(pg_engine, scope="guest:B-0")
    owner.insert_memory(NewMemory(kind="fact", content="The owner likes tea.", source="chat"))
    guest.insert_memory(NewMemory(kind="fact", content="The guest likes coffee.", source="chat"))
    guest.ensure_household()
    assert owner.list_scopes() == ["guest:B-0", "owner"]

    counts = guest.purge_scope()

    assert counts["mem_memories"] == 1 and counts["mem_entities"] == 1
    assert owner.list_scopes() == ["owner"]
    assert owner.counts() == {"fact": 1}


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
