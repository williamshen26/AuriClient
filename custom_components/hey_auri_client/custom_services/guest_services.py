"""Tool handlers for guest-hosting actions."""
from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from ..const import (
    CONF_BOOKING_ID,
    CONF_CHECK_IN,
    CONF_CHECK_IN_INSTRUCTION,
    CONF_CHECK_OUT,
    CONF_CHECK_OUT_INSTRUCTION,
    CONF_GUEST_COUNT,
    CONF_GUEST_NAME,
    CONF_HOST_CONTACT_INSTRUCTION,
    CONF_HOUSE_RULES,
    CONF_LOCAL_RECOMMENDATIONS,
    CONF_NOTES,
    CONF_PARKING_INSTRUCTION,
    CONF_PROPERTY_KNOWLEDGE,
    CONF_TRASH_DISPOSAL_INSTRUCTION,
    CONF_WIFI_INSTRUCTION,
    DOMAIN,
)


class GuestToolService:
    """Handle guest-hosting-oriented tool calls."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    def _get_guest_options(self) -> dict[str, Any]:
        """Return options from the first configured integration entry."""
        entries = self.hass.config_entries.async_entries(DOMAIN)
        if not entries:
            return {}
        return entries[0].options

    def _get_option(self, key: str, default: Any) -> Any:
        """Return a configured guest option or fallback default."""
        options = self._get_guest_options()
        return options.get(key, default)

    def _get_int_option(self, key: str, default: int) -> int:
        """Return an integer option value with fallback on parse failure."""
        value = self._get_option(key, default)
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    async def get_guest_data(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return {
            "booking_id": str(self._get_option(CONF_BOOKING_ID, "There is no booking ID configured.")),
            "guest_name": str(self._get_option(CONF_GUEST_NAME, "There is no guest name configured.")),
            "check_in": str(self._get_option(CONF_CHECK_IN, "There is no check-in time configured.")),
            "check_out": str(self._get_option(CONF_CHECK_OUT, "There is no check-out time configured.")),
            "guest_count": self._get_int_option(CONF_GUEST_COUNT, 0),
            "notes": str(self._get_option(CONF_NOTES, "There are no guest notes configured.")),
        }

    async def house_layout(self, arguments: dict[str, Any]) -> dict[str, list[str]]:
        from ..helpers import get_house_layout

        return get_house_layout(self.hass)

    async def check_in_instruction(self, arguments: dict[str, Any]) -> str:
        return str(
            self._get_option(
                CONF_CHECK_IN_INSTRUCTION,
                "There is no check-in instruction configured.",
            )
        )

    async def check_out_instruction(self, arguments: dict[str, Any]) -> str:
        return str(
            self._get_option(
                CONF_CHECK_OUT_INSTRUCTION,
                "There is no check-out instruction configured.",
            )
        )

    async def wifi_instruction(self, arguments: dict[str, Any]) -> str:
        return str(
            self._get_option(
                CONF_WIFI_INSTRUCTION,
                "There is no Wi-Fi instruction configured.",
            )
        )

    async def parking_instruction(self, arguments: dict[str, Any]) -> str:
        return str(
            self._get_option(
                CONF_PARKING_INSTRUCTION,
                "There is no parking instruction configured.",
            )
        )

    async def house_rules(self, arguments: dict[str, Any]) -> str:
        return str(
            self._get_option(
                CONF_HOUSE_RULES,
                "There are no house rules configured.",
            )
        )

    async def trash_disposal_instruction(self, arguments: dict[str, Any]) -> str:
        return str(
            self._get_option(
                CONF_TRASH_DISPOSAL_INSTRUCTION,
                "There is no trash disposal instruction configured.",
            )
        )

    async def host_contact_instruction(self, arguments: dict[str, Any]) -> str:
        return str(
            self._get_option(
                CONF_HOST_CONTACT_INSTRUCTION,
                "There is no host contact instruction configured.",
            )
        )

    async def send_host_notification(self, arguments: dict[str, Any]) -> str:
        return "Host notifications are not configured for this property."

    async def property_knowledge(self, arguments: dict[str, Any]) -> str:
        return str(
            self._get_option(
                CONF_PROPERTY_KNOWLEDGE,
                "There is no property knowledge configured.",
            )
        )

    async def local_recommendations(self, arguments: dict[str, Any]) -> str:
        return str(
            self._get_option(
                CONF_LOCAL_RECOMMENDATIONS,
                "There are no local recommendations configured.",
            )
        )
