"""Thin integration timer services."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv

from ..exceptions import ToolExecutionError
from ..const import (
    ATTR_DURATION,
    ATTR_ENTITY_ID,
    ATTR_REMAINING,
    ATTR_SATELLITE_SPEAKER,
    ATTR_TIMER_ID,
    DOMAIN,
)
from ..timer_entity import AuriTimerEntity, generate_timer_id


def _normalize_duration_to_seconds(value) -> int:
    """Convert service duration input into seconds."""
    if isinstance(value, int):
        seconds = value
    elif isinstance(value, float):
        seconds = int(value)
    elif isinstance(value, str):
        stripped = value.strip()
        try:
            seconds = int(float(stripped))
        except ValueError:
            seconds = int(cv.time_period(stripped).total_seconds())
    else:
        seconds = int(value.total_seconds())

    if seconds <= 0:
        raise ValueError("Duration must be greater than 0")

    return seconds


def _get_runtime(hass: HomeAssistant) -> dict:
    """Return the integration runtime state bucket."""
    return hass.data.setdefault(DOMAIN, {}).setdefault(
        "runtime",
        {
            "timers": {},
            "async_add_entities": None,
        },
    )


async def start_auri_timer_native(
    hass: HomeAssistant,
    duration: Any,
    satellite_speaker: str | None = None,
) -> dict[str, Any]:
    """Start an AURI timer via shared native logic."""
    runtime = _get_runtime(hass)
    add_entities = runtime.get("async_add_entities")
    if add_entities is None:
        raise HomeAssistantError("AURI timer platform is not ready")

    duration_seconds = _normalize_duration_to_seconds(duration)
    timer_id = generate_timer_id()

    timer = AuriTimerEntity(
        hass=hass,
        timer_id=timer_id,
        duration_seconds=duration_seconds,
        satellite_speaker=satellite_speaker,
    )
    runtime["timers"][timer_id] = timer

    add_entities([timer], True)
    hass.async_create_task(timer.async_start())

    return {
        ATTR_TIMER_ID: timer_id,
        ATTR_ENTITY_ID: timer.entity_id,
        ATTR_DURATION: duration_seconds,
        ATTR_REMAINING: timer.get_remaining_seconds(),
        ATTR_SATELLITE_SPEAKER: satellite_speaker,
    }


async def get_auri_timers_native(hass: HomeAssistant) -> dict[str, Any]:
    """Return active AURI timers via shared native logic."""
    runtime = _get_runtime(hass)
    timers = []

    for timer_id, timer in runtime["timers"].items():
        if not timer.available:
            continue

        remaining = timer.get_remaining_seconds()
        if remaining <= 0:
            continue

        timers.append(
            {
                ATTR_TIMER_ID: timer_id,
                ATTR_ENTITY_ID: timer.entity_id,
                ATTR_DURATION: timer.extra_state_attributes[ATTR_DURATION],
                ATTR_REMAINING: remaining,
                ATTR_SATELLITE_SPEAKER: timer.extra_state_attributes.get(
                    ATTR_SATELLITE_SPEAKER
                ),
            }
        )

    return {"timers": timers}


class TimerToolService:
    """Handle timer-oriented tool calls."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def start_timer(self, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            return await start_auri_timer_native(
                self.hass,
                duration=arguments.get("duration"),
                satellite_speaker=arguments.get("satellite_speaker"),
            )
        except (vol.error.MultipleInvalid, ValueError) as err:
            return {"retry": str(err)}
        except HomeAssistantError as err:
            raise ToolExecutionError(str(err)) from err

    async def list_timers(
        self,
        arguments: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del arguments
        try:
            return await get_auri_timers_native(self.hass)
        except HomeAssistantError as err:
            raise ToolExecutionError(str(err)) from err
