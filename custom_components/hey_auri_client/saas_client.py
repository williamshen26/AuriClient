"""SaaS client transport and realtime STT helpers."""
from __future__ import annotations

from collections.abc import AsyncIterable
import audioop
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import hmac
import json
import logging
from time import perf_counter
import secrets
from typing import Any
from urllib.parse import urlparse

import aiohttp

from homeassistant.core import HomeAssistant

from .exceptions import SaaSRequestError
from .metric_service import RequestLatencyMetricService

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class SaaSTurnResponse:
    """Normalized SaaS response for one step in the loop."""

    response_type: str
    conversation_id: str
    content: str | None = None
    turn_id: str | None = None
    tool_calls: list[dict[str, Any]] | None = None


@dataclass(slots=True)
class SaaSRealtimeSTTResult:
    """Normalized realtime STT result for fallback-safe handling."""

    transcript: str | None
    buffered_audio: bytes
    post_speech_latency_ms: int | None = None
    error: str | None = None


class SaaSClient:
    """HTTP client for the SaaS conversation backend."""

    _LATENCY_MEASUREMENT_KEY_CONVERSATION = "conversation"
    _LATENCY_MEASUREMENT_KEY_AUDIO = "audio"
    _LATENCY_MEASUREMENT_KEY_AUDIO_STREAM = "audio_stream"

    def __init__(
        self,
        hass: HomeAssistant,
        endpoint: str,
        timeout: int,
        client_id: str,
        shared_secret: str,
        metrics_service: RequestLatencyMetricService | None = None,
    ) -> None:
        self.hass = hass
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout
        self.client_id = client_id
        self.shared_secret = shared_secret
        self.metrics_service = metrics_service

    def _build_signed_headers(
        self,
        path: str,
        body: str | bytes,
        *,
        content_type: str = "application/json",
    ) -> dict[str, str]:
        timestamp = str(int(datetime.now(UTC).timestamp()))
        nonce = secrets.token_hex(16)
        if isinstance(body, bytes):
            body_to_sign = body.decode("latin-1")
        else:
            body_to_sign = body
        signature = hmac.new(
            self.shared_secret.encode("utf-8"),
            "\n".join(["POST", path, timestamp, nonce, body_to_sign]).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return {
            "Content-Type": content_type,
            "X-Client-Id": self.client_id,
            "X-Timestamp": timestamp,
            "X-Nonce": nonce,
            "X-Signature": signature,
        }

    async def post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        return await self.post_raw(
            path,
            body=body,
            content_type="application/json",
            measurement_key=self._LATENCY_MEASUREMENT_KEY_CONVERSATION,
        )

    async def post_raw(
        self,
        path: str,
        *,
        body: str | bytes,
        content_type: str,
        measurement_key: str = _LATENCY_MEASUREMENT_KEY_CONVERSATION,
    ) -> dict[str, Any]:
        started = perf_counter()
        success = False
        status_code: int | None = None
        error_type: str | None = None
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        headers = self._build_signed_headers(path, body, content_type=content_type)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    f"{self.endpoint}{path}",
                    data=body,
                    headers=headers,
                ) as response:
                    status_code = response.status
                    body = await response.text()
                    if response.status >= 400:
                        error_type = "http_error"
                        raise SaaSRequestError(
                            f"SaaS request failed: {response.status} {body}",
                            status_code=response.status,
                            response_body=body,
                        )
                    try:
                        parsed = json.loads(body)
                        success = True
                        return parsed
                    except json.JSONDecodeError as err:
                        error_type = "json_decode_error"
                        raise SaaSRequestError(
                            f"Invalid JSON response: {body}",
                            response_body=body,
                        ) from err
        except Exception as err:
            if error_type is None:
                error_type = type(err).__name__
            raise
        finally:
            latency_ms = max(int((perf_counter() - started) * 1000), 0)
            await self._record_request_latency(
                path=path,
                measurement_key=measurement_key,
                latency_ms=latency_ms,
                success=success,
                status_code=status_code,
                error_type=error_type,
            )

    async def post_multipart_audio(
        self,
        path: str,
        *,
        fields: dict[str, str],
        audio: bytes,
        filename: str,
        audio_content_type: str,
    ) -> dict[str, Any]:
        body, content_header = _build_multipart_body(
            fields,
            audio=audio,
            filename=filename,
            audio_content_type=audio_content_type,
        )
        return await self.post_raw(
            path,
            body=body,
            content_type=content_header,
            measurement_key=self._LATENCY_MEASUREMENT_KEY_AUDIO,
        )

    async def stream_realtime_transcription(
        self,
        *,
        endpoint: str,
        language: str,
        stream: AsyncIterable[bytes],
    ) -> SaaSRealtimeSTTResult:
        """Stream STT audio to realtime gateway and return transcript + buffered audio."""
        started = perf_counter()
        post_speech_started: float | None = None
        post_speech_latency_ms: int | None = None
        success = False
        status_code: int | None = None
        error_type: str | None = None

        buffered_audio = bytearray()
        sent_any = False
        ha_chunk_count = 0
        ha_total_bytes = 0
        realtime_bytes_sent = 0
        first_chunk_size: int | None = None
        wav_header_stripped = False
        header_processed = False
        header_probe = bytearray()
        ratecv_state: tuple[int, ...] | None = None
        final_completed_transcript: str | None = None
        transcript_chunks: list[str] = []
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        path = _extract_path_from_url(endpoint)

        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.ws_connect(endpoint, heartbeat=20) as ws:
                    await ws.send_json(
                        {
                            "type": "session.update",
                            "session": {
                                "language": language,
                                "input_audio_format": "pcm16",
                            },
                        }
                    )

                    async for chunk in stream:
                        if not chunk:
                            break

                        if ws.closed or ws.close_code is not None:
                            raise ConnectionError(
                                f"Realtime websocket closed before audio finished (close_code={ws.close_code})"
                            )

                        ha_chunk_count += 1
                        ha_total_bytes += len(chunk)
                        if first_chunk_size is None:
                            first_chunk_size = len(chunk)
                        sent_any = True
                        buffered_audio.extend(chunk)

                        outbound_payload = chunk
                        if not header_processed:
                            header_probe.extend(chunk)
                            (
                                header_processed,
                                outbound_payload,
                                wav_header_stripped,
                            ) = _prepare_initial_realtime_audio_payload(
                                bytes(header_probe),
                                probe_limit=128,
                            )
                            if not header_processed:
                                continue

                        try:
                            if outbound_payload:
                                upsampled_payload, ratecv_state = _upsample_pcm16_mono_16k_to_24k(
                                    outbound_payload,
                                    ratecv_state,
                                )
                                if upsampled_payload:
                                    await ws.send_bytes(upsampled_payload)
                                    realtime_bytes_sent += len(upsampled_payload)
                        except ConnectionResetError as err:
                            raise ConnectionError(
                                "Realtime websocket transport closed while sending audio chunk"
                            ) from err
                        except RuntimeError as err:
                            raise ConnectionError(
                                "Realtime websocket is closing while sending audio chunk"
                            ) from err

                    if not sent_any:
                        return SaaSRealtimeSTTResult(
                            transcript=None,
                            buffered_audio=bytes(buffered_audio),
                            error="No audio chunks were sent",
                        )

                    if not header_processed:
                        # Stream ended before header probe was resolved; flush any pending bytes.
                        (
                            _,
                            pending_payload,
                            wav_header_stripped,
                        ) = _prepare_initial_realtime_audio_payload(
                            bytes(header_probe),
                            probe_limit=0,
                        )
                        if pending_payload:
                            upsampled_payload, ratecv_state = _upsample_pcm16_mono_16k_to_24k(
                                pending_payload,
                                ratecv_state,
                            )
                            if upsampled_payload:
                                await ws.send_bytes(upsampled_payload)
                                realtime_bytes_sent += len(upsampled_payload)

                    if realtime_bytes_sent <= 0:
                        return SaaSRealtimeSTTResult(
                            transcript=None,
                            buffered_audio=bytes(buffered_audio),
                            error="No realtime audio bytes were sent upstream",
                        )

                    _LOGGER.info(
                        "Auri realtime STT upstream prepared path=%s language=%s ha_chunks=%d ha_bytes=%d sent_bytes=%d first_chunk=%s wav_header_stripped=%s upsampled_to_hz=24000",
                        path,
                        language,
                        ha_chunk_count,
                        ha_total_bytes,
                        realtime_bytes_sent,
                        first_chunk_size,
                        wav_header_stripped,
                    )

                    if ws.closed or ws.close_code is not None:
                        raise ConnectionError(
                            f"Realtime websocket closed before audio.end (close_code={ws.close_code})"
                        )

                    await ws.send_json({"type": "audio.end"})
                    post_speech_started = perf_counter()

                    while True:
                        message = await ws.receive()
                        if message.type == aiohttp.WSMsgType.TEXT:
                            done, delta_fragment, completed_text, error_message = _extract_realtime_transcript_fragment(
                                message.data
                            )
                            if error_message:
                                raise ConnectionError(error_message)
                            if delta_fragment:
                                transcript_chunks.append(delta_fragment)
                            if done:
                                if completed_text:
                                    completed = completed_text.strip()
                                    assembled = "".join(transcript_chunks).strip()
                                    if assembled:
                                        if completed == assembled:
                                            final_completed_transcript = completed
                                        elif completed in assembled:
                                            final_completed_transcript = assembled
                                        elif assembled in completed:
                                            final_completed_transcript = completed
                                        else:
                                            # Completed event should win if it contains a final utterance.
                                            final_completed_transcript = completed
                                    else:
                                        final_completed_transcript = completed

                                if not transcript_chunks and not final_completed_transcript:
                                    raise ConnectionError(
                                        "Realtime stream finished without transcript fragments"
                                    )
                                break
                            continue

                        if message.type in {
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.CLOSING,
                            aiohttp.WSMsgType.ERROR,
                        }:
                            _LOGGER.warning(
                                "Auri realtime STT websocket ended type=%s close_code=%s",
                                message.type,
                                ws.close_code,
                            )
                            if not transcript_chunks:
                                raise ConnectionError(
                                    f"Realtime websocket closed without transcript (close_code={ws.close_code})"
                                )
                            break

            transcript = (
                (final_completed_transcript or "").strip()
                or "".join(transcript_chunks).strip()
            )
            success = bool(transcript)
            if post_speech_started is not None:
                post_speech_latency_ms = max(
                    int((perf_counter() - post_speech_started) * 1000),
                    0,
                )
            _LOGGER.info(
                "Auri realtime STT stream complete path=%s success=%s ha_chunks=%d ha_bytes=%d sent_bytes=%d post_speech_ms=%s wav_header_stripped=%s upsampled_to_hz=24000",
                path,
                success,
                ha_chunk_count,
                ha_total_bytes,
                realtime_bytes_sent,
                post_speech_latency_ms,
                wav_header_stripped,
            )
            return SaaSRealtimeSTTResult(
                transcript=transcript or None,
                buffered_audio=bytes(buffered_audio),
                post_speech_latency_ms=post_speech_latency_ms,
                error=None if transcript else "Realtime transcript was empty",
            )
        except Exception as err:
            error_type = type(err).__name__
            return SaaSRealtimeSTTResult(
                transcript=None,
                buffered_audio=bytes(buffered_audio),
                post_speech_latency_ms=post_speech_latency_ms,
                error=str(err),
            )
        finally:
            if post_speech_latency_ms is not None:
                await self._record_request_latency(
                    path=path,
                    measurement_key=self._LATENCY_MEASUREMENT_KEY_AUDIO_STREAM,
                    latency_ms=post_speech_latency_ms,
                    success=success,
                    status_code=status_code,
                    error_type=error_type,
                )

    async def _record_request_latency(
        self,
        *,
        path: str,
        measurement_key: str,
        latency_ms: int,
        success: bool,
        status_code: int | None,
        error_type: str | None,
    ) -> None:
        """Emit one request latency datapoint if metrics are enabled."""
        if self.metrics_service is None or not self.metrics_service.enabled:
            return

        await self.metrics_service.async_record_request_latency(
            client_id=self.client_id,
            path=path,
            measurement_key=measurement_key,
            latency_ms=latency_ms,
            success=success,
            status_code=status_code,
            error_type=error_type,
        )

    async def send_user_turn(self, payload: dict[str, Any]) -> SaaSTurnResponse:
        response = await self.post_json("/conversation", payload)
        return normalize_turn_response(response)

    async def send_tool_results(self, payload: dict[str, Any]) -> SaaSTurnResponse:
        response = await self.post_json("/conversation", payload)
        return normalize_turn_response(response)


