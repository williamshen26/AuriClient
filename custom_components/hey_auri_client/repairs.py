"""The fix for the Repairs notice about logins that look like a service's (memory/logins.py)."""
from __future__ import annotations

from typing import Any

from homeassistant.components.repairs import RepairsFlow, RepairsFlowResult
from homeassistant.core import HomeAssistant

from .memory.logins import answers, async_logins, async_update_logins_issue, logins_form, names_text


class ServiceLoginsRepairFlow(RepairsFlow):
    """The Who uses each login question, with the logins that look like a service's set to Not a person."""

    def __init__(self, entry_id: str) -> None:
        self._entry_id = entry_id

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> RepairsFlowResult:
        # A fix flow starts with the issue's id as its input, so the question is a step of its own.
        return await self.async_step_logins()

    async def async_step_logins(self, user_input: dict[str, Any] | None = None) -> RepairsFlowResult:
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry is None:
            return self.async_abort(reason="not_set_up")
        logins = await async_logins(self.hass)
        if user_input is not None:
            self.hass.config_entries.async_update_entry(entry, options=answers(logins, user_input, entry.options))
            return self.async_create_entry(data={})

        schema, preselected = logins_form(self.hass, logins, entry.options)
        if not preselected:  # answered meanwhile, under Who uses each login
            await async_update_logins_issue(self.hass, entry)
            return self.async_abort(reason="answered")
        return self.async_show_form(
            step_id="logins", data_schema=schema, description_placeholders={"logins": names_text(preselected)}
        )


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, Any] | None
) -> RepairsFlow:
    """The fix for one of the integration's notices: there's one kind, about logins."""
    return ServiceLoginsRepairFlow(str((data or {}).get("entry_id") or ""))
