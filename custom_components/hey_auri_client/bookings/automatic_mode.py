"""What the client reports about the stay (context.stay) for the backend.

The backend decides the mode. For an account set to Automatic, it uses this
report: owner only when no stay is under way and someone from the household
is home, guest otherwise, so a cleaner between stays never gets owner mode.
"Home" is Home Assistant's presence (a person's state, usually from the
companion app's location). Persons whose login is marked Not a person under
Who uses each login don't count. In guest mode, the backend also uses it to
have Auri ask the guests their name (guest_name.py).
"""
from __future__ import annotations

from typing import Any, Mapping

from homeassistant.const import STATE_HOME
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from ..const import CONF_MEMORY_LOGIN_PEOPLE
from ..memory.guest import CHECK_OUT_GRACE
from ..memory.identity import household_persons
from .guest_name import name_request
from .schedule import stay_active


def stay_context(hass: HomeAssistant, options: Mapping[str, Any]) -> dict[str, bool]:
    """context.stay: whether a stay is under way, whether anyone from the household is home, and the guests' name."""
    now, tz = dt_util.now(), dt_util.get_default_time_zone()
    active = stay_active(options, now, tz, CHECK_OUT_GRACE)
    household_home = False
    for person in household_persons(hass, options.get(CONF_MEMORY_LOGIN_PEOPLE)):
        state = hass.states.get(person["entity_id"])
        if state is not None and state.state == STATE_HOME:
            household_home = True
            break
    return {"active": active, "household_home": household_home, **name_request(options, now, tz)}
