"""An alarm's switch (switch.auri_alarm_<id>): on while the alarm is set; alarms.py keeps it current.

Turning it off keeps the alarm without ringing; turning it on sets it again (a one-time alarm whose
day has passed rings the next time it's that time). Its attributes are the alarm's time, days,
room and next ring, and a one-line summary for the Home dashboard.

It isn't exposed to voice assistants: Auri's alarm tools handle alarms, and "turn off all the
switches" mustn't silence them.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
from homeassistant.components.switch import SwitchEntity

if TYPE_CHECKING:
    from ..alarms import AlarmRecord, AuriAlarms

_LOGGER = logging.getLogger(__name__)
# Home Assistant's own conversation agent and Auri both read the "conversation" exposure.
_ASSISTANT = "conversation"


class AuriAlarmSwitch(SwitchEntity):
    """One alarm, on or off."""

    _attr_should_poll = False
    _attr_has_entity_name = False

    def __init__(self, alarms: AuriAlarms, record: AlarmRecord) -> None:
        self._alarms = alarms
        self._record = record
        self._attr_unique_id = f"alarm_{record.id}"
        self.entity_id = alarms.entity_id(record)

    @property
    def name(self) -> str:
        return self._record.name or f"Alarm {self._record.time.strftime('%H:%M')}"

    @property
    def icon(self) -> str:
        return "mdi:alarm" if self._record.enabled else "mdi:alarm-off"

    @property
    def is_on(self) -> bool:
        return self._record.enabled

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self._alarms.attributes(self._record)

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._alarms.change(self._record, enabled=True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._alarms.change(self._record, enabled=False)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        try:
            async_expose_entity(self.hass, _ASSISTANT, self.entity_id, False)
        except Exception:  # noqa: BLE001 (exposure settings not loaded: nothing to hide it from)
            _LOGGER.debug("Couldn't hide %s from voice assistants", self.entity_id, exc_info=True)


__all__ = ["AuriAlarmSwitch"]
