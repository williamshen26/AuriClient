"""Music Assistant players and the speakers they play on: volume and mute go to the speaker first.

Music Assistant (an app, shown in Home Assistant through its integration) makes a media_player entity of its own for
each player: Auri's music_source. The device it plays on has its own entity: a satellite_speaker for a Voice PE, a
media_player for a soundbar. Home Assistant doesn't link the two: the Music Assistant entity sits on a device of its
own. Music Assistant knows the device's MAC address (device_info.identifiers, which it uses to link a device's
AirPlay, Cast and Home Assistant players into one player), and Home Assistant's device registry knows the speaker's
(with any MACs assigned to it with the assign_mac action). So the two are paired by MAC. A Music Assistant player
that plays through a Home Assistant entity (its protocol player's id is that entity's id) pairs with it directly.
Music Assistant's player ids can't be used: older versions built them from the MAC ("up20f83b0a1fc3"), newer ones
are random.

Why: volume and mute sent to a Music Assistant player only reach the speaker while it plays. On the user's hub
(2026-11-24) "把声音调到最大" went to the music_source while nothing played, and the satellite's volume stayed
where it was. So adjust_media_volume and set_media_mute act on the speaker first, then set the Music Assistant
player to match, whichever of the two the model chose.

This reads the Music Assistant integration's client (its config entry's runtime_data.mass), which isn't a public
API. Every read is guarded: if a Home Assistant or Music Assistant update changes it, pairing stops (volume goes where
the model sent it, as before), a warning is logged once, and the hourly hub summary says so (music_link
"unreadable"), which the staff page flags. tests/test_music_pairs.py checks the shapes this relies on against the
versions the tests install.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from ..cache import get_media_player_macs

_LOGGER = logging.getLogger(__name__)

MA_DOMAIN = "music_assistant"
# Music Assistant's player types (music_assistant_models PlayerType): only a "player" is one speaker (a group or a
# stereo pair plays on several; protocol, display, visualizer and light players aren't speakers Auri controls).
_PAIRED_TYPE = "player"
_DATA_WARNED = "hey_auri_client_music_pairs_warned"
_DATA_LOGGED = "hey_auri_client_music_pairs_logged"


class MusicAssistantUnreadable(Exception):
    """Music Assistant's integration doesn't look the way this module expects (an update changed it)."""


@dataclass(frozen=True, slots=True)
class MusicLink:
    """What the hub knows about its Music Assistant players.

    status: "ok" (read), "unreadable" (Music Assistant is set up but its players can't be read), or None (no
    Music Assistant). pairs: each paired Music Assistant entity and its speaker's entity (Home Assistant ids).
    """

    status: str | None
    players: int = 0
    pairs: dict[str, str] = field(default_factory=dict)


def is_music_assistant_entity(hass: HomeAssistant, entity_id: str) -> bool:
    """A media_player entity of Music Assistant's integration (by the entity registry: always right)."""
    entry = er.async_get(hass).async_get(entity_id)
    if entry is not None:
        return entry.platform == MA_DOMAIN
    state = hass.states.get(entity_id)
    attributes = state.attributes if state is not None else {}
    return attributes.get("app_id") == MA_DOMAIN or "mass_player_type" in attributes


def _music_assistant_entities(hass: HomeAssistant) -> dict[str, er.RegistryEntry]:
    """Music Assistant's media_player entities, by their player id (the entity's unique id)."""
    return {
        entry.unique_id: entry
        for entry in er.async_get(hass).entities.values()
        if entry.platform == MA_DOMAIN and entry.domain == "media_player" and entry.disabled_by is None
    }


def music_assistant_players(hass: HomeAssistant) -> dict[str, Any] | None:
    """Music Assistant's players by player id, from every loaded Music Assistant entry; None without one.

    Raises MusicAssistantUnreadable when an entry is loaded but its client or players aren't where they were.
    """
    entries = [entry for entry in hass.config_entries.async_entries(MA_DOMAIN) if entry.state is ConfigEntryState.LOADED]
    if not entries:
        return None
    players: dict[str, Any] = {}
    for entry in entries:
        mass = getattr(getattr(entry, "runtime_data", None), "mass", None)
        controller = getattr(mass, "players", None)
        if controller is None or not callable(getattr(controller, "get", None)):
            raise MusicAssistantUnreadable("its config entry has no client with players (runtime_data.mass.players)")
        try:
            for player in controller:
                players[str(player.player_id)] = player
        except (AttributeError, TypeError) as err:
            raise MusicAssistantUnreadable(f"its players can't be listed: {err}") from err
    return players


