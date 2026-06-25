"""Sticky note service helpers and native operations."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store

from ...exceptions import ToolExecutionError
from ...const import DOMAIN, STICKY_NOTE_UNIQUE_ID_PREFIX
from ...entities.sticky_note import (
    AuriStickyNoteEntity,
    generate_sticky_note_id,
)

STICKY_NOTE_STORAGE_VERSION = 1
STICKY_NOTE_STORAGE_KEY = f"{DOMAIN}_sticky_notes"
MAX_STICKY_NOTE_CONTENT_LENGTH = 8000


def _get_runtime(hass: HomeAssistant) -> dict[str, Any]:
    """Return the integration runtime state bucket."""
    runtime = hass.data.setdefault(DOMAIN, {}).setdefault("runtime", {})
    runtime.setdefault("timers", {})
    runtime.setdefault("sticky_notes", {})
    runtime.setdefault("async_add_entities", None)
    return runtime


def _get_store(hass: HomeAssistant) -> Store[dict[str, dict[str, str]]]:
    """Return sticky note storage helper."""
    runtime = _get_runtime(hass)
    return runtime.setdefault(
        "sticky_notes_store",
        Store(hass, STICKY_NOTE_STORAGE_VERSION, STICKY_NOTE_STORAGE_KEY),
    )


async def async_restore_sticky_notes(hass: HomeAssistant) -> None:
    """Restore sticky notes from persistent storage and add entities."""
    runtime = _get_runtime(hass)
    if runtime.get("sticky_notes_restored"):
        return

    add_entities: Callable | None = runtime.get("async_add_entities")
    if add_entities is None:
        return

    store = _get_store(hass)
    persisted = await store.async_load() or {}
    entities: list[AuriStickyNoteEntity] = []

    persisted_note_ids: set[str] = set()
    if isinstance(persisted, dict):
        for note_id, payload in persisted.items():
            if not isinstance(note_id, str) or not isinstance(payload, dict):
                continue

            title = payload.get("title")
            markdown = payload.get("markdown")
            created_at = payload.get("created_at")
            if not isinstance(title, str) or not isinstance(markdown, str):
                continue
            if len(markdown) > MAX_STICKY_NOTE_CONTENT_LENGTH:
                continue

            persisted_note_ids.add(note_id)

            entity = AuriStickyNoteEntity(
                note_id=note_id,
                title=title,
                markdown=markdown,
                created_at=created_at if isinstance(created_at, str) else None,
            )
            runtime["sticky_notes"][note_id] = entity
            entities.append(entity)

    if entities:
        add_entities(entities, True)

    # Remove orphaned sticky-note registry entries so deleted notes do not reappear
    # as restored unavailable entities after restart.
    entity_registry = er.async_get(hass)
    for registry_entry in list(entity_registry.entities.values()):
        unique_id = registry_entry.unique_id
        if (
            registry_entry.domain == "sensor"
            and isinstance(unique_id, str)
            and unique_id.startswith(STICKY_NOTE_UNIQUE_ID_PREFIX)
        ):
            note_id = unique_id.removeprefix(STICKY_NOTE_UNIQUE_ID_PREFIX)
            if note_id not in persisted_note_ids:
                entity_registry.async_remove(registry_entry.entity_id)

    runtime["sticky_notes_restored"] = True


def _serialize_sticky_notes(hass: HomeAssistant) -> dict[str, dict[str, str]]:
    """Serialize all runtime sticky notes for persistence."""
    runtime = _get_runtime(hass)
    notes: dict[str, dict[str, str]] = {}
    for note_id, note in runtime["sticky_notes"].items():
        notes[note_id] = note.to_record()
    return notes


async def create_auri_sticky_note_native(
    hass: HomeAssistant,
    markdown: str,
    title: str | None = None,
    note_id: str | None = None,
) -> dict[str, Any]:
    """Create a sticky note and persist it."""
    runtime = _get_runtime(hass)
    add_entities: Callable | None = runtime.get("async_add_entities")
    if add_entities is None:
        raise HomeAssistantError("AURI sensor platform is not ready")

    normalized_markdown = str(markdown)
    if not normalized_markdown.strip():
        raise ValueError("markdown is required")
    if len(normalized_markdown) > MAX_STICKY_NOTE_CONTENT_LENGTH:
        raise ValueError(
            f"markdown exceeds max length {MAX_STICKY_NOTE_CONTENT_LENGTH} characters"
        )

    resolved_note_id = str(note_id).strip() if note_id is not None else ""
    if not resolved_note_id:
        resolved_note_id = generate_sticky_note_id()

    if resolved_note_id in runtime["sticky_notes"]:
        raise ValueError(f"note_id already exists: {resolved_note_id}")

    note_title = str(title).strip() if title is not None else ""
    if not note_title:
        note_title = f"Sticky Note {resolved_note_id}"

    note = AuriStickyNoteEntity(
        note_id=resolved_note_id,
        title=note_title,
        markdown=normalized_markdown,
    )
    runtime["sticky_notes"][resolved_note_id] = note
    add_entities([note], True)

    await _get_store(hass).async_save(_serialize_sticky_notes(hass))

    return {
        "note_id": resolved_note_id,
        "entity_id": note.entity_id,
        "title": note_title,
        "markdown_length": len(normalized_markdown),
    }


async def delete_auri_sticky_note_native(
    hass: HomeAssistant,
    note_id: str,
) -> dict[str, Any]:
    """Delete a sticky note by note_id and persist the change."""
    runtime = _get_runtime(hass)

    resolved_note_id = str(note_id).strip()
    if not resolved_note_id:
        raise ValueError("note_id is required")

    note = runtime["sticky_notes"].pop(resolved_note_id, None)
    if note is None:
        raise ValueError(f"note_id not found: {resolved_note_id}")

    entity_id = note.entity_id
    await note.async_remove()

    if entity_id:
        entity_registry = er.async_get(hass)
        if entity_registry.async_get(entity_id) is not None:
            entity_registry.async_remove(entity_id)

    await _get_store(hass).async_save(_serialize_sticky_notes(hass))

    return {
        "note_id": resolved_note_id,
        "deleted": True,
    }


async def get_auri_sticky_notes_native(hass: HomeAssistant) -> dict[str, Any]:
    """Return all currently active sticky notes."""
    runtime = _get_runtime(hass)
    notes = []

    for note_id, note in runtime["sticky_notes"].items():
        if not note.available:
            continue

        attributes = note.extra_state_attributes
        notes.append(
            {
                "note_id": note_id,
                "entity_id": note.entity_id,
                "title": attributes.get("title"),
                "markdown": attributes.get("markdown"),
                "created_at": attributes.get("created_at"),
            }
        )

    return {"sticky_notes": notes}


class StickyNoteToolService:
    """Handle sticky-note-oriented tool calls."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def create_sticky_note(self, arguments: dict[str, Any]) -> dict[str, Any]:
        title = str(arguments.get("title", "")).strip()
        markdown = str(arguments.get("markdown", "")).strip()

        if not title:
            return {"retry": "title is required"}
        if not markdown:
            return {"retry": "markdown is required"}
        if len(markdown) > MAX_STICKY_NOTE_CONTENT_LENGTH:
            return {
                "retry": f"markdown exceeds max length {MAX_STICKY_NOTE_CONTENT_LENGTH} characters"
            }

        try:
            return await create_auri_sticky_note_native(
                self.hass,
                title=title,
                markdown=markdown,
            )
        except ValueError as err:
            return {"retry": str(err)}
        except HomeAssistantError as err:
            raise ToolExecutionError(str(err)) from err


__all__ = [
    "MAX_STICKY_NOTE_CONTENT_LENGTH",
    "StickyNoteToolService",
    "async_restore_sticky_notes",
    "create_auri_sticky_note_native",
    "delete_auri_sticky_note_native",
    "get_auri_sticky_notes_native",
]
