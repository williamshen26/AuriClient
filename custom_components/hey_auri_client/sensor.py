from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .custom_services.sticky_note_services import async_restore_sticky_notes


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the AURI timer sensor platform for the thin frontend integration."""
    del entry
    hass.data.setdefault(DOMAIN, {}).setdefault("runtime", {})["async_add_entities"] = async_add_entities
    await async_restore_sticky_notes(hass)
