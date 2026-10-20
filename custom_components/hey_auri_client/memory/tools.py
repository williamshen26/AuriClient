"""recall_memory, remember_memory and forget_memory, run against the local memory store.

Identity comes from the injected _turn (TurnMeta), never from tool
arguments, and the query/statement embedding arrives with the tool call
(_memory_embedding) because this client has no embedding key.
"""
from __future__ import annotations

from datetime import date, datetime, time
import re
from typing import TYPE_CHECKING, Any

from homeassistant.util import dt as dt_util

from .api import embedding_tag
from .identity import TurnMeta
from .recall import repeats
from .redaction import contains_secret
from .store import ALL_KINDS, COMMITMENT_GRACE, FACT_KINDS, NewMemory

if TYPE_CHECKING:
    from .runtime import MemoryRuntime

_MAX_CONTENT_CHARS = 500
_MAX_FORGET_IDS = 20
_MAX_REPLACED_IDS = 5
# Topics stored as private: only used for the person they are about.
_PRIVATE_HINTS = re.compile(
    r"\b(?:allerg\w*|medic\w*|diagnos\w*|pregnan\w*|health|doctor|therap\w*|prescription|"
    r"salary|income|bank|debt|loan|mortgage|phone|address|birthday|born on)\b",
    re.IGNORECASE,
)
_THE_SPEAKER = re.compile(r"^the speaker(?='s|\b)", re.IGNORECASE)
# Stored as-is, a relative date points at the wrong day later on.
_RELATIVE_DATE = re.compile(
    r"\b(?:today|tonight|tomorrow|yesterday"
    r"|(?:this|next|last|coming)\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday"
    r"|week|weekend|month|year)"
    r"|in\s+(?:a|one|two|three|four|five|six|\d+)\s+(?:days?|weeks?|months?))\b",
    re.IGNORECASE,
)
_WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_SECRET_REFUSAL = {
    "success": False,
    "refused": "secret",
    "say": "I can't store passwords, codes, or card or account numbers.",
}


def _names(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    names: list[str] = []
    for item in value:
        name = " ".join(str(item or "").split())[:60]
        if name and name.lower() not in {n.lower() for n in names}:
            names.append(name)
    return names[:5]


def _memory_ids(value: Any, limit: int) -> list[int]:
    """Memory # numbers from a tool argument: integers, or strings such as "17" or "#17"."""
    if not isinstance(value, list):
        value = [] if value in (None, "") else [value]
    ids: list[int] = []
    for item in value:
        if isinstance(item, str) and item.strip().lstrip("#").isdigit():
            item = int(item.strip().lstrip("#"))
        if isinstance(item, int) and not isinstance(item, bool) and item > 0 and item not in ids:
            ids.append(item)
    return ids[:limit]


def relative_date_words(text: str) -> str | None:
    """The first relative date in text ("this Thursday", "tomorrow"...), if any."""
    match = _RELATIVE_DATE.search(text or "")
    return match.group(0) if match else None


def _parse_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value or "").strip())
    except ValueError:
        return None


def _local_midnight(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=dt_util.get_default_time_zone())


