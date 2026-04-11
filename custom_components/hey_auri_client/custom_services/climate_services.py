"""Tool handlers for climate actions."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from ..exceptions import ToolExecutionError
from ..helpers import (
    _convert_temperature,
    _normalize_temperature_unit,
    resolve_entity_id_no_fallback,
)


class ClimateToolService:
    """Handle climate-oriented tool calls."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def _call_climate_service(
        self,
        service: str,
        service_data: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Call a climate service and normalize validation errors to retry payloads."""
        try:
            await self.hass.services.async_call(
                domain="climate",
                service=service,
                service_data=service_data,
                blocking=True,
            )
            return None
        except vol.error.MultipleInvalid as err:
            return {"retry": str(err)}
        except HomeAssistantError as err:
            raise ToolExecutionError(str(err)) from err

    async def turn_on_climate(self, arguments: dict[str, Any]) -> dict[str, Any]:
        entity_id = resolve_entity_id_no_fallback(
            self.hass,
            "climate",
            arguments.get("entity_id"),
        )
        retry = await self._call_climate_service("turn_on", {"entity_id": entity_id})
        if retry:
            return retry

        return {"success": True, "entity_id": entity_id}

    async def turn_off_climate(self, arguments: dict[str, Any]) -> dict[str, Any]:
        entity_id = resolve_entity_id_no_fallback(
            self.hass,
            "climate",
            arguments.get("entity_id"),
        )
        retry = await self._call_climate_service("turn_off", {"entity_id": entity_id})
        if retry:
            return retry

        return {"success": True, "entity_id": entity_id}

    async def set_temperature(self, arguments: dict[str, Any]) -> dict[str, Any]:
        entity_id = resolve_entity_id_no_fallback(
            self.hass,
            "climate",
            arguments.get("entity_id"),
        )
        raw_temperature = arguments.get("temperature")
        raw_temperature_high = arguments.get("target_temp_high")
        raw_temperature_low = arguments.get("target_temp_low")
        state = self.hass.states.get(entity_id)
        hvac_mode = arguments.get("hvac_mode", state.state)
        requested_unit = _normalize_temperature_unit(arguments.get("temperature_unit"))
        target_unit = _normalize_temperature_unit(
            state.attributes.get("temperature_unit", arguments.get("temperature_unit"))
        )
        service_data: dict[str, Any] = {}

        if hvac_mode in {"heat_cool", "auto"}:
            if raw_temperature_high is None or raw_temperature_low is None:
                return {
                    "retry": "target_temp_high and target_temp_low are required for heat_cool/auto hvac_mode"
                }

            try:
                requested_temperature_high = float(raw_temperature_high)
                requested_temperature_low = float(raw_temperature_low)
            except (TypeError, ValueError):
                return {"retry": "target_temp_high and target_temp_low must be numbers"}

            service_temperature_high = _convert_temperature(
                requested_temperature_high,
                from_unit=requested_unit,
                to_unit=target_unit,
            )
            service_temperature_low = _convert_temperature(
                requested_temperature_low,
                from_unit=requested_unit,
                to_unit=target_unit,
            )
            service_data = {
                "entity_id": entity_id,
                "target_temp_high": service_temperature_high,
                "target_temp_low": service_temperature_low,
            }

        if hvac_mode in {"heat", "cool"}:
            if raw_temperature is None:
                return {"retry": "temperature is required for heat/cool hvac_mode"}

            try:
                requested_temperature = float(raw_temperature)
            except (TypeError, ValueError):
                return {"retry": "temperature must be a number"}

            service_temperature = _convert_temperature(
                requested_temperature,
                from_unit=requested_unit,
                to_unit=target_unit,
            )

            service_data = {
                "entity_id": entity_id,
                "temperature": service_temperature,
            }

        if hvac_mode is not None:
            service_data["hvac_mode"] = str(hvac_mode)

        retry = await self._call_climate_service("set_temperature", service_data)
        if retry:
            return retry

        updated_state = self.hass.states.get(entity_id)
        if updated_state is None:
            raise ToolExecutionError(f"Entity not found: {entity_id}")

        return {
            "success": True,
            "entity_id": entity_id,
            "state": updated_state.state,
            "attributes": dict(updated_state.attributes),
        }

    async def set_humidity(self, arguments: dict[str, Any]) -> dict[str, Any]:
        entity_id = resolve_entity_id_no_fallback(
            self.hass,
            "climate",
            arguments.get("entity_id"),
        )
        raw_humidity = arguments.get("humidity")
        if raw_humidity is None:
            return {"retry": "humidity is required for set_humidity"}

        try:
            humidity = float(raw_humidity)
        except (TypeError, ValueError):
            return {"retry": "humidity must be a number between 0 and 100"}

        if humidity < 0 or humidity > 100:
            return {"retry": "humidity must be a number between 0 and 100"}

        retry = await self._call_climate_service(
            "set_humidity",
            {"entity_id": entity_id, "humidity": humidity},
        )
        if retry:
            return retry

        return {"success": True, "entity_id": entity_id, "humidity": humidity}

    async def set_fan_mode(self, arguments: dict[str, Any]) -> dict[str, Any]:
        entity_id = resolve_entity_id_no_fallback(
            self.hass,
            "climate",
            arguments.get("entity_id"),
        )
        fan_mode = str(arguments.get("fan_mode", "")).strip()
        if not fan_mode:
            return {"retry": "fan_mode is required for set_fan_mode"}

        state = self.hass.states.get(entity_id)
        if state is None:
            raise ToolExecutionError(f"Entity not found: {entity_id}")

        supported_fan_modes = state.attributes.get("fan_modes") or []
        if supported_fan_modes and fan_mode not in supported_fan_modes:
            return {
                "retry": f"Unsupported fan_mode '{fan_mode}'. Supported fan_modes: {supported_fan_modes}"
            }

        retry = await self._call_climate_service(
            "set_fan_mode",
            {"entity_id": entity_id, "fan_mode": fan_mode},
        )
        if retry:
            return retry

        return {"success": True, "entity_id": entity_id, "fan_mode": fan_mode}

    async def set_hvac_mode(self, arguments: dict[str, Any]) -> dict[str, Any]:
        entity_id = resolve_entity_id_no_fallback(
            self.hass,
            "climate",
            arguments.get("entity_id"),
        )
        hvac_mode = str(arguments.get("hvac_mode", "")).strip()
        if not hvac_mode:
            return {"retry": "hvac_mode is required for set_hvac_mode"}

        state = self.hass.states.get(entity_id)
        if state is None:
            raise ToolExecutionError(f"Entity not found: {entity_id}")

        supported_hvac_modes = state.attributes.get("hvac_modes") or []
        if supported_hvac_modes and hvac_mode not in supported_hvac_modes:
            return {
                "retry": f"Unsupported hvac_mode '{hvac_mode}'. Supported hvac_modes: {supported_hvac_modes}"
            }

        retry = await self._call_climate_service(
            "set_hvac_mode",
            {"entity_id": entity_id, "hvac_mode": hvac_mode},
        )
        if retry:
            return retry

        return {"success": True, "entity_id": entity_id, "hvac_mode": hvac_mode}
