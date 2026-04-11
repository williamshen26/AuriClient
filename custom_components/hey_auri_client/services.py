"""Service setup for the thin frontend integration prototype."""
from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.typing import ConfigType


async def async_setup_services(hass: HomeAssistant, config: ConfigType) -> None:
    """Service registration is intentionally disabled for the thin frontend."""
    del hass, config
