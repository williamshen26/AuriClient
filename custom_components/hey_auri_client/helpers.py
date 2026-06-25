"""Shared non-conversation helpers."""
from __future__ import annotations

from typing import Any

from .const import (
    CONF_REQUEST_TIMEOUT,
    DEFAULT_REQUEST_TIMEOUT,
)


def get_timeout_seconds(entry_options: dict[str, Any]) -> int:
    """Resolve request timeout from options."""
    return int(entry_options.get(CONF_REQUEST_TIMEOUT, DEFAULT_REQUEST_TIMEOUT))


