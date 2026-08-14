"""Tool handlers for shopping list (todo) actions."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from ...exceptions import ToolExecutionError
from ..helpers import resolve_entity_id_no_fallback

_LOGGER = logging.getLogger(__package__)


class ShoppingListToolService:
    """Handle todo/shopping-list-oriented tool calls."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get_shopping_list(self, arguments: dict[str, Any]) -> dict[str, Any]:
        entity_id = resolve_entity_id_no_fallback(
            self.hass,
            "todo",
            arguments.get("entity_id"),
        )
        raw_status = arguments.get("status")

        if not isinstance(raw_status, list) or not raw_status:
            return {"retry": "status is required and must be a non-empty list"}

        status = [str(item).strip() for item in raw_status if str(item).strip()]
        if not status:
            return {"retry": "status is required and must include at least one value"}

        try:
            result = await self.hass.services.async_call(
                domain="todo",
                service="get_items",
                service_data={"status": status},
                target={"entity_id": entity_id},
                blocking=True,
                return_response=True,
            )

            for item in self._iter_todo_items(result):
                uid = item.get("uid")
                if isinstance(uid, str):
                    item["uid"] = uid[:8]

            _LOGGER.info("get_items result: %s", result)
            if isinstance(result, dict):
                return {"success": True, **result}
            return {"success": True, "result": result}
        except HomeAssistantError as err:
            raise ToolExecutionError(str(err)) from err

    async def add_shopping_list_item(self, arguments: dict[str, Any]) -> dict[str, Any]:
        entity_id = resolve_entity_id_no_fallback(
            self.hass,
            "todo",
            arguments.get("entity_id"),
        )
        item = str(arguments.get("item", "")).strip()
        if not item:
            return {"retry": "item is required"}

        try:
            await self.hass.services.async_call(
                domain="todo",
                service="add_item",
                service_data={"item": item},
                target={"entity_id": entity_id},
                blocking=True,
            )
            return {"success": True}
        except HomeAssistantError as err:
            raise ToolExecutionError(str(err)) from err

    async def mark_shopping_list_item_complete(
        self,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        entity_id = resolve_entity_id_no_fallback(
            self.hass,
            "todo",
            arguments.get("entity_id"),
        )

        uid = arguments.get("uid")
        if not uid:
            return {"retry": "uid is required for mark_shopping_list_item_complete"}

        try:
            full_uid = await self._get_full_shopping_list_item_id(str(uid), entity_id)
        except ToolExecutionError as err:
            return {"retry": str(err)}

        try:
            await self.hass.services.async_call(
                domain="todo",
                service="update_item",
                service_data={"item": full_uid, "status": "completed"},
                target={"entity_id": entity_id},
                blocking=True,
            )
            return {"success": True}
        except HomeAssistantError as err:
            raise ToolExecutionError(str(err)) from err

    async def remove_completed_shopping_list_item(
        self,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        entity_id = resolve_entity_id_no_fallback(
            self.hass,
            "todo",
            arguments.get("entity_id"),
        )

        try:
            await self.hass.services.async_call(
                domain="todo",
                service="remove_completed_items",
                target={"entity_id": entity_id},
                blocking=True,
            )
            return {"success": True}
        except HomeAssistantError as err:
            raise ToolExecutionError(str(err)) from err

    async def _get_full_shopping_list_item_id(self, uid: str, entity_id: str) -> str:
        try:
            get_items_result = await self.hass.services.async_call(
                domain="todo",
                service="get_items",
                service_data={"status": "needs_action"},
                target={"entity_id": entity_id},
                blocking=True,
                return_response=True,
            )
            for item in self._iter_todo_items(get_items_result):
                item_uid = item.get("uid")
                if isinstance(item_uid, str) and item_uid.startswith(uid):
                    return item_uid
            raise ToolExecutionError(f"Item with uid starting with '{uid}' not found")
        except HomeAssistantError as err:
            raise ToolExecutionError(str(err)) from err

    @staticmethod
    def _iter_todo_items(response: Any) -> list[dict[str, Any]]:
        """Flatten todo service response payloads into item dicts."""
        if not isinstance(response, dict):
            return []

        items: list[dict[str, Any]] = []
        for payload in response.values():
            if not isinstance(payload, dict):
                continue
            payload_items = payload.get("items")
            if not isinstance(payload_items, list):
                continue
            items.extend(item for item in payload_items if isinstance(item, dict))
        return items
