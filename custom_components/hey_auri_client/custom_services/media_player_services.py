"""Tool handlers for media player actions."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from ..exceptions import ToolExecutionError
from ..helpers import (
    _clamp_percentage,
    _clamp_step_percentage,
    get_retry_entities,
    resolve_entity_id_no_fallback,
)


class MediaPlayerToolService:
    """Handle media player-oriented tool calls."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def _call_media_service(
        self,
        service: str,
        service_data: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Call a media_player service and normalize validation errors to retry payloads."""
        try:
            await self.hass.services.async_call(
                domain="media_player",
                service=service,
                service_data=service_data,
                blocking=True,
            )
            return None
        except vol.error.MultipleInvalid as err:
            return {"retry": str(err)}
        except HomeAssistantError as err:
            raise ToolExecutionError(str(err)) from err

    async def set_media_mute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        entity_id = resolve_entity_id_no_fallback(
            self.hass,
            "media_player",
            arguments.get("entity_id"),
        )
        is_volume_muted = arguments.get("is_volume_muted")
        if is_volume_muted is None:
            return {"retry": "is_volume_muted is required for set_media_mute"}
        retry = await self._call_media_service(
            "volume_mute",
            {
                "entity_id": entity_id,
                "is_volume_muted": is_volume_muted,
            },
        )
        if retry:
            return retry

        return {
            "success": True,
            "entity_id": entity_id,
            "is_volume_muted": is_volume_muted,
        }

    async def select_media_source(self, arguments: dict[str, Any]) -> dict[str, Any]:
        entity_id = resolve_entity_id_no_fallback(
            self.hass,
            "media_player",
            arguments.get("entity_id"),
        )
        source = str(arguments.get("source", "")).strip()
        if not source:
            return {"retry": "source is required for select_media_source"}

        state = self.hass.states.get(entity_id)
        if state is None:
            raise ToolExecutionError(f"Entity not found: {entity_id}")

        source_list = state.attributes.get("source_list")
        available_sources = source_list if isinstance(source_list, list) else []

        if source not in available_sources:
            for candidate_entity_id in get_retry_entities(self.hass, entity_id):
                candidate_state = self.hass.states.get(candidate_entity_id)
                if candidate_state is None:
                    continue

                candidate_sources = candidate_state.attributes.get("source_list")
                if isinstance(candidate_sources, list) and source in candidate_sources:
                    return {
                        "retry": (
                            f"source '{source}' is not available for '{entity_id}'. "
                            f"Please use entity_id '{candidate_entity_id}' instead. "
                            "Just do it and tell the user which device you changed, no confirmation required."
                        )
                    }

            return {
                "retry": (
                    f"source '{source}' is not available for '{entity_id}'. "
                    f"Please check the source name. Available sources for '{entity_id}': {available_sources}"
                )
            }

        retry = await self._call_media_service(
            "select_source",
            {"entity_id": entity_id, "source": source},
        )
        if retry:
            return retry

        return {"success": True, "entity_id": entity_id, "source": source}

    async def turn_on_media_player(self, arguments: dict[str, Any]) -> dict[str, Any]:
        entity_id = resolve_entity_id_no_fallback(
            self.hass,
            "media_player",
            arguments.get("entity_id"),
        )
        retry = await self._call_media_service("turn_on", {"entity_id": entity_id})
        if retry:
            return retry

        return {"success": True, "entity_id": entity_id}

    async def turn_off_media_player(self, arguments: dict[str, Any]) -> dict[str, Any]:
        entity_id = resolve_entity_id_no_fallback(
            self.hass,
            "media_player",
            arguments.get("entity_id"),
        )
        retry = await self._call_media_service("turn_off", {"entity_id": entity_id})
        if retry:
            return retry

        return {"success": True, "entity_id": entity_id}

    async def adjust_media_volume(self, arguments: dict[str, Any]) -> dict[str, Any]:
        entity_id = resolve_entity_id_no_fallback(
            self.hass,
            "media_player",
            arguments.get("entity_id"),
        )
        raw_volume_pct = arguments.get("volume_pct")
        raw_volume_step_pct = arguments.get("volume_step_pct")

        if raw_volume_pct is None and raw_volume_step_pct is None:
            return {
                "retry": "volume_pct or volume_step_pct is required for adjust_volume"
            }

        service_data: dict[str, Any] = {"entity_id": entity_id}
        response: dict[str, Any] = {
            "success": True,
            "entity_id": entity_id,
        }
        if raw_volume_pct is not None:
            target_volume_pct = _clamp_percentage(float(raw_volume_pct) * 100) / 100
            service_data["volume_level"] = target_volume_pct
            response["volume_pct"] = float(target_volume_pct)
        else:
            volume_step_pct = _clamp_step_percentage(float(raw_volume_step_pct) * 100) / 100
            state = self.hass.states.get(entity_id)
            current_volume = state.attributes.get("volume_level")
            new_volume = _clamp_step_percentage((current_volume + volume_step_pct) * 100) / 100
            service_data["volume_level"] = new_volume
            response["volume_pct"] = float(new_volume)

        try:
            retry = await self._call_media_service("volume_set", service_data)
            if retry:
                return retry
            return response
        except ToolExecutionError as err:
            retry_entities = get_retry_entities(self.hass, entity_id)
            if retry_entities:
                return {
                    "retry": (
                        f"Unable to adjust volume for '{entity_id}'. "
                        f"Please use entity_id '{retry_entities[0]}' instead. "
                        "Just do it and tell the user which device you changed, no confirmation required."
                    )
                }
            raise
