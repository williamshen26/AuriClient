"""Service setup for the thin frontend integration prototype."""
from __future__ import annotations

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from ..const import (
    ATTR_DURATION,
    ATTR_ENTITY_ID,
    ATTR_MAC,
    ATTR_SATELLITE_SPEAKER,
    ATTR_MARKDOWN,
    ATTR_NOTE_ID,
    ATTR_TITLE,
    DOMAIN,
    SERVICE_ASSIGN_MAC,
    SERVICE_CREATE_AURI_STICKY_NOTE,
    SERVICE_DELETE_AURI_STICKY_NOTE,
    SERVICE_GET_AURI_STICKY_NOTES,
    SERVICE_GET_AURI_TIMERS,
    SERVICE_START_AURI_TIMER,
)
from .custom_services.timer_services import (
    get_auri_timers_native,
    start_auri_timer_native,
)
from .custom_services.sticky_note_services import (
    MAX_STICKY_NOTE_CONTENT_LENGTH,
    create_auri_sticky_note_native,
    delete_auri_sticky_note_native,
    get_auri_sticky_notes_native,
)
from .custom_services.media_player_mac_services import assign_mac_native


CREATE_STICKY_NOTE_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_NOTE_ID): vol.All(str, vol.Length(min=1, max=128)),
        vol.Optional(ATTR_TITLE): vol.All(str, vol.Length(min=1, max=255)),
        vol.Required(ATTR_MARKDOWN): vol.All(
            str,
            vol.Length(min=1, max=MAX_STICKY_NOTE_CONTENT_LENGTH),
        ),
    }
)

DELETE_STICKY_NOTE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_NOTE_ID): vol.All(str, vol.Length(min=1, max=128)),
    }
)

START_TIMER_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DURATION): vol.Any(int, float, str),
        vol.Optional(ATTR_SATELLITE_SPEAKER): vol.All(str, vol.Length(min=1, max=255)),
    }
)

GET_TIMERS_SCHEMA = vol.Schema({})
GET_STICKY_NOTES_SCHEMA = vol.Schema({})

ASSIGN_MAC_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ENTITY_ID): cv.entity_id,
        vol.Required(ATTR_MAC): vol.All(cv.ensure_list, [cv.string]),
    }
)


async def async_setup_services(hass: HomeAssistant, config: ConfigType) -> None:
    """Set up integration-level HA services."""
    del config

    if hass.data.setdefault(DOMAIN, {}).get("services_registered"):
        return

    async def _handle_create_sticky_note(call: ServiceCall) -> dict[str, object]:
        try:
            return await create_auri_sticky_note_native(
                hass,
                markdown=call.data[ATTR_MARKDOWN],
                title=call.data.get(ATTR_TITLE),
                note_id=call.data.get(ATTR_NOTE_ID),
            )
        except (HomeAssistantError, ValueError) as err:
            raise HomeAssistantError(str(err)) from err

    async def _handle_start_timer(call: ServiceCall) -> dict[str, object]:
        try:
            return await start_auri_timer_native(
                hass,
                duration=call.data[ATTR_DURATION],
                satellite_speaker=call.data.get(ATTR_SATELLITE_SPEAKER),
            )
        except (HomeAssistantError, ValueError, vol.error.MultipleInvalid) as err:
            raise HomeAssistantError(str(err)) from err

    async def _handle_get_timers(call: ServiceCall) -> dict[str, object]:
        del call
        try:
            return await get_auri_timers_native(hass)
        except HomeAssistantError as err:
            raise HomeAssistantError(str(err)) from err

    async def _handle_get_sticky_notes(call: ServiceCall) -> dict[str, object]:
        del call
        try:
            return await get_auri_sticky_notes_native(hass)
        except HomeAssistantError as err:
            raise HomeAssistantError(str(err)) from err

    async def _handle_delete_sticky_note(call: ServiceCall) -> dict[str, object]:
        try:
            return await delete_auri_sticky_note_native(
                hass,
                note_id=call.data[ATTR_NOTE_ID],
            )
        except (HomeAssistantError, ValueError) as err:
            raise HomeAssistantError(str(err)) from err

    async def _handle_assign_mac(call: ServiceCall) -> dict[str, object]:
        try:
            return await assign_mac_native(
                hass,
                entity_id=call.data[ATTR_ENTITY_ID],
                macs=call.data[ATTR_MAC],
            )
        except (HomeAssistantError, ValueError) as err:
            raise HomeAssistantError(str(err)) from err

    hass.services.async_register(
        DOMAIN,
        SERVICE_CREATE_AURI_STICKY_NOTE,
        _handle_create_sticky_note,
        schema=CREATE_STICKY_NOTE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_DELETE_AURI_STICKY_NOTE,
        _handle_delete_sticky_note,
        schema=DELETE_STICKY_NOTE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_START_AURI_TIMER,
        _handle_start_timer,
        schema=START_TIMER_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_AURI_TIMERS,
        _handle_get_timers,
        schema=GET_TIMERS_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_AURI_STICKY_NOTES,
        _handle_get_sticky_notes,
        schema=GET_STICKY_NOTES_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_ASSIGN_MAC,
        _handle_assign_mac,
        schema=ASSIGN_MAC_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )

    hass.data[DOMAIN]["services_registered"] = True
