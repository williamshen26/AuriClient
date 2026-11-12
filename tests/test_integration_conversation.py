"""The conversation agent's results for Home Assistant: when a voice satellite keeps listening.

Uses pytest-homeassistant-custom-component (Linux only: Home Assistant's
test harness doesn't run on Windows).
"""
from __future__ import annotations

import json

import pytest

pytest.importorskip("pytest_homeassistant_custom_component.common")

from pytest_homeassistant_custom_component.common import (  # noqa: E402
    MockConfigEntry,
    async_capture_events,
    async_mock_service,
)

from homeassistant.components import conversation  # noqa: E402
from homeassistant.core import Context, HomeAssistant  # noqa: E402
from homeassistant.setup import async_setup_component  # noqa: E402

from custom_components.hey_auri_client.const import (  # noqa: E402
    CONF_CLIENT_ID,
    CONF_SHARED_SECRET,
    DOMAIN,
    EVENT_AURI_WAKE_WORD,
    EVENT_CONVERSATION_FINISHED,
)
from custom_components.hey_auri_client.conversation.agent_class import ThinOpenAIAgent  # noqa: E402
from custom_components.hey_auri_client.conversation.helpers import build_context_snapshot  # noqa: E402
from custom_components.hey_auri_client.exceptions import SaaSRequestError  # noqa: E402
from custom_components.hey_auri_client.saas_client import SaaSTurnResponse, normalize_turn_response  # noqa: E402


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


class ReplyingClient:
    """The backend, giving one final reply."""

    def __init__(self, content: str) -> None:
        self.content = content

    async def send_user_turn(self, payload: dict) -> SaaSTurnResponse:
        return SaaSTurnResponse(response_type="final", conversation_id="c1", content=self.content, mode="owner")


@pytest.fixture
async def agent(hass: HomeAssistant) -> ThinOpenAIAgent:
    await async_setup_component(hass, "homeassistant", {})
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_CLIENT_ID: "client", CONF_SHARED_SECRET: "secret"})
    entry.add_to_hass(hass)
    return ThinOpenAIAgent(hass, entry)


@pytest.mark.parametrize(
    ("reply", "keeps_listening"),
    [
        ("Which light, the kitchen or the hall?", True),
        ("Which light? ", True),
        ("Ποιο φως;", True),  # Greek's question mark
        ("哪一盏灯？", True),
        ("The kitchen? It's on now.", False),  # a question on the way, but nothing to answer
        ("The kitchen light is on.", False),
        ("", False),
    ],
)
async def test_a_reply_that_asks_something_keeps_the_satellite_listening(
    hass: HomeAssistant, agent: ThinOpenAIAgent, reply: str, keeps_listening: bool
) -> None:
    agent.client = ReplyingClient(reply)

    result = await agent.async_process(
        conversation.ConversationInput(
            text="Turn on the light", context=Context(), conversation_id="c1", device_id="satellite-1",
            satellite_id=None, language="en", agent_id="hey_auri_client",
        )
    )

    assert result.continue_conversation is keeps_listening


@pytest.mark.usefixtures("agent")
async def test_the_agent_gets_the_time_in_home_assistants_time_zone(hass: HomeAssistant, freezer) -> None:
    """Not in the process's own time zone, which comes from the host or the container.

    A process zone that matched Home Assistant's for years can stop matching: British Columbia
    stayed on UTC-7 in November 2026, while Los Angeles went back to UTC-8. America/Phoenix stays
    on UTC-7 too, and its rules don't depend on how new the time zone data is.
    """
    await hass.config.async_set_time_zone("America/Phoenix")
    freezer.move_to("2026-11-10T19:19:07+00:00")

    snapshot = await build_context_snapshot(
        hass,
        conversation.ConversationInput(
            text="What time is it?", context=Context(), conversation_id="c1", device_id=None,
            satellite_id=None, language="en", agent_id="hey_auri_client",
        ),
    )

    assert snapshot["now"] == "2026-11-10T12:19:07-07:00"
    assert snapshot["day_of_week"] == "Tuesday"
    assert snapshot["home_location"]["time_zone"] == "America/Phoenix"


class ScriptedClient:
    """The backend, giving these responses in order and keeping what it was sent."""

    def __init__(self, *responses: SaaSTurnResponse) -> None:
        self.responses = list(responses)
        self.payloads: list[dict] = []

    async def send_user_turn(self, payload: dict) -> SaaSTurnResponse:
        self.payloads.append(payload)
        if not self.responses:  # the fast path's results, sent without waiting for an answer
            return SaaSTurnResponse(response_type="final", conversation_id="c1", content="OK", mode="owner")
        return self.responses.pop(0)

    send_tool_results = send_user_turn


def final(content: str, *, listen: bool | None = None, ignored: bool = False) -> SaaSTurnResponse:
    return SaaSTurnResponse(
        response_type="final", conversation_id="c1", content=content, mode="owner", listen=listen, ignored=ignored
    )


async def ask(agent: ThinOpenAIAgent, text: str) -> conversation.ConversationResult:
    return await agent.async_process(
        conversation.ConversationInput(
            text=text, context=Context(), conversation_id="c1", device_id="satellite-1",
            satellite_id=None, language="en", agent_id="hey_auri_client",
        )
    )


def spoken(result: conversation.ConversationResult) -> str:
    return result.response.speech.get("plain", {}).get("speech", "")


