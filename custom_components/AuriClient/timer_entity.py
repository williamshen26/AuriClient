"""Standalone timer entity for the thin frontend integration."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import logging
import uuid

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_point_in_utc_time

from .const import (
	ATTR_DURATION,
	ATTR_REMAINING,
	ATTR_SATELLITE_SPEAKER,
	ATTR_TIMER_ID,
	DOMAIN,
	EVENT_AURI_TIMER_FINISHED,
)

_LOGGER = logging.getLogger(__name__)


class AuriTimerEntity(SensorEntity):
	"""Dynamic AURI timer entity."""

	_attr_should_poll = False
	_attr_icon = "mdi:timer-outline"
	_attr_has_entity_name = True

	def __init__(
		self,
		hass: HomeAssistant,
		timer_id: str,
		duration_seconds: int,
		satellite_speaker: str | None = None,
	) -> None:
		self.hass = hass
		self.timer_id = timer_id
		self._duration_seconds = duration_seconds
		self._remaining_seconds = duration_seconds
		self._satellite_speaker = satellite_speaker

		self._started_at: datetime | None = None
		self._ends_at: datetime | None = None
		self._tick_unsub = None
		self._running = False

		self._attr_name = f"AURI Timer {timer_id}"

	@property
	def native_value(self) -> int:
		"""Return remaining time in seconds."""
		return self._remaining_seconds

	@property
	def extra_state_attributes(self) -> dict:
		"""Return extra attributes."""
		return {
			ATTR_TIMER_ID: self.timer_id,
			ATTR_DURATION: self._duration_seconds,
			ATTR_REMAINING: self._remaining_seconds,
			ATTR_SATELLITE_SPEAKER: self._satellite_speaker,
			"started_at": self._started_at.isoformat() if self._started_at else None,
			"ends_at": self._ends_at.isoformat() if self._ends_at else None,
			"running": self._running,
		}

	async def async_start(self) -> None:
		"""Start the timer."""
		now = datetime.now(UTC)
		self._started_at = now
		self._ends_at = now + timedelta(seconds=self._duration_seconds)
		self._remaining_seconds = self._duration_seconds
		self._running = True
		self._schedule_next_tick()
		self.async_write_ha_state()

	def get_remaining_seconds(self) -> int:
		"""Get current remaining seconds."""
		if not self._running or self._ends_at is None:
			return self._remaining_seconds

		now = datetime.now(UTC)
		return max(0, int((self._ends_at - now).total_seconds()))

	@callback
	def _schedule_next_tick(self) -> None:
		"""Schedule next tick."""
		if not self._running or self._ends_at is None:
			return

		if self._tick_unsub is not None:
			self._tick_unsub()
			self._tick_unsub = None

		next_tick = datetime.now(UTC) + timedelta(seconds=1)
		self._tick_unsub = async_track_point_in_utc_time(
			self.hass,
			self._async_tick,
			next_tick,
		)

	@callback
	def _async_tick(self, _now: datetime) -> None:
		"""Handle timer tick."""
		if not self._running:
			return

		self._remaining_seconds = self.get_remaining_seconds()

		if self._remaining_seconds <= 0:
			self._remaining_seconds = 0
			self._running = False

			if self._tick_unsub is not None:
				self._tick_unsub()
				self._tick_unsub = None

			self.async_write_ha_state()

			_LOGGER.info(
				"AURI timer %s finished. Satellite speaker: %s",
				self.timer_id,
				self._satellite_speaker,
			)

			self.hass.bus.async_fire(
				EVENT_AURI_TIMER_FINISHED,
				{
					ATTR_TIMER_ID: self.timer_id,
					"entity_id": self.entity_id,
					ATTR_DURATION: self._duration_seconds,
					ATTR_SATELLITE_SPEAKER: self._satellite_speaker,
				},
			)

			self.hass.async_create_task(self._async_remove_self())
			return

		self.async_write_ha_state()
		self._schedule_next_tick()

	async def _async_remove_self(self) -> None:
		"""Remove timer from runtime store and HA."""
		runtime = self.hass.data[DOMAIN]["runtime"]
		runtime["timers"].pop(self.timer_id, None)
		await self.async_remove()

	async def async_will_remove_from_hass(self) -> None:
		"""Clean up before removal."""
		if self._tick_unsub is not None:
			self._tick_unsub()
			self._tick_unsub = None


def generate_timer_id() -> str:
	"""Generate a short timer id."""
	return uuid.uuid4().hex[:8]

__all__ = ["AuriTimerEntity", "generate_timer_id"]
