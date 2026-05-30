"""Tool handlers for media player actions."""
from __future__ import annotations

import asyncio
from typing import Any

import voluptuous as vol

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.components.media_player import MediaPlayerEntityFeature
from homeassistant.helpers import device_registry as dr, entity_registry as er

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

    def _get_entity_macs(self, entity_id: str) -> list[str]:
        """Resolve all device MAC addresses for an entity via HA registries."""
        device_registry = dr.async_get(self.hass)
        entity_registry = er.async_get(self.hass)

        entity_entry = entity_registry.async_get(entity_id)
        if not entity_entry or not entity_entry.device_id:
            return []

        device = device_registry.async_get(entity_entry.device_id)
        if not device:
            return []

        macs: list[str] = []

        for connection_type, value in device.connections:
            if connection_type == dr.CONNECTION_NETWORK_MAC:
                macs.append(value)

        return macs

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

    async def set_media_shuffle(self, arguments: dict[str, Any]) -> dict[str, Any]:
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
                    f"Cannot set shuffle for {transform_ha_entity_id_to_auri_entity_id(self.hass, entity_id)} "
                    "because it's currently turned off"
                )
            }

        shuffle = arguments.get("shuffle")
        if shuffle is None:
            return {"retry": "shuffle is required for set_media_shuffle"}
        if not isinstance(shuffle, bool):
            return {"retry": "shuffle must be a boolean for set_media_shuffle"}

        if not self.media_player_has_feature(entity_id, MediaPlayerEntityFeature.SHUFFLE_SET):
            retry_entities = get_retry_entities(
                self.hass,
                entity_id,
                conversation_id=conversation_id,
            )
            for retry_entity in retry_entities:
                if self.media_player_has_feature(retry_entity, MediaPlayerEntityFeature.SHUFFLE_SET):
                    return {
                        "retry": (
                            f"{transform_ha_entity_id_to_auri_entity_id(self.hass, entity_id)} does not support shuffle control. "
                            f"Please use entity_id '{transform_ha_entity_id_to_auri_entity_id(self.hass, retry_entity)}' instead. "
                            "Just do it and tell the user which device you changed, no confirmation required."
                        )
                    }
            return {
                "retry": (
                    f"{transform_ha_entity_id_to_auri_entity_id(self.hass, entity_id)} does not support shuffle control, "
                    "based on context, decide whether to retry with a different entity, do nothing, or inform the user that this action is not supported for this device."
                )
            }

        retry = await self._call_media_service(
            "shuffle_set",
            {"entity_id": entity_id, "shuffle": shuffle},
        )
        if retry:
            return retry

        return {
            "success": True,
            "entity_id": transform_ha_entity_id_to_auri_entity_id(self.hass, entity_id),
            "shuffle": shuffle,
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

        def _normalize_source_name(value: str) -> str:
            return "".join(value.split()).lower()

        def _get_sources_for_entity(target_entity_id: str) -> list[str]:
            cached_sources = get_media_player_sources(target_entity_id)
            if cached_sources:
                return [item for item in cached_sources if isinstance(item, str)]

            target_state = self.hass.states.get(target_entity_id)
            if target_state is None:
                return []

            source_list = target_state.attributes.get("source_list")
            if isinstance(source_list, list):
                return [item for item in source_list if isinstance(item, str)]
            return []

        def _match_source_by_normalized_name(
            requested_source: str,
            sources: list[str],
        ) -> str | None:
            normalized_requested_source = _normalize_source_name(requested_source)
            for source_item in sources:
                if _normalize_source_name(source_item) == normalized_requested_source:
                    return source_item
            return None

        state = self.hass.states.get(entity_id)
        if state is None:
            raise ToolExecutionError(f"Entity not found: {transform_ha_entity_id_to_auri_entity_id(self.hass, entity_id)}")

        available_sources = _get_sources_for_entity(entity_id)
        matched_source = _match_source_by_normalized_name(source, available_sources)
        if matched_source is not None:
            source = matched_source

        if matched_source is None:
            for candidate_entity_id in get_retry_entities(
                self.hass,
                entity_id,
                conversation_id=conversation_id,
            ):
                candidate_sources = _get_sources_for_entity(candidate_entity_id)
                if not candidate_sources:
                    continue

                candidate_matched_source = _match_source_by_normalized_name(source, candidate_sources)
                if candidate_matched_source is not None:
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

            max_wait_cycles = 25
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

        macs = self._get_entity_macs(entity_id)
        if macs and self.hass.services.has_service("wake_on_lan", "send_magic_packet"):
            for mac in macs:
                try:
                    await self.hass.services.async_call(
                        domain="wake_on_lan",
                        service="send_magic_packet",
                        service_data={
                            "mac": mac,
                            "broadcast_port": 9,
                        },
                        blocking=True,
                    )
                except Exception:
                    # Best-effort wake attempt; continue with standard turn_on flow.
                    pass

        retry = await self._call_media_service("turn_on", {"entity_id": entity_id})
        if retry:
            return retry

        return {"success": True, "entity_id": transform_ha_entity_id_to_auri_entity_id(self.hass, entity_id)}

    async def search_and_play_music(self, arguments: dict[str, Any]) -> dict[str, Any]:
        entity_id = resolve_entity_id_no_fallback(
            self.hass,
            "media_player",
            transform_auri_entity_id_to_ha_entity_id(arguments.get("entity_id")),
        )
        state = self.hass.states.get(entity_id)
        if state is None:
            raise ToolExecutionError(
                f"Entity not found: {transform_ha_entity_id_to_auri_entity_id(self.hass, entity_id)}"
            )

        query = arguments.get("query")
        if not isinstance(query, dict):
            return {"retry": "query is required for search_and_play_music"}

        artist = str(query.get("artist", "")).strip()
        track = str(query.get("track", "")).strip()
        album = str(query.get("album", "")).strip()
        playlist = str(query.get("playlist", "")).strip()

        if track:
            search_name = track
            media_type = "track"
        elif playlist:
            search_name = playlist
            media_type = "playlist"
        elif album:
            search_name = album
            media_type = "album"
        elif artist:
            search_name = artist
            media_type = "artist"
        else:
            return {
                "retry": "query must include at least one of: track, playlist, album, or artist"
            }

        music_assistant_entries = self.hass.config_entries.async_entries("music_assistant")
        if not music_assistant_entries:
            return {
                "error": (
                    f"Cannot search music for {transform_ha_entity_id_to_auri_entity_id(self.hass, entity_id)} "
                    "because Music Assistant integration is not configured"
                )
            }

        loaded_music_assistant_entry = next(
            (
                entry
                for entry in music_assistant_entries
                if "loaded" in str(getattr(entry, "state", "")).lower()
            ),
            None,
        )
        selected_entry = loaded_music_assistant_entry or music_assistant_entries[0]
        config_entry_id = selected_entry.entry_id

        service_data: dict[str, Any] = {
            "config_entry_id": config_entry_id,
            "name": search_name,
            "media_type": [media_type],
            "limit": 1,
            "library_only": False,
        }
        if artist:
            service_data["artist"] = artist
        if album:
            service_data["album"] = album

        try:
            search_response = await self.hass.services.async_call(
                domain="music_assistant",
                service="search",
                service_data=service_data,
                blocking=True,
                return_response=True,
            )
        except vol.error.MultipleInvalid as err:
            return {"retry": str(err)}
        except HomeAssistantError as err:
            raise ToolExecutionError(str(err)) from err
        except Exception as err:
            raise ToolExecutionError(str(err)) from err

        media_group_key_by_type = {
            "artist": "artists",
            "album": "albums",
            "track": "tracks",
            "playlist": "playlists",
        }
        media_group_key = media_group_key_by_type[media_type]

        search_payload = search_response
        if isinstance(search_payload, dict):
            if media_group_key not in search_payload and "result" in search_payload and isinstance(search_payload["result"], dict):
                search_payload = search_payload["result"]

            if media_group_key not in search_payload and len(search_payload) == 1:
                only_value = next(iter(search_payload.values()))
                if isinstance(only_value, dict):
                    search_payload = only_value

        items: list[Any] = []
        if isinstance(search_payload, dict):
            group_items = search_payload.get(media_group_key, [])
            if isinstance(group_items, list):
                items = group_items

        first_item = items[0] if items else None
        first_item_uri = first_item.get("uri") if isinstance(first_item, dict) else None
        if not isinstance(first_item_uri, str) or not first_item_uri.strip():
            return {
                "retry": (
                    f"No {media_type} results found for '{search_name}'. "
                    "Please try a different query."
                )
            }
        selected_media_name = first_item.get("name") if isinstance(first_item, dict) else None
        if not isinstance(selected_media_name, str) or not selected_media_name.strip():
            selected_media_name = search_name

        try:
            await self.hass.services.async_call(
                domain="music_assistant",
                service="play_media",
                target={"entity_id": entity_id},
                service_data={"media_id": first_item_uri},
                blocking=True,
            )
        except vol.error.MultipleInvalid as err:
            return {"retry": str(err)}
        except HomeAssistantError as err:
            raise ToolExecutionError(str(err)) from err
        except Exception as err:
            raise ToolExecutionError(str(err)) from err

        return {
            "success": True,
            "entity_id": transform_ha_entity_id_to_auri_entity_id(self.hass, entity_id),
            "selected_media_uri": first_item_uri,
            "selected_media_name": selected_media_name,
            "selected_media_type": media_type,
        }

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
        if not self.media_player_has_feature(entity_id, MediaPlayerEntityFeature.PLAY):
            retry_entities = get_retry_entities(
                self.hass,
                entity_id,
                conversation_id=conversation_id,
            )
            for retry_entity in retry_entities:
                if self.media_player_has_feature(retry_entity, MediaPlayerEntityFeature.PLAY):
                    return {
                        "retry": (
                            f"{transform_ha_entity_id_to_auri_entity_id(self.hass, entity_id)} does not support media play. "
                            f"Please use entity_id '{transform_ha_entity_id_to_auri_entity_id(self.hass, retry_entity)}' instead. "
                            "Just do it and tell the user which device you changed, no confirmation required."
                        )
                    }
            return {
                "retry": (
                    f"{transform_ha_entity_id_to_auri_entity_id(self.hass, entity_id)} does not support media play control, "
                    "based on context, decide whether to retry with a different entity, do nothing, or inform the user that this action is not supported for this device."
                )
            }

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
                if self.media_player_has_feature(retry_entity, MediaPlayerEntityFeature.PLAY):
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
        if not self.media_player_has_feature(entity_id, MediaPlayerEntityFeature.PAUSE):
            retry_entities = get_retry_entities(
                self.hass,
                entity_id,
                conversation_id=conversation_id,
            )
            for retry_entity in retry_entities:
                if self.media_player_has_feature(retry_entity, MediaPlayerEntityFeature.PAUSE):
                    return {
                        "retry": (
                            f"{transform_ha_entity_id_to_auri_entity_id(self.hass, entity_id)} does not support media pause. "
                            f"Please use entity_id '{transform_ha_entity_id_to_auri_entity_id(self.hass, retry_entity)}' instead. "
                            "Just do it and tell the user which device you changed, no confirmation required."
                        )
                    }
            return {
                "retry": (
                    f"{transform_ha_entity_id_to_auri_entity_id(self.hass, entity_id)} does not support media pause control, "
                    "based on context, decide whether to retry with a different entity, do nothing, or inform the user that this action is not supported for this device."
                )
            }

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
                if self.media_player_has_feature(retry_entity, MediaPlayerEntityFeature.PAUSE):
                    return {
                        "retry": (
                            f"Unable to pause playback for '{transform_ha_entity_id_to_auri_entity_id(self.hass, entity_id)}'. "
                            f"Please use entity_id '{transform_ha_entity_id_to_auri_entity_id(self.hass, retry_entity)}' instead. "
                            "Just do it and tell the user which device you changed, no confirmation required."
                        )
                    }
            raise

    async def play_next_track(self, arguments: dict[str, Any]) -> dict[str, Any]:
        conversation_id = arguments.get("_conversation_id")
        entity_id = resolve_entity_id_no_fallback(
            self.hass,
            "media_player",
            transform_auri_entity_id_to_ha_entity_id(arguments.get("entity_id")),
        )
        if not self.media_player_has_feature(entity_id, MediaPlayerEntityFeature.NEXT_TRACK):
            retry_entities = get_retry_entities(
                self.hass,
                entity_id,
                conversation_id=conversation_id,
            )
            for retry_entity in retry_entities:
                if self.media_player_has_feature(retry_entity, MediaPlayerEntityFeature.NEXT_TRACK):
                    return {
                        "retry": (
                            f"{transform_ha_entity_id_to_auri_entity_id(self.hass, entity_id)} does not support next track. "
                            f"Please use entity_id '{transform_ha_entity_id_to_auri_entity_id(self.hass, retry_entity)}' instead. "
                            "Just do it and tell the user which device you changed, no confirmation required."
                        )
                    }
            return {
                "retry": (
                    f"{transform_ha_entity_id_to_auri_entity_id(self.hass, entity_id)} does not support next track control, "
                    "based on context, decide whether to retry with a different entity, do nothing, or inform the user that this action is not supported for this device."
                )
            }

        state = self.hass.states.get(entity_id)
        if state and state.state == "off":
            return {
                "error": (
                    f"Cannot skip track for {transform_ha_entity_id_to_auri_entity_id(self.hass, entity_id)} "
                    "because it's currently turned off"
                )
            }

        try:
            retry = await self._call_media_service("media_next_track", {"entity_id": entity_id})
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
                if self.media_player_has_feature(retry_entity, MediaPlayerEntityFeature.NEXT_TRACK):
                    return {
                        "retry": (
                            f"Unable to skip to next track for '{transform_ha_entity_id_to_auri_entity_id(self.hass, entity_id)}'. "
                            f"Please use entity_id '{transform_ha_entity_id_to_auri_entity_id(self.hass, retry_entity)}' instead. "
                            "Just do it and tell the user which device you changed, no confirmation required."
                        )
                    }
            raise

    async def play_previous_track(self, arguments: dict[str, Any]) -> dict[str, Any]:
        conversation_id = arguments.get("_conversation_id")
        entity_id = resolve_entity_id_no_fallback(
            self.hass,
            "media_player",
            transform_auri_entity_id_to_ha_entity_id(arguments.get("entity_id")),
        )
        if not self.media_player_has_feature(entity_id, MediaPlayerEntityFeature.PREVIOUS_TRACK):
            retry_entities = get_retry_entities(
                self.hass,
                entity_id,
                conversation_id=conversation_id,
            )
            for retry_entity in retry_entities:
                if self.media_player_has_feature(retry_entity, MediaPlayerEntityFeature.PREVIOUS_TRACK):
                    return {
                        "retry": (
                            f"{transform_ha_entity_id_to_auri_entity_id(self.hass, entity_id)} does not support previous track. "
                            f"Please use entity_id '{transform_ha_entity_id_to_auri_entity_id(self.hass, retry_entity)}' instead. "
                            "Just do it and tell the user which device you changed, no confirmation required."
                        )
                    }
            return {
                "retry": (
                    f"{transform_ha_entity_id_to_auri_entity_id(self.hass, entity_id)} does not support previous track control, "
                    "based on context, decide whether to retry with a different entity, do nothing, or inform the user that this action is not supported for this device."
                )
            }

        state = self.hass.states.get(entity_id)
        if state and state.state == "off":
            return {
                "error": (
                    f"Cannot go to previous track for {transform_ha_entity_id_to_auri_entity_id(self.hass, entity_id)} "
                    "because it's currently turned off"
                )
            }

        try:
            retry = await self._call_media_service("media_previous_track", {"entity_id": entity_id})
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
                if self.media_player_has_feature(retry_entity, MediaPlayerEntityFeature.PREVIOUS_TRACK):
                    return {
                        "retry": (
                            f"Unable to go to previous track for '{transform_ha_entity_id_to_auri_entity_id(self.hass, entity_id)}'. "
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
            new_volume = _clamp_percentage((current_volume + volume_step_pct) * 100) / 100
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
