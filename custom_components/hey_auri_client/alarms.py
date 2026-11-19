"""Auri's alarms: wake-up times kept by Hey Auri Client, ringing on voice satellites.

Home Assistant has timers (a length of time) but no alarm clock: nothing in it rings at a time of
day without an automation. So the client keeps the alarms itself, in a Store so they survive
restarts, and schedules each ring with Home Assistant's async_track_point_in_utc_time. No
automation is involved.

At the alarm time, each of the alarm's satellites rings the way a timer's end rings it: a 1-second
Home Assistant timer there (AuriTimers.ring). The satellite plays its own timer ring, its LED ring
pulsing, until "stop", the wake word, its button, or 15 minutes. That works on any satellite Home
Assistant rings timers on.

An alarm rings on the satellite it was set at, or on every satellite in the room named. Set from
the phone app or the web page, it needs a room, unless the home has only one satellite. An alarm
repeats on chosen days or rings once (a one-time alarm goes once its snooze window is over). It
can skip a day, be turned off and on (its switch.auri_alarm_<id>, on the Home dashboard), and be
snoozed.

A satellite that's away at the alarm time (Home Assistant restarting, its Wi-Fi) rings as soon as
it's back, up to 15 minutes late; after that the ring is skipped. Times are the home's, in Home
Assistant's time zone. A time that doesn't exist on the night clocks go forward rings an hour
later, and one that happens twice when they go back rings once.

Guests' alarms (set in guest mode) are seen only by guests, and go at check-out or when the stay
changes. The household's alarms don't ring while Auri is in guest mode (calendars.AuriCalendars.mode):
a repeating one rings again on its next day in owner mode, a one-time one is let go.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time as dtime, timedelta, timezone, tzinfo
from functools import partial
import logging
import re
from typing import TYPE_CHECKING, Any
import uuid

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_CORE_CONFIG_UPDATE
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_point_in_utc_time, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import CONF_CHECK_OUT, DOMAIN, EVENT_ALARM_RINGING
from .entities.alarm import AuriAlarmSwitch
from .memory.guest import parse_stay_time, stay_key
from .timers import normalize_name

if TYPE_CHECKING:
    from .timers import AuriTimers

_LOGGER = logging.getLogger(__name__)

STORAGE_KEY = f"{DOMAIN}_alarms"
STORAGE_VERSION = 1
# How often rings waiting for their satellite, finished one-time alarms and ended stays are looked at.
TICK = timedelta(seconds=10)
# A satellite that's away at the alarm time still rings this long after it, once it's back.
LATE_RING_LIMIT = timedelta(minutes=15)
# After a ring: how long "snooze" and "stop" mean that alarm, and a one-time alarm is kept.
RECENT_RING = timedelta(minutes=30)
# The satellite stops ringing by itself after this long (its firmware).
RING_LENGTH = timedelta(minutes=15)
SNOOZE_MINUTES = 9
MAX_SNOOZE_MINUTES = 60
# The switch a satellite rings with (the Auri firmware exposes it), to stop a ring still going.
RING_SWITCH_NAME = "Timer Ringing"

DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_FULL_DAY_NAMES = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
WEEKDAYS = (0, 1, 2, 3, 4)
WEEKEND = (5, 6)
EVERY_DAY = (0, 1, 2, 3, 4, 5, 6)
_DAY_WORDS = {
    "daily": EVERY_DAY,
    "everyday": EVERY_DAY,
    "every day": EVERY_DAY,
    "all": EVERY_DAY,
    "weekdays": WEEKDAYS,
    "weekday": WEEKDAYS,
    "workdays": WEEKDAYS,
    "weekends": WEEKEND,
    "weekend": WEEKEND,
}
_ONCE_WORDS = frozenset({"once", "none", "never", "one-time", "one time", "no"})
_CLOCK = re.compile(r"(\d{1,2})(?::?(\d{2}))?(?::\d{2})?\s*(am|pm|a|p)?")
_FAR = datetime.max.replace(tzinfo=timezone.utc)


def new_alarm_id() -> str:
    """Auri's short id for an alarm (its switch is switch.auri_alarm_<id>)."""
    return uuid.uuid4().hex[:8]


