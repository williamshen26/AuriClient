"""Sensor for the stay Guest Settings hold: none, upcoming, staying or checked_out.

Follows Guest Settings whether a stay was entered by hand or by the booking
calendar, so installers can automate around it (heating on before check-in,
eco mode after check-out). Its attributes show the booking calendar's state.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.util import dt as dt_util

from ..bookings.schedule import current_stay, stay_state
from ..bookings.sync import BookingSync

STAY_STATES = ["none", "upcoming", "staying", "checked_out"]


class AuriGuestStaySensor(SensorEntity):
    """The held stay, changing state at check-in and check-out."""

    _attr_should_poll = False
    _attr_icon = "mdi:bed"
    _attr_has_entity_name = True
    _attr_name = "Auri Guest Stay"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = STAY_STATES

    def __init__(self, entry: ConfigEntry, bookings: BookingSync) -> None:
        self._entry = entry
        self._bookings = bookings
        self._attr_unique_id = f"{entry.entry_id}_guest_stay"
        self._unsub_change: Callable[[], None] | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._bookings.async_add_listener(self._refresh))
        self.async_on_remove(self._cancel_change)
        self._schedule_change()

    @callback
    def _refresh(self) -> None:
        self._schedule_change()
        self.async_write_ha_state()

    @callback
    def _schedule_change(self) -> None:
        self._cancel_change()
        _, change_at = stay_state(self._entry.options, dt_util.now(), dt_util.get_default_time_zone())
        if change_at is not None:
            self._unsub_change = async_track_point_in_time(self.hass, self._at_change, change_at)

    @callback
    def _at_change(self, _now: datetime) -> None:
        self._unsub_change = None
        self._refresh()

    @callback
    def _cancel_change(self) -> None:
        if self._unsub_change is not None:
            self._unsub_change()
            self._unsub_change = None

    @property
    def native_value(self) -> str:
        return stay_state(self._entry.options, dt_util.now(), dt_util.get_default_time_zone())[0]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        stay = current_stay(self._entry.options, dt_util.get_default_time_zone())
        return {
            "booking_id": (stay.booking_id or None) if stay else None,
            "check_in": stay.check_in.isoformat() if stay and stay.check_in else None,
            "check_out": stay.check_out.isoformat() if stay and stay.check_out else None,
            **self._bookings.status,
        }