def player_macs(player: Any) -> list[str]:
    """The MAC addresses Music Assistant knows for a player (formatted like Home Assistant's).

    Raises MusicAssistantUnreadable when the player has no device_info identifiers at all.
    """
    identifiers = getattr(getattr(player, "device_info", None), "identifiers", None)
    if not isinstance(identifiers, dict):
        raise MusicAssistantUnreadable("a player has no device_info.identifiers")
    mac = identifiers.get("mac_address")
    return [dr.format_mac(str(mac))] if mac else []


def _protocol_entity_ids(player: Any) -> list[str]:
    """Home Assistant entity ids among a player's output protocols (a Home Assistant player's id is its entity id)."""
    ids = [str(getattr(protocol, "output_protocol_id", "") or "") for protocol in getattr(player, "output_protocols", None) or []]
    ids.append(str(getattr(player, "player_id", "") or ""))
    return [entity_id for entity_id in ids if entity_id.startswith("media_player.")]


def _speakers_by_mac(hass: HomeAssistant) -> dict[str, list[str]]:
    """Every other media_player entity, by each MAC its device has (and the ones assigned with assign_mac)."""
    devices = dr.async_get(hass)
    by_mac: dict[str, list[str]] = {}
    for entry in er.async_get(hass).entities.values():
        if entry.domain != "media_player" or entry.platform == MA_DOMAIN or entry.disabled_by is not None:
            continue
        device = devices.async_get(entry.device_id) if entry.device_id else None
        macs = [value for kind, value in (device.connections if device else ()) if kind == dr.CONNECTION_NETWORK_MAC]
        for mac in dict.fromkeys(dr.format_mac(value) for value in [*macs, *get_media_player_macs(entry.entity_id)]):
            by_mac.setdefault(mac, []).append(entry.entity_id)
    return by_mac


def _one(candidates: Iterable[str]) -> str | None:
    found = list(dict.fromkeys(candidates))
    return found[0] if len(found) == 1 else None


def music_link(hass: HomeAssistant) -> MusicLink:
    """The hub's Music Assistant players and the speakers they're paired with. Never raises."""
    try:
        players = music_assistant_players(hass)
        if players is None:
            return MusicLink(None)
        by_mac = _speakers_by_mac(hass)
        registry = er.async_get(hass)
        pairs: dict[str, str] = {}
        entities = _music_assistant_entities(hass)
        for player_id, entry in entities.items():
            player = players.get(player_id)
            kind = getattr(player, "type", None)
            if player is None or str(getattr(kind, "value", kind)) != _PAIRED_TYPE:
                continue
            speaker = _one(entity_id for mac in player_macs(player) for entity_id in by_mac.get(mac, []))
            if speaker is None:
                direct = [
                    entity_id for entity_id in _protocol_entity_ids(player)
                    if (found := registry.async_get(entity_id)) is not None and found.platform != MA_DOMAIN
                ]
                speaker = _one(direct)
            if speaker is not None:
                pairs[entry.entity_id] = speaker
    except MusicAssistantUnreadable as err:
        if not hass.data.get(_DATA_WARNED):
            hass.data[_DATA_WARNED] = True
            _LOGGER.warning(
                "Hey Auri Client can't read Music Assistant's players (%s): volume and mute go only to the entity "
                "Auri chooses. A Home Assistant or Music Assistant update may have changed this",
                err,
            )
        return MusicLink("unreadable", len(_music_assistant_entities(hass)))
    except Exception:  # noqa: BLE001 (never break a volume command over this)
        _LOGGER.exception("Pairing Music Assistant players with their speakers failed")
        return MusicLink("unreadable", len(_music_assistant_entities(hass)))
    if hass.data.get(_DATA_LOGGED) != pairs:
        hass.data[_DATA_LOGGED] = dict(pairs)
        _LOGGER.info(
            "Music Assistant players and their speakers: %s",
            ", ".join(f"{source} -> {speaker}" for source, speaker in sorted(pairs.items())) or "none paired",
        )
    return MusicLink("ok", len(entities), pairs)


def paired(hass: HomeAssistant, entity_id: str) -> tuple[str, list[str]] | None:
    """(the speaker, its Music Assistant entities) when entity_id is either side of a pair, else None."""
    pairs = music_link(hass).pairs
    speaker = pairs.get(entity_id, entity_id)
    sources = sorted(source for source, paired_speaker in pairs.items() if paired_speaker == speaker)
    return (speaker, sources) if sources else None


def music_link_summary(hass: HomeAssistant) -> dict[str, Any]:
    """For the hourly hub summary: music_link ("ok", "unreadable" or None without Music Assistant) and music_pairs."""
    link = music_link(hass)
    return {"music_link": link.status, "music_pairs": len(link.pairs) if link.status == "ok" else None}
