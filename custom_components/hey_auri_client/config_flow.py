"""Config flow for the thin frontend integration prototype."""
from __future__ import annotations

from functools import partial
import logging
import re
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector
from homeassistant.util import dt as dt_util

from .const import (
    API_ENDPOINT,
    CONF_BOOKING_ID,
    CONF_CHECK_IN,
    CONF_CHECK_IN_INSTRUCTION,
    CONF_CHECK_OUT,
    CONF_CHECK_OUT_INSTRUCTION,
    CONF_CLIENT_ID,
    CONF_GUEST_COUNT,
    CONF_GUEST_MEMORY_ENABLED,
    CONF_GUEST_MEMORY_RESET_AT,
    CONF_GUEST_NAME,
    CONF_GUEST_PHONE_NUMBER,
    CONF_HOST_CONTACT_INSTRUCTION,
    CONF_HOUSE_RULES,
    CONF_LOCAL_RECOMMENDATIONS,
    CONF_MEMORY_ARCHIVE_RETENTION_DAYS,
    CONF_MEMORY_DAY_BOUNDARY_HOUR,
    CONF_MEMORY_DB_HOST,
    CONF_MEMORY_DB_NAME,
    CONF_MEMORY_DB_PASSWORD,
    CONF_MEMORY_DB_PORT,
    CONF_MEMORY_DB_SSLMODE,
    CONF_MEMORY_DB_USER,
    CONF_MEMORY_ENABLED,
    CONF_MEMORY_LOGIN_PEOPLE,
    CONF_MEMORY_SHARE_HOUSEHOLD,
    CONF_MEMORY_VAULT_INCLUDE_PRIVATE,
    CONF_MEMORY_VAULT_PATH,
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
    SUPPORTED_LANGUAGES,
    CONF_TRASH_DISPOSAL_INSTRUCTION,
    CONF_WIFI_INSTRUCTION,
    DEFAULT_MEMORY_ARCHIVE_RETENTION_DAYS,
    DEFAULT_MEMORY_DAY_BOUNDARY_HOUR,
    DEFAULT_MEMORY_DB_PORT,
    DEFAULT_MEMORY_DB_SSLMODE,
    DEFAULT_METRICS_MEASUREMENT,
    DEFAULT_METRICS_PORT,
    DEFAULT_NAME,
    DEFAULT_REQUEST_TIMEOUT,
    DEFAULT_STT_LANGUAGE,
    DEFAULT_TTS_STREAM_ENDPOINT,
    DOMAIN,
    CONF_TTS_STREAM_ENDPOINT,
    SSL_MODES,
)
from .memory.config import MemoryConfig
from .memory.db import MemoryDbError, test_connection
from .memory.identity import LOGIN_NOT_A_PERSON, LOGIN_SHARED, household_persons
from .memory.obsidian import validate_vault

_LOGGER = logging.getLogger(__name__)
# Memory text fields an empty input clears (rather than keeping the old value).
_MEMORY_TEXT_FIELDS = (CONF_MEMORY_DB_HOST, CONF_MEMORY_DB_NAME, CONF_MEMORY_DB_USER, CONF_MEMORY_VAULT_PATH)
# "Who uses each login" default: the person Home Assistant links the login to (not saved).
_LOGIN_LINKED = "linked"
# Guest Settings checkbox: erase guest memory and start it again from the guest details.
_RESET_GUEST_MEMORY = "reset_guest_memory"
_PHONE_NANP_10_PATTERN = re.compile(r"[2-9]\d{9}")

# Display names for the STT language dropdown only — SUPPORTED_LANGUAGES
# itself stays plain ISO 639-1 codes for everything else (Cartesia's API,
# OpenAI's transcription language param). Scoped to this file rather than
# const.py since nothing else needs it.
_LANGUAGE_DISPLAY_NAMES: dict[str, str] = {
    "en": "English",
    "fr": "French",
    "de": "German",
    "es": "Spanish",
    "pt": "Portuguese",
    "zh": "Chinese",
    "ja": "Japanese",
    "hi": "Hindi",
    "it": "Italian",
    "ko": "Korean",
    "nl": "Dutch",
    "pl": "Polish",
    "ru": "Russian",
    "sv": "Swedish",
    "tr": "Turkish",
    "tl": "Tagalog",
    "bg": "Bulgarian",
    "ro": "Romanian",
    "ar": "Arabic",
    "cs": "Czech",
    "el": "Greek",
    "fi": "Finnish",
    "hr": "Croatian",
    "ms": "Malay",
    "sk": "Slovak",
    "da": "Danish",
    "ta": "Tamil",
    "uk": "Ukrainian",
    "hu": "Hungarian",
    "no": "Norwegian",
    "vi": "Vietnamese",
    "bn": "Bengali",
    "th": "Thai",
    "he": "Hebrew",
    "ka": "Georgian",
    "id": "Indonesian",
    "te": "Telugu",
    "gu": "Gujarati",
    "kn": "Kannada",
    "ml": "Malayalam",
    "mr": "Marathi",
    "pa": "Punjabi",
}


