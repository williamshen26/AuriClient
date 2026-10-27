"""The guest's name when the booking has none (Airbnb's calendar never has one).

While a stay is under way and the host hasn't entered a Guest name, Auri
asks the guests once and keeps their answer with that stay only
(CONF_GUEST_ASKED_NAME), apart from the host's Guest name: a name the host
enters always wins, and the next stay starts without one. Guests can
correct the name they gave. The backend decides what to offer from
can_save_name and ask_name in context.stay.
"""
from __future__ import annotations

from datetime import datetime, tzinfo
from typing import Any, Mapping

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback

from ..const import CONF_GUEST_ASKED_NAME, CONF_GUEST_NAME
from ..memory.guest import stay_key
from .schedule import stay_state

# One line, as long as guest memory keeps a name.
MAX_NAME_CHARS = 60


def clean_name(value: Any) -> str:
    return " ".join(str(value or "").split())[:MAX_NAME_CHARS]


def host_guest_name(options: Mapping[str, Any]) -> str:
    """The Guest name in Guest Settings (the host's, or the booking calendar's)."""
    return clean_name(options.get(CONF_GUEST_NAME))


def _asked(options: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """What Auri asked this stay's guests (None when it hasn't, or asked an earlier stay's)."""
    asked = options.get(CONF_GUEST_ASKED_NAME)
    return asked if isinstance(asked, Mapping) and asked.get("stay") == stay_key(options) else None


def stated_guest_name(options: Mapping[str, Any]) -> str:
    """The name this stay's guests gave Auri ("" when they haven't)."""
    asked = _asked(options)
    return clean_name(asked.get("name")) if asked else ""


def guest_name(options: Mapping[str, Any]) -> str:
    """The guest's name: the host's, else the one this stay's guests gave ("" when neither)."""
    return host_guest_name(options) or stated_guest_name(options)


def can_save_name(options: Mapping[str, Any], now: datetime, tz: tzinfo) -> bool:
    """Whether the guests' own name can be saved: a stay is under way (check-in to check-out) and the host hasn't entered one."""
    return stay_state(options, now, tz)[0] == "staying" and not host_guest_name(options)


def name_request(options: Mapping[str, Any], now: datetime, tz: tzinfo) -> dict[str, bool]:
    """context.stay's can_save_name, and ask_name: nobody's given a name and Auri hasn't asked this stay."""
    can_save = can_save_name(options, now, tz)
    return {"can_save_name": can_save, "ask_name": can_save and _asked(options) is None}


def with_asked(options: Mapping[str, Any], name: str = "") -> dict[str, Any]:
    """The options, recording that this stay's guests were asked their name and what they said."""
    return {**options, CONF_GUEST_ASKED_NAME: {"stay": stay_key(options), "name": clean_name(name)}}


@callback
def async_mark_asked(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Record that Auri asked this stay's guests their name, keeping any answer already saved."""
    if _asked(entry.options) is None:
        hass.config_entries.async_update_entry(entry, options=with_asked(entry.options))
