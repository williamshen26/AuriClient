"""Build the nightly transcript and exchange memories from archived turns.

Pure functions (no Home Assistant or database imports).
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, tzinfo
from typing import Any, Iterable, Sequence

MAX_MESSAGE_CHARS = 1500
MAX_EXCHANGE_CHARS = 1500
# Short device commands ("turn off the lights") say nothing worth
# remembering; longer ones may carry a preference ("...I like it dim at night").
DEVICE_CONTROL_MAX_IGNORED_WORDS = 12


def day_window(day: date, tz: tzinfo, boundary_hour: int) -> tuple[datetime, datetime]:
    """[start, end) of a local "day" that ends at boundary_hour. 23 or 25 hours across DST changes."""
    start = datetime.combine(day, time(hour=boundary_hour), tzinfo=tz)
    end = datetime.combine(day + timedelta(days=1), time(hour=boundary_hour), tzinfo=tz)
    return start, end


def current_day(now: datetime, boundary_hour: int) -> date:
    """The local day `now` belongs to, given the day boundary (01:30 with a 04:00 boundary is yesterday)."""
    return (now - timedelta(hours=boundary_hour)).date()


def speaker_label(row: dict[str, Any], *, guest: bool = False) -> str:
    """Who said a line. In guest memory, someone who didn't say their name is "Guest"."""
    if row.get("role") == "assistant":
        return "Assistant"
    name = (row.get("speaker_name") or "").strip()
    source = row.get("speaker_source")
    if source == "ha_user" and name:
        return name
    if source == "stated" and name:
        return f"{name} (stated)"
    if guest:
        return "Guest"
    area = (row.get("area") or "").strip()
    return f"Unknown speaker ({area})" if area else "Unknown speaker"


def _clip(text: str, limit: int) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[:limit].rstrip() + " …[truncated]"


def _excluded_exchanges(rows: Iterable[dict[str, Any]]) -> set[str]:
    excluded: set[str] = set()
    for row in rows:
        kind = row.get("kind")
        if kind == "automatic":
            excluded.add(row["exchange_id"])
        elif (
            kind == "device_control"
            and row.get("role") == "user"
            and len((row.get("content") or "").split()) <= DEVICE_CONTROL_MAX_IGNORED_WORDS
        ):
            excluded.add(row["exchange_id"])
    return excluded


def build_transcript(rows: Sequence[dict[str, Any]], tz: tzinfo, *, guest: bool = False) -> list[str]:
    """"[HH:MM] Label: text" lines in order, without automatic turns and short device commands."""
    excluded = _excluded_exchanges(rows)
    lines: list[str] = []
    for row in rows:
        if row["exchange_id"] in excluded:
            continue
        text = _clip(row.get("content") or "", MAX_MESSAGE_CHARS)
        if not text:
            continue
        local = row["created_at"].astimezone(tz)
        lines.append(f"[{local:%H:%M}] {speaker_label(row, guest=guest)}: {text}")
    return lines


def chunk_lines(lines: Sequence[str], max_chars: int) -> list[str]:
    """Pack whole lines into chunks of at most max_chars."""
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in lines:
        line = line[:max_chars]
        if current and size + len(line) + 1 > max_chars:
            chunks.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks


def split_chunk(chunk: str) -> list[str]:
    """Halve a chunk at a line boundary (used when a stage says the input was too big)."""
    lines = chunk.split("\n")
    if len(lines) < 2:
        middle = len(chunk) // 2
        return [chunk[:middle], chunk[middle:]]
    middle = len(lines) // 2
    return ["\n".join(lines[:middle]), "\n".join(lines[middle:])]


def exchanges_for_memory(rows: Sequence[dict[str, Any]], *, guest: bool = False) -> list[dict[str, Any]]:
    """One "earlier conversation" memory per exchange (user message plus reply)."""
    excluded = _excluded_exchanges(rows)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if row["exchange_id"] in excluded:
            continue
        grouped.setdefault(row["exchange_id"], []).append(row)

    exchanges: list[dict[str, Any]] = []
    for exchange_id, parts in grouped.items():
        parts.sort(key=lambda row: (row["created_at"], row.get("id") or 0))
        text = " / ".join(
            f"{speaker_label(row, guest=guest)}: {' '.join((row.get('content') or '').split())}"
            for row in parts
            if row.get("content")
        )
        if not text:
            continue
        exchanges.append(
            {
                "exchange_id": exchange_id,
                "conversation_id": parts[0]["conversation_id"],
                "occurred_at": parts[0]["created_at"],
                "content": _clip(text, MAX_EXCHANGE_CHARS),
            }
        )
    return exchanges
