"""Shared non-conversation helpers."""
from __future__ import annotations

from datetime import date, datetime, time
from typing import Any

from .const import (
    CONF_REQUEST_TIMEOUT,
    DEFAULT_REQUEST_TIMEOUT,
)


def get_timeout_seconds(entry_options: dict[str, Any]) -> int:
    """Resolve request timeout from options."""
    return int(entry_options.get(CONF_REQUEST_TIMEOUT, DEFAULT_REQUEST_TIMEOUT))


def to_json_safe(value: Any) -> Any:
    """Recursively normalize a value into JSON-serializable types.

    HA entity attributes can contain datetime/date/time objects (for example
    media_position_updated_at) that json.dumps() can't serialize directly.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value

    if isinstance(value, (datetime, date, time)):
        return value.isoformat()

    if isinstance(value, dict):
        return {
            str(key): to_json_safe(item)
            for key, item in value.items()
        }

    if isinstance(value, (list, tuple, set)):
        return [to_json_safe(item) for item in value]

    return str(value)


