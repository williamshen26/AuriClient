"""Who is speaking, and who lives here.

Assist satellites never tell Home Assistant who is talking (their context
has no user), so a speaker is only known when they use Assist signed in to
Home Assistant, or after they said their name in the conversation (the
"stated" speaker, remembered for that conversation only).
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from time import monotonic
from typing import Any, Mapping

from homeassistant.core import HomeAssistant

_STATED_SPEAKER_TTL_SECONDS = 15 * 60
_STATED_SPEAKER_MAX_CONVERSATIONS = 200
# A login the household shares (a wall tablet): nobody knows who is speaking.
LOGIN_SHARED = "shared"
# A login a service uses (Zigbee2MQTT's MQTT login): its person isn't a household member.
LOGIN_NOT_A_PERSON = "not_a_person"


@dataclass(slots=True)
class Speaker:
    """The person behind one turn, as far as Home Assistant can tell."""

    source: str = "unknown"  # ha_user | stated | unknown
    name: str | None = None
    ha_user_id: str | None = None
    person_entity_id: str | None = None
    device_id: str | None = None
    area: str | None = None

    @property
    def known(self) -> bool:
        return self.source in {"ha_user", "stated"} and bool(self.name)


@dataclass(slots=True)
class TurnMeta:
    """What the client knows about one tool round; injected as _turn, never taken from the model."""

    mode: str | None
    memory_allowed: bool
    conversation_id: str | None
    speaker: Speaker | None = None


def household_persons(hass: HomeAssistant, login_people: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
    """Home Assistant persons as {entity_id, name, user_id}.

    With login_people, persons whose login is marked LOGIN_NOT_A_PERSON are left out.
    """
    hidden = {user_id for user_id, used_by in (login_people or {}).items() if used_by == LOGIN_NOT_A_PERSON}
    persons = []
    for state in hass.states.async_all("person"):
        name = str(state.attributes.get("friendly_name") or state.name or "").strip()
        if not name or state.attributes.get("user_id") in hidden:
            continue
        persons.append(
            {
                "entity_id": state.entity_id,
                "name": name,
                "user_id": state.attributes.get("user_id"),
            }
        )
    return persons


async def async_resolve_speaker(
    hass: HomeAssistant,
    *,
    user_id: str | None,
    device_id: str | None,
    area: str | None,
    login_people: Mapping[str, str] | None = None,
) -> Speaker:
    """Identify the signed-in Home Assistant user behind a turn, if any.

    login_people says who uses a login, when that isn't the person Home
    Assistant links it to (someone signed in as admin), or that a login is
    shared (LOGIN_SHARED) or not a person's (LOGIN_NOT_A_PERSON): its speaker
    is unknown, as on a voice satellite.
    """
    speaker = Speaker(device_id=device_id, area=area)
    if not user_id:
        return speaker

    user = await hass.auth.async_get_user(user_id)
    if user is None or user.system_generated:
        return speaker

    used_by = (login_people or {}).get(user_id)
    if used_by in (LOGIN_SHARED, LOGIN_NOT_A_PERSON):
        return speaker
    persons = household_persons(hass)
    person = next((p for p in persons if used_by and p["entity_id"] == used_by), None) or next(
        (p for p in persons if p.get("user_id") == user_id), None
    )
    speaker.source = "ha_user"
    speaker.ha_user_id = user_id
    speaker.person_entity_id = person["entity_id"] if person else None
    speaker.name = (person["name"] if person else None) or user.name
    return speaker


class StatedSpeakerCache:
    """Remembers, per conversation, who an unidentified speaker said they were."""

    def __init__(self) -> None:
        self._entries: OrderedDict[str, tuple[float, str, str | None]] = OrderedDict()

    def get(self, conversation_id: str | None) -> tuple[str, str | None] | None:
        if not conversation_id:
            return None
        entry = self._entries.get(conversation_id)
        if entry is None:
            return None
        expires_at, name, person_entity_id = entry
        if expires_at < monotonic():
            self._entries.pop(conversation_id, None)
            return None
        return name, person_entity_id

    def set(self, conversation_id: str | None, name: str, person_entity_id: str | None) -> None:
        if not conversation_id or not name:
            return
        self._entries[conversation_id] = (monotonic() + _STATED_SPEAKER_TTL_SECONDS, name, person_entity_id)
        self._entries.move_to_end(conversation_id)
        while len(self._entries) > _STATED_SPEAKER_MAX_CONVERSATIONS:
            self._entries.popitem(last=False)

    def clear(self) -> None:
        self._entries.clear()


class AskedOnceCache:
    """Conversations in which Auri already asked who to remember something for."""

    def __init__(self) -> None:
        self._asked: OrderedDict[str, float] = OrderedDict()
        self._awaiting: set[str] = set()

    def asked(self, conversation_id: str | None) -> bool:
        if not conversation_id:
            return False
        asked_at = self._asked.get(conversation_id)
        return asked_at is not None and asked_at + _STATED_SPEAKER_TTL_SECONDS > monotonic()

    def mark(self, conversation_id: str | None) -> None:
        if not conversation_id:
            return
        self._asked[conversation_id] = monotonic()
        self._asked.move_to_end(conversation_id)
        self._awaiting.add(conversation_id)
        while len(self._asked) > _STATED_SPEAKER_MAX_CONVERSATIONS:
            dropped, _ = self._asked.popitem(last=False)
            self._awaiting.discard(dropped)

    def take_answer(self, conversation_id: str | None) -> bool:
        """True once after asking: the next remember carries the answer."""
        if not conversation_id or conversation_id not in self._awaiting:
            return False
        self._awaiting.discard(conversation_id)
        return self.asked(conversation_id)

    def clear(self) -> None:
        self._asked.clear()
        self._awaiting.clear()
