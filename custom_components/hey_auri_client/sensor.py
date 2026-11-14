from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import API_ENDPOINT, CONF_CLIENT_ID, CONF_SHARED_SECRET, DATA_BOOKINGS, DATA_MEMORY, DOMAIN
from .conversation.custom_services.sticky_note_services import async_restore_sticky_notes
from .entities import AuriGuestStaySensor, AuriMemorySensor, AuriSessionLinkSensor
from .helpers import get_timeout_seconds
from .saas_client import SaaSClient


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the AURI timer sensor platform for the thin frontend integration."""
    runtime = hass.data.setdefault(DOMAIN, {}).setdefault("runtime", {})
    runtime["async_add_entities"] = async_add_entities
    await async_restore_sticky_notes(hass)

    entry_data = hass.data[DOMAIN].get(entry.entry_id, {})
    session_link_sensor = AuriSessionLinkSensor(
        entry,
        SaaSClient(
            hass,
            endpoint=API_ENDPOINT,
            timeout=get_timeout_seconds(entry.options),
            client_id=str(entry.data[CONF_CLIENT_ID]).strip(),
            shared_secret=str(entry.data[CONF_SHARED_SECRET]),
        ),
        # A new session page at each check-in and check-out.
        entry_data.get(DATA_BOOKINGS),
    )
    runtime["session_link_sensor"] = session_link_sensor
    entities = [session_link_sensor]
    memory = entry_data.get(DATA_MEMORY)
    if memory is not None:
        entities.append(AuriMemorySensor(entry, memory))
    bookings = entry_data.get(DATA_BOOKINGS)
    if bookings is not None:
        entities.append(AuriGuestStaySensor(entry, bookings))
    async_add_entities(entities)