def normalize_turn_response(response: dict[str, Any]) -> SaaSTurnResponse:
    """Normalize the SaaS contract into a typed object."""
    response_type = response.get("type")
    if response_type not in {"final", "tool_calls"}:
        raise SaaSRequestError(f"Unknown response type: {response}")

    return SaaSTurnResponse(
        response_type=response_type,
        conversation_id=response["conversation_id"],
        content=response.get("content"),
        turn_id=response.get("turn_id"),
        tool_calls=response.get("tool_calls"),
    )


def _build_multipart_body(
    fields: dict[str, str],
    *,
    audio: bytes,
    filename: str,
    audio_content_type: str,
) -> tuple[bytes, str]:
    """Build a deterministic multipart payload for body signing."""
    boundary = f"auri-{secrets.token_hex(12)}"
    boundary_bytes = boundary.encode("utf-8")

    chunks: list[bytes] = []
    for key, value in fields.items():
        chunks.extend(
            [
                b"--" + boundary_bytes + b"\r\n",
                f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode("utf-8"),
                str(value).encode("utf-8"),
                b"\r\n",
            ]
        )

    chunks.extend(
        [
            b"--" + boundary_bytes + b"\r\n",
            (
                f'Content-Disposition: form-data; name="audio"; filename="{filename}"\r\n'
            ).encode("utf-8"),
            f"Content-Type: {audio_content_type}\r\n\r\n".encode("utf-8"),
            audio,
            b"\r\n",
            b"--" + boundary_bytes + b"--\r\n",
        ]
    )

    body = b"".join(chunks)
    return body, f"multipart/form-data; boundary={boundary}"


