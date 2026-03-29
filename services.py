"""Service setup for the thin frontend integration prototype."""
from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.typing import ConfigType

from .custom_services.automation_services import async_setup_automation_services
from .custom_services.entity_services import async_setup_entity_services
from .custom_services.timer_services import async_setup_timer_services
from .custom_services.user_preferences_services import async_setup_user_preferences_services


async def async_setup_services(hass: HomeAssistant, config: ConfigType) -> None:
    """Set up locally executed services for the thin frontend."""
    await async_setup_automation_services(hass, config)
    await async_setup_entity_services(hass, config)
    await async_setup_timer_services(hass, config)
    await async_setup_user_preferences_services(hass, config)
