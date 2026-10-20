"""Pure memory helpers: day windows, transcripts, legacy files, the vault copy (no Home Assistant, no database)."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
import os
import zoneinfo

import pytest

from custom_components.hey_auri_client.memory.db import classify_setup_error
from custom_components.hey_auri_client.memory.legacy import (
    load_legacy_files,
    plan_legacy_import,
    relocate_legacy_user_data,
)
from custom_components.hey_auri_client.memory.obsidian import (
    remove_daily_note,
    validate_vault,
    write_daily_note,
    write_memory_note,
)
from custom_components.hey_auri_client.memory.recall import RecallRow
from custom_components.hey_auri_client.memory.transcript import (
    build_transcript,
    chunk_lines,
    current_day,
    day_window,
    exchanges_for_memory,
    split_chunk,
)


def zone(key: str) -> zoneinfo.ZoneInfo:
    """A time zone; skips the test where no tz database is installed (Windows without tzdata)."""
    try:
        return zoneinfo.ZoneInfo(key)
    except zoneinfo.ZoneInfoNotFoundError:
        pytest.skip("no time zone database here (pip install tzdata)")


VANCOUVER = "America/Vancouver"
# British Columbia stops changing clocks after March 2026; Los Angeles still does.
LOS_ANGELES = "America/Los_Angeles"


# ── day windows ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("day", "hours"),
    [
        (date(2026, 3, 7), 23),  # clocks spring forward early on March 8
        (date(2026, 10, 31), 25),  # and fall back early on November 1
        (date(2026, 10, 12), 24),
    ],
)
def test_day_window_follows_daylight_saving(day, hours):
    start, end = day_window(day, zone(LOS_ANGELES), 4)
    assert (start.hour, end.hour) == (4, 4)
    assert end.astimezone(timezone.utc) - start.astimezone(timezone.utc) == timedelta(hours=hours)


def test_current_day_uses_the_boundary():
    assert current_day(datetime(2026, 10, 13, 1, 30, tzinfo=zone(VANCOUVER)), 4) == date(2026, 10, 12)
    assert current_day(datetime(2026, 10, 13, 4, 0, tzinfo=zone(VANCOUVER)), 4) == date(2026, 10, 13)
    assert current_day(datetime(2026, 10, 13, 23, 59, tzinfo=zone(VANCOUVER)), 0) == date(2026, 10, 13)


# ── transcript ──────────────────────────────────────────────────────────────


def _turn(exchange: str, minute: int, said: str, reply: str, *, kind: str = "chat", **speaker):
    at = datetime(2026, 10, 12, 17, minute, tzinfo=timezone.utc)
    user = {"id": minute * 2, "exchange_id": exchange, "conversation_id": "c1", "role": "user", "content": said,
            "kind": kind, "created_at": at, **speaker}
    assistant = {"id": minute * 2 + 1, "exchange_id": exchange, "conversation_id": "c1", "role": "assistant",
                 "content": reply, "kind": kind, "created_at": at + timedelta(seconds=1)}
    return [user, assistant]


def test_transcript_labels_speakers_and_skips_noise():
    rows = [
        *_turn("e1", 1, "Mia hates mushrooms", "Noted.", speaker_source="ha_user", speaker_name="Will"),
        *_turn("e2", 2, "Turn off the lights", "Done.", kind="device_control", speaker_source="unknown", area="Kitchen"),
        *_turn("e3", 3, "Good morning report", "Sunny.", kind="automatic", speaker_source="automation"),
        *_turn("e4", 4, "It's Mia, I want the hallway lights dimmer at night from now on please",
               "Okay.", kind="device_control", speaker_source="stated", speaker_name="Mia"),
        *_turn("e5", 5, "What's the weather", "Rain.", speaker_source="unknown", area="Kitchen"),
    ]

    lines = build_transcript(rows, zone(VANCOUVER))

    assert lines == [
        "[10:01] Will: Mia hates mushrooms",
        "[10:01] Assistant: Noted.",
        "[10:04] Mia (stated): It's Mia, I want the hallway lights dimmer at night from now on please",
        "[10:04] Assistant: Okay.",
        "[10:05] Unknown speaker (Kitchen): What's the weather",
        "[10:05] Assistant: Rain.",
    ]
    exchanges = exchanges_for_memory(rows)
    assert [e["exchange_id"] for e in exchanges] == ["e1", "e4", "e5"]
    assert exchanges[0]["content"] == "Will: Mia hates mushrooms / Assistant: Noted."
    assert exchanges[0]["occurred_at"] == rows[0]["created_at"]


def test_long_messages_are_clipped():
    rows = _turn("e1", 1, "word " * 1000, "ok", speaker_source="ha_user", speaker_name="Will")
    line = build_transcript(rows, zone(VANCOUVER))[0]
    assert line.endswith("…[truncated]") and len(line) < 1600


def test_chunks_keep_whole_lines():
    lines = [f"line {i} " + "x" * 90 for i in range(30)]
    chunks = chunk_lines(lines, 1000)
    assert all(len(chunk) <= 1000 for chunk in chunks)
    assert "\n".join(chunks).split("\n") == lines
    first, second = split_chunk(chunks[0])
    assert f"{first}\n{second}" == chunks[0]
    assert split_chunk("abcdef") == ["abc", "def"]


# ── legacy preference files ─────────────────────────────────────────────────


def test_legacy_files_leave_the_public_folder_and_import_only_clear_matches(tmp_path):
    public = tmp_path / "www" / "user_data"
    public.mkdir(parents=True)
    (public / "abcd1234.json").write_text(json.dumps({
        "coffee": {"value": "oat milk latte", "importance": "high", "timestamp": "2026-05-01T08:00:00Z"},
        "wifi_password": {"value": "password is hunter2", "importance": "high"},
        "music": "jazz",
        "empty": {"value": ""},
    }), encoding="utf-8")
    (public / "ffff0000.json").write_text("{}", encoding="utf-8")
    (public / "notes.txt").write_text("not mine", encoding="utf-8")

    moved = relocate_legacy_user_data(str(tmp_path), "legacy")

    assert moved == ["abcd1234.json", "ffff0000.json"]
    assert sorted(os.listdir(public)) == ["notes.txt"]
    files = load_legacy_files(str(tmp_path), "legacy")
    assert [f.name for f in files] == ["abcd1234.json", "ffff0000.json"]
    assert relocate_legacy_user_data(str(tmp_path), "legacy") == []

    users = [
        {"id": "abcd1234-5678", "name": "will", "system_generated": False},
        {"id": "ffff0000-1", "name": "a", "system_generated": False},
        {"id": "ffff0000-2", "name": "b", "system_generated": False},  # same prefix: never guessed
        {"id": "abcd1234-system", "name": "Supervisor", "system_generated": True},
    ]
    persons = [{"entity_id": "person.will", "name": "Will", "user_id": "abcd1234-5678"}]
    plans, skipped = plan_legacy_import(files, users=users, persons=persons, already_imported=set())

    assert skipped == ["ffff0000.json"]
    [plan] = plans
    assert (plan.person_name, plan.person_entity_id) == ("Will", "person.will")
    statements = {m.content: m for m in plan.memories}
    assert set(statements) == {"Will's coffee: oat milk latte.", "Will's music: jazz."}  # the password is dropped
    assert statements["Will's coffee: oat milk latte."].importance == 4
    assert statements["Will's coffee: oat milk latte."].occurred_at == datetime(2026, 5, 1, 8, tzinfo=timezone.utc)

    again, _ = plan_legacy_import(files, users=users, persons=persons, already_imported={files[0].sha256})
    assert again == []


# ── vault copy ──────────────────────────────────────────────────────────────


def test_vault_validation(tmp_path):
    config = tmp_path / "config"
    (config / "www" / "vault").mkdir(parents=True)
    vault = tmp_path / "share" / "vault"
    vault.mkdir(parents=True)

    def check(path, allowed=True):
        return validate_vault(str(path), config_dir=str(config), is_allowed_path=lambda _p: allowed)

    assert check("") is None
    assert check(vault) is None
    assert check(config / "www" / "vault") == "vault_public"
    assert check(config / "www") == "vault_public"
    assert check(tmp_path / "missing") == "vault_not_found"
    assert check(vault, allowed=False) == "vault_not_allowed"


def test_vault_notes(tmp_path):
    day = date(2026, 10, 12)
    path = write_daily_note(str(tmp_path), day, "## Summary\n- Quiet day.")
    text = open(path, encoding="utf-8").read()
    assert text.startswith("---\ndate: 2026-10-12\n") and "# Monday, October 12, 2026" in text
    assert not any(name.endswith(".auri-tmp") for name in os.listdir(os.path.dirname(path)))

    rows = [
        RecallRow(id=1, kind="preference", content="Mia likes blue.", at=datetime(2026, 10, 1, tzinfo=timezone.utc), about=["Mia"]),
        RecallRow(id=2, kind="fact", content="The wifi router is in the hall.", at=None),
        RecallRow(id=3, kind="fact", content="Mia sees Dr. Lee.", at=None, about=["Mia"], sensitivity="private"),
        RecallRow(id=4, kind="commitment", content="Mia has a piano recital on Thursday, 2026-10-15.",
                  at=datetime(2026, 10, 1, tzinfo=timezone.utc), about=["Mia"],
                  due=datetime(2026, 10, 15, 7, tzinfo=timezone.utc)),  # local midnight in Vancouver
    ]
    note = open(write_memory_note(str(tmp_path), rows, tz=zone(VANCOUVER), generated_on=day), encoding="utf-8").read()
    assert note.index("## Household") < note.index("## Mia")
    assert "- Mia likes blue. (2026-09-30)" in note
    assert "- Mia sees Dr. Lee. 🔒" in note
    assert "- Mia has a piano recital on Thursday, 2026-10-15. (due 2026-10-15)" in note

    assert remove_daily_note(str(tmp_path), day) is True
    assert remove_daily_note(str(tmp_path), day) is False


# ── setup errors ────────────────────────────────────────────────────────────


class _PgError(Exception):
    def __init__(self, message: str, pgcode: str | None = None) -> None:
        super().__init__(message)
        self.pgcode = pgcode


class _DriverError(Exception):
    """Shaped like SQLAlchemy's DBAPIError: the driver's error is in .orig."""

    def __init__(self, message: str, pgcode: str | None = None) -> None:
        super().__init__(message)
        self.orig = _PgError(message, pgcode)


@pytest.mark.parametrize(
    ("message", "pgcode", "code"),
    [
        ("permission denied to create extension \"vector\"", "42501", "insufficient_privilege"),
        ("permission denied for database auri", "42501", "insufficient_privilege"),
        ('could not open extension control file "vector.control"', "58P01", "pgvector_missing"),
        ('type "vector" does not exist', "42704", "pgvector_missing"),
        ("password authentication failed for user \"auri\"", None, "invalid_auth"),
        ("server closed the connection unexpectedly", None, "cannot_connect"),
    ],
)
def test_setup_errors_get_their_own_codes(message, pgcode, code):
    assert classify_setup_error(_DriverError(message, pgcode)).code == code
