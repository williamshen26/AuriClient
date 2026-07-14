"""Tool handlers for light and switch actions."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from ...exceptions import ToolExecutionError
from ..helpers import (
    _clamp_percentage,
    _clamp_step_percentage,
    resolve_entity_id_no_fallback,
)


class LightToolService:
    """Handle light-oriented tool calls."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    def _get_supported_color_modes(self, entity_id: str) -> set[str]:
        """Return normalized supported_color_modes for a light entity."""
        state = self.hass.states.get(entity_id)
        if state is None or not entity_id.startswith("light."):
            return set()

        raw_modes = state.attributes.get("supported_color_modes")
        if isinstance(raw_modes, (list, set, tuple)):
            return {str(mode).lower() for mode in raw_modes}
        return set()

    def _light_supports_brightness(self, entity_id: str) -> bool:
        """Return True if the target light supports brightness changes."""
        modes = self._get_supported_color_modes(entity_id)
        if not modes:
            return False

        return bool(
            modes.intersection(
                {
                    "brightness",
                    "color_temp",
                    "hs",
                    "xy",
                    "rgb",
                    "rgbw",
                    "rgbww",
                    "white",
                }
            )
        )

    def _light_supports_rgb_color(self, entity_id: str) -> bool:
        """Return True if the target light supports RGB-like color control."""
        modes = self._get_supported_color_modes(entity_id)
        if not modes:
            return False

        return bool(modes.intersection({"hs", "xy", "rgb", "rgbw", "rgbww"}))

    @staticmethod
    def _is_switch_entity_requested(entity_id: Any) -> bool:
        """Return True when caller explicitly requested a switch entity."""
        return str(entity_id or "").strip().startswith("switch.")

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
            try:
                entity_id = resolve_entity_id_no_fallback(
                    self.hass,
                    "switch",
                    arguments.get("entity_id"),
                )
            except ToolExecutionError:
                return {
                    "retry": "entity_id is required and must be a valid light or switch entity, double-check your spelling and that the entity is available in list of entities",
                }

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
            try:
                entity_id = resolve_entity_id_no_fallback(
                    self.hass,
                    "switch",
                    arguments.get("entity_id"),
                )
            except ToolExecutionError:
                return {
                    "retry": "entity_id is required and must be a valid light or switch entity, double-check your spelling and that the entity is available in list of entities",
                }

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
        requested_entity_id = arguments.get("entity_id")
        if self._is_switch_entity_requested(requested_entity_id):
            return {
                "error": f"{requested_entity_id} is a switch and does not support brightness control"
            }

        entity_id = resolve_entity_id_no_fallback(
            self.hass,
            "light",
            requested_entity_id,
        )
        if not self._light_supports_brightness(entity_id):
            return {
                "error": f"{entity_id} does not support brightness control"
            }

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

    async def adjust_light_color(self, arguments: dict[str, Any]) -> dict[str, Any]:
        requested_entity_id = arguments.get("entity_id")
        if self._is_switch_entity_requested(requested_entity_id):
            return {
                "error": f"{requested_entity_id} is a switch and does not support color control"
            }

        entity_id = resolve_entity_id_no_fallback(
            self.hass,
            "light",
            requested_entity_id,
        )
        if not self._light_supports_rgb_color(entity_id):
            return {
                "error": f"{entity_id} does not support color control"
            }

        rgb_color = arguments.get("rgb_color")
        if not isinstance(rgb_color, dict):
            return {"retry": "rgb_color object with r, g, b is required for adjust_light_color"}

        r = self._normalize_rgb_channel(rgb_color.get("r"), "r")
        if isinstance(r, dict):
            return r
        g = self._normalize_rgb_channel(rgb_color.get("g"), "g")
        if isinstance(g, dict):
            return g
        b = self._normalize_rgb_channel(rgb_color.get("b"), "b")
        if isinstance(b, dict):
            return b

        normalized_rgb = [r, g, b]
        retry = await self._call_domain_turn(
            domain="light",
            service="turn_on",
            service_data={
                "entity_id": entity_id,
                "rgb_color": normalized_rgb,
            },
        )
        if retry:
            return retry

        return {
            "success": True,
            "entity_id": entity_id,
            "rgb_color": {
                "r": r,
                "g": g,
                "b": b,
            },
        }

    @staticmethod
    def _normalize_rgb_channel(value: Any, channel_name: str) -> int | dict[str, str]:
        if not isinstance(value, (int, float)):
            return {"retry": f"rgb_color.{channel_name} must be a number between 0 and 255"}
        clamped = int(round(value))
        if clamped < 0:
            clamped = 0
        if clamped > 255:
            clamped = 255
        return clamped
