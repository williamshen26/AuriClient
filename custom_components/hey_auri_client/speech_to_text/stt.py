"""Speech-to-text provider for Hey Auri cloud transcription."""
from __future__ import annotations

from collections.abc import AsyncIterable
from datetime import datetime
import io
import json
import logging
import uuid
import wave

from homeassistant.components import stt
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from ..const import (
    API_ENDPOINT,
    CONF_CLIENT_ID,
    ERROR_NO_AUTHENTICATION,
    VOICE_AGENT_ERROR_ACCOUNT_NOT_ACTIVE,
    CONF_SHARED_SECRET,
    CONF_STT_LANGUAGE,
    DEFAULT_REALTIME_WS_ENDPOINT,
    DEFAULT_STT_LANGUAGE,
    DEFAULT_STT_PIPELINE_ID,
    MIN_FALLBACK_AUDIO_BYTES,
    SUPPORTED_LANGUAGES,
    TRANSCRIBE_PATH,
)
from ..conversation.file_util import write_bytes_to_file
from ..exceptions import SaaSRequestError
from ..helpers import get_timeout_seconds
from ..metric_service import RequestLatencyMetricService
from ..saas_client import SaaSClient

_LOGGER = logging.getLogger(__name__)

# Not under /config/www: this can contain spoken commands, so it's kept off
# the HA web-accessible static path and only reachable via filesystem access
# (Samba/SSH/Studio Code Server add-ons).
_DEBUG_FAILED_AUDIO_DIR = "/config/auri_debug_audio"

# Toggle to control when captured audio gets written to _DEBUG_FAILED_AUDIO_DIR.
# Edit and restart Home Assistant to apply; not exposed as a UI option.
DEBUG_WAV_ON_FAILURE = True  # save whenever a session ends without a transcript
DEBUG_WAV_ALWAYS = False  # save every session regardless of outcome


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
        """Return all languages this entity can be selected for as a
        pipeline's STT engine (HA excludes an engine from the picker
        entirely if none of its declared languages match the pipeline's
        configured language). Kept broad so this stays selectable for any
        of our supported languages — actual per-request language no longer
        trusts what HA's pipeline Language dropdown reports back (see
        async_process_audio_stream), since it's proven unreliable, so it
        doesn't matter that these are bare codes rather than the
        region-qualified tags that dropdown would need to populate properly.
        """
        return list(SUPPORTED_LANGUAGES)

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

        # HA's Assist pipeline Language dropdown for this entity has proven
        # unreliable (doesn't consistently reflect what's actually
        # selectable), so metadata.language is no longer trusted as the
        # primary source — configured_language (now a proper dropdown in
        # config_flow.py) wins, with metadata.language only as a fallback
        # for the rare case the option is somehow unset.
        configured_language = str(
            self.entry.options.get(CONF_STT_LANGUAGE, DEFAULT_STT_LANGUAGE)
        ).strip()
        raw_language = (configured_language or metadata.language or DEFAULT_STT_LANGUAGE).strip()
        # Tolerate a region-qualified value (e.g. "en-US") from either
        # source — the backend (OpenAI transcription, matching Cartesia's
        # own convention on the TTS side) expects a bare ISO 639-1 code.
        language = raw_language.lower().split("-", 1)[0]
        if language not in SUPPORTED_LANGUAGES:
            language = DEFAULT_STT_LANGUAGE

        buffered_audio = bytearray()

        try:
            streaming_result = await self._client.stream_realtime_transcription(
                endpoint=DEFAULT_REALTIME_WS_ENDPOINT,
                stream=stream,
                language=language,
                client_session_id=session_id,
            )
            buffered_audio.extend(streaming_result.buffered_audio)
            if streaming_result.error_no == ERROR_NO_AUTHENTICATION:
                return stt.SpeechResult(VOICE_AGENT_ERROR_ACCOUNT_NOT_ACTIVE, stt.SpeechResultState.SUCCESS)
            if streaming_result.transcript:
                if _should_save_debug_audio(success=True):
                    await _save_debug_audio(
                        metadata,
                        bytes(buffered_audio),
                        session_id=session_id,
                        reason="realtime_success",
                    )
                return stt.SpeechResult(streaming_result.transcript, stt.SpeechResultState.SUCCESS)
            _LOGGER.warning(
                "Auri STT realtime streaming produced no transcript, falling back to multipart upload "
                "(error_no=%s) session_id=%s: %s",
                streaming_result.error_no or "unknown",
                session_id,
                streaming_result.error or "no transcript",
            )
            if _should_save_debug_audio(success=False):
                await _save_debug_audio(
                    metadata,
                    bytes(buffered_audio),
                    session_id=session_id,
                    reason=streaming_result.error_no or "no_transcript",
                )
        except Exception as err:
            _LOGGER.warning(
                "Auri STT realtime streaming failed, falling back to multipart upload session_id=%s: %s",
                session_id,
                err,
            )

        # Note: reader_task inside stream_realtime_transcription already consumed
        # the entire stream, so buffered_audio contains all captured audio.
        # Do not try to drain stream again to avoid race conditions.

        if buffered_audio:
            audio_chunks = buffered_audio
        else:
            # Stream already consumed by reader_task; buffered_audio is empty
            # because realtime either never sent audio or failed before capturing any
            audio_chunks = bytearray()

        if not audio_chunks:
            # No usable audio, not a technical failure: report as a successful turn
            # with no text so HA raises stt-no-text-recognized (silently ignored by
            # the satellite firmware) instead of stt-stream-failed (shows red/error).
            return stt.SpeechResult(None, stt.SpeechResultState.SUCCESS)

        if len(audio_chunks) < MIN_FALLBACK_AUDIO_BYTES:
            duration_ms = _estimate_audio_duration_ms(metadata, len(audio_chunks))
            _LOGGER.warning(
                "Auri STT fallback audio too short for transcription: %d bytes (%s ms) "
                "session_id=%s (threshold=%d bytes)",
                len(audio_chunks),
                duration_ms,
                session_id,
                MIN_FALLBACK_AUDIO_BYTES,
            )
            # Same as above: no content to give the user, but not a malfunction.
            return stt.SpeechResult(None, stt.SpeechResultState.SUCCESS)

        filename, mime_type, prepared_audio = _prepare_audio_for_upload(
            metadata,
            bytes(audio_chunks),
        )

        try:
            response = await self._client.post_multipart_audio(
                TRANSCRIBE_PATH,
                fields={
                    "pipeline_id": DEFAULT_STT_PIPELINE_ID,
                    "language": language,
                },
                audio=prepared_audio,
                filename=filename,
                audio_content_type=mime_type,
            )
            transcript = response.get("text") or response.get("transcription")
        except SaaSRequestError as err:
            if _is_empty_transcript_error(err):
                # Backend rejected the upload specifically because it found no
                # speech, not because anything malfunctioned: same
                # stt-no-text-recognized treatment as the other empty cases.
                return stt.SpeechResult(None, stt.SpeechResultState.SUCCESS)
            _LOGGER.error("Auri STT request failed: %s", err)
            return stt.SpeechResult(None, stt.SpeechResultState.ERROR)
        except Exception as err:
            _LOGGER.error("Auri STT request failed: %s", err)
            return stt.SpeechResult(None, stt.SpeechResultState.ERROR)

        if not transcript:
            # Backend responded fine, just had nothing to transcribe: same
            # stt-no-text-recognized treatment as the too-short cases above.
            return stt.SpeechResult(None, stt.SpeechResultState.SUCCESS)

        if _should_save_debug_audio(success=True):
            await _save_debug_audio(
                metadata,
                bytes(audio_chunks),
                session_id=session_id,
                reason="fallback_success",
            )

        return stt.SpeechResult(transcript, stt.SpeechResultState.SUCCESS)


