"""Standalone automation services for the thin frontend integration."""
from __future__ import annotations

import logging
import os
import time
from typing import Any

import aiofiles
import yaml

from homeassistant.components import automation
from homeassistant.components.automation.config import _async_validate_config_item
from homeassistant.config import AUTOMATION_CONFIG_PATH
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_registry import async_get

from ..file_util import read_from_file

_LOGGER = logging.getLogger(__package__)


class AutomationToolService:
    """Handle automation-oriented tool calls."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get_automation_metadata(
        self,
        arguments: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del arguments
        return {"automation_metadata": await get_automation_metadata(self.hass)}

    async def get_automation_metadata_service(
        self,
        arguments: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Compatibility alias for existing tool name."""
        return await self.get_automation_metadata(arguments)

    async def add_automation(self, arguments: dict[str, Any]) -> str | dict[str, Any]:
        return await add_automation_from_yaml(
            self.hass,
            str(arguments.get("automation_config", "")),
        )

    async def update_automation(self, arguments: dict[str, Any]) -> str:
        return await update_automation_from_yaml(
            self.hass,
            str(arguments.get("id", "")),
            str(arguments.get("automation_config", "")),
        )

    async def remove_automation(self, arguments: dict[str, Any]) -> str:
        return await remove_automation_by_id(
            self.hass,
            str(arguments.get("id", "")),
        )


async def get_automations_helper() -> list[dict[str, Any]]:
    """Load automations from automations.yaml."""
    try:
        automations_yaml = await read_from_file("/config/automations.yaml")
    except FileNotFoundError:
        return []

    automations = yaml.safe_load(automations_yaml) or []
    if isinstance(automations, dict):
        return [automations]
    return automations


async def get_automation_helper(automation_id: str) -> dict[str, Any] | None:
    """Return one automation config by id."""
    automations = await get_automations_helper()
    for item in automations:
        if automation_id == item.get("id"):
            return item
    return None


