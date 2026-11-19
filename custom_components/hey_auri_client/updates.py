"""Hey Auri Client updates itself on hubs Auri set up: to the version a manager approved, at night.

Once a night, at a random minute in the first half of the hour before memory's night job (03:00
to 03:30 with the default day boundary of 04:00), the client asks Hey Auri which version it
should run (POST /hub/update). The answer depends on the account: hubs Auri set up are in the
"try first" or "everyone" group, and every other account (a DIY customer, a test hub whose code is
copied by hand) gets no version, so nothing happens there.

With a version to run that isn't installed, it installs that version through Home Assistant's
Marketplace (its update entity, update.install with the version: a release tag such as v0.2.7)
and restarts Home Assistant, which the new code needs. Nothing restarts when nothing was
installed. It waits for a later night while a voice satellite is in a conversation or an alarm
rings within 15 minutes, and gives up
on a version after 3 failed nights; Auri staff see that on their Hubs to check list (the security
summary's update_status).
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta
import logging
import random
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, State, callback
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DATA_ALARMS, DOMAIN
from .memory.config import CONF_MEMORY_DAY_BOUNDARY_HOUR, DEFAULT_MEMORY_DAY_BOUNDARY_HOUR

if TYPE_CHECKING:
    from .saas_client import SaaSClient

_LOGGER = logging.getLogger(__name__)

UPDATE_PATH = "/hub/update"
REPOSITORY = "williamshen26/AuriClient"
ENTITY_ID = "update.hey_auri_client"
FAILED_NIGHTS_BEFORE_GIVING_UP = 3
INSTALL_TIMEOUT = timedelta(minutes=15)
# A satellite in these states is in a conversation: a restart now would cut it off.
BUSY_SATELLITE_STATES = frozenset({"listening", "processing", "responding"})
# No restart this long before an alarm: Home Assistant and the satellites might not be back in time.
ALARM_QUIET_BEFORE = timedelta(minutes=15)
_ASK_TIMEOUT_SECONDS = 20
_STORAGE_VERSION = 1
_STORAGE_KEY = f"{DOMAIN}_updates"


def version_key(version: str | None) -> tuple[int, ...]:
    """v0.2.7 → (0, 2, 7): tags have the v, manifest.json doesn't."""
    return tuple(int(part) for part in str(version or "").lstrip("v").split(".") if part.isdigit())


def same_version(first: str | None, second: str | None) -> bool:
    return bool(first) and bool(second) and version_key(first) == version_key(second)


def update_hour(options: dict[str, Any]) -> int:
    """The hour before memory's night job (its day boundary, 4 by default)."""
    try:
        boundary = int(options.get(CONF_MEMORY_DAY_BOUNDARY_HOUR, DEFAULT_MEMORY_DAY_BOUNDARY_HOUR))
    except (TypeError, ValueError):
        boundary = DEFAULT_MEMORY_DAY_BOUNDARY_HOUR
    return (boundary - 1) % 24


def find_update_entity(hass: HomeAssistant) -> State | None:
    """The Marketplace's update entity for Hey Auri Client, if it was installed from there."""
    for state in hass.states.async_all("update"):
        if f"github.com/{REPOSITORY}" in str(state.attributes.get("release_url") or ""):
            return state
    return hass.states.get(ENTITY_ID)