def _stt_language_options() -> list[selector.SelectOptionDict]:
    """STT language dropdown options, sorted alphabetically by display name."""
    options = [
        selector.SelectOptionDict(
            value=code,
            label=_LANGUAGE_DISPLAY_NAMES.get(code, code),
        )
        for code in SUPPORTED_LANGUAGES
    ]
    options.sort(key=lambda option: option["label"])
    return options

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
            menu_options=["general", "guest", "memory", "logins", "metrics"],
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
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=_stt_language_options(),
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
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

            if user_input.get(CONF_GUEST_MEMORY_ENABLED) and not MemoryConfig.from_options(self.config_entry.options).configured:
                errors[CONF_GUEST_MEMORY_ENABLED] = "guest_memory_needs_memory"

            if not errors:
                data = dict(self.config_entry.options)
                reset = bool(user_input.pop(_RESET_GUEST_MEMORY, False))
                data.update(user_input)
                if reset:
                    data[CONF_GUEST_MEMORY_RESET_AT] = dt_util.utcnow().isoformat()
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
                vol.Optional(
                    CONF_GUEST_MEMORY_ENABLED,
                    default=bool(options.get(CONF_GUEST_MEMORY_ENABLED, False)),
                ): bool,
                # Not stored: saving with it ticked records when the host asked.
                vol.Optional(_RESET_GUEST_MEMORY, default=False): bool,
            }
        )

        return self.async_show_form(step_id="guest", data_schema=schema, errors=errors)

    async def async_step_memory(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage long-term memory (PostgreSQL + optional Obsidian vault copy)."""
        errors: dict[str, str] = {}

        if user_input is not None:
            data = dict(self.config_entry.options)
            # The password is never shown in the form; leaving it empty keeps the saved one.
            password = str(user_input.pop(CONF_MEMORY_DB_PASSWORD, "") or "")
            data.update(user_input)
            for key in _MEMORY_TEXT_FIELDS:
                data[key] = str(user_input.get(key) or "").strip()
            for key in (CONF_MEMORY_DAY_BOUNDARY_HOUR, CONF_MEMORY_ARCHIVE_RETENTION_DAYS):
                if key in user_input:
                    data[key] = int(user_input[key])  # number selectors return floats
            if password:
                data[CONF_MEMORY_DB_PASSWORD] = password

            errors = await self._async_validate_memory(MemoryConfig.from_options(data))
            if not errors:
                return self.async_create_entry(title="", data=data)

        values = {**self.config_entry.options, **(user_input or {})}

        def _suggested(key: str) -> dict[str, Any]:
            return {"suggested_value": str(values.get(key) or "")}

        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_MEMORY_ENABLED,
                    default=bool(values.get(CONF_MEMORY_ENABLED, False)),
                ): bool,
                vol.Optional(CONF_MEMORY_DB_HOST, description=_suggested(CONF_MEMORY_DB_HOST)): str,
                vol.Optional(
                    CONF_MEMORY_DB_PORT,
                    default=int(values.get(CONF_MEMORY_DB_PORT, DEFAULT_MEMORY_DB_PORT)),
                ): vol.All(vol.Coerce(int), vol.Range(min=1, max=65535)),
                vol.Optional(CONF_MEMORY_DB_NAME, description=_suggested(CONF_MEMORY_DB_NAME)): str,
                vol.Optional(CONF_MEMORY_DB_USER, description=_suggested(CONF_MEMORY_DB_USER)): str,
                vol.Optional(CONF_MEMORY_DB_PASSWORD): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                ),
                vol.Optional(
                    CONF_MEMORY_DB_SSLMODE,
                    default=str(values.get(CONF_MEMORY_DB_SSLMODE, DEFAULT_MEMORY_DB_SSLMODE)),
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=list(SSL_MODES),
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Optional(CONF_MEMORY_VAULT_PATH, description=_suggested(CONF_MEMORY_VAULT_PATH)): str,
                vol.Optional(
                    CONF_MEMORY_VAULT_INCLUDE_PRIVATE,
                    default=bool(values.get(CONF_MEMORY_VAULT_INCLUDE_PRIVATE, False)),
                ): bool,
                vol.Optional(
                    CONF_MEMORY_DAY_BOUNDARY_HOUR,
                    default=int(values.get(CONF_MEMORY_DAY_BOUNDARY_HOUR, DEFAULT_MEMORY_DAY_BOUNDARY_HOUR)),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=0, max=23, step=1, mode=selector.NumberSelectorMode.BOX)
                ),
                vol.Optional(
                    CONF_MEMORY_ARCHIVE_RETENTION_DAYS,
                    default=int(
                        values.get(CONF_MEMORY_ARCHIVE_RETENTION_DAYS, DEFAULT_MEMORY_ARCHIVE_RETENTION_DAYS)
                    ),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=0, max=3650, step=1, mode=selector.NumberSelectorMode.BOX)
                ),
                vol.Optional(
                    CONF_MEMORY_SHARE_HOUSEHOLD,
                    default=bool(values.get(CONF_MEMORY_SHARE_HOUSEHOLD, False)),
                ): bool,
            }
        )

        return self.async_show_form(step_id="memory", data_schema=schema, errors=errors)

    async def _async_validate_memory(self, cfg: MemoryConfig) -> dict[str, str]:
        """Dry-run the database setup and check the vault folder; changes nothing."""
        if not cfg.enabled:
            return {}
        if not cfg.configured:
            return {"base": "memory_settings_missing"}
        try:
            await self.hass.async_add_executor_job(test_connection, cfg)
        except MemoryDbError as err:
            return {"base": err.code}
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Unexpected error testing the memory database")
            return {"base": "unknown"}
        if cfg.vault_path:
            problem = await self.hass.async_add_executor_job(
                partial(
                    validate_vault,
                    cfg.vault_path,
                    config_dir=self.hass.config.config_dir,
                    is_allowed_path=self.hass.config.is_allowed_path,
                )
            )
            if problem:
                return {CONF_MEMORY_VAULT_PATH: problem}
        return {}

    async def async_step_logins(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Say who uses each Home Assistant login, so memory knows who is speaking."""
        logins = await self._async_logins()
        if user_input is not None:
            used_by = {
                user_id: str(user_input[label])
                for label, user_id in logins
                if user_input.get(label) not in (None, "", _LOGIN_LINKED)
            }
            data = dict(self.config_entry.options)
            data[CONF_MEMORY_LOGIN_PEOPLE] = used_by
            return self.async_create_entry(title="", data=data)

        saved = self.config_entry.options.get(CONF_MEMORY_LOGIN_PEOPLE) or {}
        persons = household_persons(self.hass)
        people = household_persons(self.hass, saved)  # without logins marked "not a person"
        fields: dict[Any, Any] = {}
        for label, user_id in logins:
            linked = next((p["name"] for p in persons if p.get("user_id") == user_id), None)
            options = [
                selector.SelectOptionDict(
                    value=_LOGIN_LINKED,
                    label=f"{linked} (its person in Home Assistant)" if linked else "Nobody (no person in Home Assistant)",
                ),
                *(
                    selector.SelectOptionDict(value=p["entity_id"], label=p["name"])
                    for p in people
                    if p.get("user_id") != user_id
                ),
                selector.SelectOptionDict(value=LOGIN_SHARED, label="Shared: nobody in particular, as on a voice satellite"),
                selector.SelectOptionDict(
                    value=LOGIN_NOT_A_PERSON, label="Not a person: a service uses it (like MQTT), not a household member"
                ),
            ]
            current = saved.get(user_id, _LOGIN_LINKED)
            if current not in {option["value"] for option in options}:
                current = _LOGIN_LINKED  # the person was removed from Home Assistant
            fields[vol.Optional(label, default=current)] = selector.SelectSelector(
                selector.SelectSelectorConfig(options=options, mode=selector.SelectSelectorMode.DROPDOWN)
            )
        return self.async_show_form(step_id="logins", data_schema=vol.Schema(fields))

    async def _async_logins(self) -> list[tuple[str, str]]:
        """(field label, user id) for each login people use, labelled with its name."""
        users = sorted(
            (user for user in await self.hass.auth.async_get_users() if user.is_active and not user.system_generated),
            key=lambda user: (user.name or "").lower(),
        )
        names = [user.name or "Unnamed login" for user in users]
        return [
            (name if names.count(name) == 1 else f"{name} ({user.id[:6]})", user.id)
            for name, user in zip(names, users)
        ]

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
