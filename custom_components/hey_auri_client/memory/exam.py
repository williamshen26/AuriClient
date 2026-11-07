"""Memory exam: ask Auri things it should remember and grade the answers.

Questions come from hey_auri_memory_exam.yaml in the config folder, or
another file the action names:

    - q: What day is Mia's piano lesson?
      expected: Thursday.
      memory: Mia piano
    - q: Where are we going on vacation this summer?
      expected: Portugal, in July.

Each question is asked in a conversation of its own, exactly like a real
owner-mode turn (so its answer can only come from memory), and those
conversations are never archived. /memory/grade scores the answers; the
result is kept as an "exam" run. Write questions, not commands: they really
run, devices included.

A question's memory (words from the memory that answers it) also shows where
that memory reached Auri: the memory profile, the notes matched to the
question, a recall_memory search, or nowhere. Each question also records
how long its turn took, how many times Auri searched and what each search
returned (its words, how many memories and how long, and their scores), and
how long one /memory/embed call for the question takes: what embedding
every message would add to a turn. That call's vector then tries a search by
meaning alone, recorded as meaning_notes: what embedding every message would
add to the notes. Auri never sees it. A file that also lists memories is
asked against them, in a test memory (exam_memory.py), instead of the
household's real memory.
"""
from __future__ import annotations

from collections.abc import Sequence
import logging
import re
from time import monotonic
from typing import TYPE_CHECKING, Any
import uuid

from homeassistant.components import conversation
from homeassistant.core import Context
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
from homeassistant.util.yaml import load_yaml

from ..const import MEMORY_EXAM_CONVERSATION_PREFIX, MEMORY_EXAM_FILE
from .exam_memory import parse_exam_memory
from .recall import RecallRow, TraceEntry

if TYPE_CHECKING:
    from .api import MemoryApi
    from .runtime import MemoryRuntime
    from .space import MemorySpace

_LOGGER = logging.getLogger(__name__)

MAX_QUESTIONS = 50
GRADE_BATCH = 25
MAX_FIELD_CHARS = 2000
_MAX_MEMORY_CHARS = 200
_MAX_CASE_CHARS = 40
# How much of each memory a search's details show.
_MAX_RESULT_CHARS = 80
# meaning_notes: how far down its search looks for the answer, and how many
# memories it shows (as many as the automatic notes hold).
_MEANING_SEARCH_LIMIT = 30
_MEANING_NOTES_SHOWN = 6
GRADES = ("correct", "partial", "wrong", "missed")
# Where an answer's memory can reach Auri, in the order Auri sees them.
MEMORY_SOURCES = ("profile", "notes", "recall_memory")
_WORD = re.compile(r"\w[\w'’-]*")


def parse_exam(data: Any, name: str = MEMORY_EXAM_FILE) -> list[dict[str, str]]:
    """Questions from the exam file: a list of {q, expected, memory?, case?} (or {questions: [...]})."""
    if isinstance(data, dict):
        data = data.get("questions")
    if not isinstance(data, list):
        raise ValueError(f"{name} must be a list of items with q and expected")
    questions: list[dict[str, str]] = []
    for position, item in enumerate(data, start=1):
        if not isinstance(item, dict):
            continue
        where = f"{name}: question {position}"
        question = _clean(_text(item, ("q", "question"), where), MAX_FIELD_CHARS)
        expected = _clean(_text(item, ("expected", "answer"), where), MAX_FIELD_CHARS)
        if not (question and expected):
            continue
        entry = {"q": question, "expected": expected}
        if memory_words(memory := _clean(_text(item, ("memory",), where), _MAX_MEMORY_CHARS)):
            entry["memory"] = memory
        if case := _clean(item.get("case"), _MAX_CASE_CHARS):
            entry["case"] = case
        questions.append(entry)
    if not questions:
        raise ValueError(f"{name} has no items with both q and expected")
    return questions[:MAX_QUESTIONS]


