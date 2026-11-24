"""Music Assistant players and the speakers they play on (conversation/music_pairs.py), and the guards for it.

Two parts:
- Contract tests: music_pairs.py reads Music Assistant's integration from the inside (its config entry's
  runtime_data.mass, each player's device_info.identifiers). None of that is a public API, so these check it against
  the Home Assistant and Music Assistant versions the tests install (requirements_test.txt). When a version bump
  breaks one, the test names what moved; on hubs, the hourly summary reports music_link "unreadable" (security.py).
- Behaviour: pairs by MAC and by a Home Assistant protocol player; volume and mute go to the speaker first, then
  the Music Assistant player to match, whichever the model chose; the command goes as before when there's no pair,
  the speaker can't take it, or Music Assistant can't be read.
"""
from __future__ import annotations

import dataclasses
import logging
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("homeassistant")
# Not skipped when missing: these guard the shapes music_pairs.py reads, so the libraries must be installed
# (requirements_test.txt pins the Music Assistant client Home Assistant itself requires).
import music_assistant_models.enums as ma_enums  # noqa: E402
from music_assistant_models.player import DeviceInfo, OutputProtocol, Player  # noqa: E402
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service  # noqa: E402

from homeassistant.components.media_player import MediaPlayerEntityFeature  # noqa: E402
from homeassistant.config_entries import ConfigEntryState  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.helpers import device_registry as dr, entity_registry as er  # noqa: E402

from custom_components.hey_auri_client import cache  # noqa: E402
from custom_components.hey_auri_client.conversation import music_pairs  # noqa: E402
from custom_components.hey_auri_client.conversation.custom_services.media_player_services import (  # noqa: E402
    MediaPlayerToolService,
)
from custom_components.hey_auri_client.conversation.helpers import transform_ha_entity_id_to_auri_entity_id  # noqa: E402

VOLUME = int(MediaPlayerEntityFeature.VOLUME_SET | MediaPlayerEntityFeature.VOLUME_MUTE)
VOICE_MAC = "20:F8:3B:0A:1F:C3"


# ── contract: what music_pairs.py reads ───────────────────────────────────────────────────────────

def test_the_music_assistant_integration_keeps_its_client_in_runtime_data_mass() -> None:
    from homeassistant.components.music_assistant import MusicAssistantEntryData  # pylint: disable=import-outside-toplevel

    assert "mass" in {item.name for item in dataclasses.fields(MusicAssistantEntryData)}, (
        "Music Assistant's integration no longer keeps its client at runtime_data.mass: update music_pairs.py"
    )


def test_a_music_assistant_entitys_unique_id_is_its_player_id() -> None:
    from homeassistant.components.music_assistant.entity import MusicAssistantEntity  # pylint: disable=import-outside-toplevel

    entity = SimpleNamespace(player_id="pl0a1b2c3d4e5f")
    assert MusicAssistantEntity.unique_id.fget(entity) == "pl0a1b2c3d4e5f", (
        "A Music Assistant entity's unique id is no longer its player id: music_pairs.py finds players by it"
    )


def test_the_music_assistant_client_lists_and_finds_players() -> None:
    from music_assistant_client.players import Players  # pylint: disable=import-outside-toplevel

    assert callable(getattr(Players, "get", None)) and callable(getattr(Players, "__iter__", None))


def test_a_players_mac_is_in_its_device_info_identifiers() -> None:
    player = _player("pl1", mac=VOICE_MAC)
    assert player.device_info.identifiers[ma_enums.IdentifierType.MAC_ADDRESS] == VOICE_MAC
    assert music_pairs.player_macs(player) == ["20:f8:3b:0a:1f:c3"]
    assert ma_enums.PlayerType.PLAYER.value == "player"
    assert {"output_protocol_id", "protocol_domain"} <= {item.name for item in dataclasses.fields(OutputProtocol)}


def test_music_assistant_entities_say_what_kind_of_player_they_are() -> None:
    from homeassistant.components.music_assistant.const import ATTR_MASS_PLAYER_TYPE  # pylint: disable=import-outside-toplevel

    # is_music_assistant_entity's fallback when an entity isn't in the registry.
    assert ATTR_MASS_PLAYER_TYPE == "mass_player_type"


# ── the home ─────────────────────────────────────────────────────────────────────────────────────

def _player(player_id: str, *, mac: str | None = None, kind: str = "player", protocols: tuple[str, ...] = ()) -> Player:
    info = DeviceInfo(model="Model", manufacturer="Maker")
    if mac:
        info.mac_address = mac
    return Player(
        player_id=player_id,
        provider="universal_player",
        type=ma_enums.PlayerType(kind),
        name=player_id,
        available=True,
        device_info=info,
        output_protocols=[OutputProtocol(output_protocol_id=pid, name=pid, protocol_domain="hass") for pid in protocols],
    )