def _extract_path_from_url(url: str) -> str:
    parsed = urlparse(url)
    return parsed.path or "/"


def _extract_realtime_transcript_fragment(
    raw_event: str,
) -> tuple[bool, str | None, str | None, str | None]:
    """Return (done, delta_fragment, completed_text, error_message) for one server event."""
    try:
        event = json.loads(raw_event)
    except json.JSONDecodeError:
        return False, None, None, None

    event_type = str(event.get("type") or "")

    if event_type == "error":
        error = event.get("error") or {}
        message = str(error.get("message") or event.get("message") or "Unknown realtime error")
        return False, None, None, f"Realtime upstream error: {message}"

    if event_type in {
        "conversation.item.input_audio_transcription.delta",
    }:
        fragment = str(event.get("delta") or "")
        return False, fragment or None, None, None

    if event_type in {
        "conversation.item.input_audio_transcription.completed",
    }:
        fragment = str(event.get("text") or event.get("transcript") or "")
        if not fragment:
            item = event.get("item") or {}
            if isinstance(item, dict):
                content = item.get("content")
                if isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict):
                            part_text = str(part.get("text") or part.get("transcript") or "")
                            if part_text:
                                fragment = part_text
                                break
        return True, None, fragment or None, None

    if event_type in {"response.done", "response.completed"}:
        return False, None, None, None

    return False, None, None, None


