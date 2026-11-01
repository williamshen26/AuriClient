"""The memory exam's test memory: an exam file's own memories, in a schema of their own.

An exam file that lists memories (and the household they're about) is asked
against them instead of the household's real memory. The runtime builds them
into auri_exam_memory when the exam starts and drops it when the exam ends,
so the exam's turns never read or change the real memory, and one run can be
compared with the next.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import re
from typing import TYPE_CHECKING, Any

from .db import EXAM_MEMORY_SCHEMA
from .space import MemorySpace, local_day_start
from .store import NewMemory

if TYPE_CHECKING:
    from .runtime import MemoryRuntime

MAX_EXAM_MEMORIES = 300
EXAM_MEMORY_KINDS = ("fact", "preference", "decision", "commitment", "digest")
_MAX_TEXT_CHARS = 1000
_MAX_NAME_CHARS = 60
# Required by MemorySpace; never written, because exam turns aren't archived.
_SPOOL_KEY = "hey_auri_client_exam_memory_turn_spool"


@dataclass(slots=True)
class SeedMemory:
    """One memory of the test memory, as the exam file gives it."""

    text: str
    about: list[str] = field(default_factory=list)
    kind: str = "fact"
    importance: int = 3
    private: bool = False
    days_ago: int = 0
    due_in_days: int | None = None


@dataclass(slots=True)
class ExamMemory:
    """An exam file's household and memories."""

    people: list[str]
    pets: list[str]
    memories: list[SeedMemory]


def parse_exam_memory(data: Any, name: str) -> ExamMemory | None:
    """The test memory in an exam file, or None when it lists no memories (the real memory is asked)."""
    if not isinstance(data, dict) or "memories" not in data:
        return None
    items = data.get("memories")
    if not isinstance(items, list) or not items:
        raise ValueError(f"{name}: memories must be a list of memories, each with text")
    if len(items) > MAX_EXAM_MEMORIES:
        raise ValueError(f"{name}: at most {MAX_EXAM_MEMORIES} memories")
    household = data.get("household") or {}
    if not isinstance(household, dict):
        raise ValueError(f"{name}: household must list people and pets")
    people = _names(household.get("people"))
    known = {person.casefold() for person in people}
    pets = [pet for pet in _names(household.get("pets")) if pet.casefold() not in known]
    memories = [_seed(item, f"{name}: memory {position}") for position, item in enumerate(items, start=1)]
    return ExamMemory(people=people, pets=pets, memories=memories)


def _names(value: Any) -> list[str]:
    """Names from a list (or a single name), each once whatever its case."""
    names: list[str] = []
    for item in value if isinstance(value, list) else [value] if value else []:
        clean = " ".join(str(item or "").split())[:_MAX_NAME_CHARS]
        if clean and clean.casefold() not in {known.casefold() for known in names}:
            names.append(clean)
    return names


def _seed(item: Any, where: str) -> SeedMemory:
    if isinstance(item, str):
        item = {"text": item}
    if not isinstance(item, dict):
        raise ValueError(f"{where} must be text, or a memory with text")
    text = " ".join(str(item.get("text") or "").split())[:_MAX_TEXT_CHARS]
    if not text:
        raise ValueError(f"{where} has no text")
    kind = str(item.get("kind") or "fact").strip().lower()
    if kind not in EXAM_MEMORY_KINDS:
        raise ValueError(f"{where} has kind {kind!r}; use one of {', '.join(EXAM_MEMORY_KINDS)}")
    private = item.get("private", False)
    if not isinstance(private, bool):
        raise ValueError(f"{where}: private must be true or false")
    due = item.get("due_in_days")
    return SeedMemory(
        text=text,
        about=_names(item.get("about")),
        kind=kind,
        importance=_whole(item.get("importance"), 3, 1, 5, f"{where}: importance"),
        private=private,
        days_ago=_whole(item.get("days_ago"), 0, 0, 3650, f"{where}: days_ago"),
        due_in_days=None if due is None else _whole(due, 0, -365, 365, f"{where}: due_in_days"),
    )


def _whole(value: Any, default: int, low: int, high: int, what: str) -> int:
    if value is None:
        return default
    try:
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise ValueError
        number = int(value)
    except ValueError:
        raise ValueError(f"{what} must be a whole number") from None
    if not low <= number <= high:
        raise ValueError(f"{what} must be from {low} to {high}")
    return number


def exam_seeds(memory: ExamMemory, *, now: datetime) -> list[tuple[NewMemory, list[tuple[str, str]]]]:
    """The test memory as memories to insert, each with the (name, kind) subjects it's about.

    Dates count back from now (local time), so an exam file keeps working.
    """
    people = {name.casefold() for name in memory.people}
    pets = {name.casefold() for name in memory.pets}
    today = now.date()
    seeds: list[tuple[NewMemory, list[tuple[str, str]]]] = []
    for seed in memory.memories:
        due_at = None
        if seed.kind == "commitment" and seed.due_in_days is not None:
            # Due dates are calendar days, stored as local midnight.
            due_at = local_day_start(today + timedelta(days=seed.due_in_days), 0)
        new = NewMemory(
            kind=seed.kind,
            content=seed.text,
            source="import",
            importance=seed.importance,
            sensitivity="private" if seed.private else "normal",
            occurred_at=now - timedelta(days=seed.days_ago),
            due_at=due_at,
            # A day's summary is found by the day it's about.
            source_ref=today - timedelta(days=seed.days_ago) if seed.kind == "digest" else None,
        )
        subjects = [
            (name, "person" if name.casefold() in people else "pet" if name.casefold() in pets else "topic")
            for name in seed.about
        ]
        seeds.append((new, subjects))
    return seeds


class ExamMemorySpace(MemorySpace):
    """The test memory: owner mode, but its people are the exam file's, not Home Assistant's."""

    def __init__(self, runtime: MemoryRuntime, people: Sequence[str]) -> None:
        super().__init__(runtime, mode="owner", schema=EXAM_MEMORY_SCHEMA, spool_key=_SPOOL_KEY)
        self._exam_people = [
            {"entity_id": f"person.auri_exam_{index}_{_slug(name)}", "name": name, "user_id": None}
            for index, name in enumerate(people, start=1)
        ]

    def people(self) -> list[dict[str, Any]]:
        return list(self._exam_people)


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.casefold()).strip("_") or "person"
