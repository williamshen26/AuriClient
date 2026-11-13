"""Singleton sensor exposing the current session's public URL for QR display."""

from __future__ import annotations

import logging

import aiohttp

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.restore_state import RestoreEntity

from ..exceptions import SaaSRequestError
from ..saas_client import SaaSClient

_LOGGER = logging.getLogger(__name__)
_DEFAULT_SESSION_URL = "https://hey-auri.com"


class AuriSessionLinkSensor(SensorEntity, RestoreEntity):
    """Exposes the current session URL so a `qr-code` Lovelace card can render it."""

    _attr_should_poll = False
    _attr_icon = "mdi:qrcode"
    _attr_has_entity_name = True
    _attr_name = "Session Link"

    def __init__(self, entry: ConfigEntry, client: SaaSClient) -> None:
        self.entry = entry
        self._client = client
        self._attr_unique_id = f"{entry.entry_id}_session_link"
        self._session_url: str | None = None

    async def async_added_to_hass(self) -> None:
        """Restore the last known session URL after a restart, or fetch the account's current one."""
        await super().async_added_to_hass()

        last_state = await self.async_get_last_state()
        if last_state is not None and last_state.state != _DEFAULT_SESSION_URL:
            self._session_url = last_state.state
        if self._session_url is None:
            # A new install (or one that never had a session): show the session page without
            # waiting for someone to press Refresh Session.
            self.entry.async_create_background_task(
                self.hass, self._async_fetch_current_session(), "hey_auri_client current session"
            )

    async def _async_fetch_current_session(self) -> None:
        try:
            session_url = await self._client.request_session_url(if_missing=True)
        except (SaaSRequestError, aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.warning("Couldn't get the session page (%s); press Refresh Session to try again", err)
            return
        if self._session_url is None:  # a Refresh Session pressed meanwhile wins
            self.async_update_session_url(session_url)

    @property
    def native_value(self) -> str:
        """Return the current session URL, defaulting to the home page until a session exists."""
        return self._session_url or _DEFAULT_SESSION_URL

    def async_update_session_url(self, url: str) -> None:
        """Update the session URL and push the new state to HA."""
        self._session_url = url
        self.async_write_ha_state()