def parse_clock_time(value: Any) -> dtime:
    """A time of day: 07:00 or 19:30 (what the tools ask for), also 7, 0700 or 7:30 PM."""
    if isinstance(value, dtime):
        return value.replace(second=0, microsecond=0, tzinfo=None)
    text = str(value or "").strip().lower().replace(".", "")
    match = _CLOCK.fullmatch(text)
    problem = f"Unrecognized time {value!r}: give HH:MM on a 24-hour clock, such as 07:00 or 19:30."
    if match is None:
        raise ValueError(problem)
    hour, minute = int(match[1]), int(match[2] or 0)
    if match[3]:
        if not 1 <= hour <= 12:
            raise ValueError(problem)
        hour = hour % 12 + (12 if match[3].startswith("p") else 0)
    if hour > 23 or minute > 59:
        raise ValueError(problem)
    return dtime(hour, minute)


def parse_days(value: Any) -> tuple[int, ...]:
    """Repeat days (0 is Monday): mon to sun or their names, weekdays, weekends, daily; none or once: ()."""
    if value is None or value == "" or value == []:
        return ()
    items = value if isinstance(value, (list, tuple, set)) else re.split(r"[,;/]+|\s+and\s+", str(value))
    days: set[int] = set()
    for item in items:
        token = " ".join(str(item).strip().lower().replace(".", "").split())
        if not token or token in _ONCE_WORDS:
            continue
        if token in _DAY_WORDS:
            days.update(_DAY_WORDS[token])
            continue
        for word in token.split():
            day = next((i for i, full in enumerate(_FULL_DAY_NAMES) if len(word) >= 2 and full.startswith(word)), None)
            if day is None:
                raise ValueError(
                    f"Unrecognized day {item!r}: give days as mon, tue, wed, thu, fri, sat and sun, "
                    "or weekdays, weekends or daily (none for a one-time alarm)."
                )
            days.add(day)
    return tuple(sorted(days))


def describe_days(days: Iterable[int]) -> str:
    """How an alarm repeats: once, every day, weekdays, weekends, or its days (mon, wed, fri)."""
    chosen = tuple(sorted(set(days)))
    if not chosen:
        return "once"
    if chosen == EVERY_DAY:
        return "every day"
    if chosen == WEEKDAYS:
        return "weekdays"
    if chosen == WEEKEND:
        return "weekends"
    return ", ".join(DAY_NAMES[day] for day in chosen)


def at_local(day: date, at: dtime, tz: tzinfo) -> datetime:
    """A day and time in the home's zone, as a UTC moment.

    A time the clocks skip (02:30 the night they go forward) comes out an hour later, and one that
    happens twice (01:30 the night they go back) is the first of the two.
    """
    return datetime.combine(day, at, tzinfo=tz).astimezone(timezone.utc)


def first_date(at: dtime, now: datetime, tz: tzinfo) -> date:
    """The day a one-time alarm at this time rings: today if the time is still to come, else tomorrow."""
    today = now.astimezone(tz).date()
    return today if at_local(today, at, tz) > now else today + timedelta(days=1)


@dataclass
class Satellite:
    """A voice satellite an alarm can ring on, and its room."""

    device_id: str
    name: str
    area_id: str | None = None
    area_name: str | None = None
    area_aliases: tuple[str, ...] = ()

    @property
    def room(self) -> str:
        return self.area_name or self.name


