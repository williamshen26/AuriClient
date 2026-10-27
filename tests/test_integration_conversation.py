"""The conversation agent's results for Home Assistant: when a voice satellite keeps listening.

Uses pytest-homeassistant-custom-component (Linux only: Home Assistant's
test harness doesn't run on Windows).
"""
from __future__ import annotations

import pytest

pytest.importorskip("pytest_homeassistant_custom_component.common")

from pytest_homeassistant_custom_component.common import MockConfigEntry  # noqa: E402

from homeassistant.components import conversation  # noqa: E402
from homeassistant.core import Context, HomeAssistant  # noqa: E402
from homeassistant.setup import async_setup_component  # noqa: E402

from custom_components.hey_auri_client.const import CONF_CLIENT_ID, CONF_SHARED_SECRET, DOMAIN  # noqa: E402
from custom_components.hey_auri_client.conversation.agent_class import ThinOpenAIAgent  # noqa: E402
from custom_components.hey_auri_client.saas_client import SaaSTurnResponse  # noqa: E402


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
