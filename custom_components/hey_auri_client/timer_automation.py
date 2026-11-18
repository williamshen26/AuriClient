"""The Repairs notice for the automation that rang Auri's timers before 0.2.8, and its fix (deleting it).

Hubs had an automation (the master hub's "DO NOT DELETE" one) triggered by
hey_auri_client.auri_timer_finished, to ring the satellite and notify every phone. Timers now ring
on their own device (timers.py) and that event is no longer fired, so the automation does nothing.
Only automations made in Home Assistant's editor (automations.yaml) are found, since only those
can be deleted from here; the notice goes when none is left.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from homeassistant.config import AUTOMATION_CONFIG_PATH
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.util.yaml import load_yaml

from .const import DOMAIN, OLD_TIMER_FINISHED_EVENT

_LOGGER = logging.getLogger(__name__)

OLD_TIMER_AUTOMATION_ISSUE = "old_timer_automation"
# Home Assistant's automation integration fires it after the editor saves or deletes one
# (homeassistant.components.automation.EVENT_AUTOMATION_RELOADED, without importing it).
EVENT_AUTOMATION_RELOADED = "automation_reloaded"


def _triggers(config: dict[str, Any]) -> list[Any]:
    triggers = config.get("triggers", config.get("trigger")) or []
    return triggers if isinstance(triggers, list) else [triggers]


def is_old_timer_automation(config: Any) -> bool:
    """An automation started by the event clients before 0.2.8 fired when a timer finished."""
    if not isinstance(config, dict):
        return False
    for trigger in _triggers(config):
        if not isinstance(trigger, dict) or trigger.get("trigger", trigger.get("platform")) != "event":
            continue
        event_type = trigger.get("event_type")
        if OLD_TIMER_FINISHED_EVENT in (event_type if isinstance(event_type, list) else [event_type]):
            return True
    return False


def _read_automations(path: str) -> list[Any]:
    if not os.path.isfile(path):
        return []
    content = load_yaml(path)
    return content if isinstance(content, list) else []


async def async_old_timer_automations(hass: HomeAssistant) -> list[dict[str, str]]:
    """The editor's automations triggered by the old event, as {id, name}."""
    try:
        automations = await hass.async_add_executor_job(_read_automations, hass.config.path(AUTOMATION_CONFIG_PATH))
    except (HomeAssistantError, OSError) as err:
        _LOGGER.debug("Couldn't read the automations to look for the old timer one: %s", err)
        return []
    return [
        {"id": str(config["id"]), "name": str(config.get("alias") or config["id"])}
        for config in automations
        if is_old_timer_automation(config) and config.get("id")
    ]


async def async_update_old_timer_automation_issue(hass: HomeAssistant) -> None:
    """Raise the notice while such an automation exists; remove it once none does."""
    found = await async_old_timer_automations(hass)
    if not found:
        ir.async_delete_issue(hass, DOMAIN, OLD_TIMER_AUTOMATION_ISSUE)
        return
    ir.async_create_issue(
        hass,
        DOMAIN,
        OLD_TIMER_AUTOMATION_ISSUE,
        is_fixable=True,
        is_persistent=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=OLD_TIMER_AUTOMATION_ISSUE,
        translation_placeholders={"automations": ", ".join(f"“{item['name']}”" for item in found)},
    )


async def async_delete_old_timer_automations(hass: HomeAssistant) -> list[str]:
    """Delete them, as the automation editor does (the same code as Auri's remove_automation tool)."""
    from .conversation.custom_services.automation_services import remove_automation_by_id  # noqa: PLC0415

    deleted = []
    for item in await async_old_timer_automations(hass):
        try:
            result = await remove_automation_by_id(hass, item["id"])
        except Exception as err:  # noqa: BLE001 (a file the editor didn't write, say)
            result = {"error": str(err)}
        if result.get("success"):
            deleted.append(item["name"])
        else:
            _LOGGER.warning("Couldn't delete the automation %s: %s", item["name"], result)
    await async_update_old_timer_automation_issue(hass)
    return deleted
