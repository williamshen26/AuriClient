"""Standalone user preference services for the thin frontend integration."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import UTC, datetime
from typing import Any, Optional

from homeassistant.core import HomeAssistant

from .helpers import read_from_file, write_to_file

_LOGGER = logging.getLogger(__package__)
_MAX_USER_PREFERENCE_RECORDS = 50
_USER_DATA_DIR = "/config/www/user_data"


class UserPreferencesToolService:
    """Handle user-preference-oriented tool calls."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def update_user_preferences(self, arguments: dict[str, Any]) -> dict[str, Any]:
        user_id = arguments.get("user_id")
        if not user_id:
            return {"retry": "user_id is required for update_user_preferences"}
        if not await self._user_exists(user_id):
            return {
                "retry": f"user_id '{user_id}' was not found, ensure you are copying the exact user_id instead of hallucinating."
            }

        raw_updates = arguments.get("updates")
        if not isinstance(raw_updates, list) or not raw_updates:
            return {
                "retry": "updates is required and must be a non-empty array for update_user_preferences"
            }

        normalized_updates: dict[str, Any] = {}
        allowed_importance = {"low", "medium", "high"}
        timestamp = datetime.now(UTC).isoformat()

        for item in raw_updates:
            if not isinstance(item, dict):
                return {"retry": "each updates item must be an object"}

            key = str(item.get("key", "")).strip()
            if not key:
                return {"retry": "each updates item must include a non-empty key"}

            if "value" not in item:
                return {"retry": f"updates item '{key}' is missing value"}

            importance_raw = item.get("importance", "medium")
            importance = str(importance_raw or "medium").strip().lower()
            if importance not in allowed_importance:
                return {
                    "retry": f"updates item '{key}' has invalid importance '{importance}'. Allowed values: low, medium, high"
                }

            normalized_updates[key] = {
                "value": str(item.get("value", "")),
                "importance": importance,
                "timestamp": timestamp,
            }

        await apply_user_preference_update(str(user_id), normalized_updates)
        return {"success": True, "user_id": user_id}

    async def get_user_preferences(self, arguments: dict[str, Any]) -> dict[str, Any]:
        user_id = arguments.get("user_id")
        if not user_id:
            return {"retry": "user_id is required for get_user_preferences"}
        if not await self._user_exists(user_id):
            return {
                "retry": f"user_id '{user_id}' was not found, ensure you are copying the exact user_id instead of hallucinating."
            }

        raw_preferences = await get_user_preferences_helper(str(user_id))
        user_preferences: dict[str, Any] | str = raw_preferences
        try:
            parsed_preferences = json.loads(raw_preferences)
            if isinstance(parsed_preferences, dict):
                user_preferences = parsed_preferences
        except (TypeError, ValueError, json.JSONDecodeError):
            user_preferences = raw_preferences

        return {
            "success": True,
            "user_id": user_id,
            "user_preferences": user_preferences,
        }

    async def get_preference_keys(self, arguments: dict[str, Any]) -> dict[str, Any]:
        user_id = arguments.get("user_id")
        if user_id and not await self._user_exists(user_id):
            return {
                "retry": f"user_id '{user_id}' was not found, ensure you are copying the exact user_id instead of hallucinating."
            }

        keys = await get_preference_keys(user_id)
        return {
            "success": True,
            "user_id": user_id,
            "keys": sorted(keys),
        }

    async def get_preference_by_key(self, arguments: dict[str, Any]) -> dict[str, Any]:
        key = str(arguments.get("key", "")).strip()
        if not key:
            return {"retry": "key is required for get_preference_by_key"}

        user_id = arguments.get("user_id")
        if user_id and not await self._user_exists(user_id):
            return {
                "retry": f"user_id '{user_id}' was not found, ensure you are copying the exact user_id instead of hallucinating."
            }

        values = await get_preference_by_key(key=key, user_id=user_id)
        return {
            "success": True,
            "user_id": user_id,
            "key": key,
            "values": values if len(values) > 1 else (values[0] if values else None),
        }

    async def _user_exists(self, user_id: Any) -> bool:
        candidate = str(user_id or "").strip()
        if not candidate:
            return False

        candidate_lower = candidate.lower()
        users = await self.hass.auth.async_get_users()
        return any(str(user.id).lower().startswith(candidate_lower) for user in users)


def _preferences_file_path(user_id: str) -> str:
    return f"{_USER_DATA_DIR}/{user_id}.json"


