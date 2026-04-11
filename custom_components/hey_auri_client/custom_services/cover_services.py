"""Tool handlers for cover actions."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from ..exceptions import ToolExecutionError
from ..helpers import _clamp_percentage_zero_to_hundred, resolve_entity_id_no_fallback


class CoverToolService:
    """Handle cover-oriented tool calls."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def _call_cover_service(
        self,
        service: str,
        service_data: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Call a cover service and normalize validation errors to retry payloads."""
        try:
            await self.hass.services.async_call(
                domain="cover",
                service=service,
                service_data=service_data,
                blocking=True,
            )
            return None
        except vol.error.MultipleInvalid as err:
            return {"retry": str(err)}
        except HomeAssistantError as err:
            raise ToolExecutionError(str(err)) from err

    async def open_cover(self, arguments: dict[str, Any]) -> dict[str, Any]:
        entity_id = resolve_entity_id_no_fallback(self.hass, "cover", arguments.get("entity_id"))
        retry = await self._call_cover_service("open_cover", {"entity_id": entity_id})
        if retry:
            return retry

        return {"success": True, "entity_id": entity_id}

    async def close_cover(self, arguments: dict[str, Any]) -> dict[str, Any]:
        entity_id = resolve_entity_id_no_fallback(self.hass, "cover", arguments.get("entity_id"))
        retry = await self._call_cover_service("close_cover", {"entity_id": entity_id})
        if retry:
            return retry

        return {"success": True, "entity_id": entity_id}

    async def set_cover_position(self, arguments: dict[str, Any]) -> dict[str, Any]:
        entity_id = resolve_entity_id_no_fallback(self.hass, "cover", arguments.get("entity_id"))
        raw_position = arguments.get("position")
        if raw_position is None:
            return {"retry": "position is required for set_cover_position"}

        position = _clamp_percentage_zero_to_hundred(raw_position)
        retry = await self._call_cover_service(
            "set_cover_position",
            {"entity_id": entity_id, "position": position},
        )
        if retry:
            return retry

        return {"success": True, "entity_id": entity_id, "position": position}

    async def stop_cover(self, arguments: dict[str, Any]) -> dict[str, Any]:
        entity_id = resolve_entity_id_no_fallback(self.hass, "cover", arguments.get("entity_id"))
        retry = await self._call_cover_service("stop_cover", {"entity_id": entity_id})
        if retry:
            return retry

        return {"success": True, "entity_id": entity_id}

    async def open_cover_tilt(self, arguments: dict[str, Any]) -> dict[str, Any]:
        entity_id = resolve_entity_id_no_fallback(self.hass, "cover", arguments.get("entity_id"))
        retry = await self._call_cover_service("open_cover_tilt", {"entity_id": entity_id})
        if retry:
            return retry

        return {"success": True, "entity_id": entity_id}

    async def close_cover_tilt(self, arguments: dict[str, Any]) -> dict[str, Any]:
        entity_id = resolve_entity_id_no_fallback(self.hass, "cover", arguments.get("entity_id"))
        retry = await self._call_cover_service("close_cover_tilt", {"entity_id": entity_id})
        if retry:
            return retry

        return {"success": True, "entity_id": entity_id}

    async def set_cover_tilt_position(self, arguments: dict[str, Any]) -> dict[str, Any]:
        entity_id = resolve_entity_id_no_fallback(self.hass, "cover", arguments.get("entity_id"))
        raw_tilt_position = arguments.get("tilt_position")
        if raw_tilt_position is None:
            return {"retry": "tilt_position is required for set_cover_tilt_position"}

        tilt_position = _clamp_percentage_zero_to_hundred(raw_tilt_position)
        retry = await self._call_cover_service(
            "set_cover_tilt_position",
            {"entity_id": entity_id, "tilt_position": tilt_position},
        )
        if retry:
            return retry

        return {
            "success": True,
            "entity_id": entity_id,
            "tilt_position": tilt_position,
        }
