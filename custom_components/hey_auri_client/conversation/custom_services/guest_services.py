"""Tool handlers for guest-hosting actions."""
from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from ...bookings.guest_name import can_save_name, clean_name, guest_name, host_guest_name, with_asked
from ...const import (
    CONF_BOOKING_ID,
    CONF_CHECK_IN,
    CONF_CHECK_IN_INSTRUCTION,
    CONF_CHECK_OUT,
    CONF_CHECK_OUT_INSTRUCTION,
    CONF_GUEST_COUNT,
    CONF_GUEST_PHONE_NUMBER,
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
from ...exceptions import ToolExecutionError


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
            "success": True,
            "booking_id": str(self._get_option(CONF_BOOKING_ID, "There is no booking ID configured.")),
            # The host's, else the one the guests gave Auri this stay.
            "guest_name": guest_name(self._get_guest_options()) or "There is no guest name configured.",
            "guest_phone_number": str(
                self._get_option(
                    CONF_GUEST_PHONE_NUMBER,
                    "There is no guest phone number configured.",
                )
            ),
            "check_in": str(self._get_option(CONF_CHECK_IN, "There is no check-in time configured.")),
            "check_out": str(self._get_option(CONF_CHECK_OUT, "There is no check-out time configured.")),
            "guest_count": self._get_int_option(CONF_GUEST_COUNT, 0),
            "notes": str(self._get_option(CONF_NOTES, "There are no guest notes configured.")),
        }

    async def save_guest_name(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Keep the name the guests gave Auri, for this stay only (offered while the host hasn't entered one)."""
        name = clean_name(arguments.get("name"))
        if not any(character.isalpha() for character in name):
            return {"retry": 'name is required: the name the guest gave you, for example "Sarah".'}
        entries = self.hass.config_entries.async_entries(DOMAIN)
        if not entries:
            return {"error": "Guest Settings aren't set up."}
        entry = entries[0]
        if not can_save_name(entry.options, dt_util.now(), dt_util.get_default_time_zone()):
            if host_name := host_guest_name(entry.options):
                return {"error": f"The host already entered the guest's name: {host_name}."}
            return {"error": "No stay is under way, so there is no guest name to save."}
        self.hass.config_entries.async_update_entry(entry, options=with_asked(entry.options, name))
        return {"success": True, "guest_name": name}

    async def house_layout(self, arguments: dict[str, Any]) -> dict[str, Any]:
        from ..helpers import get_house_layout

        return {"success": True, "layout": get_house_layout(self.hass)}

    async def check_in_instruction(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return {
            "success": True,
            "content": str(
                self._get_option(
                    CONF_CHECK_IN_INSTRUCTION,
                    "There is no check-in instruction configured.",
                )
            ),
        }

    async def check_out_instruction(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return {
            "success": True,
            "content": str(
                self._get_option(
                    CONF_CHECK_OUT_INSTRUCTION,
                    "There is no check-out instruction configured.",
                )
            ),
        }

    async def wifi_instruction(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return {
            "success": True,
            "content": str(
                self._get_option(
                    CONF_WIFI_INSTRUCTION,
                    "There is no Wi-Fi instruction configured.",
                )
            ),
        }

    async def parking_instruction(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return {
            "success": True,
            "content": str(
                self._get_option(
                    CONF_PARKING_INSTRUCTION,
                    "There is no parking instruction configured.",
                )
            ),
        }

    async def house_rules(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return {
            "success": True,
            "content": str(
                self._get_option(
                    CONF_HOUSE_RULES,
                    "There are no house rules configured.",
                )
            ),
        }

    async def trash_disposal_instruction(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return {
            "success": True,
            "content": str(
                self._get_option(
                    CONF_TRASH_DISPOSAL_INSTRUCTION,
                    "There is no trash disposal instruction configured.",
                )
            ),
        }

    async def host_contact_instruction(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return {
            "success": True,
            "content": str(
                self._get_option(
                    CONF_HOST_CONTACT_INSTRUCTION,
                    "There is no host contact instruction configured.",
                )
            ),
        }

    async def send_host_notification(self, arguments: dict[str, Any]) -> dict[str, Any]:
        message = str(arguments.get("message") or "").strip()
        if not message:
            return {"retry": "message is required for send_host_notification"}

        if not self.hass.services.has_service("notify", "notify"):
            return {"error": "Host notifications are not configured for this property."}

        try:
            await self.hass.services.async_call(
                domain="notify",
                service="notify",
                service_data={
                    "title": "User Request",
                    "message": message,
                },
                blocking=True,
            )
            await self.hass.services.async_call(
                domain="notify",
                service="persistent_notification",
                service_data={
                    "title": "User Request",
                    "message": message,
                },
                blocking=True,
            )
        except Exception as err:
            raise ToolExecutionError(str(err)) from err

        return {"success": True}

    async def property_knowledge(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return {
            "success": True,
            "content": str(
                self._get_option(
                    CONF_PROPERTY_KNOWLEDGE,
                    "There is no property knowledge configured.",
                )
            ),
        }

    async def local_recommendations(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return {
            "success": True,
            "content": str(
                self._get_option(
                    CONF_LOCAL_RECOMMENDATIONS,
                    "There are no local recommendations configured.",
                )
            ),
        }