async def get_automation_metadata(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Build automation metadata from HA state and YAML storage."""
    automation_states = [
        state for state in hass.states.async_all() if state.entity_id.startswith("automation.")
    ]
    entity_registry = async_get(hass)
    metadata: list[dict[str, Any]] = []

    for state in automation_states:
        entity_id = state.entity_id
        is_enabled = state.state == "on"
        name = state.attributes.get("friendly_name", "Unknown Automation")
        description = "No description available"

        entity_entry = entity_registry.entities.get(entity_id)
        if entity_entry:
            unique_id = entity_entry.unique_id
            automation_config = await get_automation_helper(unique_id)
            if automation_config:
                description = automation_config.get("description", description)
        else:
            unique_id = "Unknown"

        metadata.append(
            {
                "entity_id": entity_id,
                "name": name,
                "description": description,
                "unique_id": unique_id,
                "enabled": is_enabled,
            }
        )

    return metadata


async def add_automation_from_yaml(
    hass: HomeAssistant, automation_config_yaml: str
) -> str | dict[str, Any]:
    """Append a new automation to automations.yaml with validation and retry feedback."""
    try:
        automation_config = yaml.safe_load(automation_config_yaml)
    except yaml.YAMLError as err:
        return {"success": False, "retry": f"Invalid automation YAML: {err}"}

    if automation_config is None:
        return {"success": False, "retry": "automation_config is required"}

    if isinstance(automation_config, list):
        if len(automation_config) != 1:
            return {
                "success": False,
                "retry": "Provide exactly one automation in add_automation.",
            }
        if not isinstance(automation_config[0], dict):
            return {
                "success": False,
                "retry": "automation_config must be a YAML mapping/object.",
            }
        normalized_config = automation_config[0]
    elif isinstance(automation_config, dict):
        normalized_config = automation_config
    else:
        return {
            "success": False,
            "retry": "automation_config must be a YAML mapping/object.",
        }

    config = {"id": str(round(time.time() * 1000))}
    config.update(normalized_config)

    if not config.get("description"):
        return {
            "success": False,
            "retry": "automation_config must include a non-empty description.",
        }

    try:
        await _async_validate_config_item(hass, config, True, False)
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Automation validation failed", exc_info=True)
        return {"success": False, "retry": f"Invalid automation config: {err}"}

    automations = [config]
    automation_file_path = os.path.join(hass.config.config_dir, AUTOMATION_CONFIG_PATH)

    current_automations: list[dict[str, Any]] = []
    try:
        async with aiofiles.open(automation_file_path, "r", encoding="utf-8") as handle:
            content = await handle.read()
            current_automations = yaml.safe_load(content) or []
    except FileNotFoundError:
        current_automations = []
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Failed to read automation file", exc_info=True)
        return {"success": False, "retry": f"Failed to read automations file: {err}"}

    try:
        async with aiofiles.open(
            automation_file_path,
            "a" if current_automations else "w",
            encoding="utf-8",
        ) as handle:
            raw_config = yaml.dump(automations, allow_unicode=True, sort_keys=False)
            if current_automations:
                await handle.write("\n" + raw_config)
            else:
                await handle.write(raw_config)
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Failed to write automation file", exc_info=True)
        return {"success": False, "retry": f"Failed to save automation: {err}"}

    try:
        await hass.services.async_call(automation.config.DOMAIN, "reload", blocking=True)
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Failed to reload automations", exc_info=True)
        return {"success": False, "retry": f"Automation saved but reload failed: {err}"}

    return "Success"


async def update_automation_from_yaml(
    hass: HomeAssistant,
    automation_id: str,
    automation_config_yaml: str,
) -> str:
    """Replace an existing automation in automations.yaml."""
    automation_config = yaml.safe_load(automation_config_yaml)
    config = {"id": automation_id}
    if isinstance(automation_config, list):
        config.update(automation_config[0])
    elif isinstance(automation_config, dict):
        config.update(automation_config)

    await _async_validate_config_item(hass, config, True, False)

    automation_file_path = os.path.join(hass.config.config_dir, AUTOMATION_CONFIG_PATH)
    try:
        async with aiofiles.open(automation_file_path, "r", encoding="utf-8") as handle:
            content = await handle.read()
            current_automations = yaml.safe_load(content) or []
    except FileNotFoundError:
        return f"Error: No automations found in {AUTOMATION_CONFIG_PATH}"

    automation_found = False
    for index, auto in enumerate(current_automations):
        if auto.get("id") == automation_id:
            current_automations[index] = config
            automation_found = True
            break

    if not automation_found:
        return f"Error: Automation with id '{automation_id}' not found"

    async with aiofiles.open(automation_file_path, "w", encoding="utf-8") as handle:
        raw_config = yaml.dump(current_automations, allow_unicode=True, sort_keys=False)
        await handle.write(raw_config)

    await hass.services.async_call(automation.config.DOMAIN, "reload", blocking=True)
    return "Success"


async def remove_automation_by_id(hass: HomeAssistant, automation_id: str) -> str:
    """Remove an automation by id from automations.yaml and the entity registry."""
    automation_file_path = os.path.join(hass.config.config_dir, AUTOMATION_CONFIG_PATH)
    try:
        async with aiofiles.open(automation_file_path, "r", encoding="utf-8") as handle:
            content = await handle.read()
            current_automations = yaml.safe_load(content) or []
    except FileNotFoundError:
        return f"Error: No automations found in {AUTOMATION_CONFIG_PATH}"

    automation_found = False
    updated_automations: list[dict[str, Any]] = []
    for auto in current_automations:
        if auto.get("id") == automation_id:
            automation_found = True
        else:
            updated_automations.append(auto)

    if not automation_found:
        return f"Error: Automation with id '{automation_id}' not found"

    async with aiofiles.open(automation_file_path, "w", encoding="utf-8") as handle:
        if updated_automations:
            raw_config = yaml.dump(updated_automations, allow_unicode=True, sort_keys=False)
            await handle.write(raw_config)
        else:
            await handle.write("[]\n")

    await hass.services.async_call(automation.config.DOMAIN, "reload", blocking=True)

    entity_reg = er.async_get(hass)
    entity_id = None
    for entity in entity_reg.entities.values():
        if entity.unique_id == automation_id and entity.platform == "automation":
            entity_id = entity.entity_id
            break
    if entity_id:
        entity_reg.async_remove(entity_id)

    return "Success"