@dataclass
class AlarmRecord:
    """One alarm: a time of day, ringing once or on its days, on its satellites."""

    id: str
    time: dtime
    # 0 is Monday; none: a one-time alarm, on `date`.
    days: tuple[int, ...] = ()
    date: date | None = None
    name: str | None = None
    enabled: bool = True
    device_ids: list[str] = field(default_factory=list)
    # Who set it, in what language; guest alarms belong to the stay they were set in.
    user_id: str | None = None
    language: str = "en"
    guest: bool = False
    stay: str | None = None
    # A repeating alarm's day that doesn't ring ("skip tomorrow's alarm").
    skip_date: date | None = None
    # Times are UTC: a snoozed ring, the next ring scheduled, and the last time it went off.
    snooze_until: datetime | None = None
    next_at: datetime | None = None
    rang_at: datetime | None = None
    rang_devices: list[str] = field(default_factory=list)
    # Satellites that were away when it went off: rung once they're back, until then.
    pending: dict[str, datetime] = field(default_factory=dict)

    @property
    def repeating(self) -> bool:
        return bool(self.days)

    def as_dict(self) -> dict[str, Any]:
        def stamp(moment: datetime | None) -> str | None:
            return moment.isoformat() if moment else None

        return {
            "id": self.id,
            "time": self.time.strftime("%H:%M"),
            "days": list(self.days),
            "date": self.date.isoformat() if self.date else None,
            "name": self.name,
            "enabled": self.enabled,
            "device_ids": list(self.device_ids),
            "user_id": self.user_id,
            "language": self.language,
            "guest": self.guest,
            "stay": self.stay,
            "skip_date": self.skip_date.isoformat() if self.skip_date else None,
            "snooze_until": stamp(self.snooze_until),
            "next_at": stamp(self.next_at),
            "rang_at": stamp(self.rang_at),
            "rang_devices": list(self.rang_devices),
            "pending": {device_id: until.isoformat() for device_id, until in self.pending.items()},
        }

    @classmethod
    def from_dict(cls, data: Any) -> AlarmRecord | None:
        if not isinstance(data, dict) or not data.get("id"):
            return None

        def moment(value: Any) -> datetime | None:
            parsed = dt_util.parse_datetime(str(value)) if value else None
            return dt_util.as_utc(parsed) if parsed is not None else None

        def day(value: Any) -> date | None:
            return dt_util.parse_date(str(value)) if value else None

        try:
            at = parse_clock_time(data.get("time"))
            days = tuple(sorted({int(d) for d in data.get("days") or [] if 0 <= int(d) <= 6}))
        except (TypeError, ValueError):
            return None
        record = cls(
            id=str(data["id"]),
            time=at,
            days=days,
            date=day(data.get("date")),
            name=data.get("name") or None,
            enabled=bool(data.get("enabled", True)),
            device_ids=[str(d) for d in data.get("device_ids") or []],
            user_id=data.get("user_id") or None,
            language=str(data.get("language") or "en"),
            guest=bool(data.get("guest")),
            stay=data.get("stay") or None,
            skip_date=day(data.get("skip_date")),
            snooze_until=moment(data.get("snooze_until")),
            next_at=moment(data.get("next_at")),
            rang_at=moment(data.get("rang_at")),
            rang_devices=[str(d) for d in data.get("rang_devices") or []],
        )
        for device_id, until in (data.get("pending") or {}).items():
            if (parsed := moment(until)) is not None:
                record.pending[str(device_id)] = parsed
        if not record.days and record.date is None:
            return None
        return record


def next_ring(record: AlarmRecord, now: datetime, tz: tzinfo) -> datetime | None:
    """When the alarm next rings (UTC), after now: its snooze, its date, or its next day not skipped."""
    if not record.enabled:
        return None
    if record.snooze_until is not None and record.snooze_until > now:
        return record.snooze_until
    if not record.days:
        if record.date is None:
            return None
        moment = at_local(record.date, record.time, tz)
        return moment if moment > now else None
    today = now.astimezone(tz).date()
    # Two weeks, so a weekly alarm whose next day is skipped still finds the one after.
    for offset in range(15):
        day = today + timedelta(days=offset)
        if day.weekday() not in record.days or day == record.skip_date:
            continue
        moment = at_local(day, record.time, tz)
        if moment > now:
            return moment
    return None


