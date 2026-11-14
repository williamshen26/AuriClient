"""Singleton sensor exposing the current session's public URL for QR display.

Anyone who has the session page's address sees the links Auri posts there (rides, places), so each
stay gets its own: a new session starts when a stay begins (check-in) and when it ends (check-out),
and a guest who kept the old address no longer sees what comes after. Refresh Session starts one
at any time.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
import logging

import aiohttp

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

from ..bookings.schedule import current_stay, stay_state
from ..bookings.sync import BookingSync
from ..exceptions import SaaSRequestError
from ..saas_client import SaaSClient

_LOGGER = logging.getLogger(__name__)
_DEFAULT_SESSION_URL = "https://hey-auri.com"
# The stay states that start a new session: check-in and check-out.
_NEW_SESSION_STATES = ("staying", "checked_out")
# Which stay the current session was started for, kept as an attribute so it survives restarts.
_STAY_ATTRIBUTE = "stay"


class AuriSessionLinkSensor(SensorEntity, RestoreEntity):
    """Exposes the current session URL so a `qr-code` Lovelace card can render it."""

    _attr_should_poll = False
    _attr_icon = "mdi:qrcode"
    _attr_has_entity_name = True
    _attr_name = "Session Link"

    def __init__(self, entry: ConfigEntry, client: SaaSClient, bookings: BookingSync | None = None) -> None:
        self.entry = entry
        self._client = client
        self._bookings = bookings
        self._attr_unique_id = f"{entry.entry_id}_session_link"
        self._session_url: str | None = None
        self._stay_session: str | None = None
        self._unsub_change: Callable[[], None] | None = None
        self._lock = asyncio.Lock()

    async def async_added_to_hass(self) -> None:
        """Restore the last known session URL after a restart, or fetch the account's current one."""
        await super().async_added_to_hass()

        last_state = await self.async_get_last_state()
        if last_state is not None and last_state.state != _DEFAULT_SESSION_URL:
            self._session_url = last_state.state
        if last_state is not None:
            self._stay_session = last_state.attributes.get(_STAY_ATTRIBUTE) or None
        if self._bookings is not None:
            # Guest Settings changed (by hand or by the booking calendar): another stay may have begun.
            self.async_on_remove(self._bookings.async_add_listener(self._on_stay_settings))
            self.async_on_remove(self._cancel_change)
            self._schedule_change()
        self.entry.async_create_background_task(self.hass, self._async_start(), "hey_auri_client session page")

    async def _async_start(self) -> None:
        # A stay that began or ended while Home Assistant was off gets its new session now.
        if await self._async_new_session_for_stay():
            return
        if self._session_url is None:
            # A new install (or one that never had a session): show the session page without
            # waiting for someone to press Refresh Session.
            await self._async_fetch_current_session()

    async def _async_fetch_current_session(self) -> None:
        try:
            session_url = await self._client.request_session_url(if_missing=True)
        except (SaaSRequestError, aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.warning("Couldn't get the session page (%s); press Refresh Session to try again", err)
            return
        if self._session_url is None:  # a Refresh Session pressed meanwhile wins
            self.async_update_session_url(session_url)

    def _stay_now(self) -> str | None:
        """The stay and its state when it calls for its own session, like "HMA1B2C3D4:staying"; else None."""
        options = self.entry.options
        tz = dt_util.get_default_time_zone()
        state, _ = stay_state(options, dt_util.now(), tz)
        if state not in _NEW_SESSION_STATES:
            return None
        stay = current_stay(options, tz)
        if stay is None:
            return None
        # A stay typed in by hand may have no booking id; its times tell it apart.
        stay_id = stay.booking_id or "-".join(
            moment.isoformat() for moment in (stay.check_in, stay.check_out) if moment is not None
        )
        return f"{stay_id}:{state}"

    async def _async_new_session_for_stay(self) -> bool:
        """Start a new session when a stay has begun or ended since the last one; True when it did."""
        async with self._lock:
            stay = self._stay_now()
            if stay is None or stay == self._stay_session:
                return False
            try:
                session_url = await self._client.request_session_url()
            except (SaaSRequestError, aiohttp.ClientError, TimeoutError) as err:
                # Tried again when Guest Settings next change, at the next check-in or check-out,
                # or after a restart.
                _LOGGER.warning("Couldn't start a new session page for the stay (%s)", err)
                return False
            self._stay_session = stay
            _LOGGER.info("New session page for the stay (%s)", stay)
            self.async_update_session_url(session_url)
            return True

    @callback
    def _on_stay_settings(self) -> None:
        self._schedule_change()
        self.entry.async_create_background_task(
            self.hass, self._async_new_session_for_stay(), "hey_auri_client session page for the stay"
        )

    @callback
    def _schedule_change(self) -> None:
        """Wake at the stay's next check-in or check-out."""
        self._cancel_change()
        _, change_at = stay_state(self.entry.options, dt_util.now(), dt_util.get_default_time_zone())
        if change_at is not None:
            self._unsub_change = async_track_point_in_time(self.hass, self._at_change, change_at)

    @callback
    def _at_change(self, _now: datetime) -> None:
        self._unsub_change = None
        self._on_stay_settings()

    @callback
    def _cancel_change(self) -> None:
        if self._unsub_change is not None:
            self._unsub_change()
            self._unsub_change = None

    @property
    def native_value(self) -> str:
        """Return the current session URL, defaulting to the home page until a session exists."""
        return self._session_url or _DEFAULT_SESSION_URL

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        return {_STAY_ATTRIBUTE: self._stay_session}

    def async_update_session_url(self, url: str) -> None:
        """Update the session URL and push the new state to HA."""
        self._session_url = url
        self.async_write_ha_state()
