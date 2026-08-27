"""Service handler for manually assigning media_player MAC addresses."""
from __future__ import annotations

import re

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.storage import Store

from ...cache import dump_media_player_macs, get_media_player_macs, upsert_media_player_macs
from ...const import MEDIA_PLAYER_MACS_STORAGE_KEY, MEDIA_PLAYER_MACS_STORAGE_VERSION

_MAC_RE = re.compile(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$")


def _normalize_mac(raw_mac: str) -> str:
    normalized = dr.format_mac(raw_mac)
    if not _MAC_RE.match(normalized):
        raise HomeAssistantError(f"'{raw_mac}' is not a valid MAC address")
    return normalized


async def assign_mac_native(
    hass: HomeAssistant,
    entity_id: str,
    macs: list[str],
) -> dict[str, object]:
    """Manually assign one or more MAC addresses to a media_player entity.

    Covers devices with multiple NICs (e.g. Ethernet + Wi-Fi) where HA's
    device registry only ever learned one MAC -- often not the one the
    device is actually listening for wake_on_lan magic packets on.
    """
    if not entity_id.startswith("media_player."):
        raise HomeAssistantError(f"{entity_id} is not a media_player entity")
    if not macs:
        raise HomeAssistantError("At least one MAC address is required")

    normalized_macs = [_normalize_mac(mac) for mac in macs]

    merged = list(dict.fromkeys([*get_media_player_macs(entity_id), *normalized_macs]))

    if upsert_media_player_macs(entity_id, merged):
        store: Store[dict[str, list[str]]] = Store(
            hass,
            MEDIA_PLAYER_MACS_STORAGE_VERSION,
            MEDIA_PLAYER_MACS_STORAGE_KEY,
        )
        await store.async_save(dump_media_player_macs())

    return {"success": True, "entity_id": entity_id, "macs": merged}
