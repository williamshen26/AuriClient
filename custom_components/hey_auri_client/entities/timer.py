"""A timer's sensor, for the dashboard's timer bars (sensor.auri_timer_<id>); timers.py keeps it current.

Its state is active or paused, and it changes only when the timer does: the timer bar counts down
by itself, as it does for Home Assistant's own timer entities. The bar takes `remaining` as the
seconds left when the state last changed, so while the timer runs, `remaining` is measured from
then.
"""
from __future__ import annotations

from collections.abc import Callable
import math
from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.util import dt as dt_util

if TYPE_CHECKING:
    from ..timers import TimerRecord


class AuriTimerEntity(SensorEntity):
    """One running or paused timer."""

    _attr_should_poll = False
    _attr_icon = "mdi:timer-outline"
    _attr_has_entity_name = False

    def __init__(
        self, record: TimerRecord, entity_id: str, device_name: Callable[[str | None], str | None]
    ) -> None:
        self._record = record
        self._device_name = device_name
        self.entity_id = entity_id

    @property
    def name(self) -> str:
        # The bar's label: the timer's name when it has one.
        return self._record.name or "Timer"

    @property
    def native_value(self) -> str:
        return "paused" if self._record.paused else "active"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        record = self._record
        now = dt_util.utcnow()
        return {
            "timer_id": record.id,
            "name": record.name,
            "duration": record.duration,
            "remaining": self._bar_remaining(now),
            "finishes_at": record.finishes_at.isoformat() if record.finishes_at else None,
            "device_id": record.device_id,
            "device": self._device_name(record.device_id),
        }

    def _bar_remaining(self, now) -> float:
        """Paused: the seconds left. Running: the seconds left when the state last changed."""
        record = self._record
        if record.finishes_at is None:
            return math.ceil(record.remaining(now))
        current = self.hass.states.get(self.entity_id) if self.hass is not None else None
        since = current.last_changed if current is not None and current.state == "active" else now
        return max(0.0, round((record.finishes_at - since).total_seconds(), 1))


__all__ = ["AuriTimerEntity"]
