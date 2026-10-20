"""Diagnostic sensor showing the state of Auri's long-term memory."""
from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory

from ..memory.runtime import MemoryRuntime, MemoryState


class AuriMemorySensor(SensorEntity):
    """disabled / starting / enabled / unavailable / error, with the last nightly run."""

    _attr_should_poll = False
    _attr_icon = "mdi:brain"
    _attr_has_entity_name = True
    _attr_name = "Auri Memory"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [state.value for state in MemoryState]
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: ConfigEntry, memory: MemoryRuntime) -> None:
        self._memory = memory
        self._attr_unique_id = f"{entry.entry_id}_memory"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._memory.async_add_listener(self.async_write_ha_state))

    @property
    def native_value(self) -> str:
        return self._memory.state.value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self._memory.status_attributes()
