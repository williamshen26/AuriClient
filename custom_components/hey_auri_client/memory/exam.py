"""Memory exam: ask Auri things it should remember and grade the answers.

Questions come from hey_auri_memory_exam.yaml in the config folder:

    - q: What day is Mia's piano lesson?
      expected: Thursday.
    - q: Where are we going on vacation this summer?
      expected: Portugal, in July.

Each question is asked in a conversation of its own, exactly like a real
owner-mode turn (so its answer can only come from memory), and those
conversations are never archived. /memory/grade scores the answers; the
result is kept as an "exam" run. Write questions, not commands: they really
run, devices included.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any
import uuid

from homeassistant.components import conversation
from homeassistant.core import Context
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
from homeassistant.util.yaml import load_yaml

from ..const import MEMORY_EXAM_CONVERSATION_PREFIX, MEMORY_EXAM_FILE

if TYPE_CHECKING:
    from .runtime import MemoryRuntime

MAX_QUESTIONS = 50
GRADE_BATCH = 25
MAX_FIELD_CHARS = 2000
GRADES = ("correct", "partial", "wrong", "missed")


def parse_exam(data: Any) -> list[dict[str, str]]:
    """Questions from the exam file: a list of {q, expected} (or {questions: [...]})."""
    if isinstance(data, dict):
        data = data.get("questions")
    if not isinstance(data, list):
        raise ValueError(f"{MEMORY_EXAM_FILE} must be a list of items with q and expected")
    questions: list[dict[str, str]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        question = " ".join(str(item.get("q") or item.get("question") or "").split())
        expected = " ".join(str(item.get("expected") or item.get("answer") or "").split())
        if question and expected:
            questions.append({"q": question[:MAX_FIELD_CHARS], "expected": expected[:MAX_FIELD_CHARS]})
    if not questions:
        raise ValueError(f"{MEMORY_EXAM_FILE} has no items with both q and expected")
    return questions[:MAX_QUESTIONS]


def score(grades: list[dict[str, Any]]) -> tuple[float, dict[str, int]]:
    """(score, counts): correct is 1, partial 0.5, wrong and missed 0."""
    counts = {name: sum(1 for grade in grades if grade["grade"] == name) for name in GRADES}
    total = len(grades) or 1
    return round((counts["correct"] + 0.5 * counts["partial"]) / total, 4), counts


async def async_run_exam(runtime: "MemoryRuntime") -> dict[str, Any]:
    """Ask every question, grade the answers, store and return the result."""
    hass = runtime.hass
    store = runtime.store
    if store is None or not runtime.enabled:
        raise HomeAssistantError("Memory isn't set up, or its database can't be reached")
    if runtime.last_known_mode == "guest":
        raise HomeAssistantError("The memory exam only runs in owner mode")

    path = hass.config.path(MEMORY_EXAM_FILE)
    try:
        questions = parse_exam(await hass.async_add_executor_job(load_yaml, path))
    except FileNotFoundError as err:
        raise HomeAssistantError(f"Create {MEMORY_EXAM_FILE} in the config folder first") from err
    except ValueError as err:
        raise HomeAssistantError(str(err)) from err

    run_id = await hass.async_add_executor_job(lambda: store.start_run("exam", dt_util.now().date()))
    if run_id is None:
        raise HomeAssistantError("A memory exam is already running")

    usage = {"prompt_tokens": 0, "completion_tokens": 0}
    try:
        answered: list[dict[str, str]] = []
        for question in questions:
            result = await conversation.async_converse(
                hass,
                question["q"],
                f"{MEMORY_EXAM_CONVERSATION_PREFIX}{uuid.uuid4().hex}",
                Context(),
                agent_id=runtime.entry.entry_id,
            )
            speech = (result.response.speech or {}).get("plain", {}).get("speech") or ""
            answered.append({**question, "answer": " ".join(str(speech).split())[:MAX_FIELD_CHARS]})

        grades: list[dict[str, Any]] = []
        for start in range(0, len(answered), GRADE_BATCH):
            batch = answered[start:start + GRADE_BATCH]
            response = await runtime.api.grade(batch)
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
        summary = {"score": exam_score, "counts": counts, "questions": len(grades), "usage": usage}
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
