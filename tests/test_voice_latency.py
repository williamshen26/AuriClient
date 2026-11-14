"""Voice latency: one shared connection opened as listening starts, the transcript as soon as it's
final, memory's embedding call started on the words heard as speech ends, and the turn timeline.

Uses pytest-homeassistant-custom-component (Linux only: Home Assistant's test harness doesn't
run on Windows).
"""
from __future__ import annotations

import asyncio
import gc
import json
import logging
from typing import Any

import aiohttp
from aiohttp import web
import pytest

pytest.importorskip("pytest_homeassistant_custom_component.common")

from pytest_homeassistant_custom_component.common import MockConfigEntry  # noqa: E402

from homeassistant.components import conversation, tts  # noqa: E402
from homeassistant.core import Context, HomeAssistant  # noqa: E402
from homeassistant.setup import async_setup_component  # noqa: E402

from custom_components.hey_auri_client import timeline  # noqa: E402
from custom_components.hey_auri_client.const import (  # noqa: E402
    API_ENDPOINT,
    CONF_CLIENT_ID,
    CONF_SHARED_SECRET,
    DATA_MEMORY,
    DOMAIN,
    ERROR_NO_AUTHENTICATION,
    ERROR_NO_UPSTREAM_REPORTED_ERROR,
)
from custom_components.hey_auri_client.conversation.agent_class import ThinOpenAIAgent  # noqa: E402
from custom_components.hey_auri_client.exceptions import SaaSRequestError  # noqa: E402
from custom_components.hey_auri_client.memory.api import MemoryApi, MemoryApiError  # noqa: E402
from custom_components.hey_auri_client.saas_client import SaaSClient, SaaSTurnResponse  # noqa: E402
from custom_components.hey_auri_client.speech_to_text.stt import AuriSpeechToTextEntity  # noqa: E402
from custom_components.hey_auri_client.text_to_speech.tts import AuriTextToSpeechEntity  # noqa: E402


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


def _entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_CLIENT_ID: "client", CONF_SHARED_SECRET: "secret"})
    entry.add_to_hass(hass)
    return entry


def _client(hass: HomeAssistant) -> SaaSClient:
    return SaaSClient(hass, endpoint=API_ENDPOINT, timeout=10, client_id="client", shared_secret="secret")


def _input(text: str) -> conversation.ConversationInput:
    return conversation.ConversationInput(
        text=text, context=Context(), conversation_id="c1", device_id=None, satellite_id=None,
        language="en", agent_id="hey_auri_client",
    )


# ── one connection, opened early ────────────────────────────────────────────


async def test_requests_share_home_assistants_connection_pool(hass: HomeAssistant, aioclient_mock) -> None:
    """Home Assistant's shared session, not a new one (and a new connection) per request."""
    aioclient_mock.post(f"{API_ENDPOINT}/conversation", json={"type": "final", "conversation_id": "c1", "content": "Done."})
    client = _client(hass)
    for _ in range(2):
        response = await client.send_user_turn({"type": "user_turn", "conversation_id": "c1"})
        assert response.content == "Done."
    assert aioclient_mock.call_count == 2


async def test_listening_opens_a_connection_to_the_cloud(hass: HomeAssistant, aioclient_mock) -> None:
    """Home Assistant prepares the agent when speech-to-text starts: a HEAD that API Gateway answers itself."""
    await async_setup_component(hass, "homeassistant", {})
    aioclient_mock.head(f"{API_ENDPOINT}/", status=404)
    agent = ThinOpenAIAgent(hass, _entry(hass))

    await agent.async_prepare("en")
    await hass.async_block_till_done(wait_background_tasks=True)

    assert [(method.upper(), str(url)) for method, url, _data, _headers in aioclient_mock.mock_calls] == [
        ("HEAD", f"{API_ENDPOINT}/")
    ]


