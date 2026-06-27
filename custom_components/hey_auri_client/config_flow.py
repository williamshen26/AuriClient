"""Config flow for the thin frontend integration prototype."""
from __future__ import annotations

import logging
import re
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .const import (
    API_ENDPOINT,
    CONF_BOOKING_ID,
    CONF_CHECK_IN,
    CONF_CHECK_IN_INSTRUCTION,
    CONF_CHECK_OUT,
    CONF_CHECK_OUT_INSTRUCTION,
    CONF_CLIENT_ID,
    CONF_GUEST_COUNT,
    CONF_GUEST_NAME,
    CONF_GUEST_PHONE_NUMBER,
    CONF_HOST_CONTACT_INSTRUCTION,
    CONF_HOUSE_RULES,
    CONF_LOCAL_RECOMMENDATIONS,
    CONF_METRICS_DATABASE,
    CONF_METRICS_ENABLED,
    CONF_METRICS_HOST,
    CONF_METRICS_MEASUREMENT,
    CONF_METRICS_PASSWORD,
    CONF_METRICS_PORT,
    CONF_METRICS_SSL,
    CONF_METRICS_USERNAME,
    CONF_NOTES,
    CONF_PARKING_INSTRUCTION,
    CONF_PROPERTY_KNOWLEDGE,
    CONF_REQUEST_TIMEOUT,
    CONF_STT_LANGUAGE,
    CONF_SHARED_SECRET,
    CONF_TRASH_DISPOSAL_INSTRUCTION,
    CONF_WIFI_INSTRUCTION,
    DEFAULT_METRICS_MEASUREMENT,
    DEFAULT_METRICS_PORT,
    DEFAULT_NAME,
    DEFAULT_REQUEST_TIMEOUT,
    DEFAULT_STT_LANGUAGE,
    DEFAULT_TTS_STREAM_ENDPOINT,
    DOMAIN,
    CONF_TTS_STREAM_ENDPOINT,
)

_LOGGER = logging.getLogger(__name__)
_PHONE_NANP_10_PATTERN = re.compile(r"[2-9]\d{9}")

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): str,
        vol.Required(CONF_CLIENT_ID): str,
        vol.Required(CONF_SHARED_SECRET): str,
    }
)


async def validate_input(data: dict[str, Any]) -> None:
    """Validate that the backend is reachable and credentials are present."""
    client_id = str(data[CONF_CLIENT_ID]).strip()
    shared_secret = str(data[CONF_SHARED_SECRET]).strip()

    if not client_id:
        raise ValueError("Client ID is required")
    if not shared_secret:
        raise ValueError("Shared secret is required")

    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(f"{API_ENDPOINT}/health") as response:
            if response.status >= 400:
                raise ConnectionError(f"Health check failed with status {response.status}")


