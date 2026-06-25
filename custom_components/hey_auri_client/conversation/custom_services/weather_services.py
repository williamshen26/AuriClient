"""Tool handlers for weather actions."""
from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from ..helpers import resolve_entity_id


class WeatherToolService:
    """Handle weather-oriented tool calls."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get_forecasts(self, arguments: dict[str, Any]) -> dict[str, Any]:
        forecast_type = arguments.get("type")
        weather_entity_id = resolve_entity_id(
            self.hass,
            "weather",
            arguments.get("entity_id"),
        )

        result = await self.hass.services.async_call(
            domain="weather",
            service="get_forecasts",
            service_data={"type": forecast_type, "entity_id": weather_entity_id},
            blocking=True,
            return_response=True,
        )
        return {
            "temperature_unit": str(self.hass.config.units.temperature_unit),
            "result": result,
        }
