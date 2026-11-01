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
question, a recall_memory search, or nowhere. A file that also lists
memories is asked against them, in a test memory (exam_memory.py), instead
of the household's real memory.
"""
from __future__ import annotations

from collections.abc import Sequence
import re
from typing import TYPE_CHECKING, Any
import uuid

from homeassistant.components import conversation
from homeassistant.core import Context
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
from homeassistant.util.yaml import load_yaml

from ..const import MEMORY_EXAM_CONVERSATION_PREFIX, MEMORY_EXAM_FILE
from .exam_memory import parse_exam_memory
from .recall import RecallRow

if TYPE_CHECKING:
    from .runtime import MemoryRuntime

MAX_QUESTIONS = 50
GRADE_BATCH = 25
MAX_FIELD_CHARS = 2000
_MAX_MEMORY_CHARS = 200
_MAX_CASE_CHARS = 40
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
    for item in data:
        if not isinstance(item, dict):
            continue
        question = _clean(item.get("q") or item.get("question"), MAX_FIELD_CHARS)
        expected = _clean(item.get("expected") or item.get("answer"), MAX_FIELD_CHARS)
        if not (question and expected):
            continue
        entry = {"q": question, "expected": expected}
        if memory_words(memory := _clean(item.get("memory"), _MAX_MEMORY_CHARS)):
            entry["memory"] = memory
        if case := _clean(item.get("case"), _MAX_CASE_CHARS):
            entry["case"] = case
        questions.append(entry)
    if not questions:
        raise ValueError(f"{name} has no items with both q and expected")
    return questions[:MAX_QUESTIONS]


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


def find_memory(words: Sequence[str], trace: Sequence[tuple[str, Sequence[RecallRow]]]) -> tuple[str, int] | None:
    """Where the memory with these words first reached Auri: (source, its place in that list), or None.

    A memory matches when every word starts one of its words, so "lesson" also matches "lessons".
    """
    if not words:
        return None
    for source, rows in trace:
        for rank, row in enumerate(rows, start=1):
            content = memory_words(row.content)
            if all(any(word.startswith(wanted) for word in content) for wanted in words):
                return source, rank
    return None


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
    """found: the share whose memory reached Auri; mrr: the mean of 1 / its place, 0 when not found."""
    found = [item for item in items if item["memory_found_in"] != "none"]
    return {
        "questions": len(items),
        "found": round(len(found) / len(items), 4),
        "mrr": round(sum(1 / item["memory_rank"] for item in found) / len(items), 4),
    }


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
                result = await conversation.async_converse(
                    hass,
                    question["q"],
                    conversation_id,
                    Context(),
                    agent_id=runtime.entry.entry_id,
                )
                speech = (result.response.speech or {}).get("plain", {}).get("speech") or ""
                item: dict[str, Any] = {**question, "answer": " ".join(str(speech).split())[:MAX_FIELD_CHARS]}
                trace = space.recall_trace.pop(conversation_id)
                if "memory" in question:
                    found = find_memory(memory_words(question["memory"]), trace)
                    item["memory_found_in"], item["memory_rank"] = found or ("none", None)
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
                grades.append(
                    {
                        **item,
                        "grade": grade.get("grade") if grade.get("grade") in GRADES else "missed",
                        "reason": str(grade.get("reason") or ""),
                    }
                )

        exam_score, counts = score(grades)
        summary: dict[str, Any] = {"score": exam_score, "counts": counts, "questions": len(grades), "usage": usage}
        if name != MEMORY_EXAM_FILE:
            summary["file"] = name
        if exam_memory:
            summary["test_memory"] = True
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
