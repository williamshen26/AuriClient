"""Guest memory for home-share stays (Airbnb and similar), from the Guest Settings.

Guests get the same memory owners have, but in a schema of their own
(auri_guest_memory) that holds the current stay only. It's started again,
seeded from the guest details in Guest Settings, when the host asks for it
or a new stay is entered (a new booking ID, or a new check-in when there is
no booking ID). An hour after check-out it's erased, and it stays closed
(nothing recalled, kept or learned) until the next stay is entered; it's
also erased when guest memory is switched off. Nothing here ever reads or
writes the owner's memory.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Mapping

from homeassistant.util import dt as dt_util

from ..const import CONF_BOOKING_ID, CONF_CHECK_IN, CONF_CHECK_OUT, CONF_GUEST_COUNT, CONF_GUEST_NAME, CONF_NOTES
from .config import CONF_GUEST_MEMORY_ENABLED, CONF_GUEST_MEMORY_RESET_AT
from .redaction import contains_secret
from .store import NewMemory

# Kept in the guest schema's meta table: which stay the schema holds.
STAY_META_KEY = "guest_stay"
# Guest memory stays available this long after check-out, for guests who leave a little late.
CHECK_OUT_GRACE = timedelta(hours=1)
_MAX_NOTE_LINES = 10
_MAX_NOTE_CHARS = 500
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def guest_memory_enabled(options: Mapping[str, Any]) -> bool:
    return bool(options.get(CONF_GUEST_MEMORY_ENABLED, False))


def parse_stay_time(value: Any) -> datetime | None:
    """A Guest Settings check-in or check-out time (local when no zone is given)."""
    parsed = dt_util.parse_datetime(str(value or "").strip()) if value else None
    if parsed is None:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt_util.get_default_time_zone())


def guest_erase_time(options: Mapping[str, Any]) -> datetime | None:
    """When the stay's guest memory is erased: an hour after check-out (None without a check-out)."""
    check_out = parse_stay_time(options.get(CONF_CHECK_OUT))
    return check_out + CHECK_OUT_GRACE if check_out else None


def stay_over(options: Mapping[str, Any], now: datetime | None = None) -> bool:
    """Whether the stay has ended (its erase time has passed); guest memory is closed until the next stay."""
    erase_at = guest_erase_time(options)
    return erase_at is not None and (now or dt_util.now()) >= erase_at


def stay_key(options: Mapping[str, Any]) -> str:
    """What identifies a stay: the booking ID, or the check-in time when there is none."""
    booking = str(options.get(CONF_BOOKING_ID) or "").strip()
    return f"booking:{booking}" if booking else f"check_in:{str(options.get(CONF_CHECK_IN) or '').strip()}"


def stay_marker(options: Mapping[str, Any]) -> dict[str, str]:
    """The stay and reset request a guest schema was last set up for."""
    return {"stay": stay_key(options), "reset_at": str(options.get(CONF_GUEST_MEMORY_RESET_AT) or "")}


def _when(moment: datetime) -> str:
    local = dt_util.as_local(moment)
    return f"{_WEEKDAYS[local.weekday()]} {local:%Y-%m-%d %H:%M}"


def seed_memories(options: Mapping[str, Any]) -> list[tuple[NewMemory, list[tuple[str, str]]]]:
    """The memories a stay starts with, from the guest details, each with the subjects it's about.

    Property information (Wi-Fi, house rules, parking...) isn't copied: Auri
    reads it from Guest Settings when asked, so it stays current. Note lines
    that are templates or look like a secret (a door code) are left out.
    """
    seeds: list[tuple[NewMemory, list[tuple[str, str]]]] = []
    now = dt_util.utcnow()

    def add(content: str, subjects: list[tuple[str, str]], importance: int) -> None:
        memory = NewMemory(kind="fact", content=content, source="import", importance=importance, occurred_at=now)
        seeds.append((memory, subjects))

    name = " ".join(str(options.get(CONF_GUEST_NAME) or "").split())[:60]
    if name:
        add(f"{name} booked this stay.", [(name, "person")], 4)
    try:
        count = int(options.get(CONF_GUEST_COUNT) or 0)
    except (TypeError, ValueError):
        count = 0
    if count > 0:
        add("1 guest is staying." if count == 1 else f"{count} guests are staying.", [("household", "household")], 4)

    check_in = parse_stay_time(options.get(CONF_CHECK_IN))
    check_out = parse_stay_time(options.get(CONF_CHECK_OUT))
    if check_in and check_out:
        add(f"The stay runs from {_when(check_in)} to {_when(check_out)}.", [("household", "household")], 5)
    elif check_in:
        add(f"The stay starts {_when(check_in)}.", [("household", "household")], 5)
    elif check_out:
        add(f"Check-out is {_when(check_out)}.", [("household", "household")], 5)

    lines = [" ".join(line.split()) for line in str(options.get(CONF_NOTES) or "").splitlines()]
    notes = [line for line in lines if line and "{{" not in line and "{%" not in line and not contains_secret(line)]
    for line in notes[:_MAX_NOTE_LINES]:
        add(f"The host's note about this stay: {line[:_MAX_NOTE_CHARS]}", [("household", "household")], 4)
    return seeds