class FakePlayers:
    """Music Assistant's client's players (music_assistant_client.players.Players): iterated and found by id."""

    def __init__(self, players: list[Player]) -> None:
        self._players = {player.player_id: player for player in players}

    def __iter__(self):
        return iter(self._players.values())

    def get(self, player_id: str) -> Player | None:
        return self._players.get(player_id)


def _entity(hass: HomeAssistant, entry: MockConfigEntry, object_id: str, *, unique_id: str,
            macs: tuple[str, ...] = (), state: str = "idle", volume: float = 0.5) -> str:
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        connections={(dr.CONNECTION_NETWORK_MAC, mac) for mac in macs},
        identifiers={(entry.domain, unique_id)},
        name=object_id,
    )
    registered = er.async_get(hass).async_get_or_create(
        "media_player", entry.domain, unique_id, config_entry=entry, device_id=device.id, suggested_object_id=object_id
    )
    hass.states.async_set(registered.entity_id, state, {"volume_level": volume, "supported_features": VOLUME})
    return registered.entity_id


@pytest.fixture
def home(hass: HomeAssistant):
    """A Voice PE and a soundbar Music Assistant plays on, an AirPlay player with no HA entity, a group, and a
    Cast speaker Music Assistant plays on through Home Assistant (no MAC)."""
    entries = {domain: MockConfigEntry(domain=domain) for domain in ("esphome", "yamaha_musiccast", "cast")}
    for entry in entries.values():
        entry.add_to_hass(hass)
    ma_entry = MockConfigEntry(domain="music_assistant", state=ConfigEntryState.LOADED)
    ma_entry.add_to_hass(hass)

    ids = SimpleNamespace(
        voice_speaker=_entity(hass, entries["esphome"], "voice_speaker", unique_id="20:F8:3B:0A:1F:C3/media", macs=(VOICE_MAC,), volume=0.3),
        soundbar=_entity(hass, entries["yamaha_musiccast"], "living_room_speaker", unique_id="AC44F2435A2D_main",
                         macs=("34:15:13:b5:63:6e", "34:15:13:b5:63:6d", "ac:44:f2:43:5a:2d")),
        cast=_entity(hass, entries["cast"], "kitchen_speaker", unique_id="cast-uuid"),
        voice=_entity(hass, ma_entry, "voice", unique_id="pl0a1b2c3d4e5f", volume=0.8),
        living_room=_entity(hass, ma_entry, "living_room_music", unique_id="up341513b5636d"),
        airplay=_entity(hass, ma_entry, "macbook", unique_id="upa4ab40fb5a60"),
        group=_entity(hass, ma_entry, "everywhere", unique_id="syncgroup_1"),
        kitchen=_entity(hass, ma_entry, "kitchen_music", unique_id="pl9f8e7d6c5b4a"),
    )
    players = [
        _player("pl0a1b2c3d4e5f", mac=VOICE_MAC),
        _player("up341513b5636d", mac="34:15:13:B5:63:6D"),
        _player("upa4ab40fb5a60", mac="a4:ab:40:fb:5a:60"),
        _player("syncgroup_1", mac=VOICE_MAC, kind="group"),
        _player("pl9f8e7d6c5b4a", protocols=(ids.cast,)),
    ]
    ma_entry.runtime_data = SimpleNamespace(mass=SimpleNamespace(players=FakePlayers(players)))
    calls = SimpleNamespace(volume=async_mock_service(hass, "media_player", "volume_set"),
                            mute=async_mock_service(hass, "media_player", "volume_mute"))
    hass.data.pop("hey_auri_client_music_pairs_warned", None)
    yield SimpleNamespace(ids=ids, ma_entry=ma_entry, calls=calls, tools=MediaPlayerToolService(hass))
    cache.set_media_player_macs({})


def _sent(calls: list) -> list[tuple[str, Any]]:
    return [(call.data["entity_id"], call.data.get("volume_level", call.data.get("is_volume_muted"))) for call in calls]


# ── pairing ──────────────────────────────────────────────────────────────────────────────────────

async def test_players_pair_with_their_speakers_by_mac_and_by_a_home_assistant_protocol(hass: HomeAssistant, home) -> None:
    link = music_pairs.music_link(hass)
    assert link.status == "ok" and link.players == 5
    assert link.pairs == {
        home.ids.voice: home.ids.voice_speaker,
        home.ids.living_room: home.ids.soundbar,  # one of the soundbar's three MACs
        home.ids.kitchen: home.ids.cast,  # no MAC: through its Home Assistant protocol player
    }  # not the AirPlay player (no HA entity with its MAC), not the group
    assert music_pairs.music_link_summary(hass) == {"music_link": "ok", "music_pairs": 3}


async def test_a_mac_assigned_by_hand_pairs_a_device_with_two_networks(hass: HomeAssistant, home) -> None:
    cache.upsert_media_player_macs(home.ids.soundbar, ["a4:ab:40:fb:5a:60"])
    assert music_pairs.music_link(hass).pairs[home.ids.airplay] == home.ids.soundbar


