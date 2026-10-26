from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DATA_BOOKINGS, DATA_MEMORY, DOMAIN
from .conversation.custom_services.sticky_note_services import async_restore_sticky_notes
from .entities import AuriGuestStaySensor, AuriMemorySensor, AuriSessionLinkSensor


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the AURI timer sensor platform for the thin frontend integration."""
    runtime = hass.data.setdefault(DOMAIN, {}).setdefault("runtime", {})
    runtime["async_add_entities"] = async_add_entities
    await async_restore_sticky_notes(hass)

    session_link_sensor = AuriSessionLinkSensor(entry)
    runtime["session_link_sensor"] = session_link_sensor
    entities = [session_link_sensor]
    entry_data = hass.data[DOMAIN].get(entry.entry_id, {})
    memory = entry_data.get(DATA_MEMORY)
    if memory is not None:
        entities.append(AuriMemorySensor(entry, memory))
    bookings = entry_data.get(DATA_BOOKINGS)
    if bookings is not None:
        entities.append(AuriGuestStaySensor(entry, bookings))
    async_add_entities(entities)
