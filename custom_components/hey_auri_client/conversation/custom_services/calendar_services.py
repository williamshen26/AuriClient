"""Calendar tools: read, add (once or repeating), change and remove, on the calendar the turn may use.

Home Assistant's calendar actions can only read and add (calendar.get_events, calendar.create_event),
and get_events leaves out each event's uid. A calendar entity can also change and remove events: Home
Assistant's own Calendar page calls async_update_event and async_delete_event on it (its
calendar/event/update and calendar/event/delete websocket commands). These tools call the entity the
same way, with the same checks: its supported features, and WEBSOCKET_EVENT_SCHEMA for an event.

Which calendar (calendars.py): the Guest Calendar in guest mode; in owner mode the Home Calendar, or
the Guest Calendar when asked for by name. The model refers to an event by a short id (e1, e2, …)
that get_calendar_events hands out and this hub maps to the event for 30 minutes.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
import re
from time import monotonic
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from ...calendars import GUEST, AuriCalendars, calendar_entity
from ...const import DOMAIN, RUNTIME_CALENDARS

CALENDAR_TOOL_NAMES = frozenset({"get_calendar_events", "add_calendar_event", "change_calendar_event", "remove_calendar_event"})
EVENT_ID_SECONDS = 30 * 60
WHICH = ("this", "following", "all")
# Looking for a repeating event's first occurrence (to change all of them): this far back.
_SERIES_LOOK_BACK = timedelta(days=3 * 365)
_FREQUENCIES = {"daily": "DAILY", "weekly": "WEEKLY", "monthly": "MONTHLY", "yearly": "YEARLY"}
_DAYS = {"mon": "MO", "tue": "TU", "wed": "WE", "thu": "TH", "fri": "FR", "sat": "SA", "sun": "SU"}
_DAY_WORDS = {"MO": "Monday", "TU": "Tuesday", "WE": "Wednesday", "TH": "Thursday", "FR": "Friday", "SA": "Saturday", "SU": "Sunday"}
WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_ASK_WHICH = (
    "This event repeats. Ask the user whether it's only this one, this one and every one after it, or all "
    "of them, then call again with which (this, following or all)."
)


def get_calendars(hass: HomeAssistant) -> AuriCalendars | None:
    return hass.data.get(DOMAIN, {}).get("runtime", {}).get(RUNTIME_CALENDARS)


@dataclass(slots=True)
class _Known:
    """An event get_calendar_events showed, under its short id."""

    entity_id: str
    role: str
    event: Any  # homeassistant.components.calendar.CalendarEvent
    seen: float


def parse_moment(value: Any) -> datetime | date | None:
    """A date (all day) or a datetime in Home Assistant's time zone, from ISO text; None when empty."""
    text = str(value or "").strip()
    if not text:
        return None
    if _DATE.fullmatch(text):
        return date.fromisoformat(text)
    moment = dt_util.parse_datetime(text)
    if moment is None:
        raise ValueError(f"Unrecognized time {text!r}: give ISO format, such as 2026-11-20T15:00:00 or 2026-11-20.")
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt_util.get_default_time_zone())
    return dt_util.as_local(moment)


def build_rrule(repeat: Any, days: Any, until: Any, all_day: bool) -> str | None:
    """The RRULE for add_calendar_event's repeat, repeat_days and repeat_until (None for a one-off event)."""
    if not repeat:
        return None
    frequency = _FREQUENCIES.get(str(repeat).strip().lower())
    if frequency is None:
        raise ValueError(f"Unrecognized repeat {repeat!r}: give daily, weekly, monthly or yearly.")
    parts = [f"FREQ={frequency}"]
    if days:
        if frequency != "WEEKLY":
            raise ValueError("repeat_days only goes with repeat weekly.")
        codes = []
        for day in days if isinstance(days, list) else [days]:
            code = _DAYS.get(str(day).strip().lower()[:3])
            if code is None:
                raise ValueError(f"Unrecognized day {day!r}: give mon, tue, wed, thu, fri, sat or sun.")
            if code not in codes:
                codes.append(code)
        parts.append("BYDAY=" + ",".join(codes))
    if until:
        text = str(until).strip()[:10]
        if not _DATE.fullmatch(text):
            raise ValueError(f"Unrecognized repeat_until {until!r}: give a date, YYYY-MM-DD.")
        stamp = text.replace("-", "")
        # Local Calendar keeps times floating (local), so UNTIL is too: the end of that day.
        parts.append(f"UNTIL={stamp}" if all_day else f"UNTIL={stamp}T235959")
    return ";".join(parts)