class MemoryToolService:
    """Tool handlers backed by a MemoryRuntime."""

    def __init__(self, runtime: "MemoryRuntime") -> None:
        self._runtime = runtime

    def _check(self, arguments: dict[str, Any]) -> tuple[TurnMeta | None, dict[str, Any] | None]:
        turn = arguments.get("_turn")
        if not isinstance(turn, TurnMeta) or turn.mode != "owner" or not turn.memory_allowed:
            return None, {
                "success": False,
                "error": "memory_unavailable",
                "say": "Long-term memory isn't available right now.",
            }
        if not self._runtime.enabled:
            return None, {
                "success": False,
                "error": "memory_unavailable",
                "say": "Memory can't be reached right now. Try again later.",
            }
        return turn, None

    def _embedding(self, arguments: dict[str, Any]) -> tuple[list[float] | None, str | None]:
        """The vector the backend attached to this tool call, if it's usable."""
        embedding = arguments.get("_memory_embedding")
        if not isinstance(embedding, dict):
            return None, None
        vector = embedding.get("vector")
        try:
            dimensions = int(embedding.get("dimensions") or 0)
        except (TypeError, ValueError):
            return None, None
        if not isinstance(vector, list) or not vector or len(vector) != dimensions:
            return None, None
        tag = embedding_tag(str(embedding.get("model") or "unknown"), dimensions)
        self._runtime.note_embedding_tag(tag)
        return [float(value) for value in vector], tag

    async def recall_memory(self, arguments: dict[str, Any]) -> dict[str, Any]:
        turn, problem = self._check(arguments)
        if problem:
            return problem
        query = " ".join(str(arguments.get("query") or "").split())
        if not query:
            return {"retry": "query is required for recall_memory"}

        date_from, date_to = _parse_date(arguments.get("date_from")), _parse_date(arguments.get("date_to"))
        if date_from and date_to and date_to < date_from:
            date_from, date_to = date_to, date_from
        kinds = [kind for kind in (arguments.get("kinds") or []) if kind in ALL_KINDS] or list(ALL_KINDS)
        vector, tag = self._embedding(arguments)

        result = await self._runtime.async_tool_recall(
            query=query,
            about=_names(arguments.get("about")),
            kinds=kinds,
            date_from=date_from,
            date_to=date_to or date_from,
            vector=vector,
            embedding_tag=tag,
            conversation_id=turn.conversation_id,
        )
        if not result.rows:
            return {
                "success": True,
                "count": 0,
                "results": "",
                "guidance": (
                    "No matching memories. Retry once with different words or dates if you haven't yet; "
                    "otherwise say you have no record of it. Never guess."
                ),
            }
        guidance = "Newer notes win; 'replaced' is history. Never read out the # numbers."
        if not self._runtime.config.share_household:
            guidance += " Private notes only help the person they are about."
        return {"success": True, "count": len(result.rows), "results": result.text, "guidance": guidance}

    async def remember_memory(self, arguments: dict[str, Any]) -> dict[str, Any]:
        turn, problem = self._check(arguments)
        if problem:
            return problem
        runtime = self._runtime

        content = " ".join(str(arguments.get("content") or "").split())[:_MAX_CONTENT_CHARS]
        if not content:
            return {"retry": "content is required for remember_memory"}
        kind = str(arguments.get("kind") or "fact")
        if kind not in FACT_KINDS:
            return {"retry": f"kind must be one of: {', '.join(FACT_KINDS)}"}
        if contains_secret(content):
            return dict(_SECRET_REFUSAL)
        if kind == "commitment" and (repetition := repeats(content)):
            # A commitment would expire two weeks after one date; a routine shouldn't.
            return {
                "retry": (
                    f'"{repetition}" means it repeats: save it as a fact or preference without due_date, '
                    'for example "Will has a piano lesson every Thursday at 3 pm."'
                )
            }
        today = dt_util.now().date()
        today_is = f"today is {_WEEKDAY_NAMES[today.weekday()]} {today.isoformat()}"
        if relative := relative_date_words(content):
            return {"retry": f'Write the actual date instead of "{relative}" ({today_is}), then call remember_memory again.'}
        if kind == "commitment" and _parse_date(arguments.get("due_date")) is None:
            return {
                "retry": (
                    f"Commitments need due_date as YYYY-MM-DD ({today_is}). Write the date in content too, "
                    'for example "Will has a piano lesson on Thursday, YYYY-MM-DD."'
                )
            }
        try:
            importance = min(max(int(arguments.get("importance") or 4), 1), 5)
        except (TypeError, ValueError):
            importance = 4

        conversation_id = turn.conversation_id
        speaker = turn.speaker
        names = _names(arguments.get("about"))
        # The first call after "Who should I remember this for?" carries the answer.
        answering = not (speaker and speaker.known) and runtime.asked_once.take_answer(conversation_id)
        if names:
            # The one name in the answer is who was speaking, for the rest of
            # this conversation: a person, even one Home Assistant doesn't know.
            answered = answering and len(names) == 1
            subjects = await runtime.async_resolve_subjects(names, default_kind="person" if answered else "topic")
            if answered and len(subjects) == 1 and subjects[0].kind == "person":
                runtime.stated_speakers.set(conversation_id, subjects[0].name, subjects[0].person_entity_id)
        else:
            who = await runtime.async_speaker_subject(speaker, conversation_id)
            if who is None:
                if not runtime.asked_once.asked(conversation_id):
                    runtime.asked_once.mark(conversation_id)
                    return {
                        "success": False,
                        "needs_person": True,
                        "ask_user": "Who should I remember this for?",
                        "household_members": runtime.household_names(),
                    }
                return {
                    "success": False,
                    "not_saved": "unattributed",
                    "say": "I couldn't tell who this is about, so I didn't save it.",
                }
            subjects = [who]

        persons = [subject for subject in subjects if subject.kind == "person"]
        if len(persons) == 1:
            content = _THE_SPEAKER.sub(persons[0].name, content)

        due_at = expires_at = None
        if kind == "commitment":
            due_day = _parse_date(arguments.get("due_date"))
            due_at = _local_midnight(due_day) if due_day else None
            expires_at = (due_at or dt_util.now()) + COMMITMENT_GRACE

        vector, tag = self._embedding(arguments)
        stated = runtime.stated_speakers.get(conversation_id)
        if speaker is not None and speaker.known:
            speaker_source, speaker_name, speaker_person = speaker.source, speaker.name, speaker.person_entity_id
        elif stated:
            speaker_source, speaker_name, speaker_person = "stated", stated[0], stated[1]
        else:
            speaker_source, speaker_name, speaker_person = "unknown", None, None

        memory = NewMemory(
            kind=kind,
            content=content,
            source="chat",
            importance=importance,
            sensitivity="private" if _PRIVATE_HINTS.search(content) else "normal",
            about_entity_ids=[subject.id for subject in subjects],
            embedding=vector,
            embedding_model=tag if vector else None,
            occurred_at=dt_util.utcnow(),
            due_at=due_at,
            expires_at=expires_at,
            conversation_id=conversation_id,
            speaker_source=speaker_source,
            speaker_name=speaker_name,
            speaker_person_entity_id=speaker_person,
        )
        requested = _memory_ids(arguments.get("replaces"), _MAX_REPLACED_IDS)
        # Only notes Auri was shown in this conversation: a number the model
        # made up can't retire anything.
        replaces = [memory_id for memory_id in requested if runtime.shown_notes.shown(conversation_id, memory_id)]
        memory_id, duplicate, replaced = await runtime.async_remember(memory, replaces=replaces)
        if duplicate:
            result: dict[str, Any] = {"success": True, "duplicate": True, "saved": content, "id": memory_id}
        else:
            result = {"success": True, "saved": content, "about": [subject.name for subject in subjects], "id": memory_id}
        if replaced:
            result["replaced"] = list(replaced.values())
        if not_replaced := [i for i in requested if i not in replaced]:
            result["not_replaced"] = not_replaced
        return result

    async def forget_memory(self, arguments: dict[str, Any]) -> dict[str, Any]:
        turn, problem = self._check(arguments)
        if problem:
            return problem
        runtime = self._runtime
        vector, tag = self._embedding(arguments)

        ids = _memory_ids(arguments.get("memory_ids"), _MAX_FORGET_IDS)
        if ids:
            forgotten = await runtime.async_forget(ids, query_vector=vector, embedding_tag=tag)
            if not forgotten:
                return {
                    "success": False,
                    "error": "None of those memories exist anymore.",
                    "retry": "Call recall_memory to find the right # numbers, or tell the user it's already forgotten.",
                }
            return {"success": True, "count": len(forgotten), "forgotten": forgotten}

        query = " ".join(str(arguments.get("query") or "").split())
        if not query:
            return {"retry": "query is required for forget_memory"}
        result = await runtime.async_tool_recall(
            query=query,
            about=[],
            kinds=list(FACT_KINDS),
            date_from=None,
            date_to=None,
            vector=vector,
            embedding_tag=tag,
            conversation_id=turn.conversation_id,
            limit=5,
        )
        rows = result.rows
        if not rows:
            return {
                "success": True,
                "count": 0,
                "forgotten": [],
                "guidance": "Nothing matched. Tell the user you have no record of that.",
            }

        gap = rows[0].score - rows[1].score if len(rows) > 1 else 1.0
        clear_match = len(rows) == 1 or (gap >= 0.1 and (vector is None or rows[0].score >= 0.75))
        if clear_match:
            forgotten = await runtime.async_forget([rows[0].id], query_vector=vector, embedding_tag=tag)
            return {"success": True, "count": len(forgotten), "forgotten": forgotten}
        return {
            "success": False,
            "needs_choice": True,
            "candidates": result.text,
            "retry": "Briefly list these to the user, ask which ones to forget, then call forget_memory with memory_ids.",
        }
