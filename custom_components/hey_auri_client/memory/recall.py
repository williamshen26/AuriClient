"""Pure helpers for recall: query terms, date hints, subject matching, note formatting.

No Home Assistant or database imports, so this module is unit-testable alone.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, tzinfo
import hashlib
import re
from time import monotonic
from typing import Iterable, Sequence

STOPWORDS = frozenset(
    """
    a about above after again against all am an and any are aren't as at be because been before being
    below between both but by can can't cannot could couldn't did didn't do does doesn't doing don't down
    during each few for from further had hadn't has hasn't have haven't having he he'd he'll he's her here
    here's hers herself him himself his how how's i i'd i'll i'm i've if in into is isn't it it's its itself
    let's me more most mustn't my myself no nor not of off on once only or other ought our ours ourselves
    out over own same shan't she she'd she'll she's should shouldn't so some such than that that's the their
    theirs them themselves then there there's these they they'd they'll they're they've this those through
    to too under until up very was wasn't we we'd we'll we're we've were weren't what what's when when's
    where where's which while who who's whom why why's with won't would wouldn't you you'd you'll you're
    you've your yours yourself yourselves
    hey auri tovi glen brigg please tell know remember remind recall said say says told ask asked
    like want wanted need get got make made going go just also really thing things something anything
    today yesterday tomorrow tonight morning afternoon evening night week weekend month year last next ago
    time day days
    """.split()
)

# Messages that never need recall: confirmations and one-word follow-ups.
_SHORT_REPLIES = frozenset(
    {
        "yes", "no", "yeah", "yep", "nope", "ok", "okay", "thanks", "thank you", "stop", "cancel", "sure",
        "please", "done", "never mind", "nevermind", "go ahead", "do it", "that's all", "that is all",
    }
)

_WORD = re.compile(r"[a-z0-9][a-z0-9'’-]*")
_FIRST_PERSON = re.compile(r"\b(?:i|me|my|mine|myself|i'm|i've|i'd|i'll)\b", re.IGNORECASE)

_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3, "apr": 4, "april": 4,
    "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7, "aug": 8, "august": 8, "sep": 9, "sept": 9,
    "september": 9, "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
_MONTH_PATTERN = "|".join(sorted(_MONTHS, key=len, reverse=True))
_MONTH_DAY = re.compile(rf"\b({_MONTH_PATTERN})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(\d{{4}}))?\b")
_DAY_MONTH = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({_MONTH_PATTERN})\b(?:,?\s+(\d{{4}}))?")
_ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_THE_NTH = re.compile(r"\bthe\s+(\d{1,2})(?:st|nd|rd|th)\b")
_WEEKDAY = re.compile(r"\b(?:(last|this|next|on)\s+)?(" + "|".join(_WEEKDAYS) + r")\b")

MAX_HINT_DAYS = 14

KIND_LABELS = {
    "fact": "fact",
    "preference": "preference",
    "decision": "decision",
    "commitment": "commitment",
    "about_assistant": "about the assistant",
    "digest": "daily digest",
    "exchange": "earlier conversation",
}


def word_list(text: str) -> list[str]:
    return _WORD.findall((text or "").lower().replace("’", "'"))


def should_auto_recall(text: str) -> bool:
    """Skip recall for very short messages and confirmations (they only recall noise)."""
    words = word_list(text)
    if len(words) < 3:
        return False
    return " ".join(words) not in _SHORT_REPLIES


def extract_terms(text: str, max_terms: int = 8) -> list[str]:
    """Meaningful words from a message, in order, without stopwords or duplicates."""
    terms: list[str] = []
    for word in word_list(text):
        word = word.strip("'-")
        if word.endswith("'s"):
            word = word[:-2]
        if (len(word) < 3 and not word.isdigit()) or word in STOPWORDS:
            continue
        if word not in terms:
            terms.append(word)
        if len(terms) >= max_terms:
            break
    return terms


def websearch_query(terms: Sequence[str]) -> str:
    """Terms joined for websearch_to_tsquery as alternatives ("peanut or mia")."""
    return " or ".join(terms)


def mentions_self(text: str) -> bool:
    """True when the speaker talks about themselves ("my", "I", "me")."""
    return bool(_FIRST_PERSON.search(text or ""))


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _closest_year(month: int, day: int, today: date) -> date | None:
    """A month/day without a year: this year's date unless that's well in the future."""
    candidate = _safe_date(today.year, month, day)
    if candidate is not None and candidate > today + timedelta(days=31):
        candidate = _safe_date(today.year - 1, month, day)
    return candidate


def parse_date_hints(text: str, today: date) -> list[date]:
    """Days a message refers to ("yesterday", "last Tuesday", "Sep 19", "the 27th").

    Hints boost memories from those days; they never filter, because a
    question about a day often means "around" that day.
    """
    lower = (text or "").lower()
    hints: list[date] = []

    def add(day: date | None) -> None:
        if day is not None and day not in hints:
            hints.append(day)

    if "day before yesterday" in lower:
        add(today - timedelta(days=2))
    if re.search(r"\byesterday\b|\blast night\b", lower):
        add(today - timedelta(days=1))
    if re.search(r"\btoday\b|\btonight\b|\bthis (?:morning|afternoon|evening)\b|\bearlier\b", lower):
        add(today)
    if re.search(r"\btomorrow\b", lower):
        add(today + timedelta(days=1))

    week_start = today - timedelta(days=today.weekday())
    if re.search(r"\blast week\b", lower):
        for offset in range(7):
            add(week_start - timedelta(days=7 - offset))
    if re.search(r"\bthis week\b", lower):
        for offset in range(today.weekday() + 1):
            add(week_start + timedelta(days=offset))

    for match in _WEEKDAY.finditer(lower):
        qualifier, weekday_name = match.group(1), match.group(2)
        weekday = _WEEKDAYS.index(weekday_name)
        if qualifier == "next":
            add(today + timedelta(days=((weekday - today.weekday()) % 7) or 7))
        elif qualifier == "this":
            add(week_start + timedelta(days=weekday))
        else:
            delta = (today.weekday() - weekday) % 7
            if delta == 0 and qualifier == "last":
                delta = 7
            add(today - timedelta(days=delta))
            # "Last Tuesday" said on a Wednesday may mean yesterday or the
            # week before; hints only boost, so keep both.
            if qualifier == "last" and delta <= 2:
                add(today - timedelta(days=delta + 7))

    for match in _ISO_DATE.finditer(lower):
        add(_safe_date(int(match.group(1)), int(match.group(2)), int(match.group(3))))

    for match in _MONTH_DAY.finditer(lower):
        month, day = _MONTHS[match.group(1)], int(match.group(2))
        add(_safe_date(int(match.group(3)), month, day) if match.group(3) else _closest_year(month, day, today))

    for match in _DAY_MONTH.finditer(lower):
        day, month = int(match.group(1)), _MONTHS[match.group(2)]
        add(_safe_date(int(match.group(3)), month, day) if match.group(3) else _closest_year(month, day, today))

    for match in _THE_NTH.finditer(lower):
        day = int(match.group(1))
        candidate = _safe_date(today.year, today.month, day)
        if candidate is None or candidate > today + timedelta(days=1):
            previous_month_end = today.replace(day=1) - timedelta(days=1)
            candidate = _safe_date(previous_month_end.year, previous_month_end.month, day)
        add(candidate)

    return hints[:MAX_HINT_DAYS]


def adjacent_days(days: Iterable[date]) -> list[date]:
    """The days right before and after each hint, excluding the hints themselves."""
    hint_set = set(days)
    adjacent: list[date] = []
    for day in hint_set:
        for neighbor in (day - timedelta(days=1), day + timedelta(days=1)):
            if neighbor not in hint_set and neighbor not in adjacent:
                adjacent.append(neighbor)
    return sorted(adjacent)


@dataclass(frozen=True, slots=True)
class SubjectEntity:
    """A person, pet, place or topic memories can be about."""

    id: int
    kind: str
    name: str
    aliases: tuple[str, ...] = ()


def match_subjects(text: str, entities: Iterable[SubjectEntity]) -> list[int]:
    """Ids of entities named in text by name or alias (whole words, names of 3+ characters)."""
    lower = (text or "").lower().replace("’", "'")
    matched: list[int] = []
    for entity in entities:
        for name in (entity.name, *entity.aliases):
            needle = name.strip().lower()
            if len(needle) < 3:
                continue
            if re.search(r"(?<![\w])" + re.escape(needle) + r"(?:'s)?(?![\w])", lower):
                matched.append(entity.id)
                break
    return matched


_REPEATS = re.compile(
    r"\b(?:every|each)\s+(?:other\s+|second\s+)?(?:\d+\s+|two\s+|three\s+|four\s+)?"
    r"(?:day|night|morning|afternoon|evening|week|weekend|weekday|month|year"
    r"|monday|tuesday|wednesday|thursday|friday|saturday|sunday)s?\b"
    r"|\b(?:once|twice|three\s+times)\s+a\s+(?:day|week|month|year)\b"
    r"|\b(?:daily|weekly|biweekly|fortnightly|monthly|yearly|annually)\b"
    r"|\bon\s+(?:mondays|tuesdays|wednesdays|thursdays|fridays|saturdays|sundays|weekdays|weekends)\b",
    re.IGNORECASE,
)


def repeats(text: str) -> str | None:
    """The words that make a statement repeat ("every Thursday", "on weekdays"), if any.

    Something that repeats is a routine (a fact or preference), never a
    one-off commitment with a due date.
    """
    match = _REPEATS.search(text or "")
    return match.group(0) if match else None


def normalize_statement(text: str) -> str:
    """Lower case, collapsed whitespace, trimmed trailing punctuation."""
    return " ".join((text or "").lower().split()).strip(" .!?;:")


def content_hash(text: str) -> str:
    """Hash used to detect a repeat of the same current memory."""
    return hashlib.sha256(normalize_statement(text).encode("utf-8")).hexdigest()


@dataclass(slots=True)
class RecallRow:
    """One memory as returned by a recall query, ready to format."""

    id: int
    kind: str
    content: str
    at: datetime | None
    importance: int = 3
    sensitivity: str = "normal"
    about: list[str] = field(default_factory=list)
    replaced: list[tuple[str, datetime | None]] = field(default_factory=list)
    score: float = 0.0
    # True for today's turns read straight from the archive: their ids are
    # archive row ids, not memory ids, so they are never shown or acted on.
    from_archive: bool = False
    # When a commitment is due (start of that day); None for everything else.
    due: datetime | None = None


# Per-kind length caps for one note, and total caps for a block of notes.
NOTE_CHAR_LIMITS = {"exchange": 700, "digest": 900}
DEFAULT_NOTE_CHARS = 500
AUTO_NOTES_TOTAL_CHARS = 3500
TOOL_NOTES_TOTAL_CHARS = 7000
PROFILE_TOTAL_CHARS = 1200


def _condense(text: str, max_chars: int) -> str:
    flat = " ".join((text or "").replace("## ", "").split())
    if len(flat) <= max_chars:
        return flat
    return flat[: max_chars - 1].rstrip() + "…"


def _local(at: datetime | None, tz: tzinfo | None) -> datetime | None:
    if at is None:
        return None
    return at.astimezone(tz) if tz is not None and at.tzinfo is not None else at


_WEEKDAY_ABBREVIATIONS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def due_label(due: datetime | date, *, tz: tzinfo | None, today: date) -> str:
    """"due Thu 2026-10-15", with "(today)" or "(past)" so an old date can't pass for an upcoming one."""
    day = (_local(due, tz) or due).date() if isinstance(due, datetime) else due
    label = f"due {_WEEKDAY_ABBREVIATIONS[day.weekday()]} {day.isoformat()}"
    if day == today:
        return label + " (today)"
    return label + " (past)" if day < today else label


