"""Runtime for the local memory store.

Owns the database connection and its health, builds the memory block sent
with each owner-mode turn, backs the memory tools, logs turns, imports
legacy preferences, and schedules the nightly job. Nothing here may slow a
conversation: database work runs in executor threads with tight timeouts,
and failures degrade to "no memory this turn".
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import StrEnum
import logging
import random
from time import monotonic
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.event import (
    async_call_later,
    async_track_point_in_time,
    async_track_time_change,
    async_track_time_interval,
)
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from ..const import CONF_BOOKING_ID, CONF_CHECK_OUT, DOMAIN, LEGACY_USER_DATA_DIR
from ..saas_client import SaaSClient
from .api import EMBEDDING_DIMENSIONS, MemoryApi, MemoryApiError, embedding_tag
from .config import CONF_MEMORY_LOGIN_PEOPLE, MemoryConfig
from .db import MEMORY_SCHEMA, MemoryDbError, build_engine, detect_vector_extension
from .identity import AskedOnceCache, Speaker, StatedSpeakerCache, household_persons
from .legacy import LegacyImportPlan, load_legacy_files, plan_legacy_import
from .migrations import apply_migrations
from .scope import parse_check_out, scopes_to_purge
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

_LOGGER = logging.getLogger(__name__)

DEFAULT_EMBEDDING_TAG = embedding_tag("gemini-embedding-001", EMBEDDING_DIMENSIONS)
TURN_CONTEXT_BUDGET_SECONDS = 0.3
_HEALTH_CHECK_INTERVAL = timedelta(seconds=60)
_PERSON_SYNC_INTERVAL_SECONDS = 30 * 60
_PROFILE_CACHE_SECONDS = 300
_CATCH_UP_DELAY_SECONDS = 300
_STATE_STORAGE_KEY = f"{DOMAIN}_memory_state"
_STATE_STORAGE_VERSION = 1
# Setup problems that retrying won't fix; the user has to change something.
_FATAL_ERROR_CODES = frozenset(
    {"psycopg2_missing", "pg_version_unsupported", "pgvector_missing", "insufficient_privilege", "schema_too_new", "invalid_auth"}
)
# How close in meaning a digest or exchange must be to something forgotten
# before it is retracted along with it.
_FORGET_DIGEST_MIN_SIM = 0.6
_FORGET_EXCHANGE_MIN_SIM = 0.7


class MemoryState(StrEnum):
    DISABLED = "disabled"
    STARTING = "starting"
    ENABLED = "enabled"
    UNAVAILABLE = "unavailable"
    ERROR = "error"


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


class MemoryRuntime:
    """One per config entry."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: SaaSClient, *, app_version: str) -> None:
        self.hass = hass
        self.entry = entry
        self.api = MemoryApi(client)
        self.app_version = app_version
        self.config = MemoryConfig.from_options(entry.options)
        self.state = MemoryState.DISABLED
        self.error: str | None = None
        self.embedding_tag = DEFAULT_EMBEDDING_TAG
        self.last_known_mode: str | None = None
        self.memory_allowed: bool | None = None
        self.stated_speakers = StatedSpeakerCache()
        self.asked_once = AskedOnceCache()
        self.shown_notes = ShownNotesCache()
        self.schema_version: int | None = None
        self.pgvector_version: str | None = None

        self._engine = None
        self._store: MemoryStore | None = None
        self._entities: list[SubjectEntity] = []
        self._person_ids: dict[str, int] = {}
        self._household_id: int | None = None
        self._persons_synced_at = 0.0
        # Keyed by (speaker entity, household sharing).
        self._profile_cache: dict[tuple[int | None, bool], tuple[float, list[RecallRow]]] = {}
        self._turn_logger = TurnLogger(hass, self._async_write_rows)
        self._unsubs: list[Callable[[], None]] = []
        self._listeners: list[Callable[[], None]] = []
        self._state_store: Store[dict[str, Any]] = Store(hass, _STATE_STORAGE_VERSION, _STATE_STORAGE_KEY)
        self._nightly = None
        self._nightly_lock = asyncio.Lock()
        self.last_nightly: dict[str, Any] | None = None
        self._check_out_unsub: Callable[[], None] | None = None
        self._guest_options = self._guest_fingerprint(entry.options)

    # ── lifecycle ───────────────────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        return self.state == MemoryState.ENABLED and self._store is not None

    @property
    def store(self) -> MemoryStore | None:
        return self._store

    @property
    def context_status(self) -> str:
        """What the backend is told: enabled, unavailable or not_configured."""
        if self.state == MemoryState.ENABLED:
            return "enabled"
        if self.state == MemoryState.DISABLED:
            return "not_configured"
        return "unavailable"

    @callback
    def async_add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener) if listener in self._listeners else None

    def _set_state(self, state: MemoryState, error: str | None = None) -> None:
        self.state = state
        self.error = error
        for listener in list(self._listeners):
            listener()

    async def async_start(self) -> None:
        """Load saved state, then connect in the background if memory is set up."""
        saved = await self._state_store.async_load() or {}
        self.last_known_mode = saved.get("last_known_mode")
        self.memory_allowed = saved.get("memory_allowed")
        self.embedding_tag = saved.get("embedding_tag") or DEFAULT_EMBEDDING_TAG

        if not self.config.configured:
            self._set_state(MemoryState.DISABLED)
            return
        self._set_state(MemoryState.STARTING)
        self.entry.async_create_background_task(self.hass, self._async_connect(), "hey_auri_client memory connect")
        self._unsubs.append(async_track_time_interval(self.hass, self._async_health_check, _HEALTH_CHECK_INTERVAL))
        # Config entries aren't unloaded when Home Assistant stops: save
        # unwritten turns here so they're written on the next start.
        self._unsubs.append(self.hass.bus.async_listen(EVENT_HOMEASSISTANT_STOP, self._async_on_stop))
        self._schedule_nightly()
        self._schedule_check_out()

    async def _async_on_stop(self, _event: Event) -> None:
        await self._turn_logger.async_shutdown()
        await self._async_save_state()

    async def async_shutdown(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        if self._check_out_unsub is not None:
            self._check_out_unsub()
            self._check_out_unsub = None
        await self._turn_logger.async_shutdown()
        await self._async_save_state()
        engine, self._engine, self._store = self._engine, None, None
        if engine is not None:
            await self.hass.async_add_executor_job(engine.dispose)

    async def _async_connect(self) -> None:
        try:
            engine, store, schema_version, pgvector_version = await self.hass.async_add_executor_job(self._connect_blocking)
        except MemoryDbError as err:
            state = MemoryState.ERROR if err.code in _FATAL_ERROR_CODES else MemoryState.UNAVAILABLE
            _LOGGER.warning("Memory store not available (%s): %s", err.code, err)
            self._set_state(state, err.code)
            return
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Memory store not available: %s", err)
            self._set_state(MemoryState.UNAVAILABLE, "cannot_connect")
            return

        self._engine, self._store = engine, store
        self.schema_version, self.pgvector_version = schema_version, pgvector_version
        await self._async_refresh_entities(sync_persons=True)
        self._set_state(MemoryState.ENABLED)
        _LOGGER.info("Memory store connected (schema v%s, pgvector %s)", schema_version, pgvector_version)

        await self._turn_logger.async_load_spool()
        self._turn_logger.async_schedule_flush()
        self.entry.async_create_background_task(self.hass, self._async_after_connect(), "hey_auri_client memory upkeep")

    def _connect_blocking(self):
        """Connect, migrate and return (engine, store, schema version, pgvector version)."""
        from sqlalchemy.exc import DBAPIError, OperationalError  # pylint: disable=import-outside-toplevel

        from .db import classify_connection_error, classify_setup_error  # pylint: disable=import-outside-toplevel

        engine = build_engine(self.config)
        try:
            with engine.connect() as conn:
                vector = detect_vector_extension(conn)
            # A pgvector installed in another schema must be on the search_path
            # before the migrations reference its type.
            if vector and vector[0] not in {"public", MEMORY_SCHEMA}:
                engine.dispose()
                engine = build_engine(self.config, vector_schema=vector[0])
            schema_version = apply_migrations(engine, app_version=self.app_version)
            with engine.connect() as conn:
                vector = detect_vector_extension(conn)
        except OperationalError as err:
            engine.dispose()
            raise classify_connection_error(err) from err
        except DBAPIError as err:
            # Rights or pgvector lost since setup: a fatal error, not something to retry every minute.
            engine.dispose()
            raise classify_setup_error(err) from err
        except Exception:
            engine.dispose()
            raise

        pgvector_version = vector[1] if vector else None
        store = MemoryStore(engine, iterative_scan=_version_at_least(pgvector_version, (0, 8)))
        store.fail_stale_runs()
        self._household_id = store.ensure_household()
        return engine, store, schema_version, pgvector_version

    async def _async_after_connect(self) -> None:
        await self.async_purge_guest_memory()
        await self.async_import_legacy()
        await self.async_backfill_embeddings()

    async def _async_health_check(self, _now: datetime | None = None) -> None:
        if self.state == MemoryState.ENABLED and self._store is not None:
            try:
                await self.hass.async_add_executor_job(self._store.ping)
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Memory store stopped responding: %s", err)
                self._set_state(MemoryState.UNAVAILABLE, "cannot_connect")
                return
            if monotonic() - self._persons_synced_at > _PERSON_SYNC_INTERVAL_SECONDS:
                await self._async_refresh_entities(sync_persons=True)
        elif self.state == MemoryState.UNAVAILABLE:
            if self._store is not None:
                try:
                    await self.hass.async_add_executor_job(self._store.ping)
                except Exception:  # noqa: BLE001
                    return
                self._set_state(MemoryState.ENABLED)
                self._turn_logger.async_schedule_flush()
            else:
                await self._async_connect()

    async def async_options_updated(self, options: dict[str, Any]) -> None:
        """Restart when memory settings changed; re-check guest memory when the booking did."""
        new_config = MemoryConfig.from_options(options)
        if new_config.fingerprint() != self.config.fingerprint():
            await self.async_shutdown()
            self.config = new_config
            self._turn_logger = TurnLogger(self.hass, self._async_write_rows)
            self._profile_cache.clear()
            self._guest_options = self._guest_fingerprint(options)
            await self.async_start()
            return
        # Settings read on every turn (household sharing) apply without a restart.
        self.config = new_config
        guest_options = self._guest_fingerprint(options)
        if guest_options != self._guest_options:
            self._guest_options = guest_options
            self._schedule_check_out()
            await self.async_purge_guest_memory()

    # ── mode ────────────────────────────────────────────────────────────────

    @callback
    def note_mode(self, mode: str | None, memory_allowed: bool) -> None:
        """Remember what the backend said about this account (it decides; we only follow)."""
        if mode is None:
            return
        if self.last_known_mode is not None and mode != self.last_known_mode:
            # A mode switch must not carry speakers or cached notes across.
            # (Learning the mode for the first time isn't a switch.)
            self.stated_speakers.clear()
            self.asked_once.clear()
            self.shown_notes.clear()
            self._profile_cache.clear()
        changed = mode != self.last_known_mode or memory_allowed != self.memory_allowed
        self.last_known_mode = mode
        self.memory_allowed = memory_allowed
        if changed:
            self._state_store.async_delay_save(self._state_snapshot, 5)

    @callback
    def note_embedding_tag(self, tag: str) -> None:
        if tag and tag != self.embedding_tag:
            self.embedding_tag = tag
            self._state_store.async_delay_save(self._state_snapshot, 5)

    @callback
    def _state_snapshot(self) -> dict[str, Any]:
        return {
            "last_known_mode": self.last_known_mode,
            "memory_allowed": self.memory_allowed,
            "embedding_tag": self.embedding_tag,
        }

    async def _async_save_state(self) -> None:
        await self._state_store.async_save(self._state_snapshot())

    # ── entities and people ─────────────────────────────────────────────────

    async def _async_refresh_entities(self, *, sync_persons: bool = False) -> None:
        store = self._store
        if store is None:
            return
        persons = self.household_people()
        try:
            if sync_persons:
                self._person_ids = await self.hass.async_add_executor_job(store.sync_persons, persons)
                self._persons_synced_at = monotonic()
            self._entities = await self.hass.async_add_executor_job(store.list_entities)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Couldn't refresh memory entities: %s", err)

    @property
    def entities(self) -> list[SubjectEntity]:
        """Known subjects (people, pets, places, topics...), refreshed after writes."""
        return self._entities

    def household_people(self) -> list[dict[str, Any]]:
        """Home Assistant persons in the household: not those of logins marked "not a person"."""
        return household_persons(self.hass, self.entry.options.get(CONF_MEMORY_LOGIN_PEOPLE))

    def _member_ids(self) -> list[int]:
        """Entity ids of the household's people and pets."""
        people = {person["entity_id"] for person in self.household_people()}
        return [
            *(entity_pk for entity_id, entity_pk in self._person_ids.items() if entity_id in people),
            *(entity.id for entity in self._entities if entity.kind == "pet"),
        ]

    def household_names(self) -> list[str]:
        names = [person["name"] for person in self.household_people()]
        names.extend(f"{entity.name} (pet)" for entity in self._entities if entity.kind == "pet")
        return names

    def household_roster(self) -> list[dict[str, str]]:
        roster = [{"name": person["name"], "kind": "person"} for person in self.household_people()]
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
        store = self._store
        if store is None:
            return []
        persons = self.household_people()
        subjects: list[Subject] = []
        unresolved: list[tuple[str, str]] = []
        for name in names:
            lowered = name.lower()
            if lowered in {"household", "the household", "everyone", "family", "the family", "home", "the home"}:
                if self._household_id is not None:
                    subjects.append(Subject(self._household_id, "household", "household"))
                continue
            person = next((p for p in persons if p["name"].lower() == lowered or p["name"].split()[0].lower() == lowered), None)
            if person is not None and person["entity_id"] not in self._person_ids:
                # A person added to Home Assistant since the last sync.
                await self._async_refresh_entities(sync_persons=True)
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
            await self._async_refresh_entities()
            for (name, kind), entity_pk in zip(unresolved, ids):
                entity = next((e for e in self._entities if e.id == entity_pk), None)
                subjects.append(Subject(entity_pk, entity.kind if entity else kind, entity.name if entity else name))
        return subjects

    async def async_speaker_subject(self, speaker: Speaker | None, conversation_id: str | None) -> Subject | None:
        """The subject for "about me": the signed-in person, or who they said they were."""
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
        return None

    # ── per-turn memory block ───────────────────────────────────────────────

    async def async_turn_context(self, text: str, speaker: Speaker, conversation_id: str | None) -> dict[str, Any]:
        """The context.memory block for a turn. Never raises; at most ~300 ms of recall."""
        block: dict[str, Any] = {"v": 1, "status": self.context_status, "scope": "owner"}
        # Notes are only attached when this account was last seen in owner
        # mode with memory allowed; the backend re-checks either way.
        if not self.enabled or self.last_known_mode == "guest" or self.memory_allowed is False:
            return block

        stated = self.stated_speakers.get(conversation_id)
        block["speaker"] = {"name": speaker.name, "source": speaker.source} if speaker.known else {"name": None, "source": "unknown"}
        block["stated_speaker"] = stated[0] if stated and not speaker.known else None
        block["household"] = self.household_roster()

        who = speaker if speaker.known else (Speaker(source="stated", name=stated[0], person_entity_id=stated[1]) if stated else None)
        speaker_subject = self._person_subject(who.name, who.person_entity_id) if who else None
        try:
            async with asyncio.timeout(TURN_CONTEXT_BUDGET_SECONDS):
                profile_rows, note_rows = await self.hass.async_add_executor_job(
                    self._turn_recall_blocking, text, speaker_subject.id if speaker_subject else None, self._member_ids()
                )
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Memory recall skipped this turn: %s", err)
            profile_rows, note_rows = [], []

        tz = dt_util.get_default_time_zone()
        private_label = not self.config.share_household
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
        store = self._store
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
        store = self._store
        if store is None:
            return [], []

        shared = self.config.share_household
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
            note_rows += store.todays_exchanges(since=_local_day_start(current_day(dt_util.now(), boundary), boundary))
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
        store = self._store
        if store is None:
            return ToolRecallResult([], "", [])
        subject_ids = [subject.id for subject in await self.async_resolve_subjects(about)] if about else []
        subject_ids += [entity_id for entity_id in match_subjects(query, self._entities) if entity_id not in subject_ids]
        today = dt_util.now().date()
        hints = parse_date_hints(query, today)
        boundary = self.config.day_boundary_hour
        range_start = _local_day_start(date_from, boundary) if date_from else None
        range_end = _local_day_start((date_to or date_from) + timedelta(days=1), boundary) if date_from else None
        # Due dates are calendar days (stored as local midnight), not memory days.
        due_start = _local_day_start(date_from, 0) if date_from else None
        due_end = _local_day_start((date_to or date_from) + timedelta(days=1), 0) if date_from else None
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
            private_label=not self.config.share_household,
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
        store = self._store
        if store is None:
            raise MemoryDbError("cannot_connect", "Memory store is not connected")
        memory_id, duplicate, replaced = await self.hass.async_add_executor_job(
            self._remember_blocking, memory, list(replaces or [])
        )
        self._profile_cache.clear()
        if memory.embedding is None:
            self.entry.async_create_background_task(self.hass, self.async_backfill_embeddings(), "hey_auri_client memory backfill")
        return memory_id, duplicate, replaced

    def _remember_blocking(self, memory: NewMemory, replaces: list[int]) -> tuple[int, bool, dict[int, str]]:
        store = self._store
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
        store = self._store
        if store is None:
            return []
        retracted = await self.hass.async_add_executor_job(lambda: store.retract(ids, reason="forget"))
        self._profile_cache.clear()
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
        """Queue one exchange (user message + reply) for the archive. Owner mode only (caller checks)."""
        if not self.config.configured or self.state == MemoryState.ERROR:
            return
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
        self._turn_logger.log_nowait(rows)

    async def _async_write_rows(self, rows: list[dict[str, Any]]) -> None:
        store = self._store
        if store is None or self.state != MemoryState.ENABLED:
            raise MemoryDbError("cannot_connect", "Memory store is not connected")
        await self.hass.async_add_executor_job(store.insert_messages, rows)

    # ── upkeep ──────────────────────────────────────────────────────────────

    async def async_import_legacy(self) -> None:
        """Import preferences from the old file-based tool, once per file."""
        store = self._store
        if store is None:
            return
        config_dir = self.hass.config.config_dir
        files = await self.hass.async_add_executor_job(load_legacy_files, config_dir, LEGACY_USER_DATA_DIR)
        if not files:
            return
        imported = set(await self.hass.async_add_executor_job(store.get_meta, "legacy_import") or [])
        users = [
            {"id": user.id, "name": user.name, "system_generated": user.system_generated}
            for user in await self.hass.auth.async_get_users()
        ]
        plans, skipped = plan_legacy_import(
            files, users=users, persons=household_persons(self.hass), already_imported=imported
        )
        if not plans and not skipped:
            return

        count = 0
        for plan in plans:
            count += await self.hass.async_add_executor_job(self._import_plan_blocking, plan)
            imported.add(plan.file.sha256)
        await self.hass.async_add_executor_job(store.set_meta, "legacy_import", sorted(imported))
        await self._async_refresh_entities()
        self._profile_cache.clear()

        lines = [f"Imported {count} saved preferences into Auri's memory."]
        if skipped:
            lines.append(
                "These files couldn't be matched to exactly one Home Assistant user and weren't imported: "
                + ", ".join(skipped)
            )
        persistent_notification.async_create(
            self.hass, " ".join(lines), title="Hey Auri memory", notification_id=f"{DOMAIN}_legacy_import"
        )
        if count:
            await self.async_backfill_embeddings()

    def _import_plan_blocking(self, plan: LegacyImportPlan) -> int:
        store = self._store
        count = 0
        with store.engine.begin() as conn:
            subject_ids = store.resolve_entities([(plan.person_name, "person")], conn)
            if plan.person_entity_id and plan.person_entity_id in self._person_ids:
                subject_ids = [self._person_ids[plan.person_entity_id]]
            for memory in plan.memories:
                memory.about_entity_ids = subject_ids
                _, duplicate = store.insert_memory(memory, conn)
                count += 0 if duplicate else 1
        return count

    async def async_backfill_embeddings(self, *, max_batches: int = 3) -> int:
        """Embed memories that have no vector (or an old model's) through /memory/embed."""
        store = self._store
        if store is None:
            return 0
        done = 0
        for _ in range(max_batches):
            rows = await self.hass.async_add_executor_job(lambda: store.missing_embeddings(self.embedding_tag, limit=100))
            if not rows:
                break
            try:
                vectors, tag = await self.api.embed([content for _, content in rows], task_type="RETRIEVAL_DOCUMENT")
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

    # ── nightly ─────────────────────────────────────────────────────────────

    def _schedule_nightly(self) -> None:
        # Spread households across a few minutes so they don't all hit the
        # cloud at the same instant.
        minute = 15 + random.randint(0, 10)
        self._unsubs.append(
            async_track_time_change(
                self.hass,
                self._async_nightly_trigger,
                hour=self.config.day_boundary_hour,
                minute=minute,
                second=random.randint(0, 59),
            )
        )

        @callback
        def _after_start(_hass: HomeAssistant) -> None:
            self._unsubs.append(async_call_later(self.hass, _CATCH_UP_DELAY_SECONDS, self._async_nightly_trigger))

        self._unsubs.append(async_at_started(self.hass, _after_start))

    async def _async_nightly_trigger(self, _now: Any = None) -> None:
        self.entry.async_create_background_task(self.hass, self.async_run_nightly(), "hey_auri_client memory nightly")

    async def async_run_nightly(self, *, day: date | None = None, force: bool = False, days_back: int = 7) -> dict[str, Any]:
        """Process one day (or catch up on missed days). Returns a summary."""
        if not self.enabled or self.memory_allowed is False:
            return {"status": "skipped", "reason": self.context_status if not self.enabled else "memory_not_allowed"}
        if day is not None and day >= current_day(dt_util.now(), self.config.day_boundary_hour):
            # A day still in progress would be marked done and its later turns never processed.
            return {"status": "skipped", "reason": "day_not_finished"}
        from .nightly import NightlyJob  # pylint: disable=import-outside-toplevel

        if self._nightly is None:
            self._nightly = NightlyJob(self)
        async with self._nightly_lock:
            if day is not None:
                result = await self._nightly.async_run_day(day, force=force)
            else:
                result = await self._nightly.async_catch_up(days_back=days_back)
        self.last_nightly = {"finished_at": dt_util.utcnow().isoformat(), **_run_summary(result)}
        self._profile_cache.clear()
        await self._async_refresh_entities()
        for listener in list(self._listeners):
            listener()
        return result

    async def async_run_exam(self) -> dict[str, Any]:
        from .exam import async_run_exam  # pylint: disable=import-outside-toplevel

        return await async_run_exam(self)

    # ── guest memory (switched off; see scope.py) ───────────────────────────

    @staticmethod
    def _guest_fingerprint(options: Any) -> tuple[str, str]:
        return str(options.get(CONF_BOOKING_ID) or ""), str(options.get(CONF_CHECK_OUT) or "")

    def _schedule_check_out(self) -> None:
        """Purge the booking's memory when its check-out time comes."""
        if self._check_out_unsub is not None:
            self._check_out_unsub()
            self._check_out_unsub = None
        check_out = parse_check_out(self.entry.options.get(CONF_CHECK_OUT))
        if check_out is None or check_out <= dt_util.now():
            return

        async def _at_check_out(_now: datetime) -> None:
            self._check_out_unsub = None
            await self.async_purge_guest_memory()

        self._check_out_unsub = async_track_point_in_time(self.hass, _at_check_out, check_out)

    async def async_purge_guest_memory(self) -> list[str]:
        """Hard-delete memory of guest bookings that ended or were replaced. Owner memory is never touched."""
        store = self._store
        if store is None:
            return []
        try:
            scopes = await self.hass.async_add_executor_job(store.list_scopes)
            stale = scopes_to_purge(
                scopes,
                booking_id=self.entry.options.get(CONF_BOOKING_ID),
                check_out=parse_check_out(self.entry.options.get(CONF_CHECK_OUT)),
                now=dt_util.now(),
            )
            for scope in stale:
                counts = await self.hass.async_add_executor_job(MemoryStore(store.engine, scope=scope).purge_scope)
                _LOGGER.info("Deleted guest memory for %s: %s", scope, counts)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Couldn't check for guest memory to delete: %s", err)
            return []
        return stale

    # ── status ──────────────────────────────────────────────────────────────

    def status_attributes(self) -> dict[str, Any]:
        return {
            "error": self.error,
            "schema_version": self.schema_version,
            "pgvector_version": self.pgvector_version,
            "embedding_model": self.embedding_tag,
            "last_known_mode": self.last_known_mode,
            "pending_log_rows": self._turn_logger.pending,
            "last_nightly_run": self.last_nightly,
        }


def _run_summary(result: dict[str, Any]) -> dict[str, Any]:
    """The compact part of a nightly result kept for the status sensor."""
    if "days" in result:
        return {
            "status": result.get("stopped") and "stopped" or result.get("status"),
            "days": {day: outcome.get("status") for day, outcome in result["days"].items()},
        }
    return {"status": result.get("status"), "reason": result.get("reason")}


def _version_at_least(version: str | None, minimum: tuple[int, ...]) -> bool:
    if not version:
        return False
    parts: list[int] = []
    for piece in version.split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts) >= minimum


def _local_day_start(day: date, boundary_hour: int) -> datetime:
    """When a local "day" starts, given the configured day boundary (e.g. 04:00)."""
    tz = dt_util.get_default_time_zone()
    return datetime.combine(day, time(hour=boundary_hour), tzinfo=tz)
