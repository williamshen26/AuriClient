"""Thin frontend Home Assistant integration prototype."""
from __future__ import annotations

from typing import Any

import homeassistant.components.conversation as ha_conversation
from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType

from .conversation.agent_class import ThinOpenAIAgent
from .cache import (
    dump_media_player_sources,
    set_media_player_sources,
    upsert_media_player_sources,
)
from .const import DATA_AGENT, DOMAIN, ROOT_RUNTIME
from .conversation.services import async_setup_services

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
PLATFORMS = ["stt", "sensor"]
MEDIA_PLAYER_SOURCES_STORAGE_VERSION = 1
MEDIA_PLAYER_SOURCES_STORAGE_KEY = f"{DOMAIN}_media_player_sources"


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the thin frontend integration."""
    await async_setup_services(hass, config)

    runtime = hass.data.setdefault(DOMAIN, {}).setdefault("runtime", dict(ROOT_RUNTIME))

    store: Store[dict[str, list[str]]] = runtime.setdefault(
        "media_player_sources_store",
        Store(
            hass,
            MEDIA_PLAYER_SOURCES_STORAGE_VERSION,
            MEDIA_PLAYER_SOURCES_STORAGE_KEY,
        ),
    )

    if runtime.get("media_player_sources_loaded") is None:
        persisted_sources = await store.async_load()
        if isinstance(persisted_sources, dict):
            sanitized: dict[str, list[str]] = {}
            for entity_id, source_list in persisted_sources.items():
                if not isinstance(entity_id, str) or not isinstance(source_list, list):
                    continue
                sanitized[entity_id] = [str(source) for source in source_list]
            set_media_player_sources(sanitized)
        runtime["media_player_sources_loaded"] = True

    @callback
    def _persist_media_player_sources() -> dict[str, list[str]]:
        return dump_media_player_sources()

    if runtime.get("media_player_sources_listener") is None:
        @callback
        def _cache_media_player_sources(event: Any) -> None:
            new_state = event.data.get("new_state")
            if new_state is None:
                return
            if not new_state.entity_id.startswith("media_player."):
                return
            if new_state.state != "on":
                return

            source_list = new_state.attributes.get("source_list")
            if isinstance(source_list, list):
                changed = upsert_media_player_sources(
                    new_state.entity_id,
                    [str(source) for source in source_list],
                )
                if changed:
                    store.async_delay_save(_persist_media_player_sources, 5)

        runtime["media_player_sources_listener"] = hass.bus.async_listen(
            EVENT_STATE_CHANGED,
            _cache_media_player_sources,
        )

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the thin frontend integration from a config entry."""
    agent = ThinOpenAIAgent(hass, entry)

    data = hass.data.setdefault(DOMAIN, {}).setdefault(entry.entry_id, {})
    data[DATA_AGENT] = agent

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    ha_conversation.async_set_agent(hass, entry, agent)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload the thin frontend integration."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False

    hass.data[DOMAIN].pop(entry.entry_id, None)
    ha_conversation.async_unset_agent(hass, entry)
    return True