class AuriAlarms:
    """Every alarm on the hub, its switch, its rings, and keeping them across restarts."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, timers: AuriTimers) -> None:
        self.hass = hass
        self.entry = entry
        self.timers = timers
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._records: dict[str, AlarmRecord] = {}
        self._entities: dict[str, AuriAlarmSwitch] = {}
        self._add_entities: AddEntitiesCallback | None = None
        self._scheduled: dict[str, CALLBACK_TYPE] = {}
        self._unsubs: list[CALLBACK_TYPE] = []
        # The mode Auri is in now, owner or guest (AuriCalendars.mode, set in __init__.py). The
        # household's alarms don't ring while it's guest: guests aren't woken by the owner's alarm.
        self.mode: Callable[[], str] = lambda: "owner"

    # -- running ------------------------------------------------------------------------------

    async def async_start(self, add_entities: AddEntitiesCallback) -> None:
        """Load the kept alarms, add their switches, and schedule them (the switch platform's setup)."""
        self._add_entities = add_entities
        await self._async_load()
        add_entities([self._new_entity(record) for record in self._records.values()])
        self._unsubs.append(async_track_time_interval(self.hass, self._async_tick, TICK, name="Auri alarms"))
        # Home Assistant's time zone changed: every alarm moves to the new zone's clock.
        self._unsubs.append(self.hass.bus.async_listen(EVENT_CORE_CONFIG_UPDATE, self._async_config_updated))
        self._async_tick()

    async def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        for unsub in self._scheduled.values():
            unsub()
        self._scheduled.clear()
        await self._store.async_save(self._data())

    async def _async_load(self) -> None:
        data = await self._store.async_load() or {}
        now = dt_util.utcnow()
        for item in data.get("alarms") or []:
            record = AlarmRecord.from_dict(item)
            if record is None:
                continue
            self._records[record.id] = record
            due = record.next_at
            if record.enabled and due is not None and due <= now:
                # It went off while Home Assistant was off (the nightly update restarts it).
                snoozed = record.snooze_until is not None
                record.snooze_until = None
                if now - due <= LATE_RING_LIMIT:
                    self._ring(record, due=due, snoozed=snoozed)
                else:
                    _LOGGER.warning(
                        "The alarm %s at %s didn't ring: Home Assistant wasn't running then",
                        record.name or record.id,
                        dt_util.as_local(due).strftime("%Y-%m-%d %H:%M"),
                    )
            self._schedule(record)
        if self._records:
            self._save()

    def _data(self) -> dict[str, Any]:
        return {"alarms": [record.as_dict() for record in self._records.values()]}

    @callback
    def _save(self) -> None:
        self._store.async_delay_save(self._data, 1)

    @staticmethod
    def _tz() -> tzinfo:
        return dt_util.get_default_time_zone()

    # -- what tools and services use ----------------------------------------------------------

    def alarms(self, *, guest: bool = False) -> list[AlarmRecord]:
        """Alarms by their next ring (ones turned off last). A guest sees only this stay's guest alarms."""
        records = list(self._records.values())
        if guest:
            stay = stay_key(self.entry.options)
            records = [record for record in records if record.guest and record.stay == stay]
        return sorted(records, key=lambda record: (record.next_at or _FAR, record.time, record.id))

    def satellites(self) -> list[Satellite]:
        """The voice satellites an alarm can ring on (devices with an Assist satellite), with their rooms."""
        devices = dr.async_get(self.hass)
        areas = ar.async_get(self.hass)
        found: dict[str, Satellite] = {}
        for entry in er.async_get(self.hass).entities.values():
            if entry.domain != "assist_satellite" or entry.device_id is None or entry.disabled_by is not None:
                continue
            device = devices.async_get(entry.device_id)
            if device is None or device.id in found:
                continue
            area_id = entry.area_id or device.area_id
            area = areas.async_get_area(area_id) if area_id else None
            found[device.id] = Satellite(
                device_id=device.id,
                name=device.name_by_user or device.name or "Satellite",
                area_id=area.id if area else None,
                area_name=area.name if area else None,
                area_aliases=tuple(sorted(area.aliases)) if area else (),
            )
        return sorted(found.values(), key=lambda satellite: (satellite.room.casefold(), satellite.device_id))

    def choose_satellites(self, room: str | None, device_id: str | None) -> tuple[list[str], dict[str, Any] | None]:
        """Where a new alarm rings: the room named, else the satellite that asked, else the home's only one.

        Returns the satellites' device ids, or the answer to give instead (which room?).
        """
        satellites = self.satellites()
        rooms = sorted({satellite.room for satellite in satellites})
        if not satellites:
            return [], {
                "success": False,
                "error": "This home has no voice satellite for an alarm to ring on. Alarms ring on voice satellites only, not on phones.",
            }
        if room:
            matched = self._match_room(room, satellites)
            if matched:
                return matched, None
            return [], {
                "success": False,
                "needs_room": True,
                "error": f"No single room with a voice satellite matches {room!r}. Ask the user which of these rooms it should ring in.",
                "rooms": rooms,
            }
        if device_id and any(satellite.device_id == device_id for satellite in satellites):
            return [device_id], None
        if len(satellites) == 1:
            return [satellites[0].device_id], None
        return [], {
            "success": False,
            "needs_room": True,
            "error": (
                "Alarms ring on a voice satellite, and this request didn't come from one. Ask the user which "
                "room the alarm should ring in, then call set_alarm again with room."
            ),
            "rooms": rooms,
        }

    @staticmethod
    def _match_room(room: str, satellites: list[Satellite]) -> list[str]:
        """The satellites in the one room (area, its aliases, or a satellite's name) that `room` names."""
        wanted = normalize_name(room)
        if not wanted:
            return []

        def labels(satellite: Satellite) -> list[str]:
            return [normalize_name(label) for label in (satellite.area_name, *satellite.area_aliases, satellite.name) if label]

        exact = [s for s in satellites if wanted in labels(s)]
        words = set(wanted.split())
        found = exact or [
            s for s in satellites if any(set(label.split()) <= words or words <= set(label.split()) for label in labels(s))
        ]
        if len({s.area_id or s.device_id for s in found}) != 1:
            return []  # nothing, or more than one room: ask which
        return [s.device_id for s in found]

    @callback
    def add(
        self,
        at: dtime,
        *,
        days: tuple[int, ...] = (),
        on_date: date | None = None,
        name: str | None = None,
        device_ids: list[str],
        user_id: str | None = None,
        language: str | None = None,
        guest: bool = False,
    ) -> tuple[AlarmRecord, bool]:
        """A new alarm, scheduled; or the same one already set (returned with True)."""
        now = dt_util.utcnow()
        name = (name or "").strip() or None
        if days:
            on_date = None
        elif on_date is None:
            on_date = first_date(at, now, self._tz())
        for record in self.alarms(guest=guest):
            if (
                record.time == at
                and record.days == days
                and (days or record.date == on_date)
                and set(record.device_ids) == set(device_ids)
                and normalize_name(record.name) == normalize_name(name)
            ):
                # Asked twice: the alarm already there, turned on.
                if not record.enabled:
                    self.change(record, enabled=True)
                return record, True
        record = AlarmRecord(
            id=new_alarm_id(),
            time=at,
            days=days,
            date=on_date,
            name=name,
            device_ids=list(device_ids),
            user_id=user_id,
            language=language or self.hass.config.language or "en",
            guest=guest,
            stay=stay_key(self.entry.options) if guest else None,
        )
        self._records[record.id] = record
        self._schedule(record)
        self._add(record)
        self._save()
        return record, False

    def find(
        self,
        records: list[AlarmRecord],
        *,
        alarm_id: str | None = None,
        name: str | None = None,
        at: dtime | None = None,
        device_id: str | None = None,
        user_id: str | None = None,
    ) -> list[AlarmRecord]:
        """The alarms a request means: by id, name or time; else the only one (or this satellite's, or the asker's)."""
        if alarm_id:
            wanted = str(alarm_id).strip().lower()
            return [record for record in records if record.id == wanted]
        found = records
        if name and (wanted := normalize_name(name)):
            named = [(record, normalize_name(record.name)) for record in records if record.name]
            exact = [record for record, own in named if own == wanted]
            found = exact or [record for record, own in named if wanted in own or own in wanted]
        if at is not None:
            same = [record for record in found if record.time == at]
            # "My 7 o'clock alarm" for the one at 19:00, when there's none at 07:00.
            found = same or [
                record for record in found if record.time.minute == at.minute and record.time.hour % 12 == at.hour % 12
            ]
        if name or at is not None or len(records) <= 1:
            return found
        for mine in (
            [record for record in records if device_id and device_id in record.device_ids],
            [record for record in records if user_id and record.user_id == user_id],
        ):
            if len(mine) == 1:
                return mine
        return records

    @callback
    def change(
        self,
        record: AlarmRecord,
        *,
        at: dtime | None = None,
        days: tuple[int, ...] | None = None,
        on_date: date | None = None,
        rename: str | None = None,
        enabled: bool | None = None,
        skip_next: bool | None = None,
    ) -> str | None:
        """Change an alarm; returns why not, if it can't be done."""
        now = dt_util.utcnow()
        tz = self._tz()
        if skip_next and not (days if days is not None else record.days):
            return "A one-time alarm has no next day to skip: cancel it or turn it off instead."
        if at is not None and at != record.time:
            record.time = at
            record.snooze_until = None
        if days is not None:
            record.days = days
        if on_date is not None:
            record.days, record.date = (), on_date
        if record.days:
            record.date = None
        elif on_date is None and (record.date is None or at_local(record.date, record.time, tz) <= now):
            # A one-time alarm whose day has passed (or that just stopped repeating): its next time.
            record.date = first_date(record.time, now, tz)
        if rename is not None:
            record.name = rename.strip() or None
        if enabled is not None:
            record.enabled = enabled
            if not enabled:
                record.snooze_until = None
                record.pending.clear()
        if skip_next is not None:
            record.skip_date = None
            if skip_next:
                upcoming = next_ring(replace(record, snooze_until=None, enabled=True), now, tz)
                record.skip_date = upcoming.astimezone(tz).date() if upcoming else None
        self._schedule(record)
        self._write(record)
        self._save()
        return None

    @callback
    def cancel(self, record: AlarmRecord) -> None:
        self._silence(record)
        self._drop(record)
        self._save()

    @callback
    def snooze(self, record: AlarmRecord, minutes: int) -> None:
        """Ring again in a few minutes (a ring still going stops now)."""
        self._silence(record)
        record.pending.clear()
        record.snooze_until = dt_util.utcnow() + timedelta(minutes=minutes)
        self._schedule(record)
        self._write(record)
        self._save()

    @callback
    def stop(self, record: AlarmRecord) -> None:
        """Stop an alarm that went off: silence it and drop its snooze. A one-time alarm is done."""
        self._silence(record)
        record.pending.clear()
        record.snooze_until = None
        if not record.repeating:
            self._drop(record)
        else:
            self._schedule(record)
            self._write(record)
        self._save()

    def recent(self, records: list[AlarmRecord], device_id: str | None) -> list[AlarmRecord]:
        """The alarms that went off in the last 30 minutes: this satellite's if any, latest first."""
        now = dt_util.utcnow()
        recent = [r for r in records if r.rang_at is not None and now - r.rang_at <= RECENT_RING]
        here = [r for r in recent if device_id and device_id in r.device_ids]
        return sorted(here or recent, key=lambda r: r.rang_at, reverse=True)

    def rang_here(self, device_id: str | None) -> dict[str, Any] | None:
        """For the turn's context: the alarm that went off on this satellite in the last 30 minutes."""
        if not device_id:
            return None
        now = dt_util.utcnow()
        recent = [
            r for r in self._records.values() if r.rang_at is not None and now - r.rang_at <= RECENT_RING and device_id in r.device_ids
        ]
        if not recent:
            return None
        record = max(recent, key=lambda r: r.rang_at)
        snoozed = record.snooze_until if record.snooze_until and record.snooze_until > now else None
        return {
            "alarm_id": record.id,
            "name": record.name,
            "time": record.time.strftime("%H:%M"),
            "minutes_ago": int((now - record.rang_at).total_seconds() // 60),
            "snoozed_until": dt_util.as_local(snoozed).strftime("%H:%M") if snoozed else None,
        }

    def ringing_soon(self, within: timedelta) -> datetime | None:
        """The next ring if it's within `within` (the nightly update waits rather than restart then)."""
        now = dt_util.utcnow()
        soon = [r.next_at for r in self._records.values() if r.enabled and r.next_at and now <= r.next_at <= now + within]
        return min(soon) if soon else None

    def room_names(self, device_ids: Iterable[str]) -> list[str]:
        rooms = {satellite.device_id: satellite.room for satellite in self.satellites()}
        names: list[str] = []
        for device_id in device_ids:
            name = rooms.get(device_id) or self._device_name(device_id)
            if name and name not in names:
                names.append(name)
        return names

    def describe(self, record: AlarmRecord, *, device_id: str | None = None) -> dict[str, Any]:
        """An alarm as tools report it (times in the home's zone)."""
        tz = self._tz()
        now = dt_util.utcnow()
        upcoming = record.next_at if record.enabled else None
        snoozed = record.snooze_until if record.snooze_until and record.snooze_until > now else None
        return {
            "alarm_id": record.id,
            "name": record.name,
            "time": record.time.strftime("%H:%M"),
            "repeats": describe_days(record.days),
            "date": record.date.isoformat() if record.date and not record.days else None,
            "enabled": record.enabled,
            "next_ring": upcoming.astimezone(tz).isoformat() if upcoming else None,
            "next_ring_day": _day_words(upcoming, now, tz) if upcoming else None,
            "skipping": record.skip_date.isoformat() if record.skip_date else None,
            "snoozed_until": snoozed.astimezone(tz).strftime("%H:%M") if snoozed else None,
            "rings_in": self.room_names(record.device_ids),
            "on_this_device": bool(device_id) and device_id in record.device_ids,
        }

    def attributes(self, record: AlarmRecord) -> dict[str, Any]:
        """The switch's attributes, for the dashboard: its time, days, room and next ring."""
        tz = self._tz()
        rooms = self.room_names(record.device_ids)
        repeats = describe_days(record.days)
        summary = [record.time.strftime("%H:%M"), repeats.capitalize() if repeats != "once" else "Once"]
        if rooms:
            summary.append(", ".join(rooms))
        if record.skip_date and record.days:
            summary.append(f"skips {DAY_NAMES[record.skip_date.weekday()].capitalize()}")
        paused = self.paused(record)
        if paused:
            summary.append("paused while guests stay")
        return {
            "alarm_id": record.id,
            "name": record.name,
            "time": record.time.strftime("%H:%M"),
            "repeats": repeats,
            "date": record.date.isoformat() if record.date and not record.days else None,
            "next_ring": record.next_at.astimezone(tz).isoformat() if record.enabled and record.next_at else None,
            "rings_in": rooms,
            "skipping": record.skip_date.isoformat() if record.skip_date else None,
            "snoozed_until": record.snooze_until.astimezone(tz).isoformat() if record.snooze_until else None,
            "guest": record.guest,
            "paused": paused,
            "summary": " · ".join(summary),
        }

    def entity_id(self, record: AlarmRecord) -> str:
        return f"switch.auri_alarm_{record.id}"

    # -- ringing ------------------------------------------------------------------------------

    def _schedule(self, record: AlarmRecord) -> None:
        if (unsub := self._scheduled.pop(record.id, None)) is not None:
            unsub()
        record.next_at = next_ring(record, dt_util.utcnow(), self._tz())
        if record.next_at is not None:
            self._scheduled[record.id] = async_track_point_in_utc_time(
                self.hass, partial(self._async_due, record.id), record.next_at
            )

    @callback
    def _async_due(self, alarm_id: str, _now: datetime) -> None:
        """An alarm's time (or its snooze's)."""
        self._scheduled.pop(alarm_id, None)
        record = self._records.get(alarm_id)
        if record is None or not record.enabled:
            return
        due = record.next_at or dt_util.utcnow()
        snoozed = record.snooze_until is not None
        record.snooze_until = None
        if self.paused(record):
            _LOGGER.debug("Alarm %s paused while guests stay: not rung", record.name or record.id)
        else:
            self._ring(record, due=due, snoozed=snoozed)
        today = dt_util.utcnow().astimezone(self._tz()).date()
        if record.skip_date is not None and record.skip_date < today:
            record.skip_date = None
        self._schedule(record)
        self._write(record)
        self._save()

    def paused(self, record: AlarmRecord) -> bool:
        """A household alarm while Auri is in guest mode: it doesn't ring (a one-time one is let go)."""
        return not record.guest and self.mode() == "guest"

    @callback
    def mode_changed(self) -> None:
        """The mode changed (AuriCalendars' listener): the switches show whether they're paused."""
        for record in self._records.values():
            self._write(record)

    def _ring(self, record: AlarmRecord, *, due: datetime, snoozed: bool) -> None:
        """Ring each of the alarm's satellites now, or once it's back (until 15 minutes after `due`)."""
        record.rang_at = dt_util.utcnow()
        record.rang_devices = []
        record.pending.clear()
        for device_id in record.device_ids:
            if self._ring_device(record, device_id):
                record.rang_devices.append(device_id)
            else:
                record.pending[device_id] = due + LATE_RING_LIMIT
        _LOGGER.debug("Alarm %s went off (%s)", record.name or record.id, "snoozed" if snoozed else "on time")
        self.hass.bus.async_fire(
            EVENT_ALARM_RINGING,
            {
                "alarm_id": record.id,
                "entity_id": self.entity_id(record),
                "name": record.name,
                "time": record.time.strftime("%H:%M"),
                "device_ids": list(record.device_ids),
                "snoozed": snoozed,
            },
        )

    def _ring_device(self, record: AlarmRecord, device_id: str) -> bool:
        return self.timers.ring(device_id, name=record.name or "Alarm", language=record.language)

    @callback
    def _async_tick(self, _now: datetime | None = None) -> None:
        """Ring satellites that are back, and let go of finished one-time alarms and ended stays."""
        now = dt_util.utcnow()
        changed = False
        for record in list(self._records.values()):
            for device_id, until in list(record.pending.items()):
                if self._ring_device(record, device_id):
                    record.pending.pop(device_id)
                    record.rang_devices.append(device_id)
                    changed = True
                elif now > until:
                    record.pending.pop(device_id)
                    _LOGGER.warning(
                        "The alarm %s didn't ring on %s: the satellite didn't come back within %s",
                        record.name or record.id,
                        self._device_name(device_id) or device_id,
                        LATE_RING_LIMIT,
                    )
                    changed = True
            if record.guest and self._stay_over(record, now):
                self._drop(record)
                changed = True
            elif self._finished(record, now):
                self._drop(record)
                changed = True
        if changed:
            self._save()

    @staticmethod
    def _finished(record: AlarmRecord, now: datetime) -> bool:
        """A one-time alarm that went off (or whose time passed unrung), once its snooze window is over."""
        if record.repeating or not record.enabled or record.next_at is not None or record.pending:
            return False
        return record.rang_at is None or now - record.rang_at > RECENT_RING

    def _stay_over(self, record: AlarmRecord, now: datetime) -> bool:
        """A guest alarm whose stay has ended (check-out) or been replaced by another."""
        options = self.entry.options
        if record.stay != stay_key(options):
            return True
        check_out = parse_stay_time(options.get(CONF_CHECK_OUT))
        return check_out is not None and now >= check_out

    def _silence(self, record: AlarmRecord) -> None:
        """Stop a ring still going on the alarm's satellites (their Timer Ringing switch, when they have one)."""
        if record.rang_at is None or dt_util.utcnow() - record.rang_at > RECENT_RING:
            return
        for device_id in record.rang_devices:
            switch = self._ring_switch(device_id)
            state = self.hass.states.get(switch) if switch else None
            if state is not None and state.state == "on":
                self.hass.async_create_task(
                    self.hass.services.async_call("switch", "turn_off", {"entity_id": switch}), eager_start=True
                )

    def _ring_switch(self, device_id: str) -> str | None:
        for entry in er.async_entries_for_device(er.async_get(self.hass), device_id):
            if entry.domain == "switch" and (
                (entry.original_name or entry.name) == RING_SWITCH_NAME or str(entry.unique_id).endswith("timer_ringing")
            ):
                return entry.entity_id
        return None

    @callback
    def _async_config_updated(self, _event: Event) -> None:
        for record in self._records.values():
            self._schedule(record)
            self._write(record)
        self._save()

    def _device_name(self, device_id: str | None) -> str | None:
        if not device_id or (device := dr.async_get(self.hass).async_get(device_id)) is None:
            return None
        return device.name_by_user or device.name

    # -- switches -----------------------------------------------------------------------------

    def _new_entity(self, record: AlarmRecord) -> AuriAlarmSwitch:
        entity = AuriAlarmSwitch(self, record)
        self._entities[record.id] = entity
        return entity

    def _add(self, record: AlarmRecord) -> None:
        entity = self._new_entity(record)
        if self._add_entities is not None:
            self._add_entities([entity])

    def _write(self, record: AlarmRecord) -> None:
        entity = self._entities.get(record.id)
        if entity is not None and entity.hass is not None:
            entity.async_write_ha_state()

    def _drop(self, record: AlarmRecord) -> None:
        if (unsub := self._scheduled.pop(record.id, None)) is not None:
            unsub()
        self._records.pop(record.id, None)
        entity = self._entities.pop(record.id, None)
        if entity is not None and entity.hass is not None:
            self.hass.async_create_task(self._async_remove(entity), eager_start=True)

    async def _async_remove(self, entity: AuriAlarmSwitch) -> None:
        """The alarm is gone: its switch goes too, from the entity registry as well."""
        registry = er.async_get(self.hass)
        if registry.async_get(entity.entity_id) is not None:
            registry.async_remove(entity.entity_id)
        else:
            await entity.async_remove()


def _day_words(moment: datetime, now: datetime, tz: tzinfo) -> str:
    """today, tomorrow (Thursday), or the weekday and date: how to say an alarm's day."""
    day = moment.astimezone(tz).date()
    today = now.astimezone(tz).date()
    weekday = _FULL_DAY_NAMES[day.weekday()].capitalize()
    if day == today:
        return f"today ({weekday})"
    if day == today + timedelta(days=1):
        return f"tomorrow ({weekday})"
    return f"{weekday} {day.isoformat()}"
