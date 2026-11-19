"""The skill guess: a voice turn's words go to the cloud as speech ends, and the turn sends back the skills.

Uses pytest-homeassistant-custom-component (Linux only: Home Assistant's test harness doesn't run on Windows).
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

pytest.importorskip("pytest_homeassistant_custom_component.common")

from pytest_homeassistant_custom_component.common import MockConfigEntry  # noqa: E402

from homeassistant.components import conversation  # noqa: E402
from homeassistant.core import Context, HomeAssistant  # noqa: E402
from homeassistant.setup import async_setup_component  # noqa: E402

from custom_components.hey_auri_client import timeline  # noqa: E402
from custom_components.hey_auri_client.const import (  # noqa: E402
    CONF_CLIENT_ID,
    CONF_SHARED_SECRET,
    DATA_SKILL_GUESS,
    DOMAIN,
)
from custom_components.hey_auri_client.conversation import skill_guess  # noqa: E402
from custom_components.hey_auri_client.conversation.agent_class import ThinOpenAIAgent  # noqa: E402
from custom_components.hey_auri_client.conversation.skill_guess import SkillGuesses  # noqa: E402
from custom_components.hey_auri_client.exceptions import SaaSRequestError  # noqa: E402
from custom_components.hey_auri_client.saas_client import SaaSTurnResponse  # noqa: E402
from custom_components.hey_auri_client.speech_to_text.stt import AuriSpeechToTextEntity  # noqa: E402


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


class GuessClient:
    """Auri's cloud answering skill guesses: each call can be held until released, or fail."""

    def __init__(self, skills: list[str] | None = None) -> None:
        self.skills = ["light"] if skills is None else skills
        self.texts: list[str] = []
        self.release = asyncio.Event()
        self.release.set()
        self.fail = False

    async def post_json(self, path: str, payload: dict[str, Any], **kwargs) -> dict[str, Any]:
        assert path == "/conversation" and payload["type"] == "skill_guess" and kwargs["timeout_seconds"] <= 2
        self.texts.append(payload["text"])
        await self.release.wait()
        if self.fail:
            raise SaaSRequestError("HTTP 500")
        return {"type": "skill_guess", "skills": self.skills}


async def _settled(guesses: SkillGuesses) -> None:
    while guesses._task is not None and not guesses._task.done():  # noqa: SLF001
        await asyncio.sleep(0)


async def test_one_guess_in_flight_and_only_the_newest_words_next(hass: HomeAssistant) -> None:
    client = GuessClient()
    client.release.clear()
    guesses = SkillGuesses(hass, client)

    guesses.prefetch("Turn on")
    await asyncio.sleep(0)
    guesses.prefetch("Turn on the kitchen")
    guesses.prefetch("Turn on the kitchen light")
    guesses.prefetch("Turn on")  # the words already in flight
    assert client.texts == ["Turn on"]

    client.release.set()
    await _settled(guesses)
    await _settled(guesses)
    assert client.texts == ["Turn on", "Turn on the kitchen light"]


async def test_a_turn_gets_the_guess_for_its_words(hass: HomeAssistant) -> None:
    guesses = SkillGuesses(hass, GuessClient(["light"]))
    guesses.prefetch("Turn on the kitchen light.")
    await _settled(guesses)

    assert await guesses.async_hints("turn on the kitchen light") == (["light"], "light", pytest.approx(0, abs=0.05))


async def test_a_guess_on_the_first_words_serves_the_whole_sentence(hass: HomeAssistant) -> None:
    """The final transcript can add words the guess didn't have yet; it can't swap in another request."""
    guesses = SkillGuesses(hass, GuessClient(["timer"]))
    guesses.prefetch("Set a timer for")
    await _settled(guesses)
    assert (await guesses.async_hints("Set a timer for ten minutes"))[0] == ["timer"]

    guesses.prefetch("What time is it")
    await _settled(guesses)
    assert (await guesses.async_hints("Turn on the light"))[:2] == ([], "")


async def test_each_guess_serves_one_turn(hass: HomeAssistant) -> None:
    guesses = SkillGuesses(hass, GuessClient(["light"]))
    guesses.prefetch("Lights on")
    await _settled(guesses)

    assert (await guesses.async_hints("Lights on"))[0] == ["light"]
    assert (await guesses.async_hints("Lights on"))[:2] == ([], "")


async def test_a_turn_waits_a_little_then_goes_without(hass: HomeAssistant) -> None:
    client = GuessClient()
    client.release.clear()
    guesses = SkillGuesses(hass, client)
    guesses.prefetch("Lights on")

    hints, guessed, waited = await guesses.async_hints("Lights on", budget=0.05)

    assert (hints, guessed) == ([], "late")
    assert 0.04 < waited < 0.5
    client.release.set()
    await _settled(guesses)


