"""Tool handlers for person-related data."""
from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant


class PersonToolService:
    """Handle person-oriented tool calls."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get_all_persons(
        self,
        arguments: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return Home Assistant persons linked to a user/client id."""
        del arguments
        persons_with_user_id: list[dict[str, str]] = []

        for state in self.hass.states.async_all("person"):
            user_id = str(state.attributes.get("user_id") or "").strip()
            if not user_id:
                continue

            name = str(state.name or state.entity_id).strip()
            persons_with_user_id.append(
                {
                    "name": name,
                    "user_id": user_id[:8],
                }
            )

        return {"success": True, "persons": persons_with_user_id}