def describe_rrule(rrule: str | None) -> str | None:
    """How an event repeats, in words, from its RRULE."""
    if not rrule:
        return None
    rule = dict(part.split("=", 1) for part in rrule.split(";") if "=" in part)
    interval = int(rule.get("INTERVAL", "1") or 1)
    unit = {"DAILY": "day", "WEEKLY": "week", "MONTHLY": "month", "YEARLY": "year"}.get(rule.get("FREQ", ""), "time")
    words = f"every {interval} {unit}s" if interval > 1 else f"every {unit}"
    if rule.get("BYDAY"):
        days = [_DAY_WORDS.get(code[-2:], code) for code in rule["BYDAY"].split(",")]
        words += " on " + ", ".join(days)
    if rule.get("UNTIL"):
        stamp = rule["UNTIL"][:8]
        words += f" until {stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}"
    elif rule.get("COUNT"):
        words += f", {rule['COUNT']} times"
    return words


def _iso(moment: datetime | date, *, end: bool = False) -> str:
    if isinstance(moment, datetime):
        return dt_util.as_local(moment).isoformat()
    # An all-day event ends the day after its last day (Home Assistant's way); say the last day.
    return (moment - timedelta(days=1)).isoformat() if end else moment.isoformat()


class CalendarToolService:
    """Handle calendar-oriented tool calls."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._known: dict[str, _Known] = {}
        self._count = 0

    # ── which calendar, and the events shown ──────────────────────────────────

    async def _calendar(self, arguments: dict[str, Any]) -> tuple[str, str, Any] | dict[str, Any]:
        calendars = get_calendars(self.hass)
        if calendars is None:
            return {"success": False, "error": "The calendar isn't ready yet: try again in a moment."}
        turn = arguments.get("_turn")
        role = calendars.role_for_turn(getattr(turn, "mode", None), arguments.get("calendar"))
        entity_id = await calendars.async_ensure(role)
        entity = calendar_entity(self.hass, entity_id)
        if entity is None:
            name = "guest calendar" if role == GUEST else "home calendar"
            return {"success": False, "error": f"The {name} isn't set up: Hey Auri Client couldn't add it to Home Assistant."}
        return role, entity_id, entity

    def _remember(self, entity_id: str, role: str, event: Any) -> str:
        now = monotonic()
        for event_id in [k for k, known in self._known.items() if now - known.seen > EVENT_ID_SECONDS]:
            del self._known[event_id]
        for event_id, known in self._known.items():
            if (known.entity_id, known.event.uid, known.event.recurrence_id) == (entity_id, event.uid, event.recurrence_id):
                self._known[event_id] = _Known(entity_id, role, event, now)
                return event_id
        self._count += 1
        event_id = f"e{self._count}"
        self._known[event_id] = _Known(entity_id, role, event, now)
        return event_id

    def _forget_series(self, entity_id: str, uid: str) -> None:
        for event_id in [k for k, known in self._known.items() if (known.entity_id, known.event.uid) == (entity_id, uid)]:
            del self._known[event_id]

    def _known_event(self, arguments: dict[str, Any]) -> _Known | dict[str, Any]:
        event_id = str(arguments.get("event_id") or "").strip()
        known = self._known.get(event_id)
        if known is None or monotonic() - known.seen > EVENT_ID_SECONDS:
            return {"retry": f"No event {event_id!r} from a recent get_calendar_events: call get_calendar_events again and use its event_id."}
        calendars = get_calendars(self.hass)
        turn = arguments.get("_turn")
        if calendars is not None and known.role not in calendars.allowed_roles(getattr(turn, "mode", None)):
            return {"success": False, "error": "That event isn't on the guests' calendar: guests can only change their own."}
        if not known.event.uid:
            return {"success": False, "error": "This calendar doesn't let Auri change that event."}
        return known

    @staticmethod
    def _describe(event_id: str | None, event: Any) -> dict[str, Any]:
        out: dict[str, Any] = {"event_id": event_id} if event_id else {}
        first_day = dt_util.as_local(event.start).date() if isinstance(event.start, datetime) else event.start
        out.update({
            "title": event.summary,
            # The weekday, so the reply names the right one (the model slipped working it out).
            "day": WEEKDAY_NAMES[first_day.weekday()],
            "start": _iso(event.start),
            "end": _iso(event.end, end=True),
            "all_day": not isinstance(event.start, datetime),
        })
        if event.location:
            out["location"] = event.location
        if event.description:
            out["description"] = event.description
        if repeats := describe_rrule(event.rrule):
            out["repeats"] = repeats
        return out

    # ── the tools ─────────────────────────────────────────────────────────────

    async def get_calendar_events(self, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            start, end = parse_moment(arguments.get("start_time")), parse_moment(arguments.get("end_time"))
        except ValueError as err:
            return {"retry": str(err)}
        if start is None or end is None:
            return {"retry": "start_time and end_time are required for get_calendar_events"}
        start, end = _as_datetime(start), _as_datetime(end, end=True)
        if end <= start:
            return {"retry": "end_time must be after start_time"}
        picked = await self._calendar(arguments)
        if isinstance(picked, dict):
            return picked
        role, entity_id, entity = picked
        events = await entity.async_get_events(self.hass, start, end)
        events = sorted(events, key=lambda event: event.start_datetime_local)
        return {
            "success": True,
            "calendar": role,
            "events": [self._describe(self._remember(entity_id, role, event), event) for event in events],
        }

    async def add_calendar_event(self, arguments: dict[str, Any]) -> dict[str, Any]:
        title = str(arguments.get("event_title") or "").strip()
        try:
            start = parse_moment(arguments.get("event_start_time"))
            end = parse_moment(arguments.get("event_end_time"))
            if not title or start is None or end is None:
                return {"retry": "event_title, event_start_time, and event_end_time are required for add_calendar_event"}
            start, end = _span(start, end)
            rrule = build_rrule(arguments.get("repeat"), arguments.get("repeat_days"), arguments.get("repeat_until"),
                                not isinstance(start, datetime))
            _check_first_day(start, rrule)
        except ValueError as err:
            return {"retry": str(err)}
        picked = await self._calendar(arguments)
        if isinstance(picked, dict):
            return picked
        role, _entity_id, entity = picked
        if not _supports(entity, "CREATE_EVENT"):
            return {"success": False, "error": "This calendar doesn't let Auri add events."}
        event = _event(title, start, end, arguments.get("event_description"), arguments.get("event_location"), rrule)
        try:
            validated = _validate(event)
            await entity.async_create_event(**validated)
        except Exception as err:  # noqa: BLE001
            return {"retry": f"The event wasn't added: {err}"}
        return {"success": True, "calendar": role, "added": self._describe(None, _shown(validated))}

    async def change_calendar_event(self, arguments: dict[str, Any]) -> dict[str, Any]:
        known = self._known_event(arguments)
        if isinstance(known, dict):
            return known
        old = known.event
        repeating = bool(old.rrule or old.recurrence_id)
        which = str(arguments.get("which") or "").strip().lower()
        if repeating and which not in WHICH:
            return {"retry": _ASK_WHICH}
        entity = calendar_entity(self.hass, known.entity_id)
        if entity is None or not _supports(entity, "UPDATE_EVENT"):
            return {"success": False, "error": "This calendar doesn't let Auri change events."}
        try:
            new_start = parse_moment(arguments.get("event_start_time"))
            new_end = parse_moment(arguments.get("event_end_time"))
        except ValueError as err:
            return {"retry": str(err)}
        series = repeating and which == "all"
        base = await self._first_occurrence(entity, old) if series else old
        try:
            start, end = _new_times(old, base, new_start, new_end, series=series)
        except ValueError as err:
            return {"retry": str(err)}
        rrule = old.rrule if repeating and which in ("following", "all") else None
        event = _event(
            _pick(arguments, "event_title", old.summary),
            start, end,
            _pick(arguments, "event_description", old.description),
            _pick(arguments, "event_location", old.location),
            rrule,
        )
        recurrence_id = old.recurrence_id if repeating and which in ("this", "following") else None
        recurrence_range = "THISANDFUTURE" if repeating and which == "following" else None
        try:
            validated = _validate(event)
            # A copy: Local Calendar rewrites the event it's given (floating times, a parsed rule).
            await entity.async_update_event(old.uid, dict(validated), recurrence_id=recurrence_id, recurrence_range=recurrence_range)
        except Exception as err:  # noqa: BLE001
            return {"retry": f"The event wasn't changed: {err}"}
        self._forget_series(known.entity_id, old.uid)
        return {"success": True, "calendar": known.role, "changed": self._describe(None, _shown(validated)),
                **({"which": which} if repeating else {})}

    async def remove_calendar_event(self, arguments: dict[str, Any]) -> dict[str, Any]:
        known = self._known_event(arguments)
        if isinstance(known, dict):
            return known
        old = known.event
        repeating = bool(old.rrule or old.recurrence_id)
        which = str(arguments.get("which") or "").strip().lower()
        if repeating and which not in WHICH:
            return {"retry": _ASK_WHICH}
        entity = calendar_entity(self.hass, known.entity_id)
        if entity is None or not _supports(entity, "DELETE_EVENT"):
            return {"success": False, "error": "This calendar doesn't let Auri remove events."}
        recurrence_id = old.recurrence_id if repeating and which in ("this", "following") else None
        recurrence_range = "THISANDFUTURE" if repeating and which == "following" else None
        try:
            await entity.async_delete_event(old.uid, recurrence_id=recurrence_id, recurrence_range=recurrence_range)
        except Exception as err:  # noqa: BLE001
            return {"success": False, "error": f"The event wasn't removed: {err}"}
        self._forget_series(known.entity_id, old.uid)
        return {"success": True, "calendar": known.role, "removed": self._describe(None, old),
                **({"which": which} if repeating else {})}

    async def _first_occurrence(self, entity: Any, event: Any) -> Any:
        """A repeating event's first occurrence (its series' start), looking back up to three years."""
        until = _as_datetime(event.start) + timedelta(minutes=1)
        found = await entity.async_get_events(self.hass, until - _SERIES_LOOK_BACK, until)
        same = [item for item in found if item.uid == event.uid]
        return min(same, key=lambda item: item.start_datetime_local) if same else event


def _as_datetime(moment: datetime | date, *, end: bool = False) -> datetime:
    """A date as the start of that day (or, for the end of a range, the start of the next) in local time."""
    if isinstance(moment, datetime):
        return moment
    day = moment + timedelta(days=1) if end else moment
    return datetime.combine(day, datetime.min.time(), tzinfo=dt_util.get_default_time_zone())


def _check_first_day(start: datetime | date, rrule: str | None) -> None:
    """A weekly event's first time must fall on one of its days: "every Thursday" starting on a Friday is a
    wrong date (the model slipped working it out), and the first event would be on the wrong day."""
    if not rrule or "BYDAY=" not in rrule:
        return
    days = rrule.split("BYDAY=", 1)[1].split(";", 1)[0].split(",")
    first = dt_util.as_local(start).date() if isinstance(start, datetime) else start
    code = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")[first.weekday()]
    if code not in days:
        wanted = ", ".join(_DAY_WORDS[day] for day in days if day in _DAY_WORDS)
        raise ValueError(
            f"event_start_time is a {WEEKDAY_NAMES[first.weekday()]} ({first.isoformat()}), but the event repeats on "
            f"{wanted}: give the date of the first {wanted.split(', ')[0]} from Current Local Time."
        )


def _is_day(moment: Any) -> bool:
    return isinstance(moment, date) and not isinstance(moment, datetime)


def _exclusive(end: datetime | date) -> datetime | date:
    """An all-day event's last day, as the model gives it, to the day after it (Home Assistant's end)."""
    return end + timedelta(days=1) if _is_day(end) else end


def _check(start: Any, end: Any) -> tuple[Any, Any]:
    if _is_day(start) != _is_day(end):
        raise ValueError("Give the start and end both as dates (all day) or both as times.")
    if end <= start:
        raise ValueError("The event must end after it starts (an all-day event: on or after the day it starts).")
    return start, end


def _span(start: datetime | date, end: datetime | date) -> tuple[Any, Any]:
    """A new event's start and end; an all-day event's end date, given as its last day, ends the day after."""
    if _is_day(start) != _is_day(end):
        raise ValueError("Give the start and end both as dates (all day) or both as times.")
    return _check(start, _exclusive(end))


def _new_times(old: Any, base: Any, new_start: Any, new_end: Any, *, series: bool) -> tuple[Any, Any]:
    """A changed event's start and end (an all-day end exclusive, as stored).

    old: the occurrence the user means; base: what's changed (the first occurrence, for all of a
    repeating event). Moving the start keeps the length. For all of a repeating event, its start (or
    end) moves by as much as the user moved this occurrence's.
    """
    start, end = base.start, base.end
    if new_start is not None:
        same_kind = _is_day(new_start) == _is_day(base.start)
        if series and same_kind:
            start = base.start + (_as_datetime(new_start) - _as_datetime(old.start))
        else:
            start = new_start
        if same_kind:
            end = start + (base.end - base.start)
        else:  # all day to a time, or the other way: an hour, or the day
            end = start + (timedelta(days=1) if _is_day(start) else timedelta(hours=1))
    if new_end is not None:
        wanted = _exclusive(new_end)
        if series and _is_day(wanted) == _is_day(old.end):
            end = base.end + (_as_datetime(wanted) - _as_datetime(old.end))
        else:
            end = wanted
    return _check(start, end)


def _event(title: Any, start: Any, end: Any, description: Any, location: Any, rrule: str | None) -> dict[str, Any]:
    event: dict[str, Any] = {"dtstart": start, "dtend": end, "summary": str(title or "").strip()}
    if description:
        event["description"] = str(description)
    if location:
        event["location"] = str(location)
    if rrule:
        event["rrule"] = rrule
    return event


def _pick(arguments: dict[str, Any], key: str, current: Any) -> Any:
    return arguments[key] if key in arguments and arguments[key] is not None else current


def _validate(event: dict[str, Any]) -> dict[str, Any]:
    """Home Assistant's own check for an event (its calendar/event/create and update commands use it)."""
    from homeassistant.components.calendar import WEBSOCKET_EVENT_SCHEMA  # noqa: PLC0415

    return WEBSOCKET_EVENT_SCHEMA(dict(event))


def _supports(entity: Any, feature: str) -> bool:
    from homeassistant.components.calendar import CalendarEntityFeature  # noqa: PLC0415

    return bool((entity.supported_features or 0) & getattr(CalendarEntityFeature, feature))


def _shown(validated: dict[str, Any]) -> Any:
    """A validated event as a CalendarEvent, to describe it back."""
    from homeassistant.components.calendar import CalendarEvent  # noqa: PLC0415

    return CalendarEvent(
        start=validated["dtstart"], end=validated["dtend"], summary=validated["summary"],
        description=validated.get("description"), location=validated.get("location"), rrule=validated.get("rrule"),
    )
