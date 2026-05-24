"""Sticky note entity for dashboard markdown display."""

from __future__ import annotations

from datetime import UTC, datetime
import uuid

from homeassistant.components.sensor import SensorEntity

from ..const import ATTR_ENTITY_ID, STICKY_NOTE_UNIQUE_ID_PREFIX


class AuriStickyNoteEntity(SensorEntity):
    """Dynamic AURI sticky note entity."""

    _attr_should_poll = False
    _attr_icon = "mdi:note-text-outline"
    _attr_has_entity_name = True

    def __init__(
        self,
        note_id: str,
        title: str,
        markdown: str,
        created_at: str | None = None,
    ) -> None:
        self.note_id = note_id
        self._title = title
        self._markdown = markdown
        self._created_at = created_at or datetime.now(UTC).isoformat()

        self._attr_name = f"AURI Sticky Note {note_id}"
        self._attr_unique_id = f"{STICKY_NOTE_UNIQUE_ID_PREFIX}{note_id}"

    @property
    def native_value(self) -> str:
        """Return a short state value for the note."""
        return self._title

    @property
    def extra_state_attributes(self) -> dict:
        """Return note content and metadata as attributes."""
        return {
            "auri_entity_type": "sticky_note",
            "note_id": self.note_id,
            "title": self._title,
            "markdown": self._markdown,
            "created_at": self._created_at,
            ATTR_ENTITY_ID: self.entity_id,
        }

    def to_record(self) -> dict[str, str]:
        """Serialize note for storage."""
        return {
            "note_id": self.note_id,
            "title": self._title,
            "markdown": self._markdown,
            "created_at": self._created_at,
        }


def generate_sticky_note_id() -> str:
    """Generate a short sticky note id."""
    return uuid.uuid4().hex[:8]


__all__ = [
    "AuriStickyNoteEntity",
    "generate_sticky_note_id",
]
