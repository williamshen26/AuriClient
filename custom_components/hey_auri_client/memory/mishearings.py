"""What speech-to-text misheard in this home, and what was meant: found by the nightly memory job, used on later turns.

The nightly job sends /memory/mishearings the day's voice windows (two or more voice requests from one device, each
within two minutes of the one before: transcript.mishearing_windows). It answers with pairs like heard "瓦登达",
meant "把灯打开" (2026-11-23: Auri took 瓦登达 for a place and searched for it). They're saved as memories of kind
"mishearing", with the words exactly as written in meta, in whatever language: a translation couldn't be matched
against what speech-to-text writes. Facts stay in English.

On a later voice turn the hub compares the words with the saved pairs (match): the same words once case, spaces and
punctuation are left out, in any script (no embedding and no minimum length: a misheard request is often a few
characters that recall skips). When they are a pair's heard words, the turn goes to Auri as the meant words, with
the heard ones as memory.heard_as (AuriService adds a rule: ask first before a door or gate); the skill guess at
speech end guesses from the meant words too (meant_words). Close words aren't matched: Auri's misheard-speech rule
asks about those, and the nightly job learns them as pairs of their own. A pair found again with other meant words
replaces the old one, so a wrong pair is corrected the night after.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .api import query_key

KIND = "mishearing"
# A pair that never comes up again is let go after this long; each time it's used or found again, it lasts this
# long from then.
TTL = timedelta(days=180)
MIN_KEY_CHARS = 2
MAX_WORDS_CHARS = 300


@dataclass(frozen=True, slots=True)
class Mishearing:
    """One saved pair (a current memory of kind "mishearing")."""

    memory_id: int
    heard: str
    meant: str
    expires_at: datetime | None = None

    @property
    def key(self) -> str:
        return query_key(self.heard)


def statement(heard: str, meant: str) -> str:
    """The memory's text: the same pair always gives the same text, so a repeat is found by its hash."""
    return f'Speech-to-text heard "{heard}" when someone said "{meant}".'


def clean_words(value: Any) -> str:
    return " ".join(str(value or "").split())[:MAX_WORDS_CHARS]


def from_rows(rows: Iterable[tuple[int, Any, datetime | None]]) -> list[Mishearing]:
    """Pairs from the store's (id, meta, expires_at) rows; rows without both words are left out."""
    pairs: list[Mishearing] = []
    for memory_id, meta, expires_at in rows:
        meta = meta if isinstance(meta, dict) else {}
        heard, meant = clean_words(meta.get("heard")), clean_words(meta.get("meant"))
        if len(query_key(heard)) >= MIN_KEY_CHARS and query_key(meant) and query_key(heard) != query_key(meant):
            pairs.append(Mishearing(int(memory_id), heard, meant, expires_at))
    return pairs


def match(text: str, pairs: Sequence[Mishearing], now: datetime) -> Mishearing | None:
    """The saved pair whose heard words these are (the newest, if several), or None."""
    key = query_key(text)
    if len(key) < MIN_KEY_CHARS:
        return None
    found = [pair for pair in pairs if pair.key == key and (pair.expires_at is None or pair.expires_at > now)]
    return max(found, key=lambda pair: pair.memory_id) if found else None


def meant_words(text: str, pairs: Sequence[Mishearing], now: datetime) -> str | None:
    """The meant words when these are a saved pair's heard words (for the skill guess)."""
    pair = match(text, pairs, now)
    return pair.meant if pair else None


def same_words(first: str, second: str) -> bool:
    return query_key(first) == query_key(second)


def heard_as(pair: Mishearing) -> dict[str, Any]:
    """The turn context's memory.heard_as for a voice turn sent as this pair's meant words."""
    return {"heard": pair.heard, "meant": pair.meant, "id": pair.memory_id}


def checked_pairs(response: Any, windows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """/memory/mishearings' answer, kept to pairs whose heard words are a request in the window they name.

    AuriService already checks the pairs against the windows; this keeps a malformed answer out of memory.
    Each pair gets the heard request's time (occurred_at), from the window.
    A pair whose meant words are the heard ones says an earlier pair was wrong (a request sent as its meant words,
    corrected with the words as heard): it comes back with "wrong": True, for the job to retire, never to save.
    """
    by_idx = {window["idx"]: window for window in windows}
    found: list[dict[str, Any]] = []
    items = response.get("mishearings") if isinstance(response, dict) else None
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        window = by_idx.get(item.get("window"))
        heard, meant = clean_words(item.get("heard")), clean_words(item.get("meant"))
        if window is None or len(query_key(heard)) < MIN_KEY_CHARS or not query_key(meant):
            continue
        said = next((exchange for exchange in window["exchanges"] if query_key(exchange["said"]) == query_key(heard)), None)
        if said is None:
            continue
        wrong = query_key(heard) == query_key(meant)
        if wrong and not said.get("taken_as"):
            continue
        found.append({"heard": said["said"], "meant": meant, "speaker": clean_words(item.get("speaker"))[:60],
                      "occurred_at": said.get("occurred_at"), **({"wrong": True} if wrong else {})})
    return found
