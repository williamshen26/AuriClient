"""Speech-to-text provider for Hey Auri cloud transcription."""
from __future__ import annotations

from collections.abc import AsyncIterable
import asyncio
import io
import logging
from time import monotonic
import uuid
import wave

from homeassistant.components import stt
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    API_ENDPOINT,
    CONF_CLIENT_ID,
    DOMAIN,
    CONF_SHARED_SECRET,
    CONF_STT_LANGUAGE,
    DEFAULT_REALTIME_WS_ENDPOINT,
    DEFAULT_STT_LANGUAGE,
    DEFAULT_STT_PIPELINE_ID,
)
from .helpers import get_timeout_seconds
from .metric_service import RequestLatencyMetricService
from .saas_client import SaaSClient

_LOGGER = logging.getLogger(__name__)

_TRANSCRIBE_PATH = "/voice/transcribe"
_WAKE_WORD_COLLISION_WINDOW_SECONDS = 1.0


class _WakeWordCollisionArbiter:
    """Allow only the earliest STT session in a short collision window."""

    def __init__(self, *, window_seconds: float) -> None:
        self._window_seconds = window_seconds
        self._lock = asyncio.Lock()
        self._last_session_started_at: float | None = None
        self._last_session_id: str | None = None

    async def claim(self, session_id: str) -> tuple[bool, str | None, float | None]:
        """Claim STT processing rights for this session."""
        now = monotonic()
        async with self._lock:
            if self._last_session_started_at is None:
                self._last_session_started_at = now
                self._last_session_id = session_id
                return True, None, None

            delta = now - self._last_session_started_at
            if delta <= self._window_seconds:
                return False, self._last_session_id, delta

            self._last_session_started_at = now
            self._last_session_id = session_id
            return True, None, None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities,
) -> None:
    """Set up Hey Auri STT provider entity."""
    async_add_entities([AuriSpeechToTextEntity(hass, entry)])


