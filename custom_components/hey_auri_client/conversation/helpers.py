"""Helpers for conversation-related integration logic."""
from __future__ import annotations

from datetime import datetime
import difflib
from typing import Any

from homeassistant.components import conversation
from homeassistant.components.homeassistant.exposed_entities import async_should_expose
from homeassistant.const import ATTR_NAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er

from ..cache import add_processed_entity, get_processed_entities
from .custom_services.user_preferences_services import get_preference_keys
from ..exceptions import ToolExecutionError


def resolve_entity_id_no_fallback(
    hass: HomeAssistant,
    domain: str,
    requested_entity_id: Any,
) -> str:
    """Resolve a valid entity id for the given domain without fallback."""
    candidate = str(requested_entity_id or "").strip()
    if candidate and candidate.startswith(f"{domain}.") and hass.states.get(candidate):
        return candidate
    raise ToolExecutionError(f"Entity not found: {candidate}")


def resolve_entity_id(
    hass: HomeAssistant,
    domain: str,
    requested_entity_id: Any,
) -> str:
    """Resolve a valid entity id for the given domain with fallback."""
    candidate = str(requested_entity_id or "").strip()
    if candidate and candidate.startswith(f"{domain}.") and hass.states.get(candidate):
        return candidate

    fallback = next(
        (
            state.entity_id
            for state in hass.states.async_all()
            if state.entity_id.startswith(f"{domain}.")
        ),
        None,
    )
    if fallback is None:
        raise ToolExecutionError(
            f"No {domain} entity is available, please add a {domain} entity to Home Assistant."
        )
    return fallback


def get_retry_entities(
    hass: HomeAssistant,
    entity_id: str,
    conversation_id: str | None = None,
) -> list[str]:
    """Return nearby/similar entities in priority order for retry suggestions."""
    state = hass.states.get(entity_id)
    if state is None:
        raise ToolExecutionError(f"Entity not found: {entity_id}")
    domain = entity_id.split(".", 1)[0]
    if not domain:
        raise ToolExecutionError(f"Entity not found: {entity_id}")

    if conversation_id:
        add_processed_entity(conversation_id, entity_id)
    excluded_entity_ids = get_processed_entities(conversation_id)

    current_area = _resolve_entity_area_id(hass, entity_id)
    current_floor = _resolve_entity_floor_id(hass, entity_id)
    current_name = str(state.name or entity_id)
    exposed_entities = [
        entity
        for entity in get_exposed_entities(hass)
        if str(entity.get("entity_id", "")).startswith(f"{domain}.")
        and entity.get("entity_id") != entity_id
        and str(entity.get("entity_id") or "") not in excluded_entity_ids
    ]

    exposed_entities.sort(
        key=lambda entity: difflib.SequenceMatcher(
            None,
            str(entity.get("name") or entity.get("entity_id") or "").casefold(),
            current_name.casefold(),
        ).ratio(),
        reverse=True,
    )

    same_area_entities = [
        str(entity.get("entity_id"))
        for entity in exposed_entities
        if current_area and entity.get("area_id") == current_area
    ]
    different_area_same_floor_entities = [
        str(entity.get("entity_id"))
        for entity in exposed_entities
        if current_floor
        and entity.get("floor_id") == current_floor
        and entity.get("area_id") != current_area
    ]
    other_area_entities = [
        str(entity.get("entity_id"))
        for entity in exposed_entities
        if (
            (not current_area or entity.get("area_id") != current_area)
            and (not current_floor or entity.get("floor_id") != current_floor)
        )
    ]

    return same_area_entities + different_area_same_floor_entities + other_area_entities