def _text(item: dict[str, Any], keys: tuple[str, ...], where: str) -> str:
    """The first of these fields that's given, which must be text.

    Unquoted, YAML reads 20:00 as a number (1200) and no as false; graded
    against those, a right answer would look wrong, so the file is refused.
    """
    for key in keys:
        value = item.get(key)
        if value is None or value == "":
            continue
        if not isinstance(value, str):
            raise ValueError(f"{where}: YAML read {key} as {value!r}, not text; put it in quotes")
        return value
    return ""


def _clean(value: Any, max_chars: int) -> str:
    return " ".join(str(value or "").split())[:max_chars]


def score(grades: list[dict[str, Any]]) -> tuple[float, dict[str, int]]:
    """(score, counts): correct is 1, partial 0.5, wrong and missed 0."""
    counts = {name: sum(1 for grade in grades if grade["grade"] == name) for name in GRADES}
    total = len(grades) or 1
    return round((counts["correct"] + 0.5 * counts["partial"]) / total, 4), counts


def memory_words(text: str | None) -> list[str]:
    """Words to match an answer's memory by (case and punctuation don't count)."""
    return _WORD.findall((text or "").casefold())


def find_memory(words: Sequence[str], trace: Sequence[TraceEntry]) -> tuple[str, int] | None:
    """Where the memory with these words first reached Auri: (source, its place in that list), or None.

    A memory matches when every word starts one of its words, so "lesson" also matches "lessons".
    """
    if not words:
        return None
    for entry in trace:
        for rank, row in enumerate(entry.rows, start=1):
            content = memory_words(row.content)
            if all(any(word.startswith(wanted) for word in content) for wanted in words):
                return entry.source, rank
    return None


def search_details(trace: Sequence[TraceEntry]) -> list[dict[str, Any]]:
    """Each recall_memory search: its words, how many memories Auri got and how long they were, and their scores."""
    return [
        {
            "query": entry.query,
            "results": len(entry.rows),
            "chars": entry.chars,
            "memories": [_scored(row) for row in entry.rows],
        }
        for entry in trace
        if entry.source == "recall_memory"
    ]


def _scored(row: RecallRow) -> dict[str, Any]:
    """A search result: its score, what the score is made of (when known), and the start of the memory."""
    scored: dict[str, Any] = {"id": row.id, "score": round(row.score, 3)}
    if row.sim is not None:
        scored["similarity"] = round(row.sim, 3)
    if row.kw is not None:
        scored["keyword"] = round(row.kw, 3)
    scored["text"] = _clean(row.content.replace("## ", ""), _MAX_RESULT_CHARS)
    return scored


def meaning_notes(
    rows: Sequence[RecallRow], trace: Sequence[TraceEntry], words: Sequence[str], *, shared: bool
) -> dict[str, Any]:
    """What a search by meaning alone would add to the automatic notes, from that search's rows.

    Like the notes, it leaves out what's already in the profile, and private
    memories unless the household shares them (exam questions have no
    speaker). With words, it also shows where that memory would land;
    memory_rank is None when the search didn't reach it.
    """
    in_profile = {row.id for entry in trace if entry.source == "profile" for row in entry.rows}
    rows = [row for row in rows if row.id not in in_profile and (shared or row.sensitivity == "normal")]
    notes: dict[str, Any] = {}
    if words:
        found = find_memory(words, [TraceEntry("meaning_notes", rows)])
        notes["memory_rank"] = found[1] if found else None
        notes["memory_similarity"] = round(rows[found[1] - 1].sim or 0, 3) if found else None
    notes["memories"] = [_scored(row) for row in rows[:_MEANING_NOTES_SHOWN]]
    return notes


async def async_meaning_notes(
    space: "MemorySpace", vector: list[float], tag: str, trace: Sequence[TraceEntry], memory: str | None
) -> dict[str, Any] | None:
    """Try that search for one question, or None when it fails. Its results only go in the exam's result."""
    store = space.store
    try:
        rows = await space.hass.async_add_executor_job(
            lambda: store.semantic_recall(
                query_vector=vector,
                embedding_model=tag,
                terms="",
                subject_ids=[],
                hint_days=[],
                adjacent_days=[],
                range_start=None,
                range_end=None,
                min_sim=0,
                limit=_MEANING_SEARCH_LIMIT,
            )
        )
    except Exception as err:  # noqa: BLE001 (a measurement must never fail the exam)
        _LOGGER.debug("Memory exam: the search by meaning failed: %s", err)
        return None
    return meaning_notes(rows, trace, memory_words(memory), shared=space.shared)


