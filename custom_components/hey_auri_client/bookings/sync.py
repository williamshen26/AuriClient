"""Keeps Guest Settings in step with the host's booking calendar.

It checks every 30 minutes, shortly after startup, and just after the current
stay's guest memory is erased (an hour after check-out), when the next
booking takes its place. Guest Settings are written only when something
changed; writing them runs the integration's usual update, which starts guest
memory again for a new booking. When the calendar can't be read, the last
stay it gave stays in place, and after 6 hours Home Assistant shows a
notification.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, time, timedelta
from functools import partial
import logging
from typing import Any, Mapping

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_call_later, async_track_point_in_time, async_track_time_interval
from homeassistant.util import dt as dt_util

from ..const import (
    CONF_BOOKING_CALENDAR_SOURCE,
    CONF_BOOKING_CALENDAR_URL,
    CONF_CHECK_OUT,
    CONF_USUAL_CHECK_IN_TIME,
    CONF_USUAL_CHECK_OUT_TIME,
    DEFAULT_USUAL_CHECK_IN_TIME,
    DEFAULT_USUAL_CHECK_OUT_TIME,
    DOMAIN,
)
from ..memory.guest import CHECK_OUT_GRACE
from .calendar import Booking, CalendarError, bookings_only_feed, fetch_calendar, link_fingerprint, parse_bookings
from .schedule import apply, choose, current_stay, following, parse_option_time, parse_time_of_day

_LOGGER = logging.getLogger(__name__)

SYNC_INTERVAL = timedelta(minutes=30)
_STARTUP_DELAY_SECONDS = 20
# Just after the erase, so guest memory has closed the old stay before the next one is entered.
_AFTER_ERASE = timedelta(seconds=30)
_NOTIFY_AFTER = timedelta(hours=6)
NOTIFICATION_ID = f"{DOMAIN}_booking_calendar"


def usual_times(options: Mapping[str, Any]) -> tuple[time, time]:
    """The host's usual check-in and check-out times, used for calendars with dates only."""
    return (
        parse_time_of_day(options.get(CONF_USUAL_CHECK_IN_TIME) or DEFAULT_USUAL_CHECK_IN_TIME, time(15)),
        parse_time_of_day(options.get(CONF_USUAL_CHECK_OUT_TIME) or DEFAULT_USUAL_CHECK_OUT_TIME, time(11)),
    )


