"""Standalone user preference services for the thin frontend integration."""
from __future__ import annotations

import json
import logging
import os
from typing import Any

from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.typing import ConfigType

from ..const import DOMAIN
from .helpers import read_from_file, write_to_file

_LOGGER = logging.getLogger(__package__)


def _preferences_file_path(user_id: str) -> str:
    return f"/config/www/user_data/{user_id}.json"


async def get_user_preferences_helper(user_id: str | None) -> str:
    """Return persisted user preferences, creating the file if needed."""
    if not user_id:
        return "{}"

    try:
        file_path = _preferences_file_path(user_id)
        data = await read_from_file(file_path)
        data_dict = json.loads(data)
        filtered_dict = {key: value for key, value in data_dict.items() if value is not None}
        return json.dumps(filtered_dict)
    except FileNotFoundError:
        file_path = _preferences_file_path(user_id)
        directory = os.path.dirname(file_path)
        os.makedirs(directory, exist_ok=True)
        empty_json = "{}"
        await write_to_file(file_path, "w", empty_json)
        return empty_json


async def apply_user_preference_update(
    user_id: str,
    user_preference: str | None = None,
    updates: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge new preference data into the user's JSON file.

    In the thin architecture, SaaS should ideally send structured `updates`. If only the
    legacy `user_preference` string is provided, it is preserved under `latest_preference`.
    """
    file_path = _preferences_file_path(user_id)
    current_json = await get_user_preferences_helper(user_id)
    current_dict = json.loads(current_json or "{}")

    if updates:
        for key, value in updates.items():
            current_dict[key] = value
    elif user_preference:
        current_dict["latest_preference"] = user_preference
    else:
        raise HomeAssistantError("Either updates or user_preference must be provided")

    updated_json = json.dumps(current_dict)
    await write_to_file(file_path, "w", updated_json)
    return {
        "path": file_path,
        "data": current_dict,
    }


async def async_setup_user_preferences_services(hass: HomeAssistant, config: ConfigType) -> None:
    """Register user preference services."""

    async def update_user_preferences(call: ServiceCall) -> ServiceResponse:
        try:
            user_id = call.data.get("user_id")
            if not user_id:
                raise HomeAssistantError("user_id is required")
            user_preference = call.data.get("user_preference")
            updates = call.data.get("updates")
            if updates is not None and not isinstance(updates, dict):
                raise HomeAssistantError("updates must be an object")
            return await apply_user_preference_update(user_id, user_preference, updates)
        except Exception as err:
            _LOGGER.error("Error updating preferences: %s", err)
            raise HomeAssistantError(f"Error updating preferences: {err}") from err

    hass.services.async_register(
        DOMAIN,
        "update_user_preferences",
        update_user_preferences,
        supports_response=SupportsResponse.ONLY,
    )

    async def get_user_preferences(call: ServiceCall) -> ServiceResponse:
        user_id = call.data.get("user_id")
        return {
            "user_id": user_id,
            "user_preferences": await get_user_preferences_helper(user_id),
        }

    hass.services.async_register(
        DOMAIN,
        "get_user_preferences",
        get_user_preferences,
        supports_response=SupportsResponse.ONLY,
    )
