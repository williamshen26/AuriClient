"""Auri's timer tools, and the start_auri_timer and get_auri_timers actions, on timers.py's AuriTimers."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from ...const import DOMAIN
from ...exceptions import ToolExecutionError
from ...timers import AuriTimers, TimerRecord, parse_duration

# Where AuriTimers sits, in hass.data[DOMAIN]["runtime"].
RUNTIME_TIMERS = "auri_timers"
# These tools get the turn (the device that asked, who asked, the language) as _turn.
TIMER_TOOL_NAMES = frozenset(
    {"start_timer", "list_timers", "cancel_timer", "change_timer", "pause_timer", "resume_timer"}
)
_TRUE = (True, "true", "True", 1, "1")


def get_timers(hass: HomeAssistant) -> AuriTimers:
    timers = hass.data.get(DOMAIN, {}).get("runtime", {}).get(RUNTIME_TIMERS)
    if timers is None:
        raise HomeAssistantError("Auri's timers aren't set up yet")
    return timers


async def start_auri_timer_native(
    hass: HomeAssistant,
    duration: Any,
    satellite_speaker: str | None = None,
    *,
    name: str | None = None,
    device_id: str | None = None,
    user_id: str | None = None,
    language: str | None = None,
) -> dict[str, Any]:
    """The start_auri_timer action: on device_id (or the satellite_speaker's device) when it can ring."""
    timers = get_timers(hass)
    seconds = parse_duration(duration)
    if device_id is None and satellite_speaker:
        speaker = er.async_get(hass).async_get(satellite_speaker)
        device_id = speaker.device_id if speaker is not None else None
    record = timers.start(seconds, name=name, device_id=device_id, user_id=user_id, language=language)
    return {"entity_id": timers.entity_id(record), **timers.describe(record)}


async def get_auri_timers_native(hass: HomeAssistant) -> dict[str, Any]:
    """The get_auri_timers action: every timer on the hub."""
    timers = get_timers(hass)
    return {"timers": [{"entity_id": timers.entity_id(r), **timers.describe(r)} for r in timers.timers()]}


class TimerToolService:
    """Auri's timer tools. A timer rings on the device the user spoke to (see timers.py)."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    def _timers(self) -> AuriTimers:
        try:
            return get_timers(self.hass)
        except HomeAssistantError as err:
            raise ToolExecutionError(str(err)) from err

    @staticmethod
    def _here(arguments: dict[str, Any]) -> str | None:
        return getattr(arguments.get("_turn"), "device_id", None)

    async def start_timer(self, arguments: dict[str, Any]) -> dict[str, Any]:
        timers = self._timers()
        turn = arguments.get("_turn")
        try:
            seconds = parse_duration(arguments.get("duration"))
        except ValueError as err:
            return {"retry": str(err)}
        try:
            record = timers.start(
                seconds,
                name=arguments.get("name"),
                device_id=getattr(turn, "device_id", None),
                user_id=getattr(turn, "user_id", None),
                language=getattr(turn, "language", None),
            )
        except HomeAssistantError as err:
            raise ToolExecutionError(str(err)) from err
        return {"success": True, "timer": timers.describe(record, device_id=self._here(arguments))}

    async def list_timers(self, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        arguments = arguments or {}
        timers = self._timers()
        here = self._here(arguments)
        return {"success": True, "timers": [timers.describe(r, device_id=here) for r in timers.timers()]}

    def _pick(
        self,
        timers: AuriTimers,
        arguments: dict[str, Any],
        prefer: Callable[[TimerRecord], bool] | None = None,
    ) -> tuple[TimerRecord | None, dict[str, Any] | None]:
        """The one timer a request means, or the answer to give instead (none, or which one?)."""
        here = self._here(arguments)
        everything = timers.timers()
        if not everything:
            return None, {"success": False, "error": "No timer is running.", "timers": []}
        found = timers.find(timer_id=arguments.get("timer_id"), name=arguments.get("name"), device_id=here)
        if len(found) > 1 and prefer is not None and not arguments.get("timer_id") and not arguments.get("name"):
            # "Pause the timer" with one running and one paused means the running one.
            preferred = [r for r in found if prefer(r)]
            found = preferred if len(preferred) == 1 else found
        if not found:
            return None, {
                "success": False,
                "error": "No timer matches. These are the timers running:",
                "timers": [timers.describe(r, device_id=here) for r in everything],
            }
        if len(found) > 1:
            return None, {
                "success": False,
                "error": (
                    "More than one timer matches. Ask the user which one (by its name or length), "
                    "then call again with its timer_id."
                ),
                "timers": [timers.describe(r, device_id=here) for r in found],
            }
        return found[0], None

    async def cancel_timer(self, arguments: dict[str, Any]) -> dict[str, Any]:
        timers = self._timers()
        here = self._here(arguments)
        if arguments.get("all") in _TRUE:
            records = timers.timers()
            if not records:
                return {"success": False, "error": "No timer is running.", "timers": []}
        else:
            record, problem = self._pick(timers, arguments)
            if problem is not None:
                return problem
            records = [record]
        cancelled = [timers.describe(r, device_id=here) for r in records]
        for record in records:
            timers.cancel(record)
        return {"success": True, "cancelled": cancelled}

    async def change_timer(self, arguments: dict[str, Any]) -> dict[str, Any]:
        add, remove = arguments.get("add"), arguments.get("remove")
        if bool(add) == bool(remove):
            return {
                "retry": (
                    "Give either add or remove, not both: the time to add to the timer or to take off it, "
                    "as seconds (60), MM:SS or HH:MM:SS."
                )
            }
        try:
            seconds = parse_duration(add or remove)
        except ValueError as err:
            return {"retry": str(err)}
        timers = self._timers()
        record, problem = self._pick(timers, arguments)
        if problem is not None:
            return problem
        try:
            timers.change(record, seconds if add else -seconds)
        except HomeAssistantError:
            return {"success": False, "error": "That timer ended a moment ago."}
        return {"success": True, "timer": timers.describe(record, device_id=self._here(arguments))}

    async def pause_timer(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._pause_or_resume(arguments, pause=True)

    async def resume_timer(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._pause_or_resume(arguments, pause=False)

    def _pause_or_resume(self, arguments: dict[str, Any], *, pause: bool) -> dict[str, Any]:
        timers = self._timers()
        record, problem = self._pick(
            timers, arguments, prefer=(lambda r: not r.paused) if pause else (lambda r: r.paused)
        )
        if problem is not None:
            return problem
        try:
            if pause:
                timers.pause(record)
            else:
                timers.resume(record)
        except HomeAssistantError:
            return {"success": False, "error": "That timer ended a moment ago."}
        return {"success": True, "timer": timers.describe(record, device_id=self._here(arguments))}
