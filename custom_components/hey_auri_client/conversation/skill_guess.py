"""The skill guess: a voice turn's words, sent as speech ends, for the skills its first model call needs.

Speech-to-text calls prefetch() with the words heard when speech ends, and again as more arrive. The turn
then waits a little for the answer (async_hints) and sends it as the turn's skill_hints. Auri's cloud
loads those skills before its first model call, which saves the get_skill_data round (about 0.6 s).
Voice turns only: a typed turn has no wait after speech to hide the call in, so it never waits for one.

At most one guess is in flight per hub: newer words wait for it, and only the newest are sent next. A
turn uses a guess only when the guess's words are the turn's text or its start, so two satellites
talking at once can't swap guesses, and only once. Any failure means no hints: the turn works as before.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from time import monotonic
from typing import Any

from homeassistant.core import HomeAssistant, callback

from ..memory.api import query_key

_LOGGER = logging.getLogger(__name__)

# How long a turn waits for a guess still in flight. The guess runs while the hub waits after the last
# words (300 ms), so it's usually back before the turn starts.
HINT_WAIT_SECONDS = 0.15
# A guess this old belongs to an earlier turn.
_STALE_SECONDS = 30.0
_REQUEST_TIMEOUT_SECONDS = 2
# Auri's cloud guesses nothing for longer text (dictation isn't a command).
_MAX_CHARS = 300


@dataclass(slots=True)
class _Guess:
    key: str  # memory.api.query_key of the words
    skills: list[str] | None  # None: the call failed
    finished: float  # time.monotonic()


class SkillGuesses:
    """The hub's skill guesses (one per entry; the agent owns it)."""

    def __init__(self, hass: HomeAssistant, client: Any) -> None:
        self.hass = hass
        self.client = client
        self._latest: _Guess | None = None  # the last finished guess, until a turn uses it
        self._task: asyncio.Task | None = None  # the guess in flight
        self._task_key: str | None = None
        # Newer words (and what to guess from instead), sent when the guess in flight finishes.
        self._pending: tuple[str, str | None] | None = None

    @callback
    def prefetch(self, words: str, *, guess_from: str | None = None) -> None:
        """Guess the skills for these words (the voice turn's so far), unless a guess for them is under way.

        guess_from: other words to guess from, for these words' turn: the meant words when these are a mishearing
        the household's memory knows (memory/mishearings.py), so "瓦登达" loads what "把灯打开" needs.
        """
        words = " ".join(str(words or "").split())
        key = query_key(words)
        if not key or len(words) > _MAX_CHARS:
            return
        if self._task is not None and not self._task.done():
            if key != self._task_key:
                self._pending = (words, guess_from)
            return
        latest = self._latest
        if latest is not None and latest.key == key and monotonic() - latest.finished < _STALE_SECONDS:
            return
        self._start(words, guess_from)

    def _start(self, words: str, guess_from: str | None = None) -> None:
        self._task_key = query_key(words)
        self._task = self.hass.async_create_background_task(
            self._async_guess(words, guess_from), "hey_auri_client skill guess"
        )

    async def _async_guess(self, words: str, guess_from: str | None = None) -> None:
        skills: list[str] | None
        text = " ".join(str(guess_from or "").split())[:_MAX_CHARS] or words
        try:
            response = await self.client.post_json(
                "/conversation", {"type": "skill_guess", "text": text}, timeout_seconds=_REQUEST_TIMEOUT_SECONDS
            )
            given = response.get("skills") if isinstance(response, dict) else None
            skills = [skill for skill in given if isinstance(skill, str) and skill] if isinstance(given, list) else []
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Skill guess failed: %s", err)
            skills = None
        self._latest = _Guess(query_key(words), skills, monotonic())
        pending, self._pending = self._pending, None
        if pending is not None:
            self._start(*pending)

    async def async_hints(self, text: str, budget: float = HINT_WAIT_SECONDS) -> tuple[list[str], str, float]:
        """The skills to send with this turn, what happened, and how long it waited (seconds).

        What happened, for the turn timeline: the skills, "none" (nothing to preload), "late" (still
        running when the wait ended), "failed", or "" (no guess: a typed turn).
        """
        key = query_key(text)
        started = monotonic()
        if not key:
            return [], "", 0.0
        deadline = started + budget
        while self._task is not None and not self._task.done():
            remaining = deadline - monotonic()
            if remaining <= 0:
                break
            await asyncio.wait({self._task}, timeout=remaining)
        waited = monotonic() - started
        guess = self._latest
        if guess is None or monotonic() - guess.finished > _STALE_SECONDS or not key.startswith(guess.key):
            in_flight = self._task is not None and not self._task.done()
            return [], "late" if in_flight else "", waited
        self._latest = None  # each guess serves one turn
        if guess.skills is None:
            return [], "failed", waited
        return list(guess.skills), " + ".join(guess.skills) or "none", waited
