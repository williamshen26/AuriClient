"""Runtime for the local memory store.

Owns the database connection and its health, and two memory spaces: the
household's (owner mode, schema auri_memory) and the current guests' (guest
mode, schema auri_guest_memory, only when Guest Settings switch it on). Each
turn uses the space for its mode and no other. Also imports legacy
preferences, keeps guest memory in step with Guest Settings, and schedules
the nightly job. Nothing here may slow a conversation: database work runs in
executor threads with tight timeouts, and failures degrade to "no memory
this turn".
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import date, datetime, timedelta
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

from ..const import DOMAIN, LEGACY_USER_DATA_DIR
from ..saas_client import SaaSClient
from .api import EMBEDDING_DIMENSIONS, MemoryApi, embedding_tag
from .config import MemoryConfig
from .db import GUEST_MEMORY_SCHEMA, MEMORY_SCHEMA, MemoryDbError, build_engine, detect_vector_extension
from .guest import STAY_META_KEY, guest_erase_time, guest_memory_enabled, seed_memories, stay_marker, stay_over
from .identity import Speaker, household_persons
from .legacy import LegacyImportPlan, load_legacy_files, plan_legacy_import
from .migrations import apply_migrations, drop_schema, reset_schema
from .recall import SubjectEntity
from .space import MemorySpace
from .store import GUEST_SCOPE, MemoryStore
from .transcript import current_day

_LOGGER = logging.getLogger(__name__)

DEFAULT_EMBEDDING_TAG = embedding_tag("gemini-embedding-001", EMBEDDING_DIMENSIONS)
_HEALTH_CHECK_INTERVAL = timedelta(seconds=60)
# How often guest memory is checked against Guest Settings, which catches an
# erase or a setup that didn't happen on time (the database was down).
_GUEST_CHECK_INTERVAL = timedelta(minutes=15)
_PERSON_SYNC_INTERVAL_SECONDS = 30 * 60
_CATCH_UP_DELAY_SECONDS = 300
_STATE_STORAGE_KEY = f"{DOMAIN}_memory_state"
_STATE_STORAGE_VERSION = 1
_OWNER_SPOOL_KEY = f"{DOMAIN}_memory_turn_spool"
_GUEST_SPOOL_KEY = f"{DOMAIN}_guest_memory_turn_spool"
# Setup problems that retrying won't fix; the user has to change something.
_FATAL_ERROR_CODES = frozenset(
    {"psycopg2_missing", "pg_version_unsupported", "pgvector_missing", "insufficient_privilege", "schema_too_new", "invalid_auth"}
)


class MemoryState(StrEnum):
    DISABLED = "disabled"
    STARTING = "starting"
    ENABLED = "enabled"
    UNAVAILABLE = "unavailable"
    ERROR = "error"


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
        self.schema_version: int | None = None
        self.pgvector_version: str | None = None
        self.owner = MemorySpace(self, mode="owner", schema=MEMORY_SCHEMA, spool_key=_OWNER_SPOOL_KEY)
        self.guest = MemorySpace(self, mode="guest", schema=GUEST_MEMORY_SCHEMA, spool_key=_GUEST_SPOOL_KEY)

        self._engine = None
        self._iterative_scan = False
        self._unsubs: list[Callable[[], None]] = []
        self._listeners: list[Callable[[], None]] = []
        self._state_store: Store[dict[str, Any]] = Store(hass, _STATE_STORAGE_VERSION, _STATE_STORAGE_KEY)
        self._nightly_jobs: dict[str, Any] = {}
        self._nightly_lock = asyncio.Lock()
        self.last_nightly: dict[str, Any] | None = None
        self._erase_unsub: Callable[[], None] | None = None
        self._guest_lock = asyncio.Lock()
        # Whether guest memory was found switched off since connecting (nothing to remove).
        self._guest_known_off = False
        self.guest_status: dict[str, Any] = {"state": "off"}

    # ── lifecycle ───────────────────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        return self.state == MemoryState.ENABLED and self.owner.store is not None

    @property
    def store(self) -> MemoryStore | None:
        """The owner's store."""
        return self.owner.store

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

    def _notify(self) -> None:
        for listener in list(self._listeners):
            listener()

    def _set_state(self, state: MemoryState, error: str | None = None) -> None:
        self.state = state
        self.error = error
        self._notify()

    def _spaces(self) -> tuple[MemorySpace, MemorySpace]:
        return self.owner, self.guest

    def space_for(self, mode: str | None) -> MemorySpace:
        """The space a turn in this mode uses (owner unless the account is in guest mode)."""
        return self.guest if mode == "guest" else self.owner

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
        self._unsubs.append(async_track_time_interval(self.hass, self._async_guest_check, _GUEST_CHECK_INTERVAL))
        # Config entries aren't unloaded when Home Assistant stops: save
        # unwritten turns here so they're written on the next start.
        self._unsubs.append(self.hass.bus.async_listen(EVENT_HOMEASSISTANT_STOP, self._async_on_stop))
        self._schedule_nightly()
        self._schedule_guest_erase()

    async def _async_on_stop(self, _event: Event) -> None:
        for space in self._spaces():
            await space.turn_logger.async_shutdown()
        await self._async_save_state()

    async def async_shutdown(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        if self._erase_unsub is not None:
            self._erase_unsub()
            self._erase_unsub = None
        for space in self._spaces():
            await space.turn_logger.async_shutdown()
        await self._async_save_state()
        engine, self._engine = self._engine, None
        for space in self._spaces():
            space.detach()
        self._guest_known_off = False
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

        self._engine = engine
        self.schema_version, self.pgvector_version = schema_version, pgvector_version
        await self.hass.async_add_executor_job(self.owner.attach_blocking, store)
        await self.owner.async_refresh_entities(sync_persons=True)
        self._set_state(MemoryState.ENABLED)
        _LOGGER.info("Memory store connected (schema v%s, pgvector %s)", schema_version, pgvector_version)

        await self.owner.turn_logger.async_load_spool()
        self.owner.turn_logger.async_schedule_flush()
        self.entry.async_create_background_task(self.hass, self._async_after_connect(), "hey_auri_client memory upkeep")

    def _connect_blocking(self):
        """Connect, migrate and return (engine, owner store, schema version, pgvector version)."""
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
        self._iterative_scan = _version_at_least(pgvector_version, (0, 8))
        store = MemoryStore(engine, iterative_scan=self._iterative_scan)
        store.fail_stale_runs()
        return engine, store, schema_version, pgvector_version

    async def _async_after_connect(self) -> None:
        await self.async_sync_guest_memory()
        if self.guest.enabled:
            await self.guest.turn_logger.async_load_spool()
            self.guest.turn_logger.async_schedule_flush()
        await self.async_import_legacy()
        await self.async_backfill_embeddings()

    async def _async_health_check(self, _now: datetime | None = None) -> None:
        store = self.owner.store
        if self.state == MemoryState.ENABLED and store is not None:
            try:
                await self.hass.async_add_executor_job(store.ping)
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Memory store stopped responding: %s", err)
                self._set_state(MemoryState.UNAVAILABLE, "cannot_connect")
                return
            if monotonic() - self.owner.persons_synced_at > _PERSON_SYNC_INTERVAL_SECONDS:
                await self.owner.async_refresh_entities(sync_persons=True)
        elif self.state == MemoryState.UNAVAILABLE:
            if store is not None:
                try:
                    await self.hass.async_add_executor_job(store.ping)
                except Exception:  # noqa: BLE001
                    return
                self._set_state(MemoryState.ENABLED)
                for space in self._spaces():
                    space.turn_logger.async_schedule_flush()
            else:
                await self._async_connect()

    async def async_options_updated(self, options: dict[str, Any]) -> None:
        """Restart when database settings changed; otherwise apply the rest and re-check guest memory."""
        new_config = MemoryConfig.from_options(options)
        if new_config.fingerprint() != self.config.fingerprint():
            await self.async_shutdown()
            self.config = new_config
            for space in self._spaces():
                space.new_turn_logger()
            await self.async_start()
            return
        # Settings read on every turn (household sharing) apply without a restart.
        self.config = new_config
        self._schedule_guest_erase()
        await self.async_sync_guest_memory()

    # ── mode ────────────────────────────────────────────────────────────────

    @callback
    def note_mode(self, mode: str | None, memory_allowed: bool) -> None:
        """Remember what the backend said about this account (it decides; we only follow)."""
        if mode is None:
            return
        if self.last_known_mode is not None and mode != self.last_known_mode:
            # A mode switch must not carry speakers or cached notes across.
            # (Learning the mode for the first time isn't a switch.)
            for space in self._spaces():
                space.clear_conversations()
                space.invalidate()
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

    # ── turns ───────────────────────────────────────────────────────────────

    async def async_turn_context(self, text: str, speaker: Speaker, conversation_id: str | None) -> dict[str, Any]:
        """The context.memory block for a turn, from the space of the account's last known mode.

        The backend decides the mode again; a block for the wrong space is dropped there.
        """
        space = self.space_for(self.last_known_mode)
        if self.memory_allowed is False:
            return {"v": 1, "status": space.context_status, "scope": space.mode}
        return await space.async_turn_context(text, speaker, conversation_id)

    @callback
    def log_turn(
        self,
        *,
        mode: str | None,
        conversation_id: str,
        speaker: Speaker,
        user_text: str,
        reply: str | None,
        kind: str,
        tool_names: list[str],
        language: str | None,
    ) -> None:
        """Queue one exchange for the archive of the space of the turn's mode (the caller checks the mode)."""
        if not self.config.configured or self.state == MemoryState.ERROR:
            return
        space = self.space_for(mode)
        if space.is_guest and (not guest_memory_enabled(self.entry.options) or space.closed):
            return
        space.log_turn(
            conversation_id=conversation_id,
            speaker=speaker,
            user_text=user_text,
            reply=reply,
            kind=kind,
            tool_names=tool_names,
            language=language,
        )

    # ── the owner's memory, for callers that predate spaces ─────────────────

    @property
    def entities(self) -> list[SubjectEntity]:
        return self.owner.entities

    def household_people(self) -> list[dict[str, Any]]:
        return self.owner.people()

    def household_names(self) -> list[str]:
        return self.owner.household_names()

    def household_roster(self) -> list[dict[str, str]]:
        return self.owner.household_roster()

    async def async_backfill_embeddings(self, *, max_batches: int = 3) -> int:
        return await self.owner.async_backfill_embeddings(max_batches=max_batches)

    # ── upkeep ──────────────────────────────────────────────────────────────

    async def async_import_legacy(self) -> None:
        """Import preferences from the old file-based tool into the owner's memory, once per file."""
        store = self.owner.store
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
            count += await self.hass.async_add_executor_job(self._import_plan_blocking, store, plan)
            imported.add(plan.file.sha256)
        await self.hass.async_add_executor_job(store.set_meta, "legacy_import", sorted(imported))
        await self.owner.async_refresh_entities()
        self.owner.invalidate()

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

    def _import_plan_blocking(self, store: MemoryStore, plan: LegacyImportPlan) -> int:
        count = 0
        person_ids = self.owner.person_ids
        with store.engine.begin() as conn:
            subject_ids = store.resolve_entities([(plan.person_name, "person")], conn)
            if plan.person_entity_id and plan.person_entity_id in person_ids:
                subject_ids = [person_ids[plan.person_entity_id]]
            for memory in plan.memories:
                memory.about_entity_ids = subject_ids
                _, duplicate = store.insert_memory(memory, conn)
                count += 0 if duplicate else 1
        return count

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
        """Process one day (or catch up on missed days), for the owner and then any guests. Returns a summary."""
        if not self.enabled or self.memory_allowed is False:
            return {"status": "skipped", "reason": self.context_status if not self.enabled else "memory_not_allowed"}
        if day is not None and day >= current_day(dt_util.now(), self.config.day_boundary_hour):
            # A day still in progress would be marked done and its later turns never processed.
            return {"status": "skipped", "reason": "day_not_finished"}

        async with self._nightly_lock:
            result = await self._async_run_space_nightly(self.owner, day=day, force=force, days_back=days_back)
            guest_result = None
            # A guest erase waits for the guests' run, so the run can't write an old stay into an erased schema.
            async with self._guest_lock:
                if self.guest.enabled:
                    guest_result = await self._async_run_space_nightly(self.guest, day=day, force=force, days_back=days_back)
            if guest_result is not None:
                result = {**result, "guest": guest_result}
        self.last_nightly = {"finished_at": dt_util.utcnow().isoformat(), **_run_summary(result)}
        if guest_result is not None:
            self.last_nightly["guest"] = _run_summary(guest_result)
        for space in self._spaces():
            space.invalidate()
            await space.async_refresh_entities()
        self._notify()
        return result

    async def _async_run_space_nightly(
        self, space: MemorySpace, *, day: date | None, force: bool, days_back: int
    ) -> dict[str, Any]:
        from .nightly import NightlyJob  # pylint: disable=import-outside-toplevel

        job = self._nightly_jobs.get(space.mode)
        if job is None or job._space is not space:  # noqa: SLF001
            job = self._nightly_jobs[space.mode] = NightlyJob(space)
        if day is not None:
            return await job.async_run_day(day, force=force)
        return await job.async_catch_up(days_back=days_back)

    async def async_run_exam(self) -> dict[str, Any]:
        from .exam import async_run_exam  # pylint: disable=import-outside-toplevel

        return await async_run_exam(self)

    # ── guest memory ────────────────────────────────────────────────────────

    def _schedule_guest_erase(self) -> None:
        """Erase guest memory when the stay ends, an hour after check-out."""
        if self._erase_unsub is not None:
            self._erase_unsub()
            self._erase_unsub = None
        erase_at = guest_erase_time(self.entry.options)
        if erase_at is None or erase_at <= dt_util.now():
            return

        async def _at_erase_time(_now: datetime) -> None:
            self._erase_unsub = None
            await self.async_sync_guest_memory()

        self._erase_unsub = async_track_point_in_time(self.hass, _at_erase_time, erase_at)

    async def _async_guest_check(self, _now: datetime | None = None) -> None:
        """Every 15 minutes: an erase or setup that couldn't happen on time (database down) happens now."""
        await self.async_sync_guest_memory()

    async def async_sync_guest_memory(self) -> str | None:
        """Bring guest memory in line with Guest Settings. Returns "reset", "erased", "removed" or None.

        - switched off: the guest schema is removed;
        - a new stay (booking ID, or check-in without one) or the host's reset:
          erased and started again from the guest details;
        - an hour after check-out: erased, and closed until the next stay
          (MemorySpace.closed: nothing is recalled, kept or learned).
        The owner's memory is never touched.
        """
        async with self._guest_lock:
            engine = self._engine
            if engine is None or not self.enabled:
                return None
            options = self.entry.options
            try:
                if not guest_memory_enabled(options):
                    if self.guest.store is None and self._guest_known_off:
                        return None
                    await self.guest.turn_logger.async_clear()
                    self.guest.detach()
                    await self.hass.async_add_executor_job(lambda: drop_schema(engine, schema=GUEST_MEMORY_SCHEMA))
                    self._guest_known_off = True
                    self.guest_status = {"state": "off"}
                    self._notify()
                    return "removed"

                self._guest_known_off = False
                if self.guest.store is None:
                    store = await self.hass.async_add_executor_job(self._attach_guest_blocking, engine)
                else:
                    store = self.guest.store
                stay = await self.hass.async_add_executor_job(store.get_meta, STAY_META_KEY) or {}
                marker = stay_marker(options)
                same_stay = stay.get("stay") == marker["stay"] and stay.get("reset_at") == marker["reset_at"]
                if stay_over(options):
                    if same_stay and stay.get("erased_at"):
                        self.guest_status = {"state": "closed", **stay}
                        return None
                    await self._async_reset_guest(engine, seed=False, marker={**marker, "erased_at": dt_util.utcnow().isoformat()})
                    return "erased"
                if same_stay and not stay.get("erased_at"):
                    self.guest_status = {"state": "on", **stay}
                    return None
                await self._async_reset_guest(engine, seed=True, marker=marker)
                return "reset"
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Couldn't update guest memory: %s", err)
                return None

    def _attach_guest_blocking(self, engine) -> MemoryStore:
        apply_migrations(engine, app_version=self.app_version, schema=GUEST_MEMORY_SCHEMA)
        store = MemoryStore(engine, schema=GUEST_MEMORY_SCHEMA, scope=GUEST_SCOPE, iterative_scan=self._iterative_scan)
        self.guest.attach_blocking(store)
        return store

    async def _async_reset_guest(self, engine, *, seed: bool, marker: dict[str, Any]) -> None:
        """Erase guest memory completely (one transaction), then add the stay's seed memories."""
        # Nothing queued for the old stay may be written into the new one.
        await self.guest.turn_logger.async_clear()
        self.guest.detach()
        seeds = seed_memories(self.entry.options) if seed else []
        marker = {**marker, "reset_done_at": dt_util.utcnow().isoformat(), "seeded": len(seeds)}

        def _seed(conn) -> None:
            store = MemoryStore(engine, schema=GUEST_MEMORY_SCHEMA, scope=GUEST_SCOPE)
            for memory, subjects in seeds:
                memory.about_entity_ids = store.resolve_entities(subjects, conn)
                store.insert_memory(memory, conn)
            store.set_meta(STAY_META_KEY, marker, conn)

        await self.hass.async_add_executor_job(
            lambda: reset_schema(engine, schema=GUEST_MEMORY_SCHEMA, app_version=self.app_version, seed=_seed)
        )
        store = MemoryStore(engine, schema=GUEST_MEMORY_SCHEMA, scope=GUEST_SCOPE, iterative_scan=self._iterative_scan)
        await self.hass.async_add_executor_job(self.guest.attach_blocking, store)
        await self.guest.async_refresh_entities()
        self.guest_status = {"state": "on" if seed else "closed", **marker}
        _LOGGER.info("Guest memory %s (%d memories from Guest Settings)", "erased" if not seed else "reset", len(seeds))
        if seeds:
            self.entry.async_create_background_task(
                self.hass, self.guest.async_backfill_embeddings(), "hey_auri_client guest memory backfill"
            )
        self._notify()

    # ── status ──────────────────────────────────────────────────────────────

    def status_attributes(self) -> dict[str, Any]:
        return {
            "error": self.error,
            "schema_version": self.schema_version,
            "pgvector_version": self.pgvector_version,
            "embedding_model": self.embedding_tag,
            "last_known_mode": self.last_known_mode,
            "pending_log_rows": self.owner.turn_logger.pending + self.guest.turn_logger.pending,
            "last_nightly_run": self.last_nightly,
            "guest_memory": self.guest_status.get("state"),
            "guest_memory_reset": self.guest_status.get("reset_done_at"),
            "guest_memory_erased": self.guest_status.get("erased_at"),
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
