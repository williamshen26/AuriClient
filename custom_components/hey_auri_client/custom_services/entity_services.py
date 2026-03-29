"""Thin integration entity services."""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.typing import ConfigType

from ..const import DOMAIN

_LOGGER = logging.getLogger(__package__)


async def async_setup_entity_services(hass: HomeAssistant, config: ConfigType) -> None:
    """Set up entity-related services."""

    async def get_entity_state(call: ServiceCall) -> ServiceResponse:
        entity_id = call.data.get("entity_id")

        if isinstance(entity_id, list):
            if len(entity_id) != 1:
                raise HomeAssistantError("Please target exactly one entity")
            entity_id = entity_id[0]

        if not entity_id:
            raise HomeAssistantError("entity_id is required")

        state = hass.states.get(entity_id)
        if state is None:
            raise HomeAssistantError(f"Entity not found: {entity_id}")

        return {
            "entity_id": entity_id,
            "state": state.state,
            "attributes": dict(state.attributes),
        }

    hass.services.async_register(
        DOMAIN,
        "get_entity_state",
        get_entity_state,
        supports_response=SupportsResponse.ONLY,
    )
