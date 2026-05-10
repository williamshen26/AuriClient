"""Tool handlers for media player actions."""
from __future__ import annotations

import asyncio
from typing import Any

import voluptuous as vol

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.components.media_player import MediaPlayerEntityFeature

from ..cache import get_media_player_sources
from ..exceptions import ToolExecutionError
from ..helpers import (
    _clamp_percentage,
    _clamp_step_percentage,
    get_retry_entities,
    resolve_entity_id_no_fallback,
    transform_auri_entity_id_to_ha_entity_id,
    transform_ha_entity_id_to_auri_entity_id,
)

class MediaPlayerToolService:
    """Handle media player-oriented tool calls."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    def media_player_has_feature(
        self,
        entity_id: str,
        feature: MediaPlayerEntityFeature,
    ) -> bool:
        """Return True if the media_player entity supports the given feature."""
        state = self.hass.states.get(entity_id)
        if state is None:
            return False

        if not entity_id.startswith("media_player."):
            return False

        supported = state.attributes.get("supported_features", 0)
        return bool(supported & int(feature))

    def decode_media_player_supported_features(
        self,
        entity_id: str,
    ) -> list[MediaPlayerEntityFeature]:
        """Return all supported MediaPlayerEntityFeature values for a media_player entity."""
        if not entity_id.startswith("media_player."):
            raise ValueError(f"{entity_id} is not a media_player entity")

        state = self.hass.states.get(entity_id)
        if state is None:
            raise ValueError(f"Entity not found: {entity_id}")

        supported = int(state.attributes.get("supported_features", 0))

        return [
            feature
            for feature in MediaPlayerEntityFeature
            if supported & int(feature)
        ]


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
        except Exception as err:
            # Some integrations raise backend-specific exceptions (for example
            # async_upnp_client UpnpConnectionError) that are not HomeAssistantError.
            raise ToolExecutionError(str(err)) from err

    async def set_media_mute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        entity_id = resolve_entity_id_no_fallback(
            self.hass,
            "media_player",
            transform_auri_entity_id_to_ha_entity_id(arguments.get("entity_id")),
        )
        state = self.hass.states.get(entity_id)
        if state and state.state == "off":
            return {
                "error": (
                    f"Cannot adjust mute for {transform_ha_entity_id_to_auri_entity_id(self.hass, entity_id)} "
                    "because it's currently turned off"
                )
            }

        is_volume_muted = arguments.get("is_volume_muted")
        if is_volume_muted is None:
            return {"retry": "is_volume_muted is required for set_media_mute"}
        
        if not self.media_player_has_feature(entity_id, MediaPlayerEntityFeature.VOLUME_MUTE):
            return {"retry": f"{transform_ha_entity_id_to_auri_entity_id(self.hass, entity_id)} does not support volume mute control, based on context, decide whether to retry with a different entity, do nothing, or inform the user that this action is not supported for this device."}

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
            "entity_id": transform_ha_entity_id_to_auri_entity_id(self.hass, entity_id),
            "is_volume_muted": is_volume_muted,
        }

    async def select_media_source(self, arguments: dict[str, Any]) -> dict[str, Any]:
        conversation_id = arguments.get("_conversation_id")
        entity_id = resolve_entity_id_no_fallback(
            self.hass,
            "media_player",
            transform_auri_entity_id_to_ha_entity_id(arguments.get("entity_id")),
        )
        source = str(arguments.get("source", "")).strip()
        if not source:
            return {"retry": "source is required for select_media_source"}

        state = self.hass.states.get(entity_id)
        if state is None:
            raise ToolExecutionError(f"Entity not found: {transform_ha_entity_id_to_auri_entity_id(self.hass, entity_id)}")

        available_sources = get_media_player_sources(entity_id)
        if not available_sources:
            source_list = state.attributes.get("source_list")
            available_sources = source_list if isinstance(source_list, list) else []

        if source not in available_sources:
            for candidate_entity_id in get_retry_entities(
                self.hass,
                entity_id,
                conversation_id=conversation_id,
            ):
                candidate_state = self.hass.states.get(candidate_entity_id)
                if candidate_state is None:
                    continue

                candidate_sources = candidate_state.attributes.get("source_list")
                if isinstance(candidate_sources, list) and source in candidate_sources:
                    return {
                        "retry": (
                            f"source '{source}' is not available for '{transform_ha_entity_id_to_auri_entity_id(self.hass, entity_id)}'. "
                            f"Please use entity_id '{transform_ha_entity_id_to_auri_entity_id(self.hass, candidate_entity_id)}' instead. "
                            "Just do it and tell the user which device you changed, no confirmation required."
                        )
                    }

            return {
                "retry": (
                    f"source '{source}' is not available for '{transform_ha_entity_id_to_auri_entity_id(self.hass, entity_id)}'. "
                    f"Please check the source name. Available sources for '{transform_ha_entity_id_to_auri_entity_id(self.hass, entity_id)}': {available_sources}"
                )
            }

        if state.state == "off":
            retry = await self._call_media_service("turn_on", {"entity_id": entity_id})
            if retry:
                return {
                    "error": (
                        f"Cannot select source for {transform_ha_entity_id_to_auri_entity_id(self.hass, entity_id)} "
                        "because we failed to turn it on first: " + retry.get("retry", str(retry))
                    )
                }

            max_wait_cycles = 20
            for _ in range(max_wait_cycles):
                await asyncio.sleep(0.5)
                state = self.hass.states.get(entity_id)
                if state is not None and state.state != "off":
                    break
            else:
                return {
                    "error": (
                        f"Cannot select source for {transform_ha_entity_id_to_auri_entity_id(self.hass, entity_id)} "
                        "because it is still turned off after trying to turn it on"
                    )
                }

        retry = await self._call_media_service(
            "select_source",
            {"entity_id": entity_id, "source": source},
        )
        if retry:
            return retry

        return {"success": True, "entity_id": transform_ha_entity_id_to_auri_entity_id(self.hass, entity_id), "source": source}

    async def turn_on_media_player(self, arguments: dict[str, Any]) -> dict[str, Any]:
        entity_id = resolve_entity_id_no_fallback(
            self.hass,
            "media_player",
            transform_auri_entity_id_to_ha_entity_id(arguments.get("entity_id")),
        )

        if not self.media_player_has_feature(entity_id, MediaPlayerEntityFeature.TURN_ON):
            return {"retry": f"{transform_ha_entity_id_to_auri_entity_id(self.hass, entity_id)} does not support turn on control, based on context, decide whether to retry with a different entity, do nothing, or inform the user that this action is not supported for this device."}

        retry = await self._call_media_service("turn_on", {"entity_id": entity_id})
        if retry:
            return retry

        return {"success": True, "entity_id": transform_ha_entity_id_to_auri_entity_id(self.hass, entity_id)}

    async def turn_off_media_player(self, arguments: dict[str, Any]) -> dict[str, Any]:
        entity_id = resolve_entity_id_no_fallback(
            self.hass,
            "media_player",
            transform_auri_entity_id_to_ha_entity_id(arguments.get("entity_id")),
        )

        if not self.media_player_has_feature(entity_id, MediaPlayerEntityFeature.TURN_OFF):
            return {"retry": f"{transform_ha_entity_id_to_auri_entity_id(self.hass, entity_id)} does not support turn off control, based on context, decide whether to retry with a different entity, do nothing, or inform the user that this action is not supported for this device."}
        
        retry = await self._call_media_service("turn_off", {"entity_id": entity_id})
        if retry:
            return retry

        return {"success": True, "entity_id": transform_ha_entity_id_to_auri_entity_id(self.hass, entity_id)}

    async def media_player_play(self, arguments: dict[str, Any]) -> dict[str, Any]:
        conversation_id = arguments.get("_conversation_id")
        entity_id = resolve_entity_id_no_fallback(
            self.hass,
            "media_player",
            transform_auri_entity_id_to_ha_entity_id(arguments.get("entity_id")),
        )
        state = self.hass.states.get(entity_id)
        if state and state.state == "off":
            return {
                "error": (
                    f"Cannot play {transform_ha_entity_id_to_auri_entity_id(self.hass, entity_id)} "
                    "because it's currently turned off"
                )
            }

        try:
            retry = await self._call_media_service("media_play", {"entity_id": entity_id})
            if retry:
                return retry

            return {"success": True, "entity_id": transform_ha_entity_id_to_auri_entity_id(self.hass, entity_id)}
        except ToolExecutionError:
            retry_entities = get_retry_entities(
                self.hass,
                entity_id,
                conversation_id=conversation_id,
            )
            for retry_entity in retry_entities:
                if self.media_player_has_feature(retry_entity, MediaPlayerEntityFeature.MEDIA_PLAY):
                    return {
                        "retry": (
                            f"Unable to start playback for '{transform_ha_entity_id_to_auri_entity_id(self.hass, entity_id)}'. "
                            f"Please use entity_id '{transform_ha_entity_id_to_auri_entity_id(self.hass, retry_entity)}' instead. "
                            "Just do it and tell the user which device you changed, no confirmation required."
                        )
                    }
            raise

    async def media_player_pause(self, arguments: dict[str, Any]) -> dict[str, Any]:
        conversation_id = arguments.get("_conversation_id")
        entity_id = resolve_entity_id_no_fallback(
            self.hass,
            "media_player",
            transform_auri_entity_id_to_ha_entity_id(arguments.get("entity_id")),
        )
        state = self.hass.states.get(entity_id)
        if state and state.state == "off":
            return {
                "error": (
                    f"Cannot pause {transform_ha_entity_id_to_auri_entity_id(self.hass, entity_id)} "
                    "because it's currently turned off"
                )
            }

        try:
            retry = await self._call_media_service("media_pause", {"entity_id": entity_id})
            if retry:
                return retry

            return {"success": True, "entity_id": transform_ha_entity_id_to_auri_entity_id(self.hass, entity_id)}
        except ToolExecutionError:
            retry_entities = get_retry_entities(
                self.hass,
                entity_id,
                conversation_id=conversation_id,
            )
            for retry_entity in retry_entities:
                if self.media_player_has_feature(retry_entity, MediaPlayerEntityFeature.MEDIA_PAUSE):
                    return {
                        "retry": (
                            f"Unable to pause playback for '{transform_ha_entity_id_to_auri_entity_id(self.hass, entity_id)}'. "
                            f"Please use entity_id '{transform_ha_entity_id_to_auri_entity_id(self.hass, retry_entity)}' instead. "
                            "Just do it and tell the user which device you changed, no confirmation required."
                        )
                    }
            raise

    async def adjust_media_volume(self, arguments: dict[str, Any]) -> dict[str, Any]:
        conversation_id = arguments.get("_conversation_id")
        entity_id = resolve_entity_id_no_fallback(
            self.hass,
            "media_player",
            transform_auri_entity_id_to_ha_entity_id(arguments.get("entity_id")),
        )
        raw_volume_pct = arguments.get("volume_pct")
        raw_volume_step_pct = arguments.get("volume_step_pct")

        if raw_volume_pct is None and raw_volume_step_pct is None:
            return {
                "retry": "volume_pct or volume_step_pct is required for adjust_volume"
            }

        state = self.hass.states.get(entity_id)
        if state and state.state == "off":
            return {
                "error": (
                    f"Cannot adjust volume for {transform_ha_entity_id_to_auri_entity_id(self.hass, entity_id)} "
                    "because it's currently turned off"
                )
            }

        service_data: dict[str, Any] = {"entity_id": entity_id}
        response: dict[str, Any] = {
            "success": True,
            "entity_id": transform_ha_entity_id_to_auri_entity_id(self.hass, entity_id),
        }
        if raw_volume_pct is not None:
            target_volume_pct = _clamp_percentage(float(raw_volume_pct) * 100) / 100
            service_data["volume_level"] = target_volume_pct
            response["volume_pct"] = float(target_volume_pct)
        else:
            volume_step_pct = _clamp_step_percentage(float(raw_volume_step_pct) * 100) / 100
            current_volume = state.attributes.get("volume_level") if state else None
            if current_volume is None:
                return {
                    "error": (
                        f"Cannot adjust volume step for '{transform_ha_entity_id_to_auri_entity_id(self.hass, entity_id)}' "
                        "because its current volume is unavailable. The device may be off or unavailable."
                    )
                }
            new_volume = _clamp_step_percentage((current_volume + volume_step_pct) * 100) / 100
            service_data["volume_level"] = new_volume
            response["volume_pct"] = float(new_volume)

        try:
            retry = await self._call_media_service("volume_set", service_data)
            if retry:
                return retry
            return response
        except ToolExecutionError as err:
            retry_entities = get_retry_entities(
                self.hass,
                entity_id,
                conversation_id=conversation_id,
            )
            for retry_entity in retry_entities:
                if self.media_player_has_feature(retry_entity, MediaPlayerEntityFeature.VOLUME_SET):
                    return {
                        "retry": (
                            f"Unable to adjust volume for '{transform_ha_entity_id_to_auri_entity_id(self.hass, entity_id)}'. "
                            f"Please use entity_id '{transform_ha_entity_id_to_auri_entity_id(self.hass, retry_entity)}' instead. "
                            "Just do it and tell the user which device you changed, no confirmation required."
                        )
                    }
            raise
