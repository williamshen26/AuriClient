"""Which stay Guest Settings should hold, given the calendar's bookings.

A stay keeps its place until its guest memory is erased (an hour after
check-out), so guests who leave a little late still have it, and the next
guests' details never arrive while the last guests are still there. Then
the next booking takes over: one already under way (booked the same day)
or the next to start.

A stay that has started is never cut short by its own calendar: Airbnb's
export lists future dates only, so a stay in progress can drop out of it.
Before check-in the calendar decides, so a cancelled booking is replaced. A
stay written by another link has no such claim: a new link starts afresh,
replacing it (or clearing it when the new calendar has nothing coming).
Calendars without times (Airbnb's) get the host's usual check-in and
check-out times, for every booking.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo
from typing import Any, Mapping

from ..const import (
    CONF_BOOKING_CALENDAR_SOURCE,
    CONF_BOOKING_ID,
    CONF_CHECK_IN,
    CONF_CHECK_OUT,
    CONF_GUEST_ASKED_NAME,
    CONF_GUEST_COUNT,
    CONF_GUEST_NAME,
    CONF_GUEST_PHONE_NUMBER,
    CONF_NOTES,
)
from .calendar import Booking

# What Guest Settings' date-and-time fields store (local time).
OPTION_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
# Cleared when a new booking takes over: they belong to the previous guests
# (with the name they gave Auri, which only counts for their stay anyway).
_PER_STAY_FIELDS = (CONF_GUEST_NAME, CONF_GUEST_COUNT, CONF_GUEST_PHONE_NUMBER, CONF_NOTES, CONF_GUEST_ASKED_NAME)


@dataclass(frozen=True, slots=True)
class Stay:
    booking: Booking
    check_in: datetime
    check_out: datetime


@dataclass(frozen=True, slots=True)
class CurrentStay:
    """The stay Guest Settings hold now."""

    booking_id: str
    check_in: datetime | None
    check_out: datetime | None


@dataclass(frozen=True, slots=True)
class Decision:
    # "keep" (leave as is), "update" (same booking), "switch" (another booking),
    # "clear" (a stay another link wrote, with nothing to replace it) or "none"
    action: str
    stay: Stay | None = None


def parse_time_of_day(value: Any, default: time) -> time:
    try:
        return time.fromisoformat(str(value).strip())
    except ValueError:
        return default


def parse_option_time(value: Any, tz: tzinfo) -> datetime | None:
    """A Guest Settings date and time (local when it has no zone)."""
    try:
        parsed = datetime.fromisoformat(str(value or "").strip())
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=tz)


def current_stay(options: Mapping[str, Any], tz: tzinfo) -> CurrentStay | None:
    booking_id = str(options.get(CONF_BOOKING_ID) or "").strip()
    check_in = parse_option_time(options.get(CONF_CHECK_IN), tz)
    check_out = parse_option_time(options.get(CONF_CHECK_OUT), tz)
    if not (booking_id or check_in or check_out):
        return None
    return CurrentStay(booking_id=booking_id, check_in=check_in, check_out=check_out)


def stay_state(options: Mapping[str, Any], now: datetime, tz: tzinfo) -> tuple[str, datetime | None]:
    """The held stay's state (none, upcoming, staying or checked_out) and when it next changes."""
    stay = current_stay(options, tz)
    if stay is None or (stay.check_in is None and stay.check_out is None):
        return "none", None
    if stay.check_in is not None and now < stay.check_in:
        return "upcoming", stay.check_in
    if stay.check_out is None or now < stay.check_out:
        return "staying", stay.check_out
    return "checked_out", None


def stay_active(options: Mapping[str, Any], now: datetime, tz: tzinfo, grace: timedelta) -> bool:
    """Whether the held stay is under way: from check-in until its guest memory is erased (check-out + grace)."""
    stay = current_stay(options, tz)
    if stay is None or (stay.check_in is None and stay.check_out is None):
        return False
    started = stay.check_in is None or now >= stay.check_in
    ended = stay.check_out is not None and now >= stay.check_out + grace
    return started and not ended


