"""Tool handlers for light and switch actions."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from ..exceptions import ToolExecutionError
from ..helpers import (
    _clamp_percentage,
    _clamp_step_percentage,
    resolve_entity_id_no_fallback,
)


class LightToolService:
    """Handle light-oriented tool calls."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def _call_domain_turn(
        self,
        *,
        domain: str,
        service: str,
        service_data: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Call light/switch services and normalize validation errors to retry payloads."""
        try:
            await self.hass.services.async_call(
                domain=domain,
                service=service,
                service_data=service_data,
                blocking=True,
            )
            return None
        except vol.error.MultipleInvalid as err:
            return {"retry": str(err)}
        except HomeAssistantError as err:
            raise ToolExecutionError(str(err)) from err

    async def turn_on_light(self, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            entity_id = resolve_entity_id_no_fallback(
                self.hass,
                "light",
                arguments.get("entity_id"),
            )
        except ToolExecutionError:
            entity_id = resolve_entity_id_no_fallback(
                self.hass,
                "switch",
                arguments.get("entity_id"),
            )

        domain = entity_id.split(".")[0]
        retry = await self._call_domain_turn(
            domain=domain,
            service="turn_on",
            service_data={"entity_id": entity_id},
        )
        if retry:
            return retry

        return {"success": True, "entity_id": entity_id}

    async def turn_off_light(self, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            entity_id = resolve_entity_id_no_fallback(
                self.hass,
                "light",
                arguments.get("entity_id"),
            )
        except ToolExecutionError:
            entity_id = resolve_entity_id_no_fallback(
                self.hass,
                "switch",
                arguments.get("entity_id"),
            )

        domain = entity_id.split(".")[0]
        retry = await self._call_domain_turn(
            domain=domain,
            service="turn_off",
            service_data={"entity_id": entity_id},
        )
        if retry:
            return retry

        return {"success": True, "entity_id": entity_id}

    async def adjust_light_brightness(self, arguments: dict[str, Any]) -> dict[str, Any]:
        entity_id = resolve_entity_id_no_fallback(
            self.hass,
            "light",
            arguments.get("entity_id"),
        )
        raw_brightness_pct = arguments.get("brightness_pct")
        raw_brightness_step_pct = arguments.get("brightness_step_pct")

        if raw_brightness_pct is None and raw_brightness_step_pct is None:
            return {
                "retry": "brightness_pct or brightness_step_pct is required for adjust_light_brightness"
            }

        service_data: dict[str, Any] = {"entity_id": entity_id}
        response: dict[str, Any] = {
            "success": True,
            "entity_id": entity_id,
        }

        if raw_brightness_pct is not None:
            target_brightness_pct = _clamp_percentage(raw_brightness_pct)
            service_data["brightness_pct"] = target_brightness_pct
            response["brightness_pct"] = target_brightness_pct
        else:
            brightness_step_pct = _clamp_step_percentage(raw_brightness_step_pct)
            service_data["brightness_step_pct"] = brightness_step_pct
            response["brightness_step_pct"] = brightness_step_pct

        retry = await self._call_domain_turn(
            domain="light",
            service="turn_on",
            service_data=service_data,
        )
        if retry:
            return retry

        return response