async def test_music_assistant_entities_are_music_sources_whatever_their_app_id(hass: HomeAssistant, home) -> None:
    # An idle or unavailable Music Assistant player has no app_id "music_assistant".
    assert transform_ha_entity_id_to_auri_entity_id(hass, home.ids.voice) == "music_source.voice"
    assert transform_ha_entity_id_to_auri_entity_id(hass, home.ids.soundbar) == home.ids.soundbar


# ── volume and mute ──────────────────────────────────────────────────────────────────────────────

async def test_volume_on_a_music_source_goes_to_its_speaker_first(hass: HomeAssistant, home) -> None:
    result = await home.tools.adjust_media_volume({"entity_id": "music_source.voice", "volume_pct": 1})
    assert _sent(home.calls.volume) == [(home.ids.voice_speaker, 1.0), (home.ids.voice, 1.0)]
    assert result == {"success": True, "entity_id": home.ids.voice_speaker, "volume_pct": 1.0}


async def test_a_step_counts_from_the_speakers_own_level(hass: HomeAssistant, home) -> None:
    # The speaker is at 0.3; the Music Assistant player says 0.8, a level the speaker isn't at.
    await home.tools.adjust_media_volume({"entity_id": "music_source.voice", "volume_step_pct": 0.1})
    assert _sent(home.calls.volume) == [(home.ids.voice_speaker, 0.4), (home.ids.voice, 0.4)]


async def test_volume_on_the_speaker_also_sets_its_music_source(hass: HomeAssistant, home) -> None:
    await home.tools.adjust_media_volume({"entity_id": home.ids.soundbar, "volume_pct": 0.25})
    assert _sent(home.calls.volume) == [(home.ids.soundbar, 0.25), (home.ids.living_room, 0.25)]


async def test_mute_goes_to_the_speaker_first(hass: HomeAssistant, home) -> None:
    result = await home.tools.set_media_mute({"entity_id": "music_source.voice", "is_volume_muted": True})
    assert _sent(home.calls.mute) == [(home.ids.voice_speaker, True), (home.ids.voice, True)]
    assert result["entity_id"] == home.ids.voice_speaker


async def test_a_speaker_that_cant_take_it_leaves_the_command_as_it_was(hass: HomeAssistant, home) -> None:
    hass.states.async_set(home.ids.soundbar, "off", {"supported_features": VOLUME})
    await home.tools.adjust_media_volume({"entity_id": "music_source.living_room_music", "volume_pct": 0.5})
    assert _sent(home.calls.volume) == [(home.ids.living_room, 0.5)]


async def test_unpaired_players_and_groups_are_left_as_they_were(hass: HomeAssistant, home) -> None:
    await home.tools.adjust_media_volume({"entity_id": "music_source.macbook", "volume_pct": 0.5})
    await home.tools.adjust_media_volume({"entity_id": "music_source.everywhere", "volume_pct": 0.5})
    assert _sent(home.calls.volume) == [(home.ids.airplay, 0.5), (home.ids.group, 0.5)]


# ── when an update breaks it ─────────────────────────────────────────────────────────────────────

async def test_unreadable_music_assistant_is_reported_and_volume_goes_as_before(
    hass: HomeAssistant, home, caplog: pytest.LogCaptureFixture
) -> None:
    home.ma_entry.runtime_data = SimpleNamespace(client=object())  # the client moved
    with caplog.at_level(logging.WARNING):
        assert music_pairs.music_link(hass).status == "unreadable"
        await home.tools.adjust_media_volume({"entity_id": "music_source.voice", "volume_pct": 1})
    assert _sent(home.calls.volume) == [(home.ids.voice, 1.0)]
    assert music_pairs.music_link_summary(hass) == {"music_link": "unreadable", "music_pairs": None}
    assert sum("can't read Music Assistant's players" in record.message for record in caplog.records) == 1  # once


async def test_players_without_identifiers_are_unreadable(hass: HomeAssistant, home) -> None:
    broken = SimpleNamespace(player_id="pl0a1b2c3d4e5f", type=ma_enums.PlayerType.PLAYER, device_info=SimpleNamespace())
    home.ma_entry.runtime_data = SimpleNamespace(mass=SimpleNamespace(players=FakePlayers.__new__(FakePlayers)))
    home.ma_entry.runtime_data.mass.players._players = {"pl0a1b2c3d4e5f": broken}  # noqa: SLF001
    assert music_pairs.music_link(hass).status == "unreadable"


async def test_without_music_assistant_there_is_nothing_to_pair(hass: HomeAssistant) -> None:
    assert music_pairs.music_link(hass) == music_pairs.MusicLink(None)
    assert music_pairs.music_link_summary(hass) == {"music_link": None, "music_pairs": None}
