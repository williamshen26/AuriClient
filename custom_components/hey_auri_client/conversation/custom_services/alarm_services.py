"""Auri's alarm tools, on alarms.py's AuriAlarms.

An alarm rings on a voice satellite: the one the user spoke to, or every satellite in the room
named. From the phone app or the web page, the tool asks which room (unless the home has only one
satellite). Guests see and change only the alarms set during their stay.
"""
from __future__ import annotations

from datetime import date, time as dtime
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from ...alarms import (
    MAX_SNOOZE_MINUTES,
    SNOOZE_MINUTES,
    AlarmRecord,
    AuriAlarms,
    at_local,
    parse_clock_time,
    parse_days,
)
from ...const import DOMAIN, RUNTIME_ALARMS
from ...exceptions import ToolExecutionError

# These tools get the turn (the device that asked, who asked, guest or owner) as _turn.
ALARM_TOOL_NAMES = frozenset(
    {"set_alarm", "list_alarms", "cancel_alarm", "change_alarm", "snooze_alarm", "stop_alarm"}
)
_TRUE = (True, "true", "True", 1, "1", "yes")
_FALSE = (False, "false", "False", 0, "0", "no")


def get_alarms(hass: HomeAssistant) -> AuriAlarms:
    alarms = hass.data.get(DOMAIN, {}).get("runtime", {}).get(RUNTIME_ALARMS)
    if alarms is None:
        raise HomeAssistantError("Auri's alarms aren't set up yet")
    return alarms


def _parse_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    parsed = dt_util.parse_date(str(value).strip())
    if parsed is None:
        raise ValueError(f"Unrecognized date {value!r}: give YYYY-MM-DD.")
    return parsed


def _flag(value: Any) -> bool | None:
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    return None


