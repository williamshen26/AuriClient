"""Turns heard from a mic left open after Auri's reply, and how many come in a row.

After a reply, a voice satellite, the phone app or the web UI can keep listening so the user
answers without the wake word (continue_conversation). Auri asks for that by ending its reply
with [listen]; the backend reads the tag and sends `listen`. Per conversation this keeps a count
and the time of the last reply that kept listening:
- a reply with [listen] keeps listening and adds one, until the count passes MAX_FOLLOW_UP_ROUNDS;
  then the mic closes and the next turn needs the wake word;
- a reply without [listen] (the fast path, an error, ignored speech, Auri's choice) sets the
  count back to 0;
- a turn within FOLLOW_UP_WINDOW_SECONDS of a reply that kept listening came from the open mic.
  The client then tells the backend (context.follow_up), so Auri may stay silent when it wasn't
  said to it. Any other turn was started by the user and starts over.
- a satellite's turn right after its firmware said the user woke it (esphome.auri_wake_word: the
  wake word or the button, also in the middle of a reply) was started by the user, whatever the
  window says: it starts over and is never treated as from the open mic.

Home Assistant doesn't tell an agent how a request started. Without the wake-word event (stock
firmware, the phone app), the window can't tell a wake-word request made soon after an unanswered
follow-up from an answer, so such a request is treated as from the open mic too. That only means
Auri may ignore it if it clearly wasn't said to Auri, so the limit closes the mic but never
refuses an ordinary turn.

Interrupting Auri is by the wake word. Talking over it without the wake word was tried and
dropped (2026-11-11): a Voice PE's speech detector hears Auri's own voice almost as surely as the
user's, so the satellite can't tell them apart.
"""
from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
import re
import time

from ..const import MAX_FOLLOW_UP_ROUNDS

# How long after a reply that kept listening a turn still counts as from the open mic: the reply
# itself, the 15 s Home Assistant listens for speech, and the turn.
FOLLOW_UP_WINDOW_SECONDS = 120.0
# How long after a satellite said the user woke it its next turn is that request: the speech,
# up to the 15 s Home Assistant listens for it, and speech-to-text.
WAKE_WINDOW_SECONDS = 30.0
_MAX_CONVERSATIONS = 200

# The echo guard: a satellite that opens its mic too early hears its own reply (ESPHome before
# 2026.11). Text with at least this many letters and digits (in any script), most of whose
# three-character pieces are in Auri's last reply, is that reply coming back.
_ECHO_MIN_CHARS = 20
_ECHO_SHARE = 0.8
_NOT_A_LETTER = re.compile(r"[\W_]+")


@dataclass
class _Chain:
    count: int = 0
    listened_at: float | None = None  # the last reply that kept listening
    last_reply: str = ""


@dataclass(frozen=True)
class TurnStart:
    """What the tracker knows about a turn before it goes to the backend."""

    open_mic: bool  # heard from a mic left open after a reply: send context.follow_up
    echo: bool  # it's Auri's own last reply coming back: answer silently
    woken: bool = False  # the satellite said the user started it (wake word or button)


def is_echo(text: str, reply: str) -> bool:
    """Whether a transcript is mostly Auri's own reply, heard back. The same in every language."""
    heard = _NOT_A_LETTER.sub("", text.casefold())
    said = _NOT_A_LETTER.sub("", reply.casefold())
    if len(heard) < _ECHO_MIN_CHARS or not said:
        return False
    pieces = [heard[i : i + 3] for i in range(len(heard) - 2)]
    said_pieces = {said[i : i + 3] for i in range(len(said) - 2)}
    return sum(piece in said_pieces for piece in pieces) >= _ECHO_SHARE * len(pieces)


class FollowUpTracker:
    """Open-mic turns and their count, per conversation (the satellite's own, or the app's)."""

    def __init__(
        self,
        *,
        max_rounds: int = MAX_FOLLOW_UP_ROUNDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_rounds = max_rounds
        self._clock = clock
        self._chains: OrderedDict[str, _Chain] = OrderedDict()
        self._woken_at: dict[str, float] = {}

    def note_wake(self, device_id: str) -> None:
        """A satellite's firmware started a request for the user: the wake word or the button."""
        self._woken_at[device_id] = self._clock()

    def begin_turn(self, conversation_id: str, device_id: str | None, text: str) -> TurnStart:
        now = self._clock()
        chain = self._chain(conversation_id)
        woken_at = self._woken_at.pop(device_id, None) if device_id else None
        woken = woken_at is not None and now - woken_at <= WAKE_WINDOW_SECONDS
        # The user started this one (the wake word or the button): never from the open mic.
        open_mic = (
            not woken and chain.listened_at is not None and now - chain.listened_at <= FOLLOW_UP_WINDOW_SECONDS
        )
        if not open_mic:
            chain.count = 0  # the user started this one: a new chain
        return TurnStart(
            open_mic=open_mic,
            echo=open_mic and is_echo(text, chain.last_reply),
            woken=woken,
        )

    def finish_turn(self, conversation_id: str, *, listen: bool, reply: str = "") -> bool:
        """Auri answered (or didn't): whether the mic stays open, within the limit."""
        chain = self._chain(conversation_id)
        if listen:
            chain.count += 1
            keep = chain.count <= self._max_rounds
        else:
            keep = False
            chain.count = 0
        chain.listened_at = self._clock() if keep else None
        chain.last_reply = reply
        return keep

    def _chain(self, conversation_id: str) -> _Chain:
        chain = self._chains.pop(conversation_id, None) or _Chain()
        self._chains[conversation_id] = chain  # most recently used last
        while len(self._chains) > _MAX_CONVERSATIONS:
            self._chains.popitem(last=False)
        return chain