async def test_a_guess_back_within_the_wait_is_used(hass: HomeAssistant) -> None:
    client = GuessClient(["alarm"])
    client.release.clear()
    guesses = SkillGuesses(hass, client)
    guesses.prefetch("Wake me up at seven")
    hass.loop.call_later(0.02, client.release.set)

    assert (await guesses.async_hints("Wake me up at seven", budget=1.0))[:2] == (["alarm"], "alarm")


async def test_a_failed_guess_means_no_hints(hass: HomeAssistant) -> None:
    client = GuessClient()
    client.fail = True
    guesses = SkillGuesses(hass, client)
    guesses.prefetch("Lights on")
    await _settled(guesses)

    assert (await guesses.async_hints("Lights on"))[:2] == ([], "failed")


async def test_nothing_to_preload_says_none(hass: HomeAssistant) -> None:
    guesses = SkillGuesses(hass, GuessClient([]))
    guesses.prefetch("What's the capital of France")
    await _settled(guesses)

    assert (await guesses.async_hints("What's the capital of France?"))[:2] == ([], "none")


async def test_a_typed_turn_never_waits_or_calls(hass: HomeAssistant) -> None:
    client = GuessClient()
    guesses = SkillGuesses(hass, client)

    hints, guessed, waited = await guesses.async_hints("Turn on the light")
    assert (hints, guessed) == ([], "") and waited < 0.005
    assert client.texts == []


async def test_an_old_guess_belongs_to_another_turn(hass: HomeAssistant, monkeypatch) -> None:
    guesses = SkillGuesses(hass, GuessClient(["light"]))
    guesses.prefetch("Lights on")
    await _settled(guesses)
    now = skill_guess.monotonic()
    monkeypatch.setattr(skill_guess, "monotonic", lambda: now + 31)

    assert (await guesses.async_hints("Lights on"))[:2] == ([], "")


async def test_no_guess_for_dictation(hass: HomeAssistant) -> None:
    client = GuessClient()
    guesses = SkillGuesses(hass, client)
    guesses.prefetch("word " * 80)
    guesses.prefetch("   ")
    await _settled(guesses)
    assert client.texts == []


# ── where it's wired in ──────────────────────────────────────────────────────


def _entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_CLIENT_ID: "client", CONF_SHARED_SECRET: "secret"})
    entry.add_to_hass(hass)
    return entry


async def test_the_words_go_to_the_skill_guess_as_speech_ends(hass: HomeAssistant) -> None:
    """Every voice turn: in English, with no memory set up."""

    class Guesses:
        def __init__(self) -> None:
            self.words: list[str] = []

        def prefetch(self, words: str) -> None:
            self.words.append(words)

    entry = _entry(hass)
    guesses = Guesses()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {DATA_SKILL_GUESS: guesses}
    entity = AuriSpeechToTextEntity(hass, entry)
    entity.hass = hass

    on_words = entity._words_handler()
    on_words("")
    on_words("Turn on")
    on_words("Turn on the kitchen light")

    assert guesses.words == ["Turn on", "Turn on the kitchen light"]


class TurnClient:
    """The backend for turns: keeps each payload, answers OK."""

    def __init__(self) -> None:
        self.payloads: list[dict] = []

    async def send_user_turn(self, payload: dict) -> SaaSTurnResponse:
        self.payloads.append(payload)
        return SaaSTurnResponse(response_type="final", conversation_id="c1", content="OK", mode="owner")


async def _ask(agent: ThinOpenAIAgent, text: str) -> None:
    await agent.async_process(
        conversation.ConversationInput(
            text=text, context=Context(), conversation_id="c1", device_id="satellite-1",
            satellite_id=None, language="en", agent_id="hey_auri_client",
        )
    )


async def test_the_turn_sends_the_guessed_skills(hass: HomeAssistant) -> None:
    await async_setup_component(hass, "homeassistant", {})
    agent = ThinOpenAIAgent(hass, _entry(hass))
    agent.client = TurnClient()
    agent.skill_guesses = SkillGuesses(hass, GuessClient(["light"]))

    timeline.speech_ended(hass)
    agent.skill_guesses.prefetch("Turn on the kitchen light")
    await _settled(agent.skill_guesses)
    await _ask(agent, "Turn on the kitchen light.")
    assert agent.client.payloads[0]["skill_hints"] == ["light"]
    assert hass.data[DOMAIN]["turn_timeline"].skill_guess == "light"

    await _ask(agent, "What time is it?")  # typed, or no guess back: nothing extra
    assert "skill_hints" not in agent.client.payloads[1]


def test_the_timeline_line_shows_the_guess() -> None:
    line = timeline.describe(timeline.TurnTimeline(skill_guess="climate + automation", guess_wait=0.042))
    assert "(skill guess climate + automation, waited 42 ms)" in line
    assert "skill guess" not in timeline.describe(timeline.TurnTimeline())