class AlarmToolService:
    """Auri's alarm tools (see alarms.py)."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    def _alarms(self) -> AuriAlarms:
        try:
            return get_alarms(self.hass)
        except HomeAssistantError as err:
            raise ToolExecutionError(str(err)) from err

    @staticmethod
    def _turn_value(arguments: dict[str, Any], key: str) -> Any:
        return getattr(arguments.get("_turn"), key, None)

    def _guest(self, arguments: dict[str, Any]) -> bool:
        return self._turn_value(arguments, "mode") == "guest"

    def _describe_all(self, alarms: AuriAlarms, records: list[AlarmRecord], arguments: dict[str, Any]) -> list[dict[str, Any]]:
        here = self._turn_value(arguments, "device_id")
        return [alarms.describe(record, device_id=here) for record in records]

    async def set_alarm(self, arguments: dict[str, Any]) -> dict[str, Any]:
        alarms = self._alarms()
        try:
            at = parse_clock_time(arguments.get("time"))
            days = parse_days(arguments.get("days"))
            on_date = _parse_date(arguments.get("date"))
        except ValueError as err:
            return {"retry": str(err)}
        if on_date is not None and days:
            return {"retry": "Give days for a repeating alarm, or a date for a one-time alarm, not both."}
        if on_date is not None and at_local(on_date, at, dt_util.get_default_time_zone()) <= dt_util.utcnow():
            return {"success": False, "error": f"{on_date.isoformat()} at {at:%H:%M} has already passed."}
        here = self._turn_value(arguments, "device_id")
        device_ids, problem = alarms.choose_satellites(arguments.get("room"), here)
        if problem is not None:
            return problem
        record, already = alarms.add(
            at,
            days=days,
            on_date=on_date,
            name=arguments.get("name"),
            device_ids=device_ids,
            user_id=self._turn_value(arguments, "user_id"),
            language=self._turn_value(arguments, "language"),
            guest=self._guest(arguments),
        )
        result: dict[str, Any] = {"success": True, "alarm": alarms.describe(record, device_id=here)}
        if already:
            result["already_set"] = True
        return result

    async def list_alarms(self, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        arguments = arguments or {}
        alarms = self._alarms()
        records = alarms.alarms(guest=self._guest(arguments))
        return {"success": True, "alarms": self._describe_all(alarms, records, arguments)}

    def _pick(
        self, alarms: AuriAlarms, arguments: dict[str, Any], *, at_key: str = "time"
    ) -> tuple[AlarmRecord | None, dict[str, Any] | None]:
        """The one alarm a request means, or the answer to give instead (none, or which one?)."""
        everything = alarms.alarms(guest=self._guest(arguments))
        if not everything:
            return None, {"success": False, "error": "No alarm is set.", "alarms": []}
        try:
            at: dtime | None = parse_clock_time(arguments[at_key]) if arguments.get(at_key) else None
        except ValueError as err:
            return None, {"retry": str(err)}
        found = alarms.find(
            everything,
            alarm_id=arguments.get("alarm_id"),
            name=arguments.get("name"),
            at=at,
            device_id=self._turn_value(arguments, "device_id"),
            user_id=self._turn_value(arguments, "user_id"),
        )
        if not found:
            return None, {
                "success": False,
                "error": "No alarm matches. These are the alarms:",
                "alarms": self._describe_all(alarms, everything, arguments),
            }
        if len(found) > 1:
            return None, {
                "success": False,
                "error": (
                    "More than one alarm matches. Ask the user which one (by its time, days or name), "
                    "then call again with its alarm_id."
                ),
                "alarms": self._describe_all(alarms, found, arguments),
            }
        return found[0], None

    async def cancel_alarm(self, arguments: dict[str, Any]) -> dict[str, Any]:
        alarms = self._alarms()
        if arguments.get("all") in _TRUE:
            records = alarms.alarms(guest=self._guest(arguments))
            if not records:
                return {"success": False, "error": "No alarm is set.", "alarms": []}
        else:
            record, problem = self._pick(alarms, arguments)
            if problem is not None:
                return problem
            records = [record]
        cancelled = self._describe_all(alarms, records, arguments)
        for record in records:
            alarms.cancel(record)
        return {"success": True, "cancelled": cancelled}

    async def change_alarm(self, arguments: dict[str, Any]) -> dict[str, Any]:
        alarms = self._alarms()
        try:
            at = parse_clock_time(arguments["time"]) if arguments.get("time") else None
            raw_days = arguments.get("days")
            # "once" makes it a one-time alarm; nothing (or an empty list) leaves its days alone.
            days = parse_days(raw_days) if raw_days not in (None, "", []) else None
            on_date = _parse_date(arguments.get("date"))
        except ValueError as err:
            return {"retry": str(err)}
        rename = arguments.get("new_name")
        enabled = _flag(arguments.get("enabled"))
        skip_next = _flag(arguments.get("skip_next"))
        if at is None and days is None and on_date is None and rename is None and enabled is None and skip_next is None:
            return {"retry": "Give what to change: time, days, date, new_name, enabled or skip_next."}
        if on_date is not None and days:
            return {"retry": "Give days for a repeating alarm, or a date for a one-time alarm, not both."}
        record, problem = self._pick(alarms, arguments, at_key="current_time")
        if problem is not None:
            return problem
        if on_date is not None and at_local(on_date, at or record.time, dt_util.get_default_time_zone()) <= dt_util.utcnow():
            return {"success": False, "error": f"{on_date.isoformat()} at {(at or record.time):%H:%M} has already passed."}
        why_not = alarms.change(
            record, at=at, days=days, on_date=on_date, rename=rename, enabled=enabled, skip_next=skip_next
        )
        if why_not is not None:
            return {"success": False, "error": why_not}
        return {"success": True, "alarm": alarms.describe(record, device_id=self._turn_value(arguments, "device_id"))}

    def _went_off(self, alarms: AuriAlarms, arguments: dict[str, Any]) -> tuple[AlarmRecord | None, dict[str, Any] | None]:
        """The alarm that went off: the one named, else the latest in the last 30 minutes (this satellite's first)."""
        visible = alarms.alarms(guest=self._guest(arguments))
        recent = alarms.recent(visible, self._turn_value(arguments, "device_id"))
        if arguments.get("alarm_id") or arguments.get("name"):
            record, problem = self._pick(alarms, arguments)
            if problem is not None:
                return None, problem
            if record not in recent:
                return None, {"success": False, "error": "That alarm hasn't gone off in the last 30 minutes."}
            return record, None
        if not recent:
            return None, {"success": False, "error": "No alarm has gone off in the last 30 minutes."}
        return recent[0], None

    async def snooze_alarm(self, arguments: dict[str, Any]) -> dict[str, Any]:
        alarms = self._alarms()
        minutes = arguments.get("minutes")
        try:
            minutes = SNOOZE_MINUTES if minutes in (None, "") else int(float(minutes))
        except (TypeError, ValueError):
            return {"retry": f"Give minutes as a whole number from 1 to {MAX_SNOOZE_MINUTES}."}
        if not 1 <= minutes <= MAX_SNOOZE_MINUTES:
            return {"retry": f"Give minutes as a whole number from 1 to {MAX_SNOOZE_MINUTES}."}
        record, problem = self._went_off(alarms, arguments)
        if problem is not None:
            return problem
        alarms.snooze(record, minutes)
        return {
            "success": True,
            "minutes": minutes,
            "alarm": alarms.describe(record, device_id=self._turn_value(arguments, "device_id")),
        }

    async def stop_alarm(self, arguments: dict[str, Any]) -> dict[str, Any]:
        alarms = self._alarms()
        record, problem = self._went_off(alarms, arguments)
        if problem is not None:
            return problem
        stopped = alarms.describe(record, device_id=self._turn_value(arguments, "device_id"))
        alarms.stop(record)
        result: dict[str, Any] = {"success": True, "stopped": stopped}
        if record.repeating:
            result["next_ring"] = alarms.describe(record)["next_ring"]
        return result