async def build_context_snapshot(
    hass: HomeAssistant,
    user_input: conversation.ConversationInput,
) -> dict[str, Any]:
    """Build the structured context payload sent to the SaaS backend."""
    user_id = user_input.context.user_id
    context_user_id = None if user_id is None else str(user_id)[:8]
    user_preferences = sorted(await get_preference_keys(context_user_id))
    user_name = await get_user_name(hass, user_id)
    now = datetime.now().astimezone()
    return {
        "user_id": context_user_id,
        "agent_id": user_input.agent_id,
        "device_id": user_input.device_id,
        "language": user_input.language,
        "text": user_input.text,
        "user_name": user_name,
        "satellite_speaker": get_device_media_player(hass, user_input.device_id),
        "request_area": get_request_area(hass, user_input.device_id),
        "exposed_entities": get_exposed_entities(hass),
        "user_preference_keys": user_preferences,
        "home_location": {
            "latitude": float(hass.config.latitude),
            "longitude": float(hass.config.longitude),
            "time_zone": hass.config.time_zone,
            "country": hass.config.country,
        },
        "temperature_unit_preference": str(hass.config.units.temperature_unit),
        "now": now.isoformat(),
        "day_of_week": now.strftime("%A"),
    }


async def get_user_name(
    hass: HomeAssistant, user_id: str
) -> str | None:
    """Resolve the current Home Assistant user's display name."""
    if user_id is None:
        return None
    user = await hass.auth.async_get_user(user_id)
    return None if user is None else user.name


def get_device_media_player(hass: HomeAssistant, device_id: str | None) -> str | None:
    """Get the first exposed media_player entity for a device."""
    if device_id is None:
        return None

    entity_registry = er.async_get(hass)
    entities = er.async_entries_for_device(entity_registry, device_id)
    for entity in entities:
        if entity.domain != "media_player":
            continue
        if async_should_expose(hass, conversation.DOMAIN, entity.entity_id):
            return entity.entity_id
    return None


def device_has_entity_domain(
    hass: HomeAssistant,
    entity_id: str,
    domain: str,
) -> bool:
    """Return whether the entity's device has another entity in the given domain."""
    entity_registry = er.async_get(hass)
    entry = entity_registry.async_get(entity_id)
    if not entry or not entry.device_id:
        return False

    device_entities = er.async_entries_for_device(entity_registry, entry.device_id)
    result = any(device_entity.domain == domain for device_entity in device_entities)
    return result


def transform_ha_entity_id_to_auri_entity_id(hass: HomeAssistant, entity_id: str) -> str:
    """Transform a Home Assistant entity_id into an AI processing format."""

    state = hass.states.get(entity_id)
    if state:
        if entity_id.startswith("media_player."):
            if state.attributes.get("app_id") == "music_assistant":
                return entity_id.replace("media_player.", "music_source.", 1)
            if device_has_entity_domain(hass, entity_id, "assist_satellite"):
                return entity_id.replace("media_player.", "satellite_speaker.", 1)

    return entity_id


def transform_auri_entity_id_to_ha_entity_id(entity_id: str) -> str:
    """Transform an AI processing entity_id back into a Home Assistant format."""
    if entity_id.startswith("satellite_speaker."):
        return entity_id.replace("satellite_speaker.", "media_player.", 1)
    if entity_id.startswith("music_source."):
        return entity_id.replace("music_source.", "media_player.", 1)
    return entity_id


def get_house_layout(hass: HomeAssistant) -> dict[str, list[str]]:
    """Return a map of floor_id to area names, with unassigned areas in no_floor."""
    area_registry = ar.async_get(hass)
    layout: dict[str, list[str]] = {}

    for area in area_registry.areas.values():
        floor_key = str(area.floor_id) if area.floor_id else "no_floor"
        area_name = area.name or area.id
        layout.setdefault(floor_key, []).append(area_name)

    for areas in layout.values():
        areas.sort(key=str.casefold)

    return dict(sorted(layout.items(), key=lambda item: (item[0] == "no_floor", item[0])))