class AuriSpeechToTextEntity(stt.SpeechToTextEntity):
    """Speech-to-text provider entity backed by Auri cloud."""

    _attr_name = "Hey Auri Cloud STT"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.entry = entry
        self._attr_unique_id = f"{entry.entry_id}_stt"
        runtime = hass.data.setdefault(DOMAIN, {}).setdefault("runtime", {})
        self._collision_arbiter = runtime.setdefault(
            "wake_word_collision_arbiter",
            _WakeWordCollisionArbiter(window_seconds=_WAKE_WORD_COLLISION_WINDOW_SECONDS),
        )
        request_latency_metrics = RequestLatencyMetricService.from_options(hass, entry)
        self._client = SaaSClient(
            hass,
            endpoint=API_ENDPOINT,
            timeout=get_timeout_seconds(entry.options),
            client_id=str(entry.data[CONF_CLIENT_ID]).strip(),
            shared_secret=str(entry.data[CONF_SHARED_SECRET]),
            metrics_service=request_latency_metrics,
        )

    @property
    def supported_languages(self) -> list[str]:
        """Return a list of supported languages."""
        configured = str(self.entry.options.get(CONF_STT_LANGUAGE, DEFAULT_STT_LANGUAGE)).strip()
        return [configured] if configured else [DEFAULT_STT_LANGUAGE]

    @property
    def supported_formats(self) -> list[stt.AudioFormats]:
        """Return supported audio formats."""
        return [stt.AudioFormats.WAV]

    @property
    def supported_codecs(self) -> list[stt.AudioCodecs]:
        """Return supported codecs."""
        return [stt.AudioCodecs.PCM]

    @property
    def supported_bit_rates(self) -> list[stt.AudioBitRates]:
        """Return supported bit rates."""
        return [stt.AudioBitRates.BITRATE_16]

    @property
    def supported_sample_rates(self) -> list[stt.AudioSampleRates]:
        """Return supported sample rates."""
        return [stt.AudioSampleRates.SAMPLERATE_16000]

    @property
    def supported_channels(self) -> list[stt.AudioChannels]:
        """Return supported audio channels."""
        return [stt.AudioChannels.CHANNEL_MONO]

    async def async_process_audio_stream(
        self,
        metadata: stt.SpeechMetadata,
        stream: AsyncIterable[bytes],
    ) -> stt.SpeechResult:
        """Upload audio stream to Auri cloud and return transcript."""
        session_id = str(uuid.uuid4())[:8]
        is_primary, winner_session_id, delta_seconds = await self._collision_arbiter.claim(session_id)
        if not is_primary:
            _LOGGER.info(
                "Suppressing duplicate STT session session_id=%s winner_session_id=%s delta_ms=%d",
                session_id,
                winner_session_id,
                int((delta_seconds or 0.0) * 1000),
            )
            return stt.SpeechResult(None, stt.SpeechResultState.ERROR)

        configured_language = str(
            self.entry.options.get(CONF_STT_LANGUAGE, DEFAULT_STT_LANGUAGE)
        ).strip()
        language = (metadata.language or configured_language or DEFAULT_STT_LANGUAGE).strip()

        buffered_audio = bytearray()
        realtime_failed = False

        try:
            streaming_result = await self._client.stream_realtime_transcription(
                endpoint=DEFAULT_REALTIME_WS_ENDPOINT,
                stream=stream,
                language=language,
            )
            buffered_audio.extend(streaming_result.buffered_audio)
            if streaming_result.transcript:
                return stt.SpeechResult(streaming_result.transcript, stt.SpeechResultState.SUCCESS)
            realtime_failed = True
            _LOGGER.warning(
                "Auri STT realtime streaming produced no transcript, falling back to multipart upload: %s",
                streaming_result.error or "no transcript",
            )
        except Exception as err:
            realtime_failed = True
            _LOGGER.warning(
                "Auri STT realtime streaming failed, falling back to multipart upload: %s",
                err,
            )

        # If realtime failed mid-stream, continue draining remaining chunks so
        # fallback has the full utterance and the Assist pipeline can finish cleanly.
        if realtime_failed:
            async for chunk in stream:
                if not chunk:
                    break
                buffered_audio.extend(chunk)

        if buffered_audio:
            audio_chunks = buffered_audio
        else:
            audio_chunks = bytearray()
            async for chunk in stream:
                if not chunk:
                    break
                audio_chunks.extend(chunk)

        if not audio_chunks:
            return stt.SpeechResult(None, stt.SpeechResultState.ERROR)

        filename, mime_type, prepared_audio = _prepare_audio_for_upload(
            metadata,
            bytes(audio_chunks),
        )

        try:
            response = await self._client.post_multipart_audio(
                _TRANSCRIBE_PATH,
                fields={
                    "pipeline_id": DEFAULT_STT_PIPELINE_ID,
                    "language": language,
                },
                audio=prepared_audio,
                filename=filename,
                audio_content_type=mime_type,
            )
            transcript = response.get("text") or response.get("transcription")
        except Exception as err:
            _LOGGER.error("Auri STT request failed: %s", err)
            return stt.SpeechResult(None, stt.SpeechResultState.ERROR)

        if not transcript:
            return stt.SpeechResult(None, stt.SpeechResultState.ERROR)

        return stt.SpeechResult(transcript, stt.SpeechResultState.SUCCESS)

def _prepare_audio_for_upload(
    metadata: stt.SpeechMetadata,
    audio_bytes: bytes,
) -> tuple[str, str, bytes]:
    """Normalize raw stream bytes into a decodable audio payload for cloud STT."""
    format_name = str(metadata.format.value)
    filename = f"command.{format_name}"
    mime_type = f"audio/{format_name}"

    if metadata.format == stt.AudioFormats.WAV and not _has_wav_header(audio_bytes):
        wav_buffer = io.BytesIO()
        with wave.open(wav_buffer, "wb") as wav_file:
            wav_file.setnchannels(metadata.channel.value)
            wav_file.setsampwidth(max(1, metadata.bit_rate.value // 8))
            wav_file.setframerate(metadata.sample_rate.value)
            wav_file.writeframes(audio_bytes)
        return filename, mime_type, wav_buffer.getvalue()

    return filename, mime_type, audio_bytes


def _has_wav_header(audio_bytes: bytes) -> bool:
    """Return True when bytes already contain a RIFF/WAVE header."""
    return len(audio_bytes) >= 12 and audio_bytes[:4] == b"RIFF" and audio_bytes[8:12] == b"WAVE"
