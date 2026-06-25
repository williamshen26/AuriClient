"""Text-to-speech provider for Hey Auri cloud TTS."""
from __future__ import annotations

from collections.abc import AsyncGenerator, Iterator
from contextlib import suppress
import asyncio
import io
import logging
from time import perf_counter
import struct
from typing import Any
import wave

from homeassistant.components import tts
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback

from ..const import (
    API_ENDPOINT,
    CONF_CLIENT_ID,
    CONF_SHARED_SECRET,
    CONF_TTS_STREAM_ENDPOINT,
    DEFAULT_TTS_LANGUAGE,
    DEFAULT_TTS_SUPPORTED_VOICES,
    DEFAULT_TTS_STREAM_ENDPOINT,
    DEFAULT_TTS_VOICE,
)
from ..helpers import get_timeout_seconds
from ..metric_service import RequestLatencyMetricService
from ..saas_client import SaaSClient

_LOGGER = logging.getLogger(__name__)

# OpenAI WAV output is expected to use:
# - 24 kHz
# - signed 16-bit PCM
# - mono
#
# We emit the WAV header before the cloud response arrives, so the format must
# be known in advance. The returned OpenAI WAV is validated before its PCM
# samples are appended.
_TTS_SAMPLE_RATE = 24_000
_TTS_SAMPLE_WIDTH_BYTES = 2
_TTS_CHANNELS = 1

# Send enough local silence immediately for Home Assistant/ESPHome to start the
# announcement before the firmware's follow-up check runs.
_INITIAL_SILENCE_MS = 500

# While waiting for the LLM text and cloud TTS response, keep the audio stream
# active with silence paced approximately in real time.
_KEEPALIVE_SILENCE_MS = 100

# Do not leave an announcement stream open indefinitely.
_MAX_PRE_SPEECH_WAIT_SECONDS = 30.0

# Number of PCM frames yielded from the completed OpenAI WAV at a time.
_PCM_FRAMES_PER_CHUNK = 4096


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities,
) -> None:
    """Set up Hey Auri TTS provider entity."""
    entity = AuriTextToSpeechEntity(hass, entry)

    _LOGGER.warning(
        "Auri TTS setup: supports_streaming_input=%s",
        entity.async_supports_streaming_input(),
    )

    async_add_entities([entity])


