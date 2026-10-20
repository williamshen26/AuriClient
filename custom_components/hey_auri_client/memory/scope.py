"""Memory scopes: the owner's memory now, one per guest booking later.

Guest memory is switched off (GUEST_MEMORY_ENABLED): in guest mode nothing
is recalled or archived. What's here is what the guest feature builds on:
each booking gets its own scope, deleted for good at check-out (or when the
booking is replaced), and the owner's scope is used again as soon as the
mode switches back. Owner data is never touched by any of it.
"""
from __future__ import annotations

from datetime import datetime
import re
from typing import Any, Iterable

from homeassistant.util import dt as dt_util

OWNER_SCOPE = "owner"
GUEST_SCOPE_PREFIX = "guest:"
GUEST_MEMORY_ENABLED = False

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def guest_scope(booking_id: Any) -> str | None:
    """Scope name for a booking, or None without a usable booking id."""
    booking = _UNSAFE.sub("", str(booking_id or "").strip())[:64]
    return f"{GUEST_SCOPE_PREFIX}{booking}" if booking else None


def scope_for_turn(mode: str | None, booking_id: Any) -> str | None:
    """The scope a turn reads and writes, or None when it gets no memory."""
    if mode == "owner":
        return OWNER_SCOPE
    if mode == "guest" and GUEST_MEMORY_ENABLED:
        return guest_scope(booking_id)
    return None


def parse_check_out(value: Any) -> datetime | None:
    """The guest options' check-out time (local when no zone is given)."""
    parsed = dt_util.parse_datetime(str(value or "").strip()) if value else None
    if parsed is None:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt_util.get_default_time_zone())


def scopes_to_purge(
    scopes: Iterable[str], *, booking_id: Any, check_out: datetime | None, now: datetime
) -> list[str]:
    """Guest scopes whose data must go: every booking but the current one, and that one after check-out."""
    current = guest_scope(booking_id)
    checked_out = check_out is not None and now >= check_out
    return sorted(
        scope
        for scope in set(scopes)
        if scope.startswith(GUEST_SCOPE_PREFIX) and (scope != current or checked_out)
    )
