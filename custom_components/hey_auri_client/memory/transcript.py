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
# Mishearing windows: voice requests from one device, each this close to the one before (the repeat or the yes
# that shows what was meant comes right after).
MISHEARING_WINDOW_GAP = timedelta(minutes=2)
MAX_MISHEARING_WINDOWS = 60  # AuriService's limit per request
MAX_WINDOW_EXCHANGES = 8
MAX_WINDOW_TEXT_CHARS = 300
MAX_WINDOW_TOOLS = 8


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


def mishearing_windows(rows: Sequence[dict[str, Any]], tz: tzinfo, *, guest: bool = False) -> list[dict[str, Any]]:
    """Runs of two or more voice exchanges from one device, each within two minutes of the one before.

    For /memory/mishearings: a request speech-to-text misheard is usually followed by the repeat, the correction
    or the yes to "did you mean" that shows what was meant. Unlike the digest's transcript, short device commands
    stay in (the meant request is often one); automatic turns and typed ones don't (archive meta "voice").
    Each exchange keeps its time as occurred_at, which isn't sent.
    """
    exchanges: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row.get("kind") == "automatic":
            continue
        exchange = exchanges.setdefault(
            row["exchange_id"],
            {"device": row.get("device_id") or "", "voice": False, "said": "", "reply": "", "speaker": "",
             "occurred_at": row["created_at"], "tools": [str(name) for name in row.get("tool_names") or []][:MAX_WINDOW_TOOLS]},
        )
        text = _clip(row.get("content") or "", MAX_WINDOW_TEXT_CHARS)
        if row.get("role") == "user":
            meta = row.get("meta") if isinstance(row.get("meta"), dict) else {}
            exchange.update(said=text, voice=bool(meta.get("voice")), speaker=speaker_label(row, guest=guest),
                            occurred_at=row["created_at"])
            if meta.get("taken_as"):  # sent as a saved mishearing's meant words (memory/mishearings.py)
                exchange["taken_as"] = _clip(str(meta["taken_as"]), MAX_WINDOW_TEXT_CHARS)
        else:
            exchange["reply"] = text
    by_device: dict[str, list[dict[str, Any]]] = {}
    for exchange in exchanges.values():
        if exchange["voice"] and exchange["said"] and exchange["device"]:
            by_device.setdefault(exchange["device"], []).append(exchange)

    runs: list[list[dict[str, Any]]] = []
    for device_exchanges in by_device.values():
        device_exchanges.sort(key=lambda exchange: exchange["occurred_at"])
        current: list[dict[str, Any]] = []
        for exchange in device_exchanges:
            if current and (
                exchange["occurred_at"] - current[-1]["occurred_at"] > MISHEARING_WINDOW_GAP
                or len(current) >= MAX_WINDOW_EXCHANGES
            ):
                if len(current) >= 2:
                    runs.append(current)
                current = []
            current.append(exchange)
        if len(current) >= 2:
            runs.append(current)
    runs.sort(key=lambda run: run[0]["occurred_at"])

    return [
        {
            "idx": index,
            "exchanges": [
                {
                    "at": f"{exchange['occurred_at'].astimezone(tz):%H:%M:%S}",
                    "speaker": exchange["speaker"],
                    "said": exchange["said"],
                    "reply": exchange["reply"],
                    "tools": exchange["tools"],
                    **({"taken_as": exchange["taken_as"]} if exchange.get("taken_as") else {}),
                    "occurred_at": exchange["occurred_at"],
                }
                for exchange in run
            ],
        }
        for index, run in enumerate(runs[:MAX_MISHEARING_WINDOWS], start=1)
    ]


def windows_to_send(windows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """The windows as /memory/mishearings takes them (without the times kept for the memories)."""
    return [
        {"idx": window["idx"],
         "exchanges": [{key: value for key, value in exchange.items() if key != "occurred_at"} for exchange in window["exchanges"]]}
        for window in windows
    ]


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
