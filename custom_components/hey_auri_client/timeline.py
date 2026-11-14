"""Where a voice turn's time goes, from the end of speech to the reply's first audio.

Logs one debug line per turn ("Auri turn timeline"), for measuring changes on a real satellite:
turn on debug logging for this module (or the whole integration), speak, read the log. The steps
are matched by order on this hub, so two satellites talking at the same moment can mix: it's a
measuring aid, not an exact trace.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import logging
from time import monotonic

from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)
_KEY = "turn_timeline"
# A timeline this old belongs to an earlier turn: an announcement's audio isn't a reply.
_STALE_SECONDS = 60.0


@dataclass(slots=True)
class TurnTimeline:
    """Moments of one turn (time.monotonic seconds); None until reached."""

    speech_end: float | None = None
    transcript: float | None = None
    agent_start: float | None = None
    context_ready: float | None = None
    agent_done: float | None = None
    tts_request: float | None = None
    first_audio: float | None = None
    cloud_seconds: float = 0.0
    cloud_requests: int = 0
    created: float = field(default_factory=monotonic)


def _current(hass: HomeAssistant) -> TurnTimeline | None:
    timeline = hass.data.get(DOMAIN, {}).get(_KEY)
    if timeline is None or monotonic() - timeline.created > _STALE_SECONDS:
        return None
    return timeline


def speech_ended(hass: HomeAssistant) -> None:
    """A new voice turn: the satellite's audio has ended."""
    hass.data.setdefault(DOMAIN, {})[_KEY] = TurnTimeline(speech_end=monotonic())


def mark(hass: HomeAssistant, step: str) -> None:
    """Note when a step was reached (transcript, agent_start, context_ready, agent_done, tts_request)."""
    timeline = _current(hass)
    if step == "agent_start" and timeline is None:
        # A typed turn, or one whose audio didn't come through Auri's speech-to-text.
        timeline = TurnTimeline()
        hass.data.setdefault(DOMAIN, {})[_KEY] = timeline
    if timeline is not None and getattr(timeline, step) is None:
        setattr(timeline, step, monotonic())


def cloud_request(hass: HomeAssistant, seconds: float) -> None:
    """One request to Auri's cloud during the turn, and how long it took."""
    timeline = _current(hass)
    if timeline is not None:
        timeline.cloud_seconds += seconds
        timeline.cloud_requests += 1


def first_audio(hass: HomeAssistant) -> None:
    """The reply's first audio reached Home Assistant: log the turn and close it."""
    timeline = _current(hass)
    if timeline is None or timeline.agent_done is None or timeline.first_audio is not None:
        return
    timeline.first_audio = monotonic()
    _LOGGER.debug("Auri turn timeline: %s", describe(timeline))


def describe(timeline: TurnTimeline) -> str:
    """The turn's steps in milliseconds, each from the previous moment reached."""

    def span(start: float | None, end: float | None) -> str:
        return "-" if start is None or end is None else f"{int((end - start) * 1000)}"

    start = timeline.speech_end if timeline.speech_end is not None else timeline.agent_start
    return (
        f"transcript {span(timeline.speech_end, timeline.transcript)} ms"
        f" | to agent {span(timeline.transcript, timeline.agent_start)} ms"
        f" | context and memory {span(timeline.agent_start, timeline.context_ready)} ms"
        f" | cloud {int(timeline.cloud_seconds * 1000)} ms in {timeline.cloud_requests} requests"
        f" | agent total {span(timeline.agent_start, timeline.agent_done)} ms"
        f" | to speech request {span(timeline.agent_done, timeline.tts_request)} ms"
        f" | speech first audio {span(timeline.tts_request, timeline.first_audio)} ms"
        f" | total {span(start, timeline.first_audio)} ms"
        + ("" if timeline.speech_end is not None else " (typed or not via Auri's speech-to-text)")
    )
