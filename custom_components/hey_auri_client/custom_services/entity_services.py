"""Thin integration entity services."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from ..exceptions import ToolExecutionError


class EntityToolService:
    """Handle generic entity/service-oriented tool calls."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def execute_service(
        self,
        arguments: dict[str, Any],
        exposed_entities: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        allowed_entity_ids = {entity["entity_id"] for entity in exposed_entities}

        for service_argument in arguments.get("list", []):
            domain = str(service_argument.get("domain", "")).strip()
            service = str(service_argument.get("service", "")).strip()

            if "." in service:
                _, _, normalized_service = service.partition(".")
                if normalized_service:
                    service = normalized_service

            service_data = dict(
                service_argument.get("service_data", service_argument.get("data", {}))
            )
            entity_id = service_data.get("entity_id", service_argument.get("entity_id"))
            area_id = service_data.get("area_id")
            device_id = service_data.get("device_id")

            if isinstance(entity_id, str):
                entity_ids = [item.strip() for item in entity_id.split(",") if item.strip()]
                service_data["entity_id"] = entity_ids
            elif isinstance(entity_id, list):
                entity_ids = entity_id
            else:
                entity_ids = []

            if entity_ids and not set(entity_ids).issubset(allowed_entity_ids):
                raise ToolExecutionError(
                    f"One or more entities are not exposed: {entity_ids}"
                )

            if not entity_ids and area_id is None and device_id is None:
                raise ToolExecutionError(
                    f"Service call requires entity_id, area_id, or device_id: {service_argument}"
                )

            if not self.hass.services.has_service(domain, service):
                raise ToolExecutionError(f"Service not found: {domain}.{service}")

            try:
                await self.hass.services.async_call(
                    domain=domain,
                    service=service,
                    service_data=service_data,
                    blocking=True,
                )
                results.append({"success": True})
            except vol.error.MultipleInvalid as err:
                results.append({"retry": str(err)})
            except HomeAssistantError:
                try:
                    result = await self.hass.services.async_call(
                        domain=domain,
                        service=service,
                        service_data=service_data,
                        blocking=True,
                        return_response=True,
                    )
                    results.append({"success": True, "response": result})
                except vol.error.MultipleInvalid as err:
                    results.append({"retry": str(err)})
                except HomeAssistantError as err:
                    results.append({"success": False, "error": str(err)})

        return results

    async def get_entity_state(self, arguments: dict[str, Any]) -> dict[str, Any]:
        entity_id = arguments.get("entity_id")
        if not entity_id:
            return {"retry": "entity_id is required for get_entity_state"}

        state = self.hass.states.get(entity_id)
        if state is None:
            raise ToolExecutionError(f"Entity not found: {entity_id}")

        return {
            "entity_id": entity_id,
            "state": state.state,
            "attributes": dict(state.attributes),
        }