def _estimate_audio_duration_ms(metadata: stt.SpeechMetadata, audio_bytes_len: int) -> int | None:
    """Estimate PCM audio duration in ms from stream metadata, or None if unknown."""
    bytes_per_second = (
        metadata.sample_rate.value * (metadata.bit_rate.value // 8) * metadata.channel.value
    )
    if bytes_per_second <= 0:
        return None
    return int(audio_bytes_len / bytes_per_second * 1000)


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


def _is_empty_transcript_error(err: SaaSRequestError) -> bool:
    """Return True when a 400 response means the backend found no speech to transcribe."""
    if err.status_code != 400 or not err.response_body:
        return False
    try:
        payload = json.loads(err.response_body)
    except json.JSONDecodeError:
        return False
    message = str(payload.get("message") or "").lower()
    return "empty" in message and "transcri" in message


def _has_wav_header(audio_bytes: bytes) -> bool:
    """Return True when bytes already contain a RIFF/WAVE header."""
    return len(audio_bytes) >= 12 and audio_bytes[:4] == b"RIFF" and audio_bytes[8:12] == b"WAVE"


def _should_save_debug_audio(*, success: bool) -> bool:
    """Decide whether to persist captured audio for this attempt, per debug flags."""
    if DEBUG_WAV_ALWAYS:
        return True
    return DEBUG_WAV_ON_FAILURE and not success


async def _save_debug_audio(
    metadata: stt.SpeechMetadata,
    audio_bytes: bytes,
    *,
    session_id: str,
    reason: str,
) -> None:
    """Persist raw captured audio to disk for manual inspection (see DEBUG_WAV_* flags)."""
    if not audio_bytes:
        return

    _, _, wav_bytes = _prepare_audio_for_upload(metadata, audio_bytes)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    file_path = f"{_DEBUG_FAILED_AUDIO_DIR}/{timestamp}_stt_{reason}_{session_id}.wav"
    try:
        await write_bytes_to_file(file_path, "wb", wav_bytes)
        _LOGGER.info(
            "Auri STT saved debug audio session_id=%s reason=%s path=%s bytes=%d",
            session_id,
            reason,
            file_path,
            len(wav_bytes),
        )
    except OSError as err:
        _LOGGER.warning(
            "Auri STT failed to save debug audio session_id=%s: %s",
            session_id,
            err,
        )