def get_exposed_entities(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Return the exposed entity snapshot sent to SaaS."""
    states = [
        state
        for state in hass.states.async_all()
        if async_should_expose(hass, conversation.DOMAIN, state.entity_id)
    ]
    entity_registry = er.async_get(hass)
    exposed_entities: list[dict[str, Any]] = []

    for state in states:
        entity = entity_registry.async_get(state.entity_id)
        aliases: list[str] = []
        if entity and entity.aliases:
            # entity.aliases can contain ComputedNameType, a HA-internal
            # sentinel meaning "use the entity's computed name as an alias"
            # (not a real string — not JSON serializable). The computed name
            # is already carried separately as "name" above, so just drop
            # the sentinel rather than try to resolve it here.
            aliases = [alias for alias in entity.aliases if isinstance(alias, str)]

        exposed_entities.append(
            {
                "entity_id": transform_ha_entity_id_to_auri_entity_id(hass, state.entity_id),
                "name": state.name,
                ATTR_NAME: state.name,
                "state": state.state.replace("\n", " ").replace(",", " "),
                "aliases": aliases,
                "floor_id": _resolve_entity_floor_id(hass, state.entity_id),
                "area_id": _resolve_entity_area_id(hass, state.entity_id),
            }
        )

    exposed_entities.sort(
        key=lambda item: (
            item.get("floor_id") is None,
            str(item.get("floor_id") or ""),
            item.get("area_id") is None,
            str(item.get("area_id") or ""),
            str(item.get("entity_id") or ""),
        )
    )

    return exposed_entities


def get_request_area(hass: HomeAssistant, device_id: str | None) -> str | None:
    """Resolve area for the current request device."""
    if not device_id:
        return None

    device_registry = dr.async_get(hass)
    area_registry = ar.async_get(hass)
    device = device_registry.async_get(device_id)
    if not device:
        return None

    if device.area_id:
        area = area_registry.async_get_area(device.area_id)
        return area.name if area else device.area_id

    return None


def _resolve_entity_area_id(hass: HomeAssistant, entity_id: str) -> str | None:
    """Resolve area id/name for an entity."""
    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)
    area_registry = ar.async_get(hass)

    entry = entity_registry.async_get(entity_id)
    if not entry:
        return None

    if entry.area_id:
        area = area_registry.async_get_area(entry.area_id)
        return area.name if area else entry.area_id

    if entry.device_id:
        device = device_registry.async_get(entry.device_id)
        if device and device.area_id:
            area = area_registry.async_get_area(device.area_id)
            return area.name if area else device.area_id

    return None


def _resolve_entity_floor_id(hass: HomeAssistant, entity_id: str) -> str | None:
    """Resolve floor id for an entity through its area mapping."""
    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)
    area_registry = ar.async_get(hass)

    entry = entity_registry.async_get(entity_id)
    if not entry:
        return None

    area_id = entry.area_id
    if area_id is None and entry.device_id:
        device = device_registry.async_get(entry.device_id)
        if device:
            area_id = device.area_id

    if area_id is None:
        return None

    area = area_registry.async_get_area(area_id)
    if area is None:
        return None

    return str(area.floor_id) if area.floor_id else None


def _clamp_percentage(value: Any) -> int:
    """Normalize a percentage-like value into an integer from 1 to 100."""
    numeric = int(round(float(value)))
    return max(1, min(100, numeric))


def _clamp_percentage_zero_to_hundred(value: Any) -> int:
    """Normalize a percentage-like value into an integer from 0 to 100."""
    numeric = int(round(float(value)))
    return max(0, min(100, numeric))


def _clamp_step_percentage(value: Any) -> int:
    """Normalize a step percentage-like value into an integer from -100 to 100."""
    numeric = int(round(float(value)))
    return max(-100, min(100, numeric))


def _normalize_temperature_unit(raw_unit: Any) -> str:
    """Normalize unit text to C or F, defaulting to C."""
    unit = str(raw_unit or "").strip().upper()
    if unit in {"F", "°F"}:
        return "F"
    return "C"


def _convert_temperature(value: float, *, from_unit: str, to_unit: str) -> float:
    """Convert temperature between C and F when units differ."""
    if from_unit == to_unit:
        return value
    if from_unit == "F" and to_unit == "C":
        return round((value - 32.0) * 5.0 / 9.0, 2)
    if from_unit == "C" and to_unit == "F":
        return round((value * 9.0 / 5.0) + 32.0, 2)
    return value