async def _resolve_preference_file_paths(user_id: Optional[str]) -> list[str]:
    """Resolve preference JSON file paths for one user or the full directory."""
    if user_id is not None:
        return [_preferences_file_path(user_id)]

    def _list_preference_files() -> list[str]:
        if not os.path.isdir(_USER_DATA_DIR):
            return []

        return [
            os.path.join(_USER_DATA_DIR, name)
            for name in os.listdir(_USER_DATA_DIR)
            if name.endswith(".json")
        ]

    return await asyncio.to_thread(_list_preference_files)


async def _read_preference_dict(file_path: str) -> dict[str, Any] | None:
    """Read one preference file and return a parsed dict, or None when unavailable."""
    try:
        data = await read_from_file(file_path)
    except FileNotFoundError:
        return None

    try:
        parsed = json.loads(data or "{}")
    except json.JSONDecodeError:
        _LOGGER.warning("Skipping invalid preference JSON file: %s", file_path)
        return None

    if not isinstance(parsed, dict):
        return None

    return parsed


def _normalize_preference_value(value: Any) -> Any:
    """Return user-facing preference value from raw stored shape."""
    if isinstance(value, dict) and "value" in value:
        return value.get("value")
    return value


async def get_preference_keys(user_id: Optional[str] = None) -> set[str]:
    """Return unique preference keys for one user or all user files."""
    collected_keys: set[str] = set()

    for file_path in await _resolve_preference_file_paths(user_id):
        parsed = await _read_preference_dict(file_path)
        if parsed is None:
            continue

        for key in parsed.keys():
            if isinstance(key, str):
                collected_keys.add(key)

    return collected_keys


async def get_preference_by_key(key: str, user_id: Optional[str] = None) -> list[Any]:
    """Return values for a preference key for one user or all user files."""
    normalized_key = str(key).strip()
    if not normalized_key:
        return []

    matched_values: list[Any] = []

    for file_path in await _resolve_preference_file_paths(user_id):
        parsed = await _read_preference_dict(file_path)
        if parsed is None or normalized_key not in parsed:
            continue

        value = parsed.get(normalized_key)
        if value is None:
            continue

        matched_values.append(_normalize_preference_value(value))

    return matched_values


async def get_user_preferences_helper(user_id: str | None) -> str:
    """Return persisted user preferences, creating the file if needed."""
    if not user_id:
        return "{}"

    try:
        file_path = _preferences_file_path(user_id)
        data = await read_from_file(file_path)
        data_dict = json.loads(data)
        filtered_dict: dict[str, Any] = {}
        for key, value in data_dict.items():
            if value is None:
                continue

            # Stored records may include metadata like importance/timestamp.
            filtered_dict[key] = _normalize_preference_value(value)
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
    updates: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge new preference data into the user's JSON file.

    In the thin architecture, SaaS should ideally send structured `updates`. If only the
    legacy `user_preference` string is provided, it is preserved under `latest_preference`.
    """
    file_path = _preferences_file_path(user_id)
    try:
        current_json = await read_from_file(file_path)
    except FileNotFoundError:
        directory = os.path.dirname(file_path)
        os.makedirs(directory, exist_ok=True)
        current_json = "{}"

    current_dict = json.loads(current_json or "{}")

    if updates:
        for key, value in updates.items():
            current_dict[key] = value
    else:
        raise ValueError("user_preference must be provided")

    # Keep at most N records. Evict lowest importance first, then oldest timestamp.
    if len(current_dict) > _MAX_USER_PREFERENCE_RECORDS:
        importance_rank = {"low": 0, "medium": 1, "high": 2}

        def _record_sort_key(item: tuple[str, Any]) -> tuple[int, str, str]:
            key, value = item
            if isinstance(value, dict):
                importance_raw = str(value.get("importance", "medium")).strip().lower()
                timestamp_raw = str(value.get("timestamp", "")).strip()
            else:
                importance_raw = "medium"
                timestamp_raw = ""

            return (
                importance_rank.get(importance_raw, importance_rank["medium"]),
                timestamp_raw,
                key,
            )

        records_to_remove = len(current_dict) - _MAX_USER_PREFERENCE_RECORDS
        removal_candidates = sorted(current_dict.items(), key=_record_sort_key)
        for key, _ in removal_candidates[:records_to_remove]:
            current_dict.pop(key, None)

    updated_json = json.dumps(current_dict)
    await write_to_file(file_path, "w", updated_json)
    return {
        "path": file_path,
        "data": current_dict,
    }
