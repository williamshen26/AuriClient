"""Button platform for the thin frontend integration."""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import API_ENDPOINT, CONF_CLIENT_ID, CONF_SHARED_SECRET, DOMAIN
from .exceptions import SaaSRequestError
from .helpers import get_timeout_seconds
from .saas_client import SaaSClient

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the AURI button platform."""
    async_add_entities([AuriSessionRefreshButton(hass, entry)])


class AuriSessionRefreshButton(ButtonEntity):
    """Mints a new session id and points the session-link sensor at it."""

    _attr_should_poll = False
    _attr_icon = "mdi:qrcode-scan"
    _attr_has_entity_name = True
    _attr_name = "Refresh Session"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._attr_unique_id = f"{entry.entry_id}_refresh_session"

        self._client = SaaSClient(
            hass,
            endpoint=API_ENDPOINT,
            timeout=get_timeout_seconds(entry.options),
            client_id=str(entry.data[CONF_CLIENT_ID]).strip(),
            shared_secret=str(entry.data[CONF_SHARED_SECRET]),
        )

    async def async_press(self) -> None:
        """Request a new session id from the backend and update the QR sensor."""
        try:
            response = await self._client.post_json("/session/refresh", {})
        except SaaSRequestError:
            _LOGGER.exception("Failed to refresh session")
            return

        session_url = str(response.get("session_url") or "").strip()
        if not session_url:
            _LOGGER.error("Session refresh response did not include a session_url: %s", response)
            return

        runtime = self.hass.data.get(DOMAIN, {}).get("runtime", {})
        session_link_sensor = runtime.get("session_link_sensor")
        if session_link_sensor is None:
            _LOGGER.error("Session refreshed but no session-link sensor is registered to display it")
            return

        session_link_sensor.async_update_session_url(session_url)
