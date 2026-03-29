"""Config flow for the thin frontend integration prototype."""
from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.data_entry_flow import FlowResult

from .const import (
    API_ENDPOINT,
    CONF_CLIENT_ID,
    CONF_METRICS_DATABASE,
    CONF_METRICS_ENABLED,
    CONF_METRICS_HOST,
    CONF_METRICS_MEASUREMENT,
    CONF_METRICS_PASSWORD,
    CONF_METRICS_PORT,
    CONF_METRICS_SSL,
    CONF_METRICS_USERNAME,
    CONF_REQUEST_TIMEOUT,
    CONF_SHARED_SECRET,
    DEFAULT_METRICS_MEASUREMENT,
    DEFAULT_METRICS_PORT,
    DEFAULT_NAME,
    DEFAULT_REQUEST_TIMEOUT,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

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
        """Manage integration options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        options = self.config_entry.options

        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_REQUEST_TIMEOUT,
                    default=int(options.get(CONF_REQUEST_TIMEOUT, DEFAULT_REQUEST_TIMEOUT)),
                ): vol.All(vol.Coerce(int), vol.Range(min=1, max=300)),
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

        return self.async_show_form(step_id="init", data_schema=schema)
