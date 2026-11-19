"""The integration's switches: one per alarm (alarms.py)."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DATA_ALARMS, DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Each alarm's switch, from the alarms kept across restarts; then each new one as it's set."""
    alarms = hass.data.get(DOMAIN, {}).get(entry.entry_id, {}).get(DATA_ALARMS)
    if alarms is not None:
        await alarms.async_start(async_add_entities)