async def async_embed_question(api: "MemoryApi", text: str) -> tuple[float, list[float], str] | None:
    """One /memory/embed call for this text: how long it took from here, its vector and its tag. None when it fails.

    The time is what embedding every message would add to a turn.
    """
    started = monotonic()
    try:
        vectors, tag = await api.embed([text], task_type="RETRIEVAL_QUERY")
    except Exception as err:  # noqa: BLE001 (a measurement must never fail the exam)
        _LOGGER.debug("Memory exam: timing an embedding call failed: %s", err)
        return None
    return round(monotonic() - started, 2), vectors[0], tag


def recall_summary(grades: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    """Recall and MRR over the questions that name their memory (None when none does).

    not_correct splits the answers that weren't correct: Auri had the memory
    (memory_shown) or it never reached Auri (memory_not_shown).
    """
    named = [grade for grade in grades if grade.get("memory_found_in")]
    if not named:
        return None
    summary = _recall_numbers(named)
    summary["found_in"] = {
        source: sum(1 for grade in named if grade["memory_found_in"] == source) for source in (*MEMORY_SOURCES, "none")
    }
    not_correct = [grade for grade in named if grade.get("grade") != "correct"]
    summary["not_correct"] = {
        "memory_shown": sum(1 for grade in not_correct if grade["memory_found_in"] != "none"),
        "memory_not_shown": sum(1 for grade in not_correct if grade["memory_found_in"] == "none"),
    }
    cases = list(dict.fromkeys(grade["case"] for grade in named if grade.get("case")))
    if cases:
        summary["by_case"] = {case: _recall_numbers([grade for grade in named if grade.get("case") == case]) for case in cases}
    return summary


def _recall_numbers(items: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """found: the share whose memory reached Auri; mrr: the mean of 1 / its place, 0 when not found.

    The profile is sent whole with every message, so its order isn't a search
    result: a memory in it counts as first.
    """
    found = [item for item in items if item["memory_found_in"] != "none"]
    places = [1 if item["memory_found_in"] == "profile" else item["memory_rank"] for item in found]
    return {
        "questions": len(items),
        "found": round(len(found) / len(items), 4),
        "mrr": round(sum(1 / place for place in places) / len(items), 4),
        **_turn_numbers(items),
    }


def turn_summary(grades: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """The turn numbers over every question, whether or not it names its memory."""
    return {"questions": len(grades), **_turn_numbers(grades)}


def _turn_numbers(items: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Averages per question (seconds, searches, embed_seconds) and per search (results, chars).

    The per-search ones are left out when nobody searched, and embed_seconds
    when no embedding call could be timed.
    """
    numbers: dict[str, Any] = {
        "seconds": round(sum(item["seconds"] for item in items) / len(items), 1),
        "searches": round(sum(item["searches"] for item in items) / len(items), 2),
    }
    searches = [search for item in items for search in item.get("search_results") or ()]
    if searches:
        numbers["results_per_search"] = round(sum(search["results"] for search in searches) / len(searches), 1)
        numbers["chars_per_search"] = round(sum(search["chars"] for search in searches) / len(searches))
    timed = [item["embed_seconds"] for item in items if item.get("embed_seconds") is not None]
    if timed:
        numbers["embed_seconds"] = round(sum(timed) / len(timed), 2)
    return numbers


async def async_run_exam(runtime: "MemoryRuntime", file_name: str | None = None) -> dict[str, Any]:
    """Ask every question, grade the answers, store and return the result."""
    hass = runtime.hass
    store = runtime.store
    if store is None or not runtime.enabled:
        raise HomeAssistantError("Memory isn't set up, or its database can't be reached")
    if runtime.last_known_mode == "guest":
        raise HomeAssistantError("The memory exam only runs in owner mode")

    name = file_name or MEMORY_EXAM_FILE
    try:
        data = await hass.async_add_executor_job(load_yaml, hass.config.path(name))
        questions = parse_exam(data, name)
        exam_memory = parse_exam_memory(data, name)
    except FileNotFoundError as err:
        raise HomeAssistantError(f"Create {name} in the config folder first") from err
    except ValueError as err:
        raise HomeAssistantError(str(err)) from err

    run_id = await hass.async_add_executor_job(lambda: store.start_run("exam", dt_util.now().date()))
    if run_id is None:
        raise HomeAssistantError("A memory exam is already running")

    usage = {"prompt_tokens": 0, "completion_tokens": 0}
    try:
        answered: list[dict[str, Any]] = []
        try:
            space = await runtime.async_open_exam_memory(exam_memory) if exam_memory else runtime.owner
            space.recall_trace.clear()
            for question in questions:
                conversation_id = f"{MEMORY_EXAM_CONVERSATION_PREFIX}{uuid.uuid4().hex}"
                started = monotonic()
                result = await conversation.async_converse(
                    hass,
                    question["q"],
                    conversation_id,
                    Context(),
                    agent_id=runtime.entry.entry_id,
                )
                seconds = round(monotonic() - started, 1)
                speech = (result.response.speech or {}).get("plain", {}).get("speech") or ""
                trace = space.recall_trace.pop(conversation_id)
                searched = search_details(trace)
                # After the turn, so it never changes the turn's own seconds or what Auri saw.
                embedded = await async_embed_question(runtime.api, question["q"])
                item: dict[str, Any] = {
                    **question,
                    "answer": " ".join(str(speech).split())[:MAX_FIELD_CHARS],
                    "seconds": seconds,
                    "searches": len(searched),
                    "embed_seconds": embedded[0] if embedded else None,
                }
                if "memory" in question:
                    found = find_memory(memory_words(question["memory"]), trace)
                    item["memory_found_in"], item["memory_rank"] = found or ("none", None)
                if embedded and (
                    notes := await async_meaning_notes(space, embedded[1], embedded[2], trace, question.get("memory"))
                ):
                    item["meaning_notes"] = notes
                if searched:
                    item["search_results"] = searched
                answered.append(item)
        finally:
            # The test memory only lives while its questions are asked.
            if exam_memory:
                await runtime.async_close_exam_memory()

        grades: list[dict[str, Any]] = []
        for start in range(0, len(answered), GRADE_BATCH):
            batch = answered[start:start + GRADE_BATCH]
            response = await runtime.api.grade([{key: item[key] for key in ("q", "expected", "answer")} for item in batch])
            for key in usage:
                usage[key] += int((response.get("usage") or {}).get(key) or 0)
            by_idx = {grade.get("idx"): grade for grade in response.get("grades") or [] if isinstance(grade, dict)}
            for position, item in enumerate(batch):
                grade = by_idx.get(position) or {}
                # The long details go last, after the grade.
                details = {key: item.pop(key) for key in ("meaning_notes", "search_results") if key in item}
                grades.append(
                    {
                        **item,
                        "grade": grade.get("grade") if grade.get("grade") in GRADES else "missed",
                        "reason": str(grade.get("reason") or ""),
                        **details,
                    }
                )

        exam_score, counts = score(grades)
        summary: dict[str, Any] = {"score": exam_score, "counts": counts, "questions": len(grades), "usage": usage}
        if name != MEMORY_EXAM_FILE:
            summary["file"] = name
        if exam_memory:
            summary["test_memory"] = True
        summary["turns"] = turn_summary(grades)
        if (recall := recall_summary(grades)) is not None:
            summary["memory"] = recall
        await hass.async_add_executor_job(
            lambda: store.finish_run(run_id, status="succeeded", stats={**summary, "grades": grades})
        )
    except Exception as err:
        error = f"{type(err).__name__}: {err}"
        await hass.async_add_executor_job(
            lambda: store.finish_run(run_id, status="failed", stats={"usage": usage}, error=error)
        )
        raise
    return {**summary, "grades": grades}
