"""SaaS client transport and realtime STT helpers."""
from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterable, Awaitable, Callable
import asyncio
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

from .const import (
    ERROR_NO_AIOHTTP_CLIENT,
    ERROR_NO_AUDIO_TOO_SHORT,
    ERROR_NO_AUTHENTICATION,
    ERROR_NO_CONNECTION,
    ERROR_NO_EMPTY_TRANSCRIPT,
    ERROR_NO_LOST_COLLISION,
    ERROR_NO_NO_AUDIO_CHUNKS,
    ERROR_NO_NO_UPSTREAM_AUDIO,
    ERROR_NO_TIMEOUT,
    ERROR_NO_UNHANDLED,
    ERROR_NO_UPSTREAM_REPORTED_ERROR,
    ERROR_NO_WS_CLOSED_BEFORE_END,
    ERROR_NO_WS_CLOSED_DURING_STREAM,
    ERROR_NO_WS_CLOSED_NO_TRANSCRIPT,
    LATENCY_MEASUREMENT_KEY_AUDIO,
    LATENCY_MEASUREMENT_KEY_AUDIO_STREAM,
    LATENCY_MEASUREMENT_KEY_CONVERSATION,
    LATENCY_MEASUREMENT_KEY_TTS,
    LATENCY_MEASUREMENT_KEY_TTS_STREAM,
    MIN_REALTIME_AUDIO_BYTES,
    TTS_STREAM_PATH,
)
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
    error_no: str | None = None


class AuthenticationError(Exception):
    """Raised when realtime websocket authentication is rejected."""