@pytest.mark.parametrize("failure", [aiohttp.ClientConnectionError("offline"), TimeoutError()])
async def test_a_failed_warm_up_is_harmless(hass: HomeAssistant, aioclient_mock, failure: Exception) -> None:
    aioclient_mock.head(f"{API_ENDPOINT}/", exc=failure)
    await _client(hass).async_warm_up()


# ── the transcript, and the words as speech ends ────────────────────────────


class FakeGateway:
    """The realtime gateway: words while the audio comes, then what follows audio.end.

    What follows audio.end is sent alongside the reading, as the gateway relays OpenAI's events
    while it reads the hub (so the hub's close is answered at once).
    """

    def __init__(
        self,
        *,
        during: tuple[str, ...] = (),
        after: tuple[str, ...] = (),
        completed: str | None = None,
        completed_at: float = 0.0,
        other_events_at: tuple[float, ...] = (),
        error: str | None = None,
        close_code: int | None = None,
    ) -> None:
        self.during = during
        self.after = after  # words after audio.end, 20 ms apart
        self.completed = completed
        self.completed_at = completed_at  # seconds after audio.end, at the earliest
        self.other_events_at = other_events_at  # OpenAI's events that carry no words (item.done)
        self.error = error
        self.close_code = close_code  # refuse the connection with this code instead
        self.words_sent = asyncio.Event()

    async def handle(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        if self.close_code is not None:
            await ws.close(code=self.close_code)
            return ws
        tail: asyncio.Task | None = None
        try:
            async for message in ws:
                if message.type == aiohttp.WSMsgType.BINARY and not self.words_sent.is_set():
                    for words in self.during:
                        await ws.send_json(_delta(words))
                    self.words_sent.set()
                elif message.type == aiohttp.WSMsgType.TEXT and json.loads(message.data).get("type") == "audio.end":
                    tail = asyncio.create_task(self._after_speech(ws))
        finally:
            if tail is not None:
                tail.cancel()
        return ws

    async def _after_speech(self, ws: web.WebSocketResponse) -> None:
        loop = asyncio.get_running_loop()
        ended = loop.time()

        async def until(offset: float) -> None:
            await asyncio.sleep(max(0.0, ended + offset - loop.time()))

        try:
            if self.error:
                await ws.send_json({"type": "error", "error": {"message": self.error}})
            for offset in self.other_events_at:
                await until(offset)
                await ws.send_json({"type": "conversation.item.done"})
            for words in self.after:
                await asyncio.sleep(0.02)
                await ws.send_json(_delta(words))
            if self.completed is not None:
                await until(self.completed_at)
                await ws.send_json(
                    {"type": "conversation.item.input_audio_transcription.completed", "transcript": self.completed}
                )
        except (ConnectionError, RuntimeError):
            pass  # the hub has closed


def _delta(words: str) -> dict[str, Any]:
    return {"type": "conversation.item.input_audio_transcription.delta", "delta": words}


async def _speech(gateway: FakeGateway, *, chunks: int = 20):
    """Home Assistant's audio: 16 kHz 16-bit mono, 100 ms a chunk. The first words come back partway."""
    chunk = b"\x10\x00" * 1600
    for index in range(chunks):
        yield chunk
        if index == 0 and gateway.close_code is None:
            await asyncio.wait_for(gateway.words_sent.wait(), 5)
            await asyncio.sleep(0.05)  # for the words to reach the hub


async def _listen(hass: HomeAssistant, aiohttp_client, gateway: FakeGateway, heard: list[str] | None = None):
    app = web.Application()
    app.router.add_get("/ws/realtime", gateway.handle)
    server = await aiohttp_client(app)
    return await _client(hass).stream_realtime_transcription(
        endpoint=str(server.make_url("/ws/realtime")),
        language="en",
        stream=_speech(gateway),
        client_session_id="test",
        on_words=None if heard is None else heard.append,
    )


@pytest.mark.usefixtures("socket_enabled")  # a local gateway on 127.0.0.1
async def test_the_transcript_returns_as_soon_as_it_is_final(hass: HomeAssistant, aiohttp_client) -> None:
    gateway = FakeGateway(during=("Turn on the",), after=(" kitchen", " light."), completed="Turn on the kitchen light.")
    heard: list[str] = []

    result = await _listen(hass, aiohttp_client, gateway, heard)

    assert result.transcript == "Turn on the kitchen light."
    assert result.error is None
    # The words heard when speech ended, then all of them as each new one came.
    assert heard == ["Turn on the", "Turn on the kitchen", "Turn on the kitchen light."]
    # Nothing waited for after the final transcript (the hub used to wait 300 ms more).
    assert result.post_speech_latency_ms is not None and result.post_speech_latency_ms < 250


@pytest.mark.usefixtures("socket_enabled")  # a local gateway on 127.0.0.1
async def test_the_wait_ends_300_ms_after_the_last_words(hass: HomeAssistant, aiohttp_client) -> None:
    """OpenAI's other events after the commit don't extend it, and a late final isn't waited for."""
    gateway = FakeGateway(
        during=("Turn off the lights.",),
        other_events_at=(0.1, 0.2),  # committed, item.added, item.done: no words
        completed="Turn off the lights.",
        completed_at=1.0,
    )

    result = await _listen(hass, aiohttp_client, gateway)

    assert result.transcript == "Turn off the lights."
    # 300 ms after the words (which came before audio.end), not 300 ms after the last event (500).
    assert result.post_speech_latency_ms is not None and 250 <= result.post_speech_latency_ms < 420


@pytest.mark.usefixtures("socket_enabled")  # a local gateway on 127.0.0.1
async def test_speech_ending_before_any_words_is_reported_too(hass: HomeAssistant, aiohttp_client) -> None:
    """The first call marks the end of speech (the turn timeline), words or not."""
    gateway = FakeGateway(after=("Stop.",), completed="Stop.")
    heard: list[str] = []

    result = await _listen(hass, aiohttp_client, gateway, heard)

    assert result.transcript == "Stop."
    assert heard == ["", "Stop."]


@pytest.mark.usefixtures("socket_enabled")  # a local gateway on 127.0.0.1
async def test_without_a_final_transcript_the_streamed_words_are_used(hass: HomeAssistant, aiohttp_client) -> None:
    gateway = FakeGateway(during=("Turn on the",), after=(" kitchen light.",))

    result = await _listen(hass, aiohttp_client, gateway)

    assert result.transcript == "Turn on the kitchen light."


@pytest.mark.usefixtures("socket_enabled")  # a local gateway on 127.0.0.1
async def test_a_refused_connection_reads_as_an_account_problem(hass: HomeAssistant, aiohttp_client) -> None:
    """The gateway closes with 1008 when the account isn't allowed (and the hub says so)."""
    result = await _listen(hass, aiohttp_client, FakeGateway(close_code=1008))

    assert result.transcript is None
    assert result.error_no == ERROR_NO_AUTHENTICATION


@pytest.mark.usefixtures("socket_enabled")  # a local gateway on 127.0.0.1
async def test_an_upstream_error_ends_the_listen(hass: HomeAssistant, aiohttp_client) -> None:
    gateway = FakeGateway(during=("Turn on",), error="The transcription failed")

    result = await _listen(hass, aiohttp_client, gateway)

    assert result.transcript is None
    assert result.error_no == ERROR_NO_UPSTREAM_REPORTED_ERROR


async def test_the_words_go_to_memory_as_speech_ends(hass: HomeAssistant) -> None:
    entry = _entry(hass)

    class Memory:
        def __init__(self) -> None:
            self.words: list[str] = []

        def prefetch_turn(self, words: str) -> None:
            self.words.append(words)

    memory = Memory()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {DATA_MEMORY: memory}
    entity = AuriSpeechToTextEntity(hass, entry)
    entity.hass = hass

    on_words = entity._words_handler()
    on_words("")
    ended = hass.data[DOMAIN]["turn_timeline"].speech_end
    on_words("把灯调到")
    on_words("把灯调到我喜欢的亮度")

    assert memory.words == ["把灯调到", "把灯调到我喜欢的亮度"]
    assert hass.data[DOMAIN]["turn_timeline"].speech_end == ended  # one turn, from the first call


# ── the embedding call, started early ───────────────────────────────────────


class EmbedClient:
    """/memory/embed, counting calls. A call can be held until released, or fail."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.release = asyncio.Event()
        self.release.set()
        self.fail = False

    async def post_json(self, path: str, payload: dict[str, Any], **_kwargs) -> dict[str, Any]:
        assert path == "/memory/embed"
        self.calls.append(payload["texts"])
        await self.release.wait()
        if self.fail:
            raise SaaSRequestError("Request failed", status_code=500, response_body="{}")
        return {"model": "fake", "dimensions": 768, "embeddings": [[float(len(text))] * 768 for text in payload["texts"]]}


async def _embed(api: MemoryApi, text: str, scope: str = "owner") -> tuple[list[list[float]], str]:
    return await api.embed([text], task_type="RETRIEVAL_QUERY", scope=scope)


async def test_a_prefetched_query_serves_its_turn(hass: HomeAssistant) -> None:
    client = EmbedClient()
    api = MemoryApi(client, hass)

    api.prefetch_query("把灯调到我喜欢的亮度", scope="owner")
    vectors, tag = await _embed(api, "把灯调到我喜欢的亮度。")  # the final transcript adds the full stop

    assert client.calls == [["把灯调到我喜欢的亮度"]]
    assert tag == "fake@768" and len(vectors) == 1


async def test_a_turn_waits_for_a_prefetch_still_in_flight(hass: HomeAssistant) -> None:
    client = EmbedClient()
    client.release.clear()
    api = MemoryApi(client, hass)

    api.prefetch_query("家里有什么不能吃的吗", scope="owner")
    turn = asyncio.ensure_future(_embed(api, "家里有什么不能吃的吗？"))
    await asyncio.sleep(0.01)
    assert not turn.done()
    client.release.set()
    await turn

    assert len(client.calls) == 1


async def test_other_words_or_another_mode_get_their_own_call(hass: HomeAssistant) -> None:
    client = EmbedClient()
    api = MemoryApi(client, hass)

    api.prefetch_query("把灯调到", scope="owner")
    await _embed(api, "把灯调到我喜欢的亮度")  # more words came after speech ended
    api.prefetch_query("把灯调到我喜欢的亮度", scope="owner")
    await _embed(api, "把灯调到我喜欢的亮度", scope="guest")  # the turn is in the other mode

    assert client.calls == [["把灯调到"], ["把灯调到我喜欢的亮度"], ["把灯调到我喜欢的亮度"], ["把灯调到我喜欢的亮度"]]


async def test_a_prefetch_is_used_once(hass: HomeAssistant) -> None:
    client = EmbedClient()
    api = MemoryApi(client, hass)

    api.prefetch_query("Apaga las luces de la sala", scope="owner")
    api.prefetch_query("Apaga las luces de la sala.", scope="owner")  # the same words again
    await _embed(api, "Apaga las luces de la sala")
    await _embed(api, "Apaga las luces de la sala")

    assert len(client.calls) == 2


async def test_a_failed_prefetch_fails_its_turn_as_its_own_call_would(hass: HomeAssistant) -> None:
    client = EmbedClient()
    client.fail = True
    api = MemoryApi(client, hass)

    api.prefetch_query("把灯调到我喜欢的亮度", scope="owner")
    with pytest.raises(MemoryApiError):
        await _embed(api, "把灯调到我喜欢的亮度")


async def test_an_unused_failed_prefetch_reports_nothing(hass: HomeAssistant) -> None:
    """Home Assistant's test harness fails the test if a task's error goes unretrieved."""
    client = EmbedClient()
    client.fail = True
    api = MemoryApi(client, hass)

    api.prefetch_query("把灯调到我喜欢的亮度", scope="owner")
    await hass.async_block_till_done(wait_background_tasks=True)
    api._prefetched.clear()
    gc.collect()


# ── the turn timeline ───────────────────────────────────────────────────────


class ReplyingClient:
    async def send_user_turn(self, payload: dict) -> SaaSTurnResponse:
        await asyncio.sleep(0.01)
        return SaaSTurnResponse(response_type="final", conversation_id="c1", content="Done.", mode="owner")


async def test_a_voice_turn_logs_where_its_time_went(hass: HomeAssistant, caplog) -> None:
    await async_setup_component(hass, "homeassistant", {})
    agent = ThinOpenAIAgent(hass, _entry(hass))
    agent.client = ReplyingClient()

    timeline.speech_ended(hass)
    timeline.mark(hass, "transcript")
    await agent.async_process(_input("Turn on the kitchen light"))
    timeline.mark(hass, "tts_request")
    with caplog.at_level(logging.DEBUG, logger=timeline.__name__):
        timeline.first_audio(hass)
        timeline.first_audio(hass)  # more audio from the same reply

    turn = hass.data[DOMAIN]["turn_timeline"]
    moments = [turn.speech_end, turn.transcript, turn.agent_start, turn.context_ready, turn.agent_done,
               turn.tts_request, turn.first_audio]
    assert None not in moments and moments == sorted(moments)
    assert turn.cloud_requests == 1 and turn.cloud_seconds >= 0.01
    lines = [record.getMessage() for record in caplog.records if "Auri turn timeline" in record.getMessage()]
    assert len(lines) == 1
    assert " - ms" not in lines[0] and "typed" not in lines[0]


async def test_a_typed_turn_is_timed_from_the_agent(hass: HomeAssistant, caplog) -> None:
    await async_setup_component(hass, "homeassistant", {})
    agent = ThinOpenAIAgent(hass, _entry(hass))
    agent.client = ReplyingClient()

    await agent.async_process(_input("Turn on the kitchen light"))
    with caplog.at_level(logging.DEBUG, logger=timeline.__name__):
        timeline.first_audio(hass)

    assert "typed or not via Auri's speech-to-text" in caplog.text


async def test_a_reply_streams_through_the_shared_pool_and_ends_the_timeline(
    hass: HomeAssistant, aioclient_mock, caplog
) -> None:
    await async_setup_component(hass, "homeassistant", {})
    audio = b"RIFF" + b"\x00" * 4 + b"WAVE" + b"\x01\x00" * 100
    aioclient_mock.post(f"{API_ENDPOINT}/tts/stream", content=audio)
    entity = AuriTextToSpeechEntity(hass, _entry(hass))
    timeline.mark(hass, "agent_start")
    timeline.mark(hass, "agent_done")

    async def message():
        yield "The kitchen light is on."

    with caplog.at_level(logging.DEBUG, logger=timeline.__name__):
        response = await entity.async_stream_tts_audio(
            tts.TTSAudioRequest(language="en", options={}, message_gen=message())
        )
        received = b"".join([chunk async for chunk in response.data_gen])

    assert received == audio
    assert aioclient_mock.call_count == 1
    turn = hass.data[DOMAIN]["turn_timeline"]
    assert turn.agent_done <= turn.tts_request <= turn.first_audio
    assert "Auri turn timeline" in caplog.text


async def test_speech_with_no_turn_logs_nothing(hass: HomeAssistant, caplog) -> None:
    """An announcement, say: no turn led to it."""
    with caplog.at_level(logging.DEBUG, logger=timeline.__name__):
        timeline.mark(hass, "tts_request")
        timeline.first_audio(hass)

    assert "Auri turn timeline" not in caplog.text