def format_note(
    row: RecallRow,
    *,
    tz: tzinfo | None = None,
    max_chars: int | None = None,
    today: date | None = None,
    private_label: bool = True,
) -> str:
    """• [#812 · 2026-10-12 18:04 · preference · about Mia · private] text (replaced: "...", 2026-08-03)

    Commitments also show their due date: [#58 · 2026-10-13 08:02 · commitment · due Thu 2026-10-15 (past)].
    private_label=False leaves "private" out, for a household that shares its memories.
    """
    local = _local(row.at, tz)
    labels = [local.strftime("%Y-%m-%d %H:%M") if local else "undated", KIND_LABELS.get(row.kind, row.kind)]
    if row.due is not None:
        labels.append(due_label(row.due, tz=tz, today=today or datetime.now(tz).date()))
    if not row.from_archive:
        labels.insert(0, f"#{row.id}")
    if row.about:
        labels.append("about " + ", ".join(row.about))
    if private_label and row.sensitivity == "private":
        labels.append("private")
    limit = max_chars or NOTE_CHAR_LIMITS.get(row.kind, DEFAULT_NOTE_CHARS)
    line = f"• [{' · '.join(labels)}] {_condense(row.content, limit)}"
    if row.replaced:
        history = []
        for content, at in row.replaced[:2]:
            replaced_at = _local(at, tz)
            history.append(f'"{_condense(content, 160)}", {replaced_at.strftime("%Y-%m-%d") if replaced_at else "undated"}')
        line += " (replaced: " + "; ".join(history) + ")"
    return line


