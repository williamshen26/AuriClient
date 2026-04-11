"""Tool handlers for calendar actions."""
from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from ..exceptions import ToolExecutionError
from ..helpers import resolve_entity_id


class CalendarToolService:
    """Handle calendar-oriented tool calls."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get_calendar_events(self, arguments: dict[str, Any]) -> dict[str, Any]:
        entity_id = resolve_entity_id(self.hass, "calendar", arguments.get("entity_id"))
        start_time = arguments.get("start_time")
        end_time = arguments.get("end_time")
        if not start_time or not end_time:
            return {"retry": "start_time and end_time are required for get_calendar_events"}
        if end_time <= start_time:
            return {"retry": "end_time must be after start_time"}
        try:
            return await self.hass.services.async_call(
                domain="calendar",
                service="get_events",
                service_data={
                    "start_date_time": start_time,
                    "end_date_time": end_time,
                },
                target={"entity_id": entity_id},
                blocking=True,
                return_response=True,
            )
        except HomeAssistantError as err:
            raise ToolExecutionError(str(err)) from err

    async def add_calendar_event(self, arguments: dict[str, Any]) -> dict[str, Any]:
        entity_id = resolve_entity_id(self.hass, "calendar", arguments.get("entity_id"))
        event_title = arguments.get("event_title")
        event_description = arguments.get("event_description", "")
        event_start_time = arguments.get("event_start_time")
        event_end_time = arguments.get("event_end_time")
        event_location = arguments.get("event_location", "")
        if not event_title or not event_start_time or not event_end_time:
            return {
                "retry": "event_title, event_start_time, and event_end_time are required for add_calendar_event"
            }
        try:
            await self.hass.services.async_call(
                domain="calendar",
                service="create_event",
                service_data={
                    "summary": event_title,
                    "description": event_description,
                    "start_date_time": event_start_time,
                    "end_date_time": event_end_time,
                    "location": event_location,
                },
                target={"entity_id": entity_id},
                blocking=True,
            )
            return {"success": True}
        except HomeAssistantError as err:
            raise ToolExecutionError(str(err)) from err