def _at(moment: date | datetime, at: time, tz: tzinfo) -> datetime:
    if isinstance(moment, datetime):
        return moment.astimezone(tz) if moment.tzinfo else moment.replace(tzinfo=tz)
    return datetime.combine(moment, at, tzinfo=tz)


def stay_for(booking: Booking, *, check_in_time: time, check_out_time: time, tz: tzinfo) -> Stay:
    return Stay(booking, _at(booking.start, check_in_time, tz), _at(booking.end, check_out_time, tz))


def choose(
    bookings: list[Booking],
    *,
    now: datetime,
    tz: tzinfo,
    check_in_time: time,
    check_out_time: time,
    grace: timedelta,
    current: CurrentStay | None,
    protected: bool = True,
) -> Decision:
    """What to do with Guest Settings now.

    protected: the held stay was written by this link or typed by hand, so it
    keeps its place while under way. A stay another link wrote doesn't.
    """
    stays = [stay_for(b, check_in_time=check_in_time, check_out_time=check_out_time, tz=tz) for b in bookings]
    same = next((s for s in stays if current is not None and current.booking_id and s.booking.code == current.booking_id), None)
    if current is not None and current.check_out is not None and now < current.check_out + grace:
        if same is not None:
            return Decision("update", same)
        if protected and (current.check_in is None or now >= current.check_in):
            return Decision("keep")  # under way, so it stays even if the calendar dropped it

    upcoming = sorted((s for s in stays if s.check_out + grace > now), key=lambda s: s.check_in)
    if not upcoming:
        return Decision("clear") if current is not None and not protected else Decision("none")
    first = upcoming[0]
    if current is not None and first.booking.code == current.booking_id:
        return Decision("update", first)
    return Decision("switch", first)


def following(
    bookings: list[Booking],
    *,
    after_code: str | None,
    after_check_in: datetime | None,
    now: datetime,
    tz: tzinfo,
    check_in_time: time,
    check_out_time: time,
) -> Stay | None:
    """The next booking to start after the one Guest Settings hold."""
    floor = max(now, after_check_in) if after_check_in is not None else now
    stays = sorted(
        (stay_for(b, check_in_time=check_in_time, check_out_time=check_out_time, tz=tz) for b in bookings),
        key=lambda s: s.check_in,
    )
    return next((s for s in stays if s.booking.code != after_code and s.check_in > floor), None)


def format_option_time(moment: datetime, tz: tzinfo) -> str:
    return moment.astimezone(tz).strftime(OPTION_TIME_FORMAT)


def apply(options: Mapping[str, Any], decision: Decision, *, tz: tzinfo, source: str | None = None) -> dict[str, Any]:
    """Guest Settings with the decision applied (a copy; unchanged for "keep" and "none").

    source: the fingerprint of the link that wrote the stay, recorded with it.
    """
    updated = dict(options)
    if decision.action == "clear":
        for key in (CONF_BOOKING_ID, CONF_CHECK_IN, CONF_CHECK_OUT, CONF_BOOKING_CALENDAR_SOURCE, *_PER_STAY_FIELDS):
            updated.pop(key, None)
        return updated
    stay = decision.stay
    if stay is None or decision.action not in ("update", "switch"):
        return updated
    updated[CONF_CHECK_IN] = format_option_time(stay.check_in, tz)
    updated[CONF_CHECK_OUT] = format_option_time(stay.check_out, tz)
    if source:
        updated[CONF_BOOKING_CALENDAR_SOURCE] = source
    booking = stay.booking
    if decision.action == "switch":
        updated[CONF_BOOKING_ID] = booking.code
        for key in _PER_STAY_FIELDS:
            updated.pop(key, None)
        if booking.guest_name:
            updated[CONF_GUEST_NAME] = booking.guest_name
        if booking.guest_count:
            updated[CONF_GUEST_COUNT] = booking.guest_count
        return updated
    # The same booking: what the host entered stays; the calendar only fills in what's missing.
    if booking.guest_name and not str(updated.get(CONF_GUEST_NAME) or "").strip():
        updated[CONF_GUEST_NAME] = booking.guest_name
    if booking.guest_count and not updated.get(CONF_GUEST_COUNT):
        updated[CONF_GUEST_COUNT] = booking.guest_count
    return updated