class AutoUpdater:
    """Asks for the approved version once a night, installs it, and restarts Home Assistant."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: SaaSClient) -> None:
        self.hass = hass
        self.entry = entry
        self.client = client
        self._store: Store[dict[str, Any]] = Store(hass, _STORAGE_VERSION, _STORAGE_KEY)
        # What Hey Auri said last ("" until it was asked), and the failed nights for that version.
        self._asked = False
        self._target: str | None = None
        self._failures = 0
        self._unsub: Callable[[], None] | None = None
        self._running = asyncio.Lock()

    async def async_start(self) -> None:
        stored = await self._store.async_load()
        if isinstance(stored, dict):
            self._asked = bool(stored.get("asked"))
            self._target = str(stored.get("target") or "") or None
            self._failures = int(stored.get("failures") or 0)
        self.async_schedule()

    @callback
    def async_schedule(self) -> None:
        """Every night at a random minute of the first half of the hour before memory's night job."""
        self.async_stop()
        self._unsub = async_track_time_change(
            self.hass,
            self._async_nightly,
            hour=update_hour(dict(self.entry.options)),
            minute=random.randint(0, 29),
            second=random.randint(0, 59),
        )

    @callback
    def async_stop(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    async def _async_nightly(self, _now: datetime) -> None:
        await self.async_check()

    def summary(self) -> dict[str, Any]:
        """For the hub's security summary: update_status and update_target."""
        if not self._asked:
            return {"update_status": None, "update_target": None}
        if self._target is None:
            return {"update_status": "off", "update_target": None}
        state = find_update_entity(self.hass)
        if state is None:
            status = "no_entity"
        elif same_version(state.attributes.get("installed_version"), self._target):
            status = "current"
        elif self._failures >= FAILED_NIGHTS_BEFORE_GIVING_UP:
            status = "failed"
        else:
            status = "pending"
        return {"update_status": status, "update_target": self._target}

    async def async_check(self) -> dict[str, Any]:
        """The nightly check, also run by the check_for_update action. Never raises; returns what happened."""
        async with self._running:
            try:
                return await self._async_check()
            except Exception:  # noqa: BLE001 (an update must never break the integration)
                _LOGGER.exception("Couldn't check for a Hey Auri Client update")
                return {"result": "error"}

    async def _async_check(self) -> dict[str, Any]:
        try:
            answer = await self.client.post_json(UPDATE_PATH, {}, timeout_seconds=_ASK_TIMEOUT_SECONDS, measurement_key="update")
        except Exception as err:  # noqa: BLE001 (asked again the next night)
            _LOGGER.debug("Couldn't ask Hey Auri which version to run: %s", err)
            return {"result": "unreachable"}
        target = str(answer.get("version") or "") or None
        if target != self._target:
            self._failures = 0  # a new version gets its own tries
        self._asked, self._target = True, target
        await self._async_save()
        if target is None:
            return {"result": "off"}

        state = find_update_entity(self.hass)
        if state is None:
            _LOGGER.warning(
                "Hey Auri Client should run %s, but it wasn't installed from the Marketplace, so it can't update itself",
                target,
            )
            return {"result": "no_entity", "version": target}
        if same_version(state.attributes.get("installed_version"), target):
            return {"result": "current", "version": target}
        if self._failures >= FAILED_NIGHTS_BEFORE_GIVING_UP:
            return {"result": "failed", "version": target}
        if busy := [s.entity_id for s in self.hass.states.async_all("assist_satellite") if s.state in BUSY_SATELLITE_STATES]:
            _LOGGER.info("Not updating Hey Auri Client now: %s is in a conversation", ", ".join(busy))
            return {"result": "busy", "version": target}
        alarms = self.hass.data.get(DOMAIN, {}).get(self.entry.entry_id, {}).get(DATA_ALARMS)
        if alarms is not None and (soon := alarms.ringing_soon(ALARM_QUIET_BEFORE)) is not None:
            _LOGGER.info("Not updating Hey Auri Client now: an alarm rings at %s", dt_util.as_local(soon).strftime("%H:%M"))
            return {"result": "busy", "version": target}

        _LOGGER.info("Updating Hey Auri Client from %s to %s", state.attributes.get("installed_version"), target)
        try:
            async with asyncio.timeout(INSTALL_TIMEOUT.total_seconds()):
                await self.hass.services.async_call(
                    "update", "install", {"entity_id": state.entity_id, "version": target}, blocking=True
                )
        except Exception as err:  # noqa: BLE001 (counted; tried again the next night)
            return await self._async_failed(target, f"{type(err).__name__}: {err}")

        installed = find_update_entity(self.hass)
        if installed is None or not same_version(installed.attributes.get("installed_version"), target):
            return await self._async_failed(target, "the Marketplace didn't report the new version as installed")

        self._failures = 0
        await self._async_save()
        _LOGGER.info("Hey Auri Client %s is installed: restarting Home Assistant", target)
        await self.hass.services.async_call("homeassistant", "restart", {}, blocking=False)
        return {"result": "installed", "version": target}

    async def _async_failed(self, target: str, why: str) -> dict[str, Any]:
        self._failures += 1
        await self._async_save()
        _LOGGER.warning(
            "Couldn't install Hey Auri Client %s (night %s of %s): %s",
            target, self._failures, FAILED_NIGHTS_BEFORE_GIVING_UP, why,
        )
        return {"result": "install_failed", "version": target}

    async def _async_save(self) -> None:
        await self._store.async_save({"asked": self._asked, "target": self._target, "failures": self._failures})
