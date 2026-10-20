"""Thin integration entity services."""

from __future__ import annotations

from typing import Any

from ..helpers import (
    resolve_entity_id_no_fallback,
    transform_auri_entity_id_to_ha_entity_id,
    transform_ha_entity_id_to_auri_entity_id,
)
import voluptuous as vol

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from ...const import CONF_STT_LANGUAGE, DOMAIN, SUPPORTED_LANGUAGES
from ...exceptions import ToolExecutionError


_FALLBACK_SUPPORTED_DOMAINS = {"light", "switch", "media_player"}


def _fallback_correction_message(entity_id: str, hallucinated_tool: str) -> str:
    return (
        f"{hallucinated_tool} is not a real tool and must not be called again; "
        f"it was executed once as a one-time fallback for {entity_id}. Going forward, "
        "use the tool provided by this entity's dedicated skill instead."
    )


class EntityToolService:
    """Handle generic entity/service-oriented tool calls."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def turn_on(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self._turn_on_off(arguments, service="turn_on")

    async def turn_off(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self._turn_on_off(arguments, service="turn_off")

    async def _turn_on_off(self, arguments: dict[str, Any], *, service: str) -> dict[str, Any]:
        """Fallback for hallucinated generic turn_on/turn_off tool calls.

        The agent occasionally calls the raw HA service name instead of the
        domain-specific tool (turn_on_light, turn_on_climate, etc.). Rather
        than failing the turn outright, resolve the entity's domain and
        dispatch the equivalent domain service directly.
        """
        raw_entity_id = arguments.get("entity_id")
        if not raw_entity_id:
            return {"retry": f"entity_id is required for {service}"}

        candidate = transform_auri_entity_id_to_ha_entity_id(str(raw_entity_id).strip())
        domain = candidate.split(".")[0]
        if domain not in _FALLBACK_SUPPORTED_DOMAINS:
            return {
                "retry": (
                    f"{candidate} does not support {service}; use the domain-specific tool for "
                    f"this entity instead (e.g. {service}_light, {service}_climate, {service}_media_player)."
                )
            }

        try:
            entity_id = resolve_entity_id_no_fallback(self.hass, domain, candidate)
        except ToolExecutionError:
            return {
                "retry": f"Entity not found: {candidate}, double-check your spelling and that the entity is available in list of entities"
            }

        try:
            await self.hass.services.async_call(
                domain=domain,
                service=service,
                service_data={"entity_id": entity_id},
                blocking=True,
            )
        except vol.error.MultipleInvalid as err:
            return {"retry": str(err)}
        except HomeAssistantError as err:
            raise ToolExecutionError(str(err)) from err

        auri_entity_id = transform_ha_entity_id_to_auri_entity_id(self.hass, entity_id)
        return {"success": True, "retry": _fallback_correction_message(auri_entity_id, service)}

    async def call_service(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Fallback for hallucinated generic call_service tool calls.

        The agent occasionally calls a generic call_service tool (domain/
        service/target) instead of the tool provided by the entity's
        dedicated skill. Execute it once as a best-effort fallback for a
        limited set of user-facing domains, then steer the agent back to the
        real tool for any future turn.
        """
        domain = str(arguments.get("domain") or "").strip()
        service = str(arguments.get("service") or "").strip()
        if not domain or not service:
            return {"retry": "domain and service are required for call_service"}

        if domain not in _FALLBACK_SUPPORTED_DOMAINS:
            return {
                "retry": (
                    f"call_service does not support domain '{domain}'; use the tool "
                    "provided by this entity's dedicated skill instead of call_service."
                )
            }

        target = arguments.get("target") if isinstance(arguments.get("target"), dict) else {}
        service_data_raw = arguments.get("service_data", arguments.get("data", {}))
        service_data = dict(service_data_raw) if isinstance(service_data_raw, dict) else {}
        raw_entity_id = target.get("entity_id") or service_data.get("entity_id") or arguments.get("entity_id")
        if not raw_entity_id:
            return {"retry": "target.entity_id is required for call_service"}

        candidate = transform_auri_entity_id_to_ha_entity_id(str(raw_entity_id).strip())
        try:
            entity_id = resolve_entity_id_no_fallback(self.hass, domain, candidate)
        except ToolExecutionError:
            return {
                "retry": f"Entity not found: {candidate}, double-check your spelling and that the entity is available in list of entities"
            }

        if not self.hass.services.has_service(domain, service):
            return {
                "retry": (
                    f"Service not found: {domain}.{service}. Use the tool provided by "
                    "this entity's dedicated skill instead of call_service."
                )
            }

        service_data["entity_id"] = entity_id
        try:
            await self.hass.services.async_call(
                domain=domain,
                service=service,
                service_data=service_data,
                blocking=True,
            )
        except vol.error.MultipleInvalid as err:
            return {"retry": str(err)}
        except HomeAssistantError as err:
            raise ToolExecutionError(str(err)) from err

        auri_entity_id = transform_ha_entity_id_to_auri_entity_id(self.hass, entity_id)
        return {"success": True, "retry": _fallback_correction_message(auri_entity_id, "call_service")}

    async def set_language(self, arguments: dict[str, Any]) -> dict[str, Any]:
        language = str(arguments.get("language") or "").strip().lower()
        if not language:
            return {"retry": "language is required for set_language"}

        if language not in SUPPORTED_LANGUAGES:
            return {
                "retry": f"{language} is not supported. Supported languages: {', '.join(SUPPORTED_LANGUAGES)}"
            }

        entries = self.hass.config_entries.async_entries(DOMAIN)
        if not entries:
            return {"error": "Auri integration is not configured."}

        entry = entries[0]
        data = dict(entry.options)
        data[CONF_STT_LANGUAGE] = language
        self.hass.config_entries.async_update_entry(entry, options=data)

        return {"success": True, "language": language}
