"""Sensor for the mode Auri is in now: owner or guest.

By the backend's rule for the account's setting (calendars.py): owner or guest as set, or for an
account set to Automatic, owner only when no stay is under way and someone from the household is
home. The dashboard shows the Guest Calendar and the Guest Shopping List while it says guest, and the
household's otherwise; installers can automate around it too. Its attributes give the account's
setting, both calendars and both shopping lists.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry

from ..calendars import GUEST, HOME, AuriCalendars


class AuriModeSensor(SensorEntity):
    """owner or guest."""

    _attr_has_entity_name = False
    _attr_name = "Auri Mode"
    _attr_icon = "mdi:account-switch"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["owner", "guest"]
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, calendars: AuriCalendars) -> None:
        self._calendars = calendars
        self._attr_unique_id = f"{entry.entry_id}_mode"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._calendars.async_add_listener(self.async_write_ha_state))

    @property
    def native_value(self) -> str:
        return self._calendars.mode()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "account_mode": self._calendars.account_mode or "unknown",
            "home_calendar": self._calendars.entity_for(HOME),
            "guest_calendar": self._calendars.entity_for(GUEST),
            "home_shopping_list": self._calendars.list_for(HOME),
            "guest_shopping_list": self._calendars.list_for(GUEST),
        }
