"""Mishearings (memory/mishearings.py): the nightly job's windows, the pairs it keeps, the store, and the match on
later turns. The nightly job end to end is in test_memory_nightly_pg.py; the turn and the guess in
test_integration_memory.py.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import zoneinfo

import pytest

from custom_components.hey_auri_client.memory import mishearings
from custom_components.hey_auri_client.memory.mishearings import Mishearing
from custom_components.hey_auri_client.memory.recall import websearch_query
from custom_components.hey_auri_client.memory.store import MemoryStore, NewMemory, new_exchange_id
from custom_components.hey_auri_client.memory.transcript import mishearing_windows, windows_to_send

NOW = datetime(2026, 11, 23, 17, 0, tzinfo=timezone.utc)
TZ = zoneinfo.ZoneInfo("America/Los_Angeles")
PAIRS = [
    Mishearing(1, "瓦登达", "把灯打开"),
    Mishearing(2, "Turn on the lice", "turn on the lights"),
    Mishearing(3, "Apaga la lus", "Apaga la luz"),
]


# ── the match ────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("瓦登达", 1),
        ("瓦登达。", 1),  # punctuation, case and spaces don't count
        ("turn on the LICE!", 2),
        ("apaga la lus", 3),
        ("Turn on the lices", None),  # close isn't the same: Auri's misheard-speech rule asks about it
        ("Apaga las luz", None),
        ("把灯打开", None),  # the meant words themselves
        ("Turn on the kitchen light", None),
        ("瓦登", None),
        ("", None),
    ],
)
def test_only_the_same_words_match_in_any_script(text, expected):
    pair = mishearings.match(text, PAIRS, NOW)
    assert (pair.memory_id if pair else None) == expected
    assert mishearings.meant_words(text, PAIRS, NOW) == (pair.meant if pair else None)


def test_the_newest_pair_for_the_same_words_wins():
    pairs = [Mishearing(1, "瓦登达", "把灯打开"), Mishearing(7, "瓦登达", "把灯关上")]
    assert mishearings.match("瓦登达", pairs, NOW).memory_id == 7
    assert mishearings.heard_as(pairs[1]) == {"heard": "瓦登达", "meant": "把灯关上", "id": 7}


def test_an_expired_pair_matches_nothing():
    pairs = [Mishearing(1, "瓦登达", "把灯打开", expires_at=NOW - timedelta(seconds=1))]
    assert mishearings.match("瓦登达", pairs, NOW) is None
    assert mishearings.meant_words("瓦登达", pairs, NOW) is None


def test_rows_without_both_words_are_left_out():
    rows = [
        (1, {"heard": "瓦登达", "meant": "把灯打开", "speakers": ["William"]}, None),
        (2, {"heard": "瓦登达"}, None),
        (3, {"heard": "x", "meant": "y"}, None),  # heard words too short
        (4, {"heard": "Same words", "meant": "same words!"}, None),
        (5, None, None),
    ]
    assert mishearings.from_rows(rows) == [Mishearing(1, "瓦登达", "把灯打开")]


def test_the_same_pair_always_gives_the_same_text():
    assert mishearings.statement("瓦登达", "把灯打开") == 'Speech-to-text heard "瓦登达" when someone said "把灯打开".'


# ── the windows ──────────────────────────────────────────────────────────────────────────────────

def _rows(*turns, start=NOW):
    """Archive rows: (seconds after start, device, said, reply, tools, voice, kind)."""
    rows = []
    for seconds, device, said, reply, tools, voice, kind in turns:
        at = start + timedelta(seconds=seconds)
        common = {"exchange_id": new_exchange_id(), "conversation_id": "c1", "kind": kind, "device_id": device,
                  "tool_names": tools, "meta": {"voice": True} if voice else {}}
        rows.append({**common, "role": "user", "content": said, "speaker_source": "ha_user", "speaker_name": "William",
                     "created_at": at})
        rows.append({**common, "role": "assistant", "content": reply, "speaker_source": "assistant", "created_at": at})
    return sorted(rows, key=lambda row: row["created_at"])


def test_a_window_is_voice_requests_from_one_device_each_within_two_minutes():
    rows = _rows(
        (0, "sat-1", "瓦登达", "我没听清“瓦登达”，您能再说一遍吗？", [], True, "chat"),
        (9, "sat-1", "把灯打开", "好的", ["turn_on_light"], True, "device_control"),  # a short command stays in
        (20, "sat-2", "What time is it?", "Five.", [], True, "chat"),  # another device: its own run
        (100, "sat-1", "Thanks", "You're welcome.", [], True, "chat"),
        (400, "sat-1", "Turn on the lice", "Did you mean the lights?", [], True, "chat"),  # 5 min later: a new run
        (405, "sat-1", "Yes", "Done.", ["turn_on_light"], True, "device_control"),
        (500, "phone", "Remind me", "Sure.", [], False, "chat"),  # typed
        (510, "phone", "Of the dentist", "Done.", [], False, "chat"),
        (600, "sat-1", "Lights off", "", [], True, "automatic"),
        (610, "sat-1", "Thanks", "Bye.", [], True, "chat"),
    )
    windows = mishearing_windows(rows, TZ)

    assert [[exchange["said"] for exchange in window["exchanges"]] for window in windows] == [
        ["瓦登达", "把灯打开", "Thanks"],
        ["Turn on the lice", "Yes"],
    ]
    first = windows[0]["exchanges"][1]
    assert windows[0]["idx"] == 1 and windows[1]["idx"] == 2
    assert first["speaker"] == "William" and first["reply"] == "好的" and first["tools"] == ["turn_on_light"]
    assert first["at"] == "09:00:09"  # local time
    assert first["occurred_at"] == NOW + timedelta(seconds=9)


def test_a_request_sent_as_a_mishearings_meant_words_says_so():
    rows = _rows(
        (0, "sat-1", "瓦登达", "好的", ["turn_on_light"], True, "device_control"),
        (9, "sat-1", "不是，把灯关上", "好的", ["turn_off_light"], True, "device_control"),
    )
    rows[0]["meta"] = rows[1]["meta"] = {"voice": True, "taken_as": "把灯打开"}  # the first exchange's two rows
    (window,) = mishearing_windows(rows, TZ)
    first, second = window["exchanges"]
    assert first["taken_as"] == "把灯打开" and "taken_as" not in second


def test_long_runs_are_cut_and_what_is_sent_has_no_times():
    rows = _rows(*[(10 * i, "sat-1", f"request {i}", "ok", [], True, "chat") for i in range(11)])
    windows = mishearing_windows(rows, TZ)
    assert [len(window["exchanges"]) for window in windows] == [8, 3]
    sent = windows_to_send(windows)
    assert "occurred_at" not in sent[0]["exchanges"][0] and sent[0]["exchanges"][0]["said"] == "request 0"
    json.dumps(sent)  # goes out as JSON


def test_a_single_voice_request_is_no_window():
    assert mishearing_windows(_rows((0, "sat-1", "瓦登达", "?", [], True, "chat")), TZ) == []


# ── the answer ───────────────────────────────────────────────────────────────────────────────────

def test_only_pairs_the_window_shows_are_kept_as_the_window_has_them():
    rows = _rows(
        (0, "sat-1", "瓦登达", "我没听清", [], True, "chat"),
        (9, "sat-1", "把灯打开", "好的", ["turn_on_light"], True, "device_control"),
    )
    windows = mishearing_windows(rows, TZ)
    response = {"mishearings": [
        {"window": 1, "heard": "瓦登达！", "meant": "把灯打开", "speaker": "William"},
        {"window": 1, "heard": "瓦登", "meant": "把灯打开"},  # not one of the window's requests
        {"window": 2, "heard": "瓦登达", "meant": "把灯打开"},  # no such window
        {"window": 1, "heard": "瓦登达", "meant": "瓦登达"},
        "瓦登达",
    ]}
    assert mishearings.checked_pairs(response, windows) == [
        {"heard": "瓦登达", "meant": "把灯打开", "speaker": "William", "occurred_at": NOW},
    ]
    assert mishearings.checked_pairs({"mishearings": None}, windows) == []
    assert mishearings.checked_pairs("nonsense", windows) == []


def test_the_same_words_after_a_request_taken_as_others_mark_a_wrong_pair():
    rows = _rows(
        (0, "sat-1", "打开灯带", "好的", ["turn_on_light"], True, "device_control"),
        (9, "sat-1", "不是，打开灯带", "好的", ["turn_on_light"], True, "device_control"),
    )
    plain = mishearing_windows(rows, TZ)
    rows[0]["meta"] = rows[1]["meta"] = {"voice": True, "taken_as": "打开灯"}
    taken = mishearing_windows(rows, TZ)
    answer = {"mishearings": [{"window": 1, "heard": "打开灯带", "meant": "打开灯带", "speaker": "William"}]}
    (pair,) = mishearings.checked_pairs(answer, taken)
    assert pair["wrong"] is True and pair["heard"] == "打开灯带"
    assert mishearings.checked_pairs(answer, plain) == []  # not sent as other words: nothing to retire


# ── the store ────────────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def store(pg_engine):
    store = MemoryStore(pg_engine, iterative_scan=True)
    store.ensure_household()
    return store


def _save(store: MemoryStore, heard: str, meant: str, *, speaker: str = "William", expires_at=None) -> tuple[int, bool]:
    return store.insert_memory(NewMemory(
        kind=mishearings.KIND, content=mishearings.statement(heard, meant), source="nightly",
        expires_at=expires_at or NOW + mishearings.TTL, meta={"heard": heard, "meant": meant, "speakers": [speaker]},
    ))


def test_a_saved_pair_comes_back_with_its_words(store):
    memory_id, again = _save(store, "瓦登达", "把灯打开")
    assert not again
    rows = store.current_mishearings()
    assert rows == [(memory_id, {"heard": "瓦登达", "meant": "把灯打开", "speakers": ["William"]}, NOW + mishearings.TTL)]
    assert mishearings.from_rows(rows) == [Mishearing(memory_id, "瓦登达", "把灯打开", NOW + mishearings.TTL)]


def test_a_pair_found_again_is_counted_kept_longer_and_knows_who(store):
    memory_id, _ = _save(store, "瓦登达", "把灯打开")
    same_id, again = _save(store, "瓦登达", "把灯打开", speaker="Lillian")
    assert (same_id, again) == (memory_id, True)
    later = NOW + mishearings.TTL + timedelta(days=30)
    with store.engine.begin() as conn:
        store.renew_mishearing(conn, memory_id, speaker="Lillian", expires_at=later)
        store.renew_mishearing(conn, memory_id, speaker="Lillian", expires_at=NOW)  # never shorter
    (row,) = store.current_mishearings()
    assert row[1]["speakers"] == ["William", "Lillian"] and row[2] == later
    import sqlalchemy

    with store.engine.connect() as conn:
        assert conn.execute(sqlalchemy.text(
            f"SELECT confirmed_count FROM auri_memory.mem_memories WHERE id = {memory_id}")).scalar() == 2


def test_pairs_stay_out_of_recall_and_the_embedding_backfill(store):
    _save(store, "Turn on the lice", "turn on the lights")
    store.insert_memory(NewMemory(kind="fact", content="The office lights are on a timer.", source="chat", importance=5))
    assert [content for _, content in store.missing_embeddings("fake-embed@768", limit=10)] == [
        "The office lights are on a timer."]
    household = store.ensure_household()
    profile = store.profile(speaker_entity_id=None, household_entity_id=household, shared=True)
    notes = store.auto_recall(terms=websearch_query(["lights"]), subject_ids=[], hint_days=[], adjacent_days=[],
                              speaker_entity_id=None, shared=True)
    assert [row.content for row in profile + notes] == ["The office lights are on a timer."] * 2


def test_the_archive_keeps_the_device_and_the_voice_mark(store):
    exchange = new_exchange_id()
    store.insert_messages([
        {"conversation_id": "c1", "exchange_id": exchange, "role": "user", "content": "瓦登达", "kind": "chat",
         "device_id": "sat-1", "meta": {"voice": True}, "created_at": NOW},
    ])
    (row,) = store.messages_between(NOW - timedelta(seconds=1), NOW + timedelta(seconds=1))
    assert row["device_id"] == "sat-1" and row["meta"] == {"voice": True}
