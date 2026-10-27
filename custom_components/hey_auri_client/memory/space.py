"""One audience's memory, in its own schema: the household's (owner mode) or the guests' (guest mode).

A space owns everything bound to its store: the subjects it knows, the
per-conversation caches, the memory block of a turn, what the memory tools
do, and the archive of turns. A turn only ever uses one space, so guests
can't reach the owner's memory and owners never see the guests'.

The guest space differs only where noted: the guests aren't Home Assistant
users or people, everyone in the party shares memories, and a party of one
is that guest without asking.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import logging
from time import monotonic
from typing import TYPE_CHECKING, Any

from homeassistant.core import callback
from homeassistant.util import dt as dt_util

from ..bookings.guest_name import guest_name
from ..const import CONF_GUEST_COUNT
from .api import MemoryApiError
from .config import CONF_MEMORY_LOGIN_PEOPLE
from .db import MemoryDbError
from .guest import stay_over
from .identity import AskedOnceCache, Speaker, StatedSpeakerCache, household_persons
from .recall import (
    AUTO_NOTES_TOTAL_CHARS,
    PROFILE_TOTAL_CHARS,
    TOOL_NOTES_TOTAL_CHARS,
    RecallRow,
    ShownNotesCache,
    SubjectEntity,
    adjacent_days,
    extract_terms,
    format_notes_with_rows,
    match_subjects,
    mentions_self,
    parse_date_hints,
    should_auto_recall,
    websearch_query,
)
from .redaction import redact
from .store import ALL_KINDS, MemoryStore, NewMemory, new_exchange_id
from .transcript import current_day
from .turn_log import TurnLogger

if TYPE_CHECKING:
    from .runtime import MemoryRuntime

_LOGGER = logging.getLogger(__name__)

TURN_CONTEXT_BUDGET_SECONDS = 0.3
_PROFILE_CACHE_SECONDS = 300
# How close in meaning a digest or exchange must be to something forgotten
# before it is retracted along with it.
_FORGET_DIGEST_MIN_SIM = 0.6
_FORGET_EXCHANGE_MIN_SIM = 0.7
_HOUSEHOLD_WORDS = frozenset(
    {"household", "the household", "everyone", "family", "the family", "home", "the home", "guests", "the guests"}
)


@dataclass(slots=True)
class Subject:
    """A resolved subject a memory is about."""

    id: int
    kind: str
    name: str
    person_entity_id: str | None = None


@dataclass(slots=True)
class ToolRecallResult:
    rows: list[RecallRow]
    text: str
    # The rows that fit in text, i.e. the ones the model sees with their # number.
    shown: list[RecallRow]


class MemorySpace:
    """The memory of one audience, bound to one schema."""

    def __init__(self, runtime: MemoryRuntime, *, mode: str, schema: str, spool_key: str) -> None:
        self.runtime = runtime
        self.hass = runtime.hass
        self.entry = runtime.entry
        # The turn mode this space serves: "owner" or "guest".
        self.mode = mode
        self.schema = schema
        self.store: MemoryStore | None = None
        self.stated_speakers = StatedSpeakerCache()
        self.asked_once = AskedOnceCache()
        self.shown_notes = ShownNotesCache()
        self._spool_key = spool_key
        self._entities: list[SubjectEntity] = []
        self._person_ids: dict[str, int] = {}
        self._household_id: int | None = None
        self._persons_synced_at = 0.0
        # Keyed by (speaker entity, shared).
        self._profile_cache: dict[tuple[int | None, bool], tuple[float, list[RecallRow]]] = {}
        self.turn_logger = TurnLogger(self.hass, self._async_write_rows, spool_key=spool_key)

    # ── basics ──────────────────────────────────────────────────────────────

    @property
    def is_guest(self) -> bool:
        return self.mode == "guest"

    @property
    def closed(self) -> bool:
        """Guest memory after a stay has ended, until the next one: nothing is recalled, kept or learned."""
        return self.is_guest and stay_over(self.entry.options)

    @property
    def enabled(self) -> bool:
        return self.runtime.enabled and self.store is not None and not self.closed

    @property
    def context_status(self) -> str:
        """What the backend is told: enabled, unavailable or not_configured."""
        if self.runtime.enabled and (self.store is None or self.closed):
            return "not_configured"  # guest memory switched off, or the stay is over
        return self.runtime.context_status

    @property
    def config(self):
        return self.runtime.config

    @property
    def api(self):
        return self.runtime.api

    @property
    def embedding_tag(self) -> str:
        return self.runtime.embedding_tag

    def note_embedding_tag(self, tag: str) -> None:
        self.runtime.note_embedding_tag(tag)

    @property
    def shared(self) -> bool:
        """Everyone's memories, private ones too, are used whoever is speaking."""
        return self.is_guest or self.config.share_household

    def attach_blocking(self, store: MemoryStore) -> None:
        """Start using a store (in the executor: makes sure the household entity exists)."""
        self._household_id = store.ensure_household()
        self.store = store
        self._entities, self._person_ids, self._persons_synced_at = [], {}, 0.0
        self.invalidate()

    def detach(self) -> None:
        self.store = None
        self._household_id = None
        self._entities, self._person_ids = [], {}
        self.clear_conversations()
        self.invalidate()

    def invalidate(self) -> None:
        """Forget cached profiles; call after every write."""
        self._profile_cache.clear()

    def clear_conversations(self) -> None:
        """Forget per-conversation state: who said who they were, what was shown."""
        self.stated_speakers.clear()
        self.asked_once.clear()
        self.shown_notes.clear()

    def new_turn_logger(self) -> None:
        """A fresh logger after a shutdown (a stopped logger stays stopped)."""
        self.turn_logger = TurnLogger(self.hass, self._async_write_rows, spool_key=self._spool_key)

    # ── who is speaking ─────────────────────────────────────────────────────

    def speaker_for(self, speaker: Speaker | None) -> Speaker | None:
        """The speaker as this space sees them: guests are never Home Assistant users."""
        if speaker is None or not self.is_guest or speaker.source != "ha_user":
            return speaker
        return Speaker(device_id=speaker.device_id, area=speaker.area)

    def _only_guest(self) -> str | None:
        """The guest's name (the host's, or the one the guests gave) when Guest Settings name a party of one."""
        name = guest_name(self.entry.options)
        try:
            count = int(self.entry.options.get(CONF_GUEST_COUNT) or 0)
        except (TypeError, ValueError):
            count = 0
        return name if name and count == 1 else None

    def party_size(self) -> int | None:
        if not self.is_guest:
            return None
        try:
            count = int(self.entry.options.get(CONF_GUEST_COUNT) or 0)
        except (TypeError, ValueError):
            return None
        return count if count > 0 else None

    # ── entities and people ─────────────────────────────────────────────────

    async def async_refresh_entities(self, *, sync_persons: bool = False) -> None:
        store = self.store
        if store is None:
            return
        persons = self.people()
        try:
            if sync_persons and not self.is_guest:
                self._person_ids = await self.hass.async_add_executor_job(store.sync_persons, persons)
                self._persons_synced_at = monotonic()
            self._entities = await self.hass.async_add_executor_job(store.list_entities)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Couldn't refresh memory entities: %s", err)

    @property
    def persons_synced_at(self) -> float:
        return self._persons_synced_at

    @property
    def person_ids(self) -> dict[str, int]:
        """{Home Assistant person entity_id: entity id} (owner space only)."""
        return self._person_ids

    @property
    def entities(self) -> list[SubjectEntity]:
        """Known subjects (people, pets, places, topics...), refreshed after writes."""
        return self._entities

    def people(self) -> list[dict[str, Any]]:
        """Home Assistant persons in the household (not those of logins marked "not a person"); none for guests."""
        if self.is_guest:
            return []
        return household_persons(self.hass, self.entry.options.get(CONF_MEMORY_LOGIN_PEOPLE))

    def _member_ids(self) -> list[int]:
        """Entity ids of the household's (or the guest party's) people and pets."""
        if self.is_guest:
            return [entity.id for entity in self._entities if entity.kind in ("person", "pet")]
        people = {person["entity_id"] for person in self.people()}
        return [
            *(entity_pk for entity_id, entity_pk in self._person_ids.items() if entity_id in people),
            *(entity.id for entity in self._entities if entity.kind == "pet"),
        ]

    def _roster_people(self) -> list[str]:
        if self.is_guest:
            names = [entity.name for entity in self._entities if entity.kind == "person"]
            # The guest's name, the host's or the one the guests gave, even before memory has anything about them.
            name = guest_name(self.entry.options)
            if name and name.lower() not in (known.lower() for known in names):
                names.insert(0, name)
            return names
        return [person["name"] for person in self.people()]

    def household_names(self) -> list[str]:
        names = self._roster_people()
        names.extend(f"{entity.name} (pet)" for entity in self._entities if entity.kind == "pet")
        return names

    def household_roster(self) -> list[dict[str, str]]:
        roster = [{"name": name, "kind": "person"} for name in self._roster_people()]
        roster.extend({"name": entity.name, "kind": "pet"} for entity in self._entities if entity.kind == "pet")
        return roster[:25]

    def _person_subject(self, name: str | None, person_entity_id: str | None) -> Subject | None:
        if person_entity_id and person_entity_id in self._person_ids:
            return Subject(self._person_ids[person_entity_id], "person", name or person_entity_id, person_entity_id)
        if name:
            lowered = name.lower()
            for entity in self._entities:
                if entity.kind == "person" and (entity.name.lower() == lowered or lowered in (a.lower() for a in entity.aliases)):
                    return Subject(entity.id, "person", entity.name, person_entity_id)
        return None

    async def async_resolve_subjects(self, names: list[str], *, default_kind: str = "topic") -> list[Subject]:
        """Resolve names to subjects: household persons first, then known entities, else a new entity.

        With default_kind "person", a name only known as a topic becomes a person.
        """
        store = self.store
        if store is None:
            return []
        persons = self.people()
        subjects: list[Subject] = []
        unresolved: list[tuple[str, str]] = []
        for name in names:
            lowered = name.lower()
            if lowered in _HOUSEHOLD_WORDS:
                if self._household_id is not None:
                    subjects.append(Subject(self._household_id, "household", "household"))
                continue
            person = next((p for p in persons if p["name"].lower() == lowered or p["name"].split()[0].lower() == lowered), None)
            if person is not None and person["entity_id"] not in self._person_ids:
                # A person added to Home Assistant since the last sync.
                await self.async_refresh_entities(sync_persons=True)
            subject = self._person_subject(person["name"], person["entity_id"]) if person else self._person_subject(name, None)
            if subject is None:
                entity = next(
                    (e for e in self._entities if e.name.lower() == lowered or lowered in (a.lower() for a in e.aliases)),
                    None,
                )
                if entity is not None and not (entity.kind == "topic" and default_kind == "person"):
                    subject = Subject(entity.id, entity.kind, entity.name)
            if subject is None:
                unresolved.append((person["name"] if person else name, "person" if person else default_kind))
            elif subject.id not in {s.id for s in subjects}:
                subjects.append(subject)

        if unresolved:
            ids = await self.hass.async_add_executor_job(store.resolve_entities, unresolved)
            await self.async_refresh_entities()
            for (name, kind), entity_pk in zip(unresolved, ids):
                entity = next((e for e in self._entities if e.id == entity_pk), None)
                subjects.append(Subject(entity_pk, entity.kind if entity else kind, entity.name if entity else name))
        return subjects

    async def async_speaker_subject(self, speaker: Speaker | None, conversation_id: str | None) -> Subject | None:
        """The subject for "about me": the signed-in person, who they said they were, or the only guest."""
        speaker = self.speaker_for(speaker)
        if speaker is not None and speaker.known:
            subject = self._person_subject(speaker.name, speaker.person_entity_id)
            if subject is not None:
                return subject
            if speaker.name:
                resolved = await self.async_resolve_subjects([speaker.name], default_kind="person")
                return resolved[0] if resolved else None
        stated = self.stated_speakers.get(conversation_id)
        if stated:
            subject = self._person_subject(stated[0], stated[1])
            if subject is None:
                resolved = await self.async_resolve_subjects([stated[0]], default_kind="person")
                subject = resolved[0] if resolved else None
            return subject
        if self.is_guest and (only_guest := self._only_guest()):
            resolved = await self.async_resolve_subjects([only_guest], default_kind="person")
            return resolved[0] if resolved else None
        return None

    # ── per-turn memory block ───────────────────────────────────────────────

    async def async_turn_context(self, text: str, speaker: Speaker, conversation_id: str | None) -> dict[str, Any]:
        """The context.memory block for a turn in this space. Never raises; at most ~300 ms of recall."""
        block: dict[str, Any] = {"v": 1, "status": self.context_status, "scope": self.mode}
        if not self.enabled:
            return block

        speaker = self.speaker_for(speaker) or Speaker()
        stated = self.stated_speakers.get(conversation_id)
        block["speaker"] = {"name": speaker.name, "source": speaker.source} if speaker.known else {"name": None, "source": "unknown"}
        block["stated_speaker"] = stated[0] if stated and not speaker.known else None
        block["household"] = self.household_roster()
        if (party_size := self.party_size()) is not None:
            block["party_size"] = party_size

        who = speaker if speaker.known else (Speaker(source="stated", name=stated[0], person_entity_id=stated[1]) if stated else None)
        speaker_subject = self._person_subject(who.name, who.person_entity_id) if who else None
        if speaker_subject is None and self.is_guest and (only_guest := self._only_guest()):
            speaker_subject = self._person_subject(only_guest, None)
        try:
            async with asyncio.timeout(TURN_CONTEXT_BUDGET_SECONDS):
                profile_rows, note_rows = await self.hass.async_add_executor_job(
                    self._turn_recall_blocking, text, speaker_subject.id if speaker_subject else None, self._member_ids()
                )
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Memory recall skipped this turn: %s", err)
            profile_rows, note_rows = [], []

        tz = dt_util.get_default_time_zone()
        private_label = not self.shared
        block["profile"], shown_profile = format_notes_with_rows(
            profile_rows, total_chars=PROFILE_TOTAL_CHARS, tz=tz, private_label=private_label
        )
        block["notes"], shown_notes = format_notes_with_rows(
            note_rows, total_chars=AUTO_NOTES_TOTAL_CHARS, tz=tz, private_label=private_label
        )
        self.shown_notes.add(conversation_id, [*shown_profile, *shown_notes])
        self._touch_later([row for row in note_rows if not row.from_archive])
        return block

    def _touch_later(self, rows: list[RecallRow]) -> None:
        """Count recalls in the background; losing a count is harmless."""
        store = self.store
        if not rows or store is None:
            return

        async def _touch() -> None:
            try:
                await self.hass.async_add_executor_job(store.touch, [row.id for row in rows])
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Couldn't record memory access: %s", err)

        self.entry.async_create_background_task(self.hass, _touch(), "hey_auri_client memory touch")

    def _turn_recall_blocking(
        self, text: str, speaker_entity_id: int | None, member_ids: list[int]
    ) -> tuple[list[RecallRow], list[RecallRow]]:
        store = self.store
        if store is None:
            return [], []

        shared = self.shared
        cached = self._profile_cache.get((speaker_entity_id, shared))
        if cached and cached[0] > monotonic():
            profile_rows = cached[1]
        else:
            profile_rows = store.profile(
                speaker_entity_id=speaker_entity_id,
                household_entity_id=self._household_id,
                member_ids=member_ids,
                shared=shared,
            )
            self._profile_cache[(speaker_entity_id, shared)] = (monotonic() + _PROFILE_CACHE_SECONDS, profile_rows)

        if not should_auto_recall(text):
            return profile_rows, []

        today = dt_util.now().date()
        terms = extract_terms(text)
        subject_ids = match_subjects(text, self._entities)
        if speaker_entity_id is not None and mentions_self(text) and speaker_entity_id not in subject_ids:
            subject_ids.append(speaker_entity_id)
        hints = parse_date_hints(text, today)
        note_rows = store.auto_recall(
            terms=websearch_query(terms),
            subject_ids=subject_ids,
            hint_days=hints,
            adjacent_days=adjacent_days(hints),
            speaker_entity_id=speaker_entity_id,
            shared=shared,
        )
        if today in hints:
            # Turns since the current memory day began aren't processed until
            # tonight; include the latest (after midnight that day began yesterday).
            boundary = self.config.day_boundary_hour
            note_rows += store.todays_exchanges(since=local_day_start(current_day(dt_util.now(), boundary), boundary))
        profile_ids = {row.id for row in profile_rows}
        return profile_rows, [row for row in note_rows if row.id not in profile_ids]

    # ── tools ───────────────────────────────────────────────────────────────

    async def async_tool_recall(
        self,
        *,
        query: str,
        about: list[str],
        kinds: list[str],
        date_from: date | None,
        date_to: date | None,
        vector: list[float] | None,
        embedding_tag: str | None,
        conversation_id: str | None,
        limit: int = 8,
    ) -> ToolRecallResult:
        store = self.store
        if store is None:
            return ToolRecallResult([], "", [])
        subject_ids = [subject.id for subject in await self.async_resolve_subjects(about)] if about else []
        subject_ids += [entity_id for entity_id in match_subjects(query, self._entities) if entity_id not in subject_ids]
        today = dt_util.now().date()
        hints = parse_date_hints(query, today)
        boundary = self.config.day_boundary_hour
        range_start = local_day_start(date_from, boundary) if date_from else None
        range_end = local_day_start((date_to or date_from) + timedelta(days=1), boundary) if date_from else None
        # Due dates are calendar days (stored as local midnight), not memory days.
        due_start = local_day_start(date_from, 0) if date_from else None
        due_end = local_day_start((date_to or date_from) + timedelta(days=1), 0) if date_from else None
        rows = await self.hass.async_add_executor_job(
            lambda: store.semantic_recall(
                query_vector=vector,
                embedding_model=embedding_tag,
                terms=websearch_query(extract_terms(query)),
                subject_ids=subject_ids,
                hint_days=hints,
                adjacent_days=adjacent_days(hints),
                range_start=range_start,
                range_end=range_end,
                due_start=due_start,
                due_end=due_end,
                kinds=[kind for kind in kinds if kind in ALL_KINDS] or list(ALL_KINDS),
                limit=limit,
            )
        )
        self._touch_later(rows)
        text, shown = format_notes_with_rows(
            rows,
            total_chars=TOOL_NOTES_TOTAL_CHARS,
            tz=dt_util.get_default_time_zone(),
            private_label=not self.shared,
        )
        self.shown_notes.add(conversation_id, shown)
        return ToolRecallResult(rows, text, shown)

    async def async_remember(
        self, memory: NewMemory, *, replaces: list[int] | None = None
    ) -> tuple[int, bool, dict[int, str]]:
        """Save a memory, retiring the memories it corrects (kept as history).

        Returns (id, duplicate, {replaced id: its statement}). Callers pass
        only # numbers Auri was shown; anything that isn't a current fact,
        preference, decision or plan is left alone.
        """
        store = self.store
        if store is None:
            raise MemoryDbError("cannot_connect", "Memory store is not connected")
        memory_id, duplicate, replaced = await self.hass.async_add_executor_job(
            self._remember_blocking, store, memory, list(replaces or [])
        )
        self.invalidate()
        if memory.embedding is None:
            self.entry.async_create_background_task(self.hass, self.async_backfill_embeddings(), "hey_auri_client memory backfill")
        return memory_id, duplicate, replaced

    @staticmethod
    def _remember_blocking(store: MemoryStore, memory: NewMemory, replaces: list[int]) -> tuple[int, bool, dict[int, str]]:
        with store.engine.begin() as conn:
            memory_id, duplicate = store.insert_memory(memory, conn)
            replaced_ids = store.supersede(conn, replaces, memory_id) if replaces else []
        contents = store.get_contents(replaced_ids)
        return memory_id, duplicate, {i: contents[i] for i in replaced_ids if i in contents}

    async def async_forget(
        self,
        ids: list[int],
        *,
        query_vector: list[float] | None,
        embedding_tag: str | None,
    ) -> list[str]:
        """Retract memories (never deleted); also retract matching exchanges and queue digest rewrites."""
        store = self.store
        if store is None:
            return []
        retracted = await self.hass.async_add_executor_job(lambda: store.retract(ids, reason="forget"))
        self.invalidate()
        if retracted and query_vector is not None and embedding_tag:
            ids_text = ",".join(str(memory_id) for memory_id, _, _ in retracted)
            await self.hass.async_add_executor_job(
                lambda: store.retract_similar(
                    query_vector, embedding_tag, kinds=["exchange"], min_sim=_FORGET_EXCHANGE_MIN_SIM, reason="forget"
                )
            )
            await self.hass.async_add_executor_job(
                lambda: store.retract_similar(
                    query_vector, embedding_tag, kinds=["digest"], min_sim=_FORGET_DIGEST_MIN_SIM,
                    reason=f"scrub_pending:{ids_text}",
                )
            )
        return [content for _, kind, content in retracted if kind != "digest"]

    # ── turn logging ────────────────────────────────────────────────────────

    @callback
    def log_turn(
        self,
        *,
        conversation_id: str,
        speaker: Speaker,
        user_text: str,
        reply: str | None,
        kind: str,
        tool_names: list[str],
        language: str | None,
    ) -> None:
        """Queue one exchange (user message + reply) for this space's archive (the caller checks the mode)."""
        speaker = self.speaker_for(speaker) or Speaker()
        stated = self.stated_speakers.get(conversation_id)
        if speaker.known:
            source, name, person = speaker.source, speaker.name, speaker.person_entity_id
        elif stated:
            source, name, person = "stated", stated[0], stated[1]
        else:
            source, name, person = ("automation" if kind == "automatic" else "unknown"), None, None
        now = dt_util.utcnow()
        exchange_id = new_exchange_id()
        common = {
            "conversation_id": conversation_id,
            "exchange_id": exchange_id,
            "kind": kind,
            "device_id": speaker.device_id,
            "area": speaker.area,
            "language": language,
            "tool_names": tool_names,
        }
        rows = [
            {
                **common,
                "role": "user",
                "content": redact(user_text or "")[0][:4000],
                "speaker_source": source,
                "ha_user_id": speaker.ha_user_id,
                "person_entity_id": person,
                "speaker_name": name,
                "created_at": now,
            }
        ]
        if reply:
            rows.append(
                {
                    **common,
                    "role": "assistant",
                    "content": redact(reply)[0][:4000],
                    "speaker_source": "assistant",
                    "created_at": now + timedelta(microseconds=1),
                }
            )
        self.turn_logger.log_nowait(rows)

    async def _async_write_rows(self, rows: list[dict[str, Any]]) -> None:
        store = self.store
        if store is None or not self.runtime.enabled:
            raise MemoryDbError("cannot_connect", "Memory store is not connected")
        await self.hass.async_add_executor_job(store.insert_messages, rows)

    # ── upkeep ──────────────────────────────────────────────────────────────

    async def async_backfill_embeddings(self, *, max_batches: int = 3) -> int:
        """Embed memories that have no vector (or an old model's) through /memory/embed."""
        store = self.store
        if store is None:
            return 0
        done = 0
        for _ in range(max_batches):
            rows = await self.hass.async_add_executor_job(lambda: store.missing_embeddings(self.embedding_tag, limit=100))
            if not rows:
                break
            try:
                vectors, tag = await self.api.embed(
                    [content for _, content in rows], task_type="RETRIEVAL_DOCUMENT", scope=self.mode
                )
            except MemoryApiError as err:
                _LOGGER.debug("Embedding backfill paused: %s", err)
                break
            self.note_embedding_tag(tag)
            await self.hass.async_add_executor_job(
                store.set_embeddings, [(memory_id, vector) for (memory_id, _), vector in zip(rows, vectors)], tag
            )
            done += len(rows)
            if len(rows) < 100:
                break
        return done


def local_day_start(day: date, boundary_hour: int) -> datetime:
    """When a local "day" starts, given the configured day boundary (e.g. 04:00)."""
    tz = dt_util.get_default_time_zone()
    return datetime.combine(day, time(hour=boundary_hour), tzinfo=tz)