@pytest.mark.parametrize(
    ("reply", "keeps_listening"),
    [
        (final("First, salt the steak.", listen=True), True),  # Auri expects an answer right away
        (final("Which light?", listen=False), False),  # the backend's flag decides
        (final("Which light?"), True),  # an older backend sends no flag: a question keeps listening
    ],
)
async def test_the_backend_says_whether_to_keep_listening(
    hass: HomeAssistant, agent: ThinOpenAIAgent, reply: SaaSTurnResponse, keeps_listening: bool
) -> None:
    agent.client = ScriptedClient(reply)

    assert (await ask(agent, "Walk me through a steak recipe")).continue_conversation is keeps_listening


async def test_speech_from_the_open_mic_is_flagged_and_may_go_unanswered(
    hass: HomeAssistant, agent: ThinOpenAIAgent
) -> None:
    agent.client = ScriptedClient(final("First, salt the steak.", listen=True), final("", listen=False, ignored=True))
    finished = async_capture_events(hass, EVENT_CONVERSATION_FINISHED)
    await ask(agent, "Walk me through a steak recipe")

    result = await ask(agent, "and then he said the referee was blind")
    await hass.async_block_till_done()

    assert "follow_up" not in agent.client.payloads[0]["context"]  # started with the wake word
    assert agent.client.payloads[1]["context"]["follow_up"] is True
    assert spoken(result) == ""
    assert result.continue_conversation is False
    assert len(finished) == 1  # the overheard words weren't a conversation turn


async def test_auri_hearing_its_own_reply_gets_no_answer(hass: HomeAssistant, agent: ThinOpenAIAgent) -> None:
    reply = "First, take the steak out of the fridge thirty minutes before cooking."
    agent.client = ScriptedClient(final(reply, listen=True))
    await ask(agent, "How do I cook a steak?")

    result = await ask(agent, "take the steak out of the fridge thirty minutes before cooking")

    assert len(agent.client.payloads) == 1  # never sent to the backend
    assert spoken(result) == ""
    assert result.continue_conversation is False


async def test_a_device_command_closes_the_mic(hass: HomeAssistant, agent: ThinOpenAIAgent) -> None:
    hass.states.async_set("light.kitchen", "off")
    calls = async_mock_service(hass, "light", "turn_on")
    turn_on = {
        "id": "t1", "type": "function",
        "function": {"name": "turn_on_light", "arguments": json.dumps({"entity_id": "light.kitchen"})},
    }
    agent.client = ScriptedClient(
        final("Which light, the kitchen or the hall?", listen=True),
        SaaSTurnResponse(response_type="tool_calls", conversation_id="c1", turn_id="r1", tool_calls=[turn_on], mode="owner"),
    )
    await ask(agent, "Turn on the light")

    result = await ask(agent, "The kitchen")  # the answer, heard from the open mic
    await hass.async_block_till_done()  # the fast path's results go out in the background

    assert len(calls) == 1
    assert agent.client.payloads[1]["context"]["follow_up"] is True
    assert result.continue_conversation is False
    agent.client.responses.append(final("Sure."))
    await ask(agent, "Now the hall")  # the mic had closed: this one needed the wake word
    assert "follow_up" not in agent.client.payloads[-1]["context"]



async def test_an_error_closes_the_mic(hass: HomeAssistant, agent: ThinOpenAIAgent) -> None:
    class FailingOnce(ScriptedClient):
        async def send_user_turn(self, payload: dict) -> SaaSTurnResponse:
            if len(self.payloads) == 1:
                self.payloads.append(payload)
                raise SaaSRequestError("SaaS request failed: 500")
            return await super().send_user_turn(payload)

    agent.client = FailingOnce(final("Which light, the kitchen or the hall?", listen=True), final("Sure."))
    await ask(agent, "Turn on the light")

    result = await ask(agent, "The kitchen")  # from the open mic, but the backend fails
    await ask(agent, "Turn on the hall light")

    assert result.continue_conversation is False
    assert agent.client.payloads[1]["context"]["follow_up"] is True
    assert "follow_up" not in agent.client.payloads[2]["context"]  # the mic had closed

@pytest.mark.parametrize(
    ("fields", "listen", "ignored"),
    [
        ({"listen": True}, True, False),
        ({"listen": False, "ignored": True}, False, True),
        ({}, None, False),  # an older backend
        ({"listen": "yes", "ignored": "true"}, None, False),  # only real booleans
    ],
)
def test_the_backend_flags_are_read_only_as_booleans(fields: dict, listen: bool | None, ignored: bool) -> None:
    response = normalize_turn_response({"type": "final", "conversation_id": "c1", "content": "x", **fields})

    assert response.listen is listen
    assert response.ignored is ignored


async def test_a_satellite_woken_by_the_wake_word_starts_a_fresh_request(
    hass: HomeAssistant, agent: ThinOpenAIAgent
) -> None:
    agent.client = ScriptedClient(final("First, salt the steak.", listen=True), final("It's sunny tomorrow."))
    await ask(agent, "Walk me through a steak recipe")
    hass.bus.async_fire(EVENT_AURI_WAKE_WORD, {"device_id": "satellite-1"})
    await hass.async_block_till_done()

    await ask(agent, "What's the weather tomorrow?")  # within the follow-up window, but said with the wake word

    assert "follow_up" not in agent.client.payloads[1]["context"]  # never treated as overheard
