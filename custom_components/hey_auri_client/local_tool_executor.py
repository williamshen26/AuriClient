"""Local execution orchestrator for tool calls."""
from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

from homeassistant.core import HomeAssistant

from .custom_services.automation_services import AutomationToolService
from .custom_services.calendar_services import CalendarToolService
from .custom_services.climate_services import ClimateToolService
from .custom_services.cover_services import CoverToolService
from .custom_services.entity_services import EntityToolService
from .custom_services.light_services import LightToolService
from .custom_services.media_player_services import MediaPlayerToolService
from .custom_services.person_services import PersonToolService
from .custom_services.shopping_list_services import ShoppingListToolService
from .custom_services.timer_services import TimerToolService
from .custom_services.user_preferences_services import UserPreferencesToolService
from .custom_services.weather_services import WeatherToolService
from .exceptions import ToolExecutionError

try:
    from const import SKILL_REGISTRY  # type: ignore
except Exception:  # pragma: no cover
    SKILL_REGISTRY = {}


class LocalToolExecutor:
    """Executes supported tool calls locally in Home Assistant."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.entity = EntityToolService(hass)
        self.light = LightToolService(hass)
        self.cover = CoverToolService(hass)
        self.climate = ClimateToolService(hass)
        self.media_player = MediaPlayerToolService(hass)
        self.weather = WeatherToolService(hass)
        self.person = PersonToolService(hass)
        self.user_preferences = UserPreferencesToolService(hass)
        self.shopping_list = ShoppingListToolService(hass)
        self.automation = AutomationToolService(hass)
        self.timer = TimerToolService(hass)
        self.calendar = CalendarToolService(hass)
        self._handlers: dict[str, Callable[[dict[str, Any]], Awaitable[Any]]] = {
            "get_entity_state": self.entity.get_entity_state,
            "get_skill_data": self._get_skill_data,
            "get_all_persons": self.person.get_all_persons,
            "get_automation_metadata_service": self.automation.get_automation_metadata,
            "list_timers": self.timer.list_timers,
            "turn_on_light": self.light.turn_on_light,
            "turn_off_light": self.light.turn_off_light,
            "adjust_light_brightness": self.light.adjust_light_brightness,
            "open_cover": self.cover.open_cover,
            "close_cover": self.cover.close_cover,
            "set_cover_position": self.cover.set_cover_position,
            "stop_cover": self.cover.stop_cover,
            "open_cover_tilt": self.cover.open_cover_tilt,
            "close_cover_tilt": self.cover.close_cover_tilt,
            "set_cover_tilt_position": self.cover.set_cover_tilt_position,
            "turn_on_climate": self.climate.turn_on_climate,
            "turn_off_climate": self.climate.turn_off_climate,
            "set_temperature": self.climate.set_temperature,
            "set_humidity": self.climate.set_humidity,
            "set_fan_mode": self.climate.set_fan_mode,
            "set_hvac_mode": self.climate.set_hvac_mode,
            "turn_on_media_player": self.media_player.turn_on_media_player,
            "turn_off_media_player": self.media_player.turn_off_media_player,
            "adjust_media_volume": self.media_player.adjust_media_volume,
            "select_media_source": self.media_player.select_media_source,
            "set_media_mute": self.media_player.set_media_mute,
            "get_forecasts": self.weather.get_forecasts,
            "update_user_preferences": self.user_preferences.update_user_preferences,
            "get_user_preferences": self.user_preferences.get_user_preferences,
            "get_preference_keys": self.user_preferences.get_preference_keys,
            "get_preference_by_key": self.user_preferences.get_preference_by_key,
            "get_shopping_list": self.shopping_list.get_shopping_list,
            "add_shopping_list_item": self.shopping_list.add_shopping_list_item,
            "mark_shopping_list_item_complete": self.shopping_list.mark_shopping_list_item_complete,
            "remove_completed_shopping_list_item": self.shopping_list.remove_completed_shopping_list_item,
            "add_automation": self.automation.add_automation,
            "update_automation": self.automation.update_automation,
            "remove_automation": self.automation.remove_automation,
            "start_timer": self.timer.start_timer,
            "get_calendar_events": self.calendar.get_calendar_events,
            "add_calendar_event": self.calendar.add_calendar_event,
        }

    async def execute_tool_call(
        self,
        tool_call: dict[str, Any],
        exposed_entities: list[dict[str, Any]],
    ) -> dict[str, Any]:
        function_payload = tool_call.get("function") or {}
        function_name = function_payload.get("name")
        if not function_name:
            raise ToolExecutionError(f"Missing function name in tool call: {tool_call}")

        arguments_raw = function_payload.get("arguments") or "{}"
        try:
            arguments = json.loads(arguments_raw)
        except json.JSONDecodeError as err:
            raise ToolExecutionError(
                f"Invalid tool arguments for {function_name}: {arguments_raw}"
            ) from err
        if not isinstance(arguments, dict):
            raise ToolExecutionError(
                f"Tool arguments for {function_name} must be a JSON object"
            )

        if function_name == "execute_service":
            result = await self.entity.execute_service(arguments, exposed_entities)
        elif function_name in self._handlers:
            result = await self._handlers[function_name](arguments)
        else:
            raise ToolExecutionError(
                f"Unsupported tool: {function_name}, consider update your Auri client to the latest version that supports this tool."
            )

        return {
            "tool_call_id": tool_call.get("id"),
            "name": function_name,
            "result": result,
        }

    async def _get_skill_data(self, arguments: dict[str, Any]) -> dict[str, Any]:
        skill_name = str(arguments.get("skill", "")).strip().lower()
        if not skill_name:
            return {
                "success": False,
                "error": "skill is required",
                "available_skills": list(SKILL_REGISTRY.keys()),
            }

        skill = SKILL_REGISTRY.get(skill_name)
        if not skill:
            return {
                "success": False,
                "error": f"Unknown skill: {skill_name}",
                "available_skills": list(SKILL_REGISTRY.keys()),
            }

        return {
            "success": True,
            "skill": skill_name,
            "description": skill.get("description", ""),
            "prompt": skill.get("prompt", ""),
            "tools": skill.get("tools", []),
        }
