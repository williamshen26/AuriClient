"""The fixes for the integration's Repairs notices: logins that look like a service's (memory/logins.py),
and names that may be one pet or person spelled two ways (memory/names.py)."""
from __future__ import annotations

from typing import Any

from homeassistant.components.repairs import RepairsFlow, RepairsFlowResult
from homeassistant.core import HomeAssistant

from .const import DATA_MEMORY, DOMAIN
from .memory.logins import answers, async_logins, async_update_logins_issue, logins_form, names_text
from .memory.names import (
    SIMILAR_NAMES_ISSUE,
    NamePair,
    async_apply_name_answers,
    async_name_pairs,
    async_update_names_issue,
    names_form,
    names_text as pairs_text,
)


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


class SimilarNamesRepairFlow(RepairsFlow):
    """For each pair of names a letter or so apart: the same pet or person (spelled which way), or two."""

    def __init__(self, entry_id: str) -> None:
        self._entry_id = entry_id
        self._pairs: list[NamePair] = []

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> RepairsFlowResult:
        return await self.async_step_names()

    async def async_step_names(self, user_input: dict[str, Any] | None = None) -> RepairsFlowResult:
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        runtime = (self.hass.data.get(DOMAIN, {}).get(self._entry_id) or {}).get(DATA_MEMORY)
        if entry is None or runtime is None:
            return self.async_abort(reason="not_set_up")
        if user_input is not None:
            # The pairs the form showed, so an answer can't land on a pair it wasn't about.
            await async_apply_name_answers(runtime, self._pairs, user_input)
            await async_update_names_issue(self.hass, entry, runtime)
            return self.async_create_entry(data={})

        self._pairs = await async_name_pairs(runtime)
        if not self._pairs:
            await async_update_names_issue(self.hass, entry, runtime)
            return self.async_abort(reason="answered")
        return self.async_show_form(
            step_id="names", data_schema=names_form(self._pairs), description_placeholders={"names": pairs_text(self._pairs)}
        )


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, Any] | None
) -> RepairsFlow:
    """The fix for one of the integration's notices, by the notice's id."""
    entry_id = str((data or {}).get("entry_id") or "")
    if issue_id.startswith(SIMILAR_NAMES_ISSUE):
        return SimilarNamesRepairFlow(entry_id)
    return ServiceLoginsRepairFlow(entry_id)