class AuriTextToSpeechEntity(tts.TextToSpeechEntity):
    """Text-to-speech provider entity backed by Auri cloud."""

    _attr_name = "Hey Auri Cloud TTS"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._attr_unique_id = f"{entry.entry_id}_tts"

        request_latency_metrics = RequestLatencyMetricService.from_options(
            hass,
            entry,
        )

        self._client = SaaSClient(
            hass,
            endpoint=API_ENDPOINT,
            timeout=get_timeout_seconds(entry.options),
            client_id=str(entry.data[CONF_CLIENT_ID]).strip(),
            shared_secret=str(entry.data[CONF_SHARED_SECRET]),
            tts_stream_endpoint=str(
                entry.options.get(
                    CONF_TTS_STREAM_ENDPOINT,
                    DEFAULT_TTS_STREAM_ENDPOINT,
                )
            ).strip(),
            metrics_service=request_latency_metrics,
        )

    @property
    def supported_languages(self) -> list[str]:
        """Return the list of supported languages."""
        return [DEFAULT_TTS_LANGUAGE]

    @property
    def default_language(self) -> str:
        """Return the default language."""
        return DEFAULT_TTS_LANGUAGE

    @property
    def supported_options(self) -> list[str]:
        """Return supported TTS options."""
        return ["voice"]

    @property
    def default_options(self) -> dict[str, Any]:
        """Return default TTS options."""
        return {"voice": DEFAULT_TTS_VOICE}

    @callback
    def async_get_supported_voices(
        self,
        language: str,
    ) -> list[tts.Voice] | None:
        """Return supported voices for a language."""
        normalized_language = str(language or "").strip().lower()

        if not normalized_language.startswith(DEFAULT_TTS_LANGUAGE):
            return None

        return [
            tts.Voice(
                voice_id=voice_id,
                name=_format_voice_name(voice_id),
            )
            for voice_id in DEFAULT_TTS_SUPPORTED_VOICES
        ]

    async def async_get_tts_audio(
        self,
        message: str,
        language: str,
        options: dict[str, Any] | None = None,
    ) -> tts.TtsAudioType:
        """Generate complete WAV bytes by collecting stream chunks."""
        started = perf_counter()

        payload = self._build_payload(
            message=message,
            language=language,
            options=options,
        )

        _LOGGER.warning(
            "Auri TTS stream-collection request started message_length=%d",
            len(payload.get("text") or ""),
        )

        audio = await self._client.request_tts_audio(payload)

        _LOGGER.warning(
            "Auri TTS stream-collection completed in %d ms bytes=%d",
            int((perf_counter() - started) * 1000),
            len(audio),
        )

        if not audio:
            return None

        return "wav", audio

    async def async_stream_tts_audio(
        self,
        request: tts.TTSAudioRequest,
    ) -> tts.TTSAudioResponse:
        """Generate stream-first TTS audio with stream-collected fallback."""
        message = await _collect_message(request.message_gen)
        payload = self._build_payload(
            message=message,
            language=request.language,
            options=request.options,
        )

        async def _data_gen() -> AsyncGenerator[bytes, None]:
            _LOGGER.warning("Auri TTS data gen payload message is %s", payload.get("text"))
            stream_started = perf_counter()
            stream_has_data = False
            try:
                async for chunk in self._client.stream_tts_audio(payload):
                    if not chunk:
                        continue

                    if not stream_has_data:
                        _LOGGER.warning(
                            "Auri TTS first stream chunk after %d ms",
                            int((perf_counter() - stream_started) * 1000),
                        )

                    stream_has_data = True
                    yield chunk
            except asyncio.CancelledError:
                _LOGGER.debug("Auri TTS stream cancelled")
                raise
            except Exception as err:  # noqa: BLE001
                if stream_has_data:
                    _LOGGER.warning(
                        "Auri TTS stream failed after yielding audio; skipping fallback to avoid mixed output: %s",
                        err,
                    )
                    return

                _LOGGER.warning(
                    "Auri TTS stream failed before first audio chunk; retrying with stream-collected fallback: %s",
                    err,
                )
                fallback_started = perf_counter()
                fallback_audio = await self._client.request_tts_audio(payload)
                _LOGGER.warning(
                    "Auri TTS fallback stream collection completed in %d ms (bytes=%d)",
                    int((perf_counter() - fallback_started) * 1000),
                    len(fallback_audio),
                )
                if fallback_audio:
                    yield fallback_audio
                return

            if not stream_has_data:
                _LOGGER.warning(
                    "Auri TTS stream returned no audio; retrying with stream-collected fallback",
                )
                fallback_started = perf_counter()
                fallback_audio = await self._client.request_tts_audio(payload)
                _LOGGER.warning(
                    "Auri TTS fallback stream collection completed in %d ms (bytes=%d)",
                    int((perf_counter() - fallback_started) * 1000),
                    len(fallback_audio),
                )
                if fallback_audio:
                    yield fallback_audio

        return tts.TTSAudioResponse(
            extension="wav",
            data_gen=_data_gen(),
        )


    def _build_payload(
        self,
        *,
        message: str,
        language: str,
        options: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Build the authenticated Auri TTS request payload."""
        clean_message = str(message or "").strip()

        if not clean_message:
            raise ValueError("TTS message is empty")

        voice = str(
            (options or {}).get("voice") or DEFAULT_TTS_VOICE
        ).strip().lower()

        return {
            "text": clean_message,
            "language": (
                str(language or DEFAULT_TTS_LANGUAGE).strip()
                or DEFAULT_TTS_LANGUAGE
            ),
            "voice": voice,
        }


async def _collect_message(message_gen) -> str:
    """Collect streamed assistant text into one OpenAI TTS input."""
    parts: list[str] = []

    async for part in message_gen:
        chunk = str(part or "")

        if chunk:
            parts.append(chunk)

    return "".join(parts).strip()


def _build_streaming_wav_header(
    *,
    sample_rate: int,
    sample_width_bytes: int,
    channels: int,
) -> bytes:
    """Build a PCM WAV header whose data length is unknown.

    0xFFFFFFFF is commonly used for streaming WAV data when the final length
    is not yet known. The stream ends at HTTP EOF.
    """
    bits_per_sample = sample_width_bytes * 8
    block_align = channels * sample_width_bytes
    byte_rate = sample_rate * block_align
    unknown_size = 0xFFFFFFFF

    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        unknown_size,
        b"WAVE",
        b"fmt ",
        16,  # PCM fmt chunk length
        1,  # PCM audio format
        channels,
        sample_rate,
        byte_rate,
        block_align,
        bits_per_sample,
        b"data",
        unknown_size,
    )


def _make_silence_pcm(milliseconds: int) -> bytes:
    """Return signed PCM16 silence for the configured OpenAI audio format."""
    frame_count = int(
        _TTS_SAMPLE_RATE * milliseconds / 1000
    )

    byte_count = (
        frame_count
        * _TTS_CHANNELS
        * _TTS_SAMPLE_WIDTH_BYTES
    )

    return b"\x00" * byte_count


def _iter_validated_wav_pcm(audio: bytes) -> Iterator[bytes]:
    """Validate an OpenAI WAV and yield only its PCM samples."""
    try:
        wav_buffer = io.BytesIO(audio)
        wav_file = wave.open(wav_buffer, "rb")
    except (EOFError, wave.Error) as err:
        raise ValueError("Cloud TTS returned an invalid WAV file") from err

    with wav_file:
        sample_rate = wav_file.getframerate()
        sample_width = wav_file.getsampwidth()
        channels = wav_file.getnchannels()
        compression = wav_file.getcomptype()

        if compression != "NONE":
            raise ValueError(
                f"Unsupported cloud WAV compression: {compression}"
            )

        if sample_rate != _TTS_SAMPLE_RATE:
            raise ValueError(
                "Unexpected cloud WAV sample rate: "
                f"{sample_rate}; expected {_TTS_SAMPLE_RATE}"
            )

        if sample_width != _TTS_SAMPLE_WIDTH_BYTES:
            raise ValueError(
                "Unexpected cloud WAV sample width: "
                f"{sample_width}; expected {_TTS_SAMPLE_WIDTH_BYTES}"
            )

        if channels != _TTS_CHANNELS:
            raise ValueError(
                "Unexpected cloud WAV channels: "
                f"{channels}; expected {_TTS_CHANNELS}"
            )

        while True:
            pcm_chunk = wav_file.readframes(_PCM_FRAMES_PER_CHUNK)

            if not pcm_chunk:
                break

            yield pcm_chunk


def _format_voice_name(voice_id: str) -> str:
    """Return a human-readable name for the UI dropdown."""
    return voice_id.replace("_", " ").strip().title()