def _prepare_initial_realtime_audio_payload(
    initial_bytes: bytes,
    *,
    probe_limit: int,
) -> tuple[bool, bytes, bool]:
    """Prepare first payload bytes for realtime by stripping WAV header when possible."""
    if not initial_bytes:
        return False, b"", False

    if len(initial_bytes) < 12:
        return False, b"", False

    is_wav = initial_bytes[:4] == b"RIFF" and initial_bytes[8:12] == b"WAVE"
    if not is_wav:
        return True, initial_bytes, False

    data_index = initial_bytes.find(b"data")
    if data_index != -1 and len(initial_bytes) >= data_index + 8:
        return True, initial_bytes[data_index + 8 :], True

    if probe_limit > 0 and len(initial_bytes) < probe_limit:
        return False, b"", False

    stripped = _strip_wav_header_if_present(initial_bytes)
    if stripped is initial_bytes:
        return True, initial_bytes, False
    return True, stripped, True


def _strip_wav_header_if_present(audio: bytes) -> bytes:
    """Strip RIFF/WAVE header and return PCM bytes for realtime upstream."""
    if len(audio) >= 12 and audio[:4] == b"RIFF" and audio[8:12] == b"WAVE":
        data_index = audio.find(b"data")
        if data_index != -1 and len(audio) >= data_index + 8:
            return audio[data_index + 8 :]
        if len(audio) > 44:
            return audio[44:]
        return b""
    return audio


def _upsample_pcm16_mono_16k_to_24k(
    chunk: bytes,
    state: tuple[int, ...] | None,
) -> tuple[bytes, tuple[int, ...] | None]:
    """Upsample signed 16-bit mono PCM from 16kHz to 24kHz."""
    if not chunk:
        return b"", state
    converted, next_state = audioop.ratecv(chunk, 2, 1, 16000, 24000, state)
    return converted, next_state
