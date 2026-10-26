"""Reads a booking calendar (an iCal link) into its bookings.

Airbnb exports each reservation as an all-day event titled "Reserved", with
the reservation's link (holding its code, like HMABCDE123) and the last 4
digits of the guest's phone in the description: no name, party size or
times. Dates the host blocked are events titled "Airbnb (Not available)".
VRBO, Booking.com and property-management tools export the same format;
some put the guest's name in the title and the party size in the
description, and those are used when they're there. The phone digits are
never kept.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import hashlib
import re
from typing import Any
from urllib.parse import urlparse

FETCH_TIMEOUT_SECONDS = 20
_MAX_CALENDAR_CHARS = 5_000_000
_AIRBNB_LINK = re.compile(r"/reservations/details/([A-Z0-9]{10})\b", re.IGNORECASE)
_AIRBNB_CODE = re.compile(r"\b(HM[A-Z0-9]{8})\b")
_BLOCK_WORDS = ("not available", "unavailable", "blocked", "closed", "owner stay", "maintenance")
_GENERIC_TITLES = {"reserved", "booked", "booking", "reservation", "confirmed", "occupied", "airbnb", "vrbo"}
_TITLE_PREFIX = re.compile(r"^(?:reserved|booked|booking|reservation|confirmed)\s*[-:–—]\s*", re.IGNORECASE)
_GUEST_COUNT = re.compile(r"(?:number of guests|guests?|party size)\s*[:=]\s*(\d{1,2})\b", re.IGNORECASE)
_SIMPLE_ID = re.compile(r"[\w.-]{1,40}")
_MAX_NAME_CHARS = 60


class CalendarError(Exception):
    """The calendar couldn't be fetched or read. Messages never contain the link, which is a secret."""


@dataclass(frozen=True, slots=True)
class Booking:
    """One reservation in the calendar."""

    code: str  # the platform's reservation code, or a stable ID made from the event
    start: date | datetime  # a date for all-day events (Airbnb), a time when the calendar gives one
    end: date | datetime
    guest_name: str | None = None
    guest_count: int | None = None


@dataclass(frozen=True, slots=True)
class Fetched:
    text: str | None  # None when the calendar hasn't changed since the last fetch (HTTP 304)
    etag: str | None = None
    last_modified: str | None = None


def valid_calendar_url(url: str) -> bool:
    parsed = urlparse(url.strip())
    return parsed.scheme.lower() in ("https", "http", "webcal") and bool(parsed.hostname)


def link_fingerprint(url: str) -> str:
    """A short stand-in for a calendar link (the link itself is a secret), to tell links apart."""
    return hashlib.sha256(url.strip().encode("utf-8")).hexdigest()[:16]


def bookings_only_feed(url: str) -> bool:
    """Booking.com titles every booking "CLOSED - Not available": its feed holds bookings only."""
    host = (urlparse(url.strip()).hostname or "").lower()
    return host == "booking.com" or host.endswith(".booking.com")


async def fetch_calendar(
    session: Any, url: str, *, etag: str | None = None, last_modified: str | None = None
) -> Fetched:
    """Download the calendar with an aiohttp session; Fetched.text is None when it's unchanged."""
    import aiohttp  # pylint: disable=import-outside-toplevel

    target = url.strip()
    if target.lower().startswith("webcal://"):
        target = "https://" + target[len("webcal://") :]
    headers = {}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    try:
        async with session.get(
            target, headers=headers, timeout=aiohttp.ClientTimeout(total=FETCH_TIMEOUT_SECONDS)
        ) as response:
            if response.status == 304:
                return Fetched(text=None, etag=etag, last_modified=last_modified)
            if response.status >= 400:
                raise CalendarError(f"The calendar link answered HTTP {response.status}")
            text = await response.text()
            if len(text) > _MAX_CALENDAR_CHARS:
                raise CalendarError("The calendar is too large")
            return Fetched(text=text, etag=response.headers.get("ETag"), last_modified=response.headers.get("Last-Modified"))
    except (aiohttp.ClientError, asyncio.TimeoutError, UnicodeDecodeError) as err:
        # The error's own message can include the address; only its kind is reported.
        raise CalendarError(f"Couldn't reach the calendar link ({type(err).__name__})") from err


def _is_block(title: str) -> bool:
    lowered = title.lower()
    return any(word in lowered for word in _BLOCK_WORDS)


def _guest_name(summary: str) -> str | None:
    """A name in the event's title ("Reserved - Sarah Lee", "Sarah Lee"); None for generic titles."""
    title = _TITLE_PREFIX.sub("", summary.strip()).strip()
    if not title or title.lower() in _GENERIC_TITLES or _is_block(title) or len(title) > _MAX_NAME_CHARS:
        return None
    if any(char.isdigit() for char in title) or "@" in title or "/" in title:
        return None
    return title


def _guest_count(description: str) -> int | None:
    match = _GUEST_COUNT.search(description)
    count = int(match.group(1)) if match else 0
    return count if 0 < count <= 50 else None


def _reservation_code(description: str) -> str | None:
    match = _AIRBNB_LINK.search(description) or _AIRBNB_CODE.search(description)
    return match.group(1).upper() if match else None


def _stable_id(uid: str | None, start: date | datetime, end: date | datetime) -> str:
    """A booking ID for calendars without reservation codes, the same every time the event is read."""
    raw = (uid or "").strip() or f"{start.isoformat()}/{end.isoformat()}"
    local = raw.split("@", 1)[0]
    if _SIMPLE_ID.fullmatch(local):
        return local
    return "event-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _day(moment: date | datetime) -> date:
    return moment.date() if isinstance(moment, datetime) else moment


def parse_bookings(text: str, *, bookings_only: bool = False) -> list[Booking]:
    """The bookings in an iCal file, by start date; blocked dates and cancelled events are left out."""
    # pylint: disable=import-outside-toplevel
    from ical.calendar_stream import IcsCalendarStream
    from ical.exceptions import CalendarParseError

    try:
        calendar = IcsCalendarStream.calendar_from_ics(text)
    except CalendarParseError as err:
        raise CalendarError("The link doesn't return a valid iCal calendar") from err

    bookings = []
    for event in calendar.events:
        if str(event.status or "").upper().endswith("CANCELLED"):
            continue
        summary = str(event.summary or "")
        description = str(event.description or "")
        code = _reservation_code(description)
        if code is None and _is_block(summary) and not bookings_only:
            continue
        start = event.dtstart
        end = event.dtend or (start if isinstance(start, datetime) else start + timedelta(days=1))
        bookings.append(
            Booking(
                code=code or _stable_id(event.uid, start, end),
                start=start,
                end=end,
                guest_name=_guest_name(summary),
                guest_count=_guest_count(description),
            )
        )
    return sorted(bookings, key=lambda booking: _day(booking.start))