def format_notes(
    rows: Iterable[RecallRow], *, total_chars: int, tz: tzinfo | None = None, today: date | None = None
) -> str:
    """Format rows in order until the block reaches total_chars."""
    return format_notes_with_rows(rows, total_chars=total_chars, tz=tz, today=today)[0]


def format_notes_with_rows(
    rows: Iterable[RecallRow],
    *,
    total_chars: int,
    tz: tzinfo | None = None,
    today: date | None = None,
    private_label: bool = True,
) -> tuple[str, list[RecallRow]]:
    """format_notes, plus the rows that fit in the block."""
    today = today or datetime.now(tz).date()
    lines: list[str] = []
    shown: list[RecallRow] = []
    used = 0
    for row in rows:
        line = format_note(row, tz=tz, today=today, private_label=private_label)
        if used + len(line) + 1 > total_chars:
            break
        lines.append(line)
        shown.append(row)
        used += len(line) + 1
    return "\n".join(lines), shown


class ShownNotesCache:
    """Per conversation, the # numbers of the memories Auri was shown.

    A tool that changes a memory by number (remember_memory's replaces) only
    acts on these, so a number the model invents can't retire anything.
    """

    def __init__(self, ttl_seconds: float = 15 * 60, max_conversations: int = 200) -> None:
        self._ttl = ttl_seconds
        self._max = max_conversations
        self._entries: OrderedDict[str, tuple[float, set[int]]] = OrderedDict()

    def add(self, conversation_id: str | None, rows: Iterable[RecallRow]) -> None:
        """Record rows shown with their # number (archive rows have none)."""
        ids = {row.id for row in rows if not row.from_archive}
        if not conversation_id or not ids:
            return
        expires_at, known = self._entries.get(conversation_id, (0.0, set()))
        if expires_at < monotonic():
            known = set()
        self._entries[conversation_id] = (monotonic() + self._ttl, known | ids)
        self._entries.move_to_end(conversation_id)
        while len(self._entries) > self._max:
            self._entries.popitem(last=False)

    def shown(self, conversation_id: str | None, memory_id: int) -> bool:
        entry = self._entries.get(conversation_id) if conversation_id else None
        return entry is not None and entry[0] >= monotonic() and memory_id in entry[1]

    def clear(self) -> None:
        self._entries.clear()