def _normalize_guest_phone_number(phone_number: Any) -> str | None:
    """Normalize guest phone number to +1 E.164 for reliable SMS routing.

    Returns an empty string when no phone number is provided, so this field can stay optional.
    Returns None when the phone number format is invalid.
    """
    raw = str(phone_number or "").strip()
    if not raw:
        return ""

    if raw.startswith("+"):
        digits = re.sub(r"\D", "", raw[1:])
        if len(digits) == 11 and digits.startswith("1") and _PHONE_NANP_10_PATTERN.fullmatch(digits[1:]):
            normalized = f"+{digits}"
        else:
            return None
    elif raw.startswith("00"):
        digits = re.sub(r"\D", "", raw[2:])
        if len(digits) == 11 and digits.startswith("1") and _PHONE_NANP_10_PATTERN.fullmatch(digits[1:]):
            normalized = f"+{digits}"
        else:
            return None
    else:
        digits = re.sub(r"\D", "", raw)
        if len(digits) == 10 and _PHONE_NANP_10_PATTERN.fullmatch(digits):
            normalized = f"+1{digits}"
        elif len(digits) == 11 and digits.startswith("1") and _PHONE_NANP_10_PATTERN.fullmatch(digits[1:]):
            normalized = f"+{digits}"
        else:
            return None

    return normalized


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for the thin frontend integration."""

    VERSION = 1

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Return the options flow for this handler."""
        return OptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial setup step."""
        if user_input is None:
            return self.async_show_form(step_id="user", data_schema=STEP_USER_DATA_SCHEMA)

        errors: dict[str, str] = {}

        try:
            await validate_input(user_input)
        except ConnectionError:
            errors["base"] = "cannot_connect"
        except Exception:
            _LOGGER.exception("Unexpected exception during thin integration setup")
            errors["base"] = "unknown"
        else:
            return self.async_create_entry(
                title=user_input.get(CONF_NAME, DEFAULT_NAME),
                data={
                    CONF_CLIENT_ID: user_input[CONF_CLIENT_ID],
                    CONF_SHARED_SECRET: user_input[CONF_SHARED_SECRET],
                },
            )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )


class OptionsFlow(config_entries.OptionsFlow):
    """Handle options for the thin frontend integration."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Show options group menu."""
        return self.async_show_menu(
            step_id="init",
            menu_options=["general", "guest", "metrics"],
        )

    async def async_step_general(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage general options."""
        if user_input is not None:
            data = dict(self.config_entry.options)
            data.update(user_input)
            return self.async_create_entry(title="", data=data)

        options = self.config_entry.options

        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_REQUEST_TIMEOUT,
                    default=int(options.get(CONF_REQUEST_TIMEOUT, DEFAULT_REQUEST_TIMEOUT)),
                ): vol.All(vol.Coerce(int), vol.Range(min=1, max=300)),
                vol.Optional(
                    CONF_STT_LANGUAGE,
                    default=str(options.get(CONF_STT_LANGUAGE, DEFAULT_STT_LANGUAGE)),
                ): str,
                vol.Optional(
                    CONF_TTS_STREAM_ENDPOINT,
                    default=str(options.get(CONF_TTS_STREAM_ENDPOINT, DEFAULT_TTS_STREAM_ENDPOINT)),
                ): str,
            }
        )

        return self.async_show_form(step_id="general", data_schema=schema)

    async def async_step_guest(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage guest options."""
        errors: dict[str, str] = {}

        if user_input is not None:
            normalized_phone_number = _normalize_guest_phone_number(
                user_input.get(CONF_GUEST_PHONE_NUMBER, "")
            )
            if normalized_phone_number is None:
                errors[CONF_GUEST_PHONE_NUMBER] = "invalid_guest_phone_number"
            else:
                user_input = dict(user_input)
                user_input[CONF_GUEST_PHONE_NUMBER] = normalized_phone_number

            if not errors:
                data = dict(self.config_entry.options)
                data.update(user_input)
                return self.async_create_entry(title="", data=data)

        options = user_input or self.config_entry.options

        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_BOOKING_ID,
                    default=str(options.get(CONF_BOOKING_ID, "")),
                ): str,
                vol.Optional(
                    CONF_GUEST_NAME,
                    default=str(options.get(CONF_GUEST_NAME, "")),
                ): str,
                vol.Optional(
                    CONF_GUEST_PHONE_NUMBER,
                    default=str(options.get(CONF_GUEST_PHONE_NUMBER, "")),
                ): str,
                vol.Optional(
                    CONF_CHECK_IN,
                    default=str(options.get(CONF_CHECK_IN, "")),
                ): selector.DateTimeSelector(),
                vol.Optional(
                    CONF_CHECK_OUT,
                    default=str(options.get(CONF_CHECK_OUT, "")),
                ): selector.DateTimeSelector(),
                vol.Optional(
                    CONF_GUEST_COUNT,
                    default=int(options.get(CONF_GUEST_COUNT, 4)),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=1,
                        max=20,
                        step=1,
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                vol.Optional(
                    CONF_NOTES,
                    default=str(options.get(CONF_NOTES, "")),
                ): selector.TemplateSelector(),
                vol.Optional(
                    CONF_CHECK_IN_INSTRUCTION,
                    default=str(
                        options.get(
                            CONF_CHECK_IN_INSTRUCTION,
                            "",
                        )
                    ),
                ): selector.TemplateSelector(),
                vol.Optional(
                    CONF_CHECK_OUT_INSTRUCTION,
                    default=str(
                        options.get(
                            CONF_CHECK_OUT_INSTRUCTION,
                            "",
                        )
                    ),
                ): selector.TemplateSelector(),
                vol.Optional(
                    CONF_WIFI_INSTRUCTION,
                    default=str(
                        options.get(
                            CONF_WIFI_INSTRUCTION,
                            "",
                        )
                    ),
                ): selector.TemplateSelector(),
                vol.Optional(
                    CONF_PARKING_INSTRUCTION,
                    default=str(
                        options.get(
                            CONF_PARKING_INSTRUCTION,
                            "",
                        )
                    ),
                ): selector.TemplateSelector(),
                vol.Optional(
                    CONF_HOUSE_RULES,
                    default=str(
                        options.get(
                            CONF_HOUSE_RULES,
                            "",
                        )
                    ),
                ): selector.TemplateSelector(),
                vol.Optional(
                    CONF_TRASH_DISPOSAL_INSTRUCTION,
                    default=str(
                        options.get(
                            CONF_TRASH_DISPOSAL_INSTRUCTION,
                            "",
                        )
                    ),
                ): selector.TemplateSelector(),
                vol.Optional(
                    CONF_HOST_CONTACT_INSTRUCTION,
                    default=str(
                        options.get(
                            CONF_HOST_CONTACT_INSTRUCTION,
                            "",
                        )
                    ),
                ): selector.TemplateSelector(),
                vol.Optional(
                    CONF_PROPERTY_KNOWLEDGE,
                    default=str(
                        options.get(
                            CONF_PROPERTY_KNOWLEDGE,
                            "",
                        )
                    ),
                ): selector.TemplateSelector(),
                vol.Optional(
                    CONF_LOCAL_RECOMMENDATIONS,
                    default=str(
                        options.get(
                            CONF_LOCAL_RECOMMENDATIONS,
                            "",
                        )
                    ),
                ): selector.TemplateSelector(),
            }
        )

        return self.async_show_form(step_id="guest", data_schema=schema, errors=errors)

    async def async_step_metrics(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage metrics options."""
        if user_input is not None:
            data = dict(self.config_entry.options)
            data.update(user_input)
            return self.async_create_entry(title="", data=data)

        options = self.config_entry.options

        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_METRICS_ENABLED,
                    default=bool(options.get(CONF_METRICS_ENABLED, False)),
                ): bool,
                vol.Optional(
                    CONF_METRICS_HOST,
                    default=str(options.get(CONF_METRICS_HOST, "")),
                ): str,
                vol.Optional(
                    CONF_METRICS_PORT,
                    default=int(options.get(CONF_METRICS_PORT, DEFAULT_METRICS_PORT)),
                ): vol.All(vol.Coerce(int), vol.Range(min=1, max=65535)),
                vol.Optional(
                    CONF_METRICS_DATABASE,
                    default=str(options.get(CONF_METRICS_DATABASE, "")),
                ): str,
                vol.Optional(
                    CONF_METRICS_USERNAME,
                    default=str(options.get(CONF_METRICS_USERNAME, "")),
                ): str,
                vol.Optional(
                    CONF_METRICS_PASSWORD,
                    default=str(options.get(CONF_METRICS_PASSWORD, "")),
                ): str,
                vol.Optional(
                    CONF_METRICS_MEASUREMENT,
                    default=str(
                        options.get(CONF_METRICS_MEASUREMENT, DEFAULT_METRICS_MEASUREMENT)
                    ),
                ): str,
                vol.Optional(
                    CONF_METRICS_SSL,
                    default=bool(options.get(CONF_METRICS_SSL, False)),
                ): bool,
            }
        )

        return self.async_show_form(step_id="metrics", data_schema=schema)