class BookingSync:
    """Reads the booking calendar and writes the stay it finds into Guest Settings."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.status: dict[str, Any] = {"calendar": "off"}
        self._lock = asyncio.Lock()
        self._listeners: list[Callable[[], None]] = []
        self._unsubs: list[Callable[[], None]] = []
        self._transition_unsub: Callable[[], None] | None = None
        self._bookings: list[Booking] | None = None
        self._etag: str | None = None
        self._last_modified: str | None = None
        self._failing_since: datetime | None = None
        self._notified = False
        self._settings = self._settings_of(entry.options)

    @staticmethod
    def _settings_of(options: Mapping[str, Any]) -> tuple[str, str, str]:
        return (
            str(options.get(CONF_BOOKING_CALENDAR_URL) or "").strip(),
            str(options.get(CONF_USUAL_CHECK_IN_TIME) or ""),
            str(options.get(CONF_USUAL_CHECK_OUT_TIME) or ""),
        )

    @property
    def url(self) -> str:
        return str(self.entry.options.get(CONF_BOOKING_CALENDAR_URL) or "").strip()

    def async_add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener) if listener in self._listeners else None

    def _notify(self) -> None:
        for listener in list(self._listeners):
            listener()

    async def async_start(self) -> None:
        if not self.url:
            self.status = {"calendar": "off"}
            self._notify()
            return
        self.status = {**self.status, "calendar": "on"}
        self._unsubs.append(async_track_time_interval(self.hass, self._async_tick, SYNC_INTERVAL))
        self._unsubs.append(async_call_later(self.hass, _STARTUP_DELAY_SECONDS, self._async_tick))
        self._schedule_transition()

    async def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        if self._transition_unsub is not None:
            self._transition_unsub()
            self._transition_unsub = None

    async def async_options_updated(self, options: Mapping[str, Any]) -> None:
        """Start again when the link or the usual times change; otherwise follow the stay's new check-out."""
        settings = self._settings_of(options)
        if settings != self._settings:
            self._settings = settings
            await self.async_stop()
            self._bookings = None
            self._etag = self._last_modified = None
            self._recovered()
            self.status = {"calendar": "off"}
            await self.async_start()
        elif self.url:
            self._schedule_transition()
        self._notify()

    async def _async_tick(self, _now: datetime | None = None) -> None:
        await self.async_sync()

    async def async_sync(self) -> dict[str, Any]:
        """Read the calendar and update Guest Settings; returns what it found."""
        async with self._lock:
            url = self.url
            if not url:
                return dict(self.status)
            now = dt_util.now()
            cached = self._bookings is not None
            try:
                fetched = await fetch_calendar(
                    async_get_clientsession(self.hass),
                    url,
                    etag=self._etag if cached else None,
                    last_modified=self._last_modified if cached else None,
                )
                if fetched.text is not None or not cached:
                    self._bookings = await self.hass.async_add_executor_job(
                        partial(parse_bookings, fetched.text or "", bookings_only=bookings_only_feed(url))
                    )
                    self._etag, self._last_modified = fetched.etag, fetched.last_modified
            except CalendarError as err:
                return self._failed(str(err), now)
            self._recovered()
            return self._apply(self._bookings or [], now, url)

    def _apply(self, bookings: list[Booking], now: datetime, url: str) -> dict[str, Any]:
        tz = dt_util.get_default_time_zone()
        options = self.entry.options
        check_in_time, check_out_time = usual_times(options)
        source = link_fingerprint(url)
        # A stay this link wrote, or one typed by hand, keeps its place while under way; another link's doesn't.
        held_source = str(options.get(CONF_BOOKING_CALENDAR_SOURCE) or "")
        decision = choose(
            bookings,
            now=now,
            tz=tz,
            check_in_time=check_in_time,
            check_out_time=check_out_time,
            grace=CHECK_OUT_GRACE,
            current=current_stay(options, tz),
            protected=held_source in ("", source),
        )
        updated = apply(options, decision, tz=tz, source=source)
        changed = updated != dict(options)
        if changed:
            self.hass.config_entries.async_update_entry(self.entry, options=updated)
            _LOGGER.info(
                "Booking calendar: %s %s",
                {"switch": "moved to booking", "update": "updated booking", "clear": "cleared the previous link's booking"}.get(
                    decision.action, decision.action
                ),
                decision.stay.booking.code if decision.stay else "",
            )
        held = current_stay(updated, tz)
        upcoming = following(
            bookings,
            after_code=held.booking_id if held else None,
            after_check_in=held.check_in if held else None,
            now=now,
            tz=tz,
            check_in_time=check_in_time,
            check_out_time=check_out_time,
        )
        self.status = {
            "calendar": "on",
            "last_sync": now.isoformat(),
            "bookings": len(bookings),
            "next_booking": upcoming.booking.code if upcoming else None,
            "next_check_in": upcoming.check_in.isoformat() if upcoming else None,
        }
        self._schedule_transition()
        self._notify()
        return {**self.status, "action": decision.action, "changed": changed}

    @callback
    def _schedule_transition(self) -> None:
        """Check again just after the held stay's guest memory is erased."""
        if self._transition_unsub is not None:
            self._transition_unsub()
            self._transition_unsub = None
        check_out = parse_option_time(self.entry.options.get(CONF_CHECK_OUT), dt_util.get_default_time_zone())
        if not self.url or check_out is None:
            return
        at = check_out + CHECK_OUT_GRACE + _AFTER_ERASE
        if at > dt_util.now():
            self._transition_unsub = async_track_point_in_time(self.hass, self._async_at_transition, at)

    async def _async_at_transition(self, _now: datetime) -> None:
        self._transition_unsub = None
        await self.async_sync()

    def _failed(self, message: str, now: datetime) -> dict[str, Any]:
        if self._failing_since is None:
            self._failing_since = now
            _LOGGER.warning("Booking calendar: %s", message)
        self.status = {**self.status, "calendar": "on", "error": message, "last_attempt": now.isoformat()}
        if not self._notified and now - self._failing_since >= _NOTIFY_AFTER:
            persistent_notification.async_create(
                self.hass,
                f"Auri hasn't been able to read the booking calendar since "
                f"{dt_util.as_local(self._failing_since):%Y-%m-%d %H:%M}: {message}. Guest Settings keep the last "
                "stay it found. Check the link under Hey Auri Client → Configure → Booking calendar.",
                title="Hey Auri",
                notification_id=NOTIFICATION_ID,
            )
            self._notified = True
        self._notify()
        return dict(self.status)

    def _recovered(self) -> None:
        if self._notified:
            persistent_notification.async_dismiss(self.hass, NOTIFICATION_ID)
        self._failing_since = None
        self._notified = False
        self.status.pop("error", None)
        self.status.pop("last_attempt", None)