class SaaSClient:
    """HTTP client for the SaaS conversation backend."""

    def __init__(
        self,
        hass: HomeAssistant,
        endpoint: str,
        timeout: int,
        client_id: str,
        shared_secret: str,
        tts_stream_endpoint: str | None = None,
        metrics_service: RequestLatencyMetricService | None = None,
    ) -> None:
        self.hass = hass
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout
        self.client_id = client_id
        self.shared_secret = shared_secret
        self.tts_stream_endpoint = (
            tts_stream_endpoint.strip() if tts_stream_endpoint else None
        )
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

    def _build_websocket_auth_headers(self, path: str) -> dict[str, str]:
        """Build HMAC-signed headers for WebSocket upgrade request (GET with empty body)."""
        timestamp = str(int(datetime.now(UTC).timestamp()))
        nonce = secrets.token_hex(16)
        signature = hmac.new(
            self.shared_secret.encode("utf-8"),
            "\n".join(["GET", path, timestamp, nonce, ""]).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return {
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
            measurement_key=LATENCY_MEASUREMENT_KEY_CONVERSATION,
        )

    async def post_raw(
        self,
        path: str,
        *,
        body: str | bytes,
        content_type: str,
        measurement_key: str = LATENCY_MEASUREMENT_KEY_CONVERSATION,
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

    async def post_binary(
        self,
        path: str,
        *,
        body: str | bytes,
        content_type: str,
        measurement_key: str = LATENCY_MEASUREMENT_KEY_TTS,
        timeout_seconds: int | None = None,
    ) -> tuple[bytes, str]:
        """POST and return binary response bytes with content-type."""
        started = perf_counter()
        success = False
        status_code: int | None = None
        error_type: str | None = None
        effective_timeout = timeout_seconds or self.timeout
        timeout = aiohttp.ClientTimeout(total=effective_timeout)
        headers = self._build_signed_headers(path, body, content_type=content_type)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    f"{self.endpoint}{path}",
                    data=body,
                    headers=headers,
                ) as response:
                    status_code = response.status
                    response_bytes = await response.read()
                    if response.status >= 400:
                        error_type = "http_error"
                        raise SaaSRequestError(
                            f"SaaS request failed: {response.status} {_extract_error_message(response_bytes)}",
                            status_code=response.status,
                            response_body=response_bytes.decode("utf-8", errors="ignore"),
                        )
                    success = True
                    response_content_type = str(response.headers.get("Content-Type") or "application/octet-stream")
                    return response_bytes, response_content_type
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

    async def stream_binary(
        self,
        path: str,
        *,
        body: str | bytes,
        content_type: str,
        measurement_key: str = LATENCY_MEASUREMENT_KEY_TTS_STREAM,
        chunk_size: int = 4096,
        timeout_seconds: int | None = None,
        request_url: str | None = None,
    ) -> AsyncGenerator[bytes, None]:
        """POST and yield binary response chunks progressively."""
        started = perf_counter()
        success = False
        status_code: int | None = None
        error_type: str | None = None
        effective_timeout = timeout_seconds or self.timeout
        timeout = aiohttp.ClientTimeout(
            total=None,
            connect=effective_timeout,
            sock_connect=effective_timeout,
            sock_read=effective_timeout,
        )
        headers = self._build_signed_headers(path, body, content_type=content_type)
        url = request_url or f"{self.endpoint}{path}"

        session = aiohttp.ClientSession(timeout=timeout)
        response: aiohttp.ClientResponse | None = None
        try:
            response = await session.post(
                url,
                data=body,
                headers=headers,
            )
            status_code = response.status
            if response.status >= 400:
                error_type = "http_error"
                response_bytes = await response.read()
                raise SaaSRequestError(
                    f"SaaS request failed: {response.status} {_extract_error_message(response_bytes)}",
                    status_code=response.status,
                    response_body=response_bytes.decode("utf-8", errors="ignore"),
                )

            async for chunk in response.content.iter_chunked(chunk_size):
                if chunk:
                    yield chunk

            success = True
        except asyncio.CancelledError:
            error_type = "cancelled"
            raise
        except Exception as err:
            if error_type is None:
                error_type = type(err).__name__
            raise
        finally:
            try:
                if response is not None:
                    response.release()
            finally:
                await session.close()
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
            measurement_key=LATENCY_MEASUREMENT_KEY_AUDIO,
        )

    async def stream_realtime_transcription(
        self,
        *,
        endpoint: str,
        language: str,
        stream: AsyncIterable[bytes],
        client_session_id: str | None = None,
        collision_register: Callable[[bytearray], Awaitable[None]] | None = None,
        collision_wait: Callable[[], Awaitable[bool]] | None = None,
    ) -> SaaSRealtimeSTTResult:
        """Stream STT audio to realtime gateway and return transcript + buffered audio."""
        session_id = client_session_id or "unknown"
        post_speech_started: float | None = None
        post_speech_latency_ms: int | None = None
        success = False
        status_code: int | None = None
        error_type: str | None = None

        buffered_audio = bytearray()
        sent_any = False
        realtime_bytes_sent = 0
        header_processed = False
        header_probe = bytearray()
        ratecv_state: tuple[int, ...] | None = None
        final_completed_transcript: str | None = None
        completed_transcripts: list[str] = []
        transcript_chunks: list[str] = []
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        path = _extract_path_from_url(endpoint)
        ws_auth_headers = self._build_websocket_auth_headers(path)

        # Unbounded: the gateway now defers accepting this connection until its own
        # upstream OpenAI connect resolves (up to connect_timeout), so this queue must
        # not backpressure-block read_ha_audio() while that's in flight. Chunks are
        # small and bounded by utterance length, so unbounded growth here is safe.
        audio_queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        stream_stats: dict[str, Any] = {
            "chunk_count": 0,
            "ended_reason": "stream_exhausted",
            "time_to_first_chunk_ms": None,
            "elapsed_ms": None,
        }

        async def read_ha_audio() -> None:
            read_started = perf_counter()
            first_chunk_at: float | None = None
            try:
                async for chunk in stream:
                    if not chunk:
                        stream_stats["ended_reason"] = "empty_chunk_received"
                        _LOGGER.warning(
                            "Auri realtime STT stream reader got a falsy chunk and is ending "
                            "capture early session_id=%s chunk_count=%d elapsed_ms=%d",
                            session_id,
                            stream_stats["chunk_count"],
                            int((perf_counter() - read_started) * 1000),
                        )
                        break
                    if first_chunk_at is None:
                        first_chunk_at = perf_counter()
                    stream_stats["chunk_count"] += 1
                    buffered_audio.extend(chunk)
                    await audio_queue.put(chunk)
            finally:
                stream_stats["elapsed_ms"] = int((perf_counter() - read_started) * 1000)
                stream_stats["time_to_first_chunk_ms"] = (
                    int((first_chunk_at - read_started) * 1000)
                    if first_chunk_at is not None
                    else None
                )
                _LOGGER.info(
                    "Auri realtime STT stream reader finished session_id=%s reason=%s "
                    "chunk_count=%d bytes=%d elapsed_ms=%d time_to_first_chunk_ms=%s",
                    session_id,
                    stream_stats["ended_reason"],
                    stream_stats["chunk_count"],
                    len(buffered_audio),
                    stream_stats["elapsed_ms"],
                    stream_stats["time_to_first_chunk_ms"],
                )
                await audio_queue.put(None)

        reader_task = asyncio.create_task(read_ha_audio())

        if collision_register is not None:
            # As early as possible, before any per-session network setup (gateway
            # connect, auth) whose variable latency would otherwise skew which
            # sessions land within the same collision window as each other.
            await collision_register(buffered_audio)

        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.ws_connect(endpoint, headers=ws_auth_headers, heartbeat=20) as ws:
                    await ws.send_json(
                        {
                            "type": "session.update",
                            "session": {
                                "language": language,
                                "input_audio_format": "pcm16",
                                "client_session_id": client_session_id,
                            },
                        }
                    )

                    if collision_wait is not None:
                        # Connection is already warm; buffered_audio keeps growing via
                        # read_ha_audio() regardless of whether we've started draining
                        # audio_queue, so waiting here doesn't lose any captured audio -
                        # it just delays when we start forwarding it upstream. Whatever
                        # of the window already elapsed during connect setup is time we
                        # don't have to wait again here.
                        cleared = await collision_wait()
                        if not cleared:
                            _LOGGER.info(
                                "Auri realtime STT session_id=%s lost wake-word collision "
                                "arbitration, aborting before sending audio upstream",
                                session_id,
                            )
                            return SaaSRealtimeSTTResult(
                                transcript=None,
                                buffered_audio=bytes(buffered_audio),
                                error="Lost wake-word collision arbitration",
                                error_no=ERROR_NO_LOST_COLLISION,
                            )

                    while True:
                        chunk = await audio_queue.get()
                        if chunk is None:
                            break

                        if ws.closed or ws.close_code is not None:
                            if ws.close_code == 1008:
                                raise AuthenticationError(
                                    "Realtime websocket authentication failed (close_code=1008)"
                                )
                            raise ConnectionError(
                                f"Realtime websocket closed before audio finished (close_code={ws.close_code})"
                            )

                        sent_any = True
                        outbound_payload = chunk
                        if not header_processed:
                            header_probe.extend(chunk)
                            (
                                header_processed,
                                outbound_payload,
                                _,
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
                        _LOGGER.warning(
                            "Auri realtime STT no audio chunks sent before stream ended session_id=%s",
                            session_id,
                        )
                        return SaaSRealtimeSTTResult(
                            transcript=None,
                            buffered_audio=bytes(buffered_audio),
                            error="No audio chunks were sent",
                            error_no=ERROR_NO_NO_AUDIO_CHUNKS,
                        )

                    if not header_processed:
                        # Stream ended before header probe was resolved; flush any pending bytes.
                        (
                            _,
                            pending_payload,
                            _,
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

                    if len(buffered_audio) < MIN_REALTIME_AUDIO_BYTES:
                        duration_ms = int(len(buffered_audio) / 32000 * 1000)
                        _LOGGER.warning(
                            "Auri realtime STT aborted because audio was too short: %d bytes (%d ms) "
                            "session_id=%s chunk_count=%d stream_ended_reason=%s "
                            "stream_elapsed_ms=%s time_to_first_chunk_ms=%s (threshold=%d bytes)",
                            len(buffered_audio),
                            duration_ms,
                            session_id,
                            stream_stats["chunk_count"],
                            stream_stats["ended_reason"],
                            stream_stats["elapsed_ms"],
                            stream_stats["time_to_first_chunk_ms"],
                            MIN_REALTIME_AUDIO_BYTES,
                        )
                        return SaaSRealtimeSTTResult(
                            transcript=None,
                            buffered_audio=bytes(buffered_audio),
                            error="Realtime audio too short",
                            error_no=ERROR_NO_AUDIO_TOO_SHORT,
                        )

                    if realtime_bytes_sent <= 0:
                        return SaaSRealtimeSTTResult(
                            transcript=None,
                            buffered_audio=bytes(buffered_audio),
                            error="No realtime audio bytes were sent upstream",
                            error_no=ERROR_NO_NO_UPSTREAM_AUDIO,
                        )

                    if ws.closed or ws.close_code is not None:
                        if ws.close_code == 1008:
                            raise AuthenticationError(
                                "Realtime websocket authentication failed (close_code=1008)"
                            )
                        raise ConnectionError(
                            f"Realtime websocket closed before audio.end (close_code={ws.close_code})"
                        )

                    await ws.send_json({"type": "audio.end"})
                    post_speech_started = perf_counter()
                    idle_timeout_seconds = 0.3
                    max_wait_seconds = 1.5
                    last_transcription_event_at = post_speech_started
                    saw_transcription_event = False

                    while True:
                        if post_speech_started is None:
                            remaining_total = max_wait_seconds
                        else:
                            elapsed = perf_counter() - post_speech_started
                            remaining_total = max_wait_seconds - elapsed

                        if remaining_total <= 0:
                            break

                        receive_timeout = min(idle_timeout_seconds, remaining_total)

                        try:
                            message = await ws.receive(timeout=receive_timeout)
                        except asyncio.TimeoutError:
                            if (
                                saw_transcription_event
                                and perf_counter() - last_transcription_event_at >= idle_timeout_seconds
                            ):
                                break
                            continue

                        if message.type == aiohttp.WSMsgType.TEXT:
                            done, delta_fragment, completed_text, error_message = _extract_realtime_transcript_fragment(
                                message.data
                            )
                            if error_message:
                                raise ConnectionError(error_message)
                            if delta_fragment:
                                transcript_chunks.append(delta_fragment)
                                saw_transcription_event = True
                                last_transcription_event_at = perf_counter()
                            if completed_text:
                                completed = completed_text.strip()
                                if completed:
                                    completed_transcripts.append(completed)
                                    saw_transcription_event = True
                                    last_transcription_event_at = perf_counter()
                            if done:
                                # Continue receiving until idle/max wait so paused speech segments can merge.
                                continue
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
                                if ws.close_code == 1008:
                                    raise AuthenticationError(
                                        "Realtime websocket authentication failed (close_code=1008)"
                                    )
                                raise ConnectionError(
                                    f"Realtime websocket closed without transcript (close_code={ws.close_code})"
                                )
                            break

            if completed_transcripts:
                final_completed_transcript = " ".join(completed_transcripts).strip()

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

            if not transcript:
                duration_ms = int(len(buffered_audio) / 32000 * 1000)
                _LOGGER.warning(
                    "Auri realtime STT returned empty transcript after sending %d bytes and "
                    "buffering %d bytes (%d ms of 16k audio) session_id=%s",
                    realtime_bytes_sent,
                    len(buffered_audio),
                    duration_ms,
                    session_id,
                )

            return SaaSRealtimeSTTResult(
                transcript=transcript or None,
                buffered_audio=bytes(buffered_audio),
                post_speech_latency_ms=post_speech_latency_ms,
                error=None if transcript else "Realtime transcript was empty",
                error_no=None if transcript else ERROR_NO_EMPTY_TRANSCRIPT,
            )
        except Exception as err:
            error_type = type(err).__name__
            if not reader_task.done():
                await reader_task
            return SaaSRealtimeSTTResult(
                transcript=None,
                buffered_audio=bytes(buffered_audio),
                post_speech_latency_ms=post_speech_latency_ms,
                error=str(err),
                error_no=self._classify_realtime_error_no(err),
            )
        finally:
            if not reader_task.done():
                await reader_task
            if post_speech_latency_ms is not None:
                await self._record_request_latency(
                    path=path,
                    measurement_key=LATENCY_MEASUREMENT_KEY_AUDIO_STREAM,
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

    def _classify_realtime_error_no(self, err: Exception) -> str:
        """Map runtime exceptions to stable realtime STT error numbers."""
        if isinstance(err, AuthenticationError):
            return ERROR_NO_AUTHENTICATION

        if isinstance(err, asyncio.TimeoutError):
            return ERROR_NO_TIMEOUT

        if isinstance(err, aiohttp.ClientError):
            return ERROR_NO_AIOHTTP_CLIENT

        if isinstance(err, ConnectionError):
            message = str(err)
            if "closed before audio finished" in message:
                return ERROR_NO_WS_CLOSED_DURING_STREAM
            if "closed before audio.end" in message:
                return ERROR_NO_WS_CLOSED_BEFORE_END
            if "closed without transcript" in message:
                return ERROR_NO_WS_CLOSED_NO_TRANSCRIPT
            if "Realtime upstream error" in message:
                return ERROR_NO_UPSTREAM_REPORTED_ERROR
            return ERROR_NO_CONNECTION

        return ERROR_NO_UNHANDLED

    async def send_user_turn(self, payload: dict[str, Any]) -> SaaSTurnResponse:
        response = await self.post_json("/conversation", payload)
        return normalize_turn_response(response)

    async def send_tool_results(self, payload: dict[str, Any]) -> SaaSTurnResponse:
        response = await self.post_json("/conversation", payload)
        return normalize_turn_response(response)

    async def request_tts_audio(
        self,
        payload: dict[str, Any],
    ) -> bytes:
        """Request complete WAV audio by collecting stream chunks."""
        collected = bytearray()
        async for chunk in self.stream_tts_audio(payload):
            if chunk:
                collected.extend(chunk)
        return bytes(collected)

    async def stream_tts_audio(
        self,
        payload: dict[str, Any],
    ) -> AsyncGenerator[bytes, None]:
        """Request stream-first TTS WAV audio chunks."""
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True)

        if self.tts_stream_endpoint:
            request_url, sign_path = _resolve_tts_stream_target(self.tts_stream_endpoint)
            _LOGGER.warning(
                "Auri TTS stream target url=%s sign_path=%s configured_endpoint=%s",
                request_url,
                sign_path,
                self.tts_stream_endpoint,
            )
            async for chunk in self.stream_binary(
                sign_path,
                body=body,
                content_type="application/json",
                measurement_key=LATENCY_MEASUREMENT_KEY_TTS_STREAM,
                timeout_seconds=max(self.timeout, 120),
                request_url=request_url,
            ):
                yield chunk
            return

        _LOGGER.warning(
            "Auri TTS stream target url=%s sign_path=%s configured_endpoint=<empty>",
            f"{self.endpoint}{TTS_STREAM_PATH}",
            TTS_STREAM_PATH,
        )
        async for chunk in self.stream_binary(
            TTS_STREAM_PATH,
            body=body,
            content_type="application/json",
            measurement_key=LATENCY_MEASUREMENT_KEY_TTS_STREAM,
            timeout_seconds=max(self.timeout, 120),
        ):
            yield chunk


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


def _resolve_tts_stream_target(endpoint: str) -> tuple[str, str]:
    """Return (request_url, signing_path) for configured TTS stream endpoint.

    Accepts either:
    - full endpoint: https://.../tts/stream
    - base Function URL: https://.../
    """
    parsed = urlparse(endpoint.strip())
    normalized_path = (parsed.path or "/").strip()

    if not normalized_path or normalized_path == "/":
        # Function URL base was provided; append the stream route.
        normalized_path = TTS_STREAM_PATH
    elif normalized_path != "/" and normalized_path.endswith("/"):
        normalized_path = normalized_path.rstrip("/")

    request_url = parsed._replace(path=normalized_path).geturl()
    return request_url, normalized_path


def _extract_error_message(body: bytes) -> str:
    """Extract readable error text from HTTP response bytes."""
    if not body:
        return ""

    try:
        payload = json.loads(body.decode("utf-8", errors="ignore"))
    except json.JSONDecodeError:
        return body.decode("utf-8", errors="ignore")[:300]

    if isinstance(payload, dict):
        message = payload.get("message") or payload.get("error")
        if message:
            return str(message)

    return str(payload)[:300]


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
        return False, None, fragment or None, None

    if event_type in {"response.done", "response.completed"}:
        return True, None, None, None

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
