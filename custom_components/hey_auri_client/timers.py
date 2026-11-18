"""Auri's timers: Home Assistant's own timers on the device that asked, mirrored for the dashboard.

A timer starts on the device the request came from whenever Home Assistant can ring there (a
"timer device"): a voice satellite rings itself while its LED ring counts down, and on the phone
app that phone gets a notification. A request from anywhere else (the web page, an automation, a
satellite without timers) gets Auri's own timer instead; its finish is pushed to the asker's
phones, or to everyone's (but Auri support's) when the asker has none.

Every timer, Home Assistant's own included (set through its built-in agent, say), has a
sensor.auri_timer_<id> for the dashboard's timer bars and for Auri's list_timers. Its state is
active or paused, with the duration, the time left and when it finishes.

Timers are kept in a Store and survive restarts (the nightly update restarts Home Assistant):
once Home Assistant is back and the timer's device has reconnected, each one resumes with the time
it had left, and one that ran out meanwhile rings at once. A satellite keeps showing the timers it
had until it's told they ended, so the old ones are cancelled on it first.

Home Assistant tells only the device about its timers' changes, so Auri looks at its timers once a
second: that's how a timer that finished, or one changed through Home Assistant's own agent, is
noticed.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
import math
import re
import time
from typing import Any
import uuid

from homeassistant.components.intent import TimerEventType, TimerInfo, TimerManager
from homeassistant.components.intent.const import TIMER_DATA
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_DEVICE_ID
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN, EVENT_TIMER_FINISHED
from .entities.timer import AuriTimerEntity

_LOGGER = logging.getLogger(__name__)

STORAGE_KEY = f"{DOMAIN}_timers"
STORAGE_VERSION = 1
SYNC_INTERVAL = timedelta(seconds=1)
# After a restart, how long a timer waits for its device to reconnect before it's dropped.
RESTORE_WAIT = timedelta(minutes=15)
# A timer on a device that's gone with this little left finished there (rather than being cancelled).
FINISH_SLACK_SECONDS = 2.0
# What Home Assistant sends a phone when one of its own timers finishes (mobile_app/timers.py).
PHONE_PUSH_DATA = {
    "group": "timers",
    # Android
    "channel": "Timers",
    "importance": "high",
    "ttl": 0,
    "priority": "high",
    # iOS
    "push": {"interruption-level": "time-sensitive"},
}
STATUS_ACTIVE = "active"
STATUS_PAUSED = "paused"

_NOT_A_WORD = re.compile(r"[^\w\s]")


def new_timer_id() -> str:
    """Auri's short id for a timer (its sensor is sensor.auri_timer_<id>)."""
    return uuid.uuid4().hex[:8]


def normalize_name(name: str | None) -> str:
    """A timer name for comparing: lower case, without punctuation or extra spaces."""
    return " ".join(_NOT_A_WORD.sub(" ", str(name or "").casefold()).split())


def parse_duration(value: Any) -> int:
    """Whole seconds from a duration: seconds (300), MM:SS, HH:MM:SS or ISO 8601 (PT5M)."""
    if isinstance(value, timedelta):
        seconds = value.total_seconds()
    elif isinstance(value, bool) or value is None:
        seconds = None
    elif isinstance(value, (int, float)):
        seconds = float(value)
    else:
        parsed = dt_util.parse_duration(str(value).strip())
        seconds = None if parsed is None else parsed.total_seconds()
    if seconds is None:
        raise ValueError(
            f"Unrecognized duration {value!r}: give seconds (300), MM:SS (05:00) or HH:MM:SS (01:30:00)."
        )
    if round(seconds) <= 0:
        raise ValueError("A timer needs more than 0 seconds.")
    return int(round(seconds))


async def async_push_to_phone(hass: HomeAssistant, device_id: str, message: str) -> None:
    """Push a timer's end to one phone, as Home Assistant pushes its own timers' (mobile_app/timers.py).

    The mobile app integration is imported only here, once there's a phone to push to.
    """
    from homeassistant.components.mobile_app import device_action  # noqa: PLC0415

    await device_action.async_call_action_from_config(
        hass, {CONF_DEVICE_ID: device_id, "message": message, "data": PHONE_PUSH_DATA}, {}, None
    )


def _split(seconds: int) -> tuple[int | None, int | None, int | None]:
    """Hours, minutes and seconds, as Home Assistant keeps a timer's spoken length (None for none)."""
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return hours or None, minutes or None, secs or None


@dataclass
class TimerRecord:
    """One of Auri's timers: on a timer device (native_id is Home Assistant's id), or Auri's own."""

    id: str
    duration: int
    name: str | None = None
    # The device it rings on; None for Auri's own timer (which notifies phones).
    device_id: str | None = None
    native_id: str | None = None
    language: str = "en"
    # Who asked, for an own timer's notification.
    user_id: str | None = None
    # While it runs, when it finishes; while it's paused, the seconds it has left.
    finishes_at: datetime | None = None
    seconds_left: float | None = None
    # After a restart, until it's started again on its device.
    restoring: bool = False

    @property
    def paused(self) -> bool:
        return self.finishes_at is None

    def remaining(self, now: datetime) -> float:
        if self.finishes_at is None:
            return max(0.0, float(self.seconds_left or 0))
        return max(0.0, (self.finishes_at - now).total_seconds())

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "duration": self.duration,
            "name": self.name,
            "device_id": self.device_id,
            "native_id": self.native_id,
            "language": self.language,
            "user_id": self.user_id,
            "finishes_at": self.finishes_at.isoformat() if self.finishes_at else None,
            "seconds_left": self.seconds_left,
        }

    @classmethod
    def from_dict(cls, data: Any) -> TimerRecord | None:
        if not isinstance(data, dict) or not data.get("id"):
            return None
        finishes_at = dt_util.parse_datetime(str(data["finishes_at"])) if data.get("finishes_at") else None
        seconds_left = data.get("seconds_left")
        if finishes_at is None and not isinstance(seconds_left, (int, float)):
            return None
        try:
            duration = max(1, int(data.get("duration") or 0))
        except (TypeError, ValueError):
            return None
        return cls(
            id=str(data["id"]),
            duration=duration,
            name=data.get("name") or None,
            device_id=data.get("device_id") or None,
            native_id=data.get("native_id") or None,
            language=str(data.get("language") or "en"),
            user_id=data.get("user_id") or None,
            finishes_at=finishes_at,
            seconds_left=None if finishes_at is not None else float(seconds_left),
        )


class AuriTimers:
    """Every timer on the hub, its sensor, and keeping both across restarts."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._records: dict[str, TimerRecord] = {}
        self._entities: dict[str, AuriTimerEntity] = {}
        self._add_entities: AddEntitiesCallback | None = None
        # Old timers to cancel on a satellite once it's back: {device_id, native_id, name, language, until}.
        self._stale: list[dict[str, Any]] = []
        self._restore_until = dt_util.utcnow() + RESTORE_WAIT
        self._unsub: CALLBACK_TYPE | None = None

    # -- running ------------------------------------------------------------------------------

    async def async_start(self, add_entities: AddEntitiesCallback) -> None:
        """Load the kept timers, add their sensors, and start following them (the sensor platform's setup)."""
        self._add_entities = add_entities
        await self._async_load()
        self._restore_until = dt_util.utcnow() + RESTORE_WAIT
        add_entities([self._new_entity(record) for record in self._records.values()])
        self._unsub = async_track_time_interval(self.hass, self._async_sync, SYNC_INTERVAL, name="Auri timers")
        self._async_sync()

    async def async_stop(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None
        await self._store.async_save(self._data())

    async def _async_load(self) -> None:
        data = await self._store.async_load() or {}
        manager = self._manager()
        for item in data.get("timers") or []:
            record = TimerRecord.from_dict(item)
            if record is None:
                continue
            if record.device_id is not None and (manager is None or record.native_id not in manager.timers):
                # Home Assistant restarted: started again once its device is back.
                record.restoring = True
            self._records[record.id] = record
        now = dt_util.utcnow()
        for item in data.get("stale") or []:
            until = dt_util.parse_datetime(str(item.get("until") or "")) if isinstance(item, dict) else None
            if until is not None and until > now and item.get("device_id") and item.get("native_id"):
                self._stale.append(dict(item))

    def _data(self) -> dict[str, Any]:
        return {"timers": [record.as_dict() for record in self._records.values()], "stale": self._stale}

    @callback
    def _save(self) -> None:
        self._store.async_delay_save(self._data, 1)

    def _manager(self) -> TimerManager | None:
        return self.hass.data.get(TIMER_DATA)

    # -- what tools and services use ----------------------------------------------------------

    def timers(self) -> list[TimerRecord]:
        """Every timer, the one ending first first (paused ones by their time left)."""
        now = dt_util.utcnow()
        return sorted(self._records.values(), key=lambda record: record.remaining(now))

    @callback
    def start(
        self,
        seconds: int,
        *,
        name: str | None = None,
        device_id: str | None = None,
        user_id: str | None = None,
        language: str | None = None,
    ) -> TimerRecord:
        """Start a timer on device_id when it's a timer device, otherwise Auri's own."""
        now = dt_util.utcnow()
        record = TimerRecord(
            id=new_timer_id(),
            duration=seconds,
            name=(name or "").strip() or None,
            language=language or self.hass.config.language or "en",
            finishes_at=now + timedelta(seconds=seconds),
        )
        manager = self._manager()
        if device_id and manager is not None and manager.is_timer_device(device_id):
            record.native_id = manager.start_timer(
                device_id, *_split(seconds), language=record.language, name=record.name
            )
            record.device_id = device_id
        else:
            record.user_id = user_id
        self._records[record.id] = record
        self._add(record)
        self._save()
        return record

    def find(
        self, *, timer_id: str | None = None, name: str | None = None, device_id: str | None = None
    ) -> list[TimerRecord]:
        """The timers a request means: by id, else by name, else the only one (or this device's only one)."""
        records = self.timers()
        if timer_id:
            wanted = str(timer_id).strip().lower()
            return [r for r in records if wanted in (r.id, (r.native_id or "").lower())]
        if name and (wanted := normalize_name(name)):
            named = [(r, normalize_name(r.name)) for r in records if r.name]
            exact = [r for r, own in named if own == wanted]
            return exact or [r for r, own in named if wanted in own or own in wanted]
        if len(records) <= 1:
            return records
        here = [r for r in records if device_id and r.device_id == device_id]
        return here if len(here) == 1 else records

    @callback
    def cancel(self, record: TimerRecord) -> None:
        manager = self._manager()
        if record.restoring:
            self._forget_on_device(record)
        elif record.native_id and manager is not None:
            try:
                manager.cancel_timer(record.native_id)
            except HomeAssistantError:
                pass  # it finished or was cancelled a moment ago
        self._drop(record)
        self._save()

    @callback
    def change(self, record: TimerRecord, seconds: int) -> None:
        """Add time to a timer (or, with negative seconds, take some off). Raises when it ended meanwhile."""
        now = dt_util.utcnow()
        manager = self._manager()
        if self._on_device(record) and manager is not None:
            manager.add_time(record.native_id, seconds)
            self._take(record, manager.timers[record.native_id], now)
        else:
            left = max(0.0, record.remaining(now) + seconds)
            if record.paused:
                record.seconds_left = left
            else:
                record.finishes_at = now + timedelta(seconds=left)
            record.duration = max(record.duration, math.ceil(left))
        self._write(record)
        self._save()

    @callback
    def pause(self, record: TimerRecord) -> None:
        if record.paused:
            return
        now = dt_util.utcnow()
        manager = self._manager()
        if self._on_device(record) and manager is not None:
            manager.pause_timer(record.native_id)
            self._take(record, manager.timers[record.native_id], now)
        else:
            record.seconds_left, record.finishes_at = record.remaining(now), None
        self._write(record)
        self._save()

    @callback
    def resume(self, record: TimerRecord) -> None:
        if not record.paused:
            return
        now = dt_util.utcnow()
        manager = self._manager()
        if self._on_device(record) and manager is not None:
            manager.unpause_timer(record.native_id)
            self._take(record, manager.timers[record.native_id], now)
        else:
            record.finishes_at, record.seconds_left = now + timedelta(seconds=record.remaining(now)), None
        self._write(record)
        self._save()

    def device_name(self, device_id: str | None) -> str | None:
        if not device_id or (device := dr.async_get(self.hass).async_get(device_id)) is None:
            return None
        return device.name_by_user or device.name

    def describe(self, record: TimerRecord, *, device_id: str | None = None) -> dict[str, Any]:
        """A timer as tools report it (seconds)."""
        return {
            "timer_id": record.id,
            "name": record.name,
            "duration": record.duration,
            "remaining": math.ceil(record.remaining(dt_util.utcnow())),
            "status": STATUS_PAUSED if record.paused else STATUS_ACTIVE,
            # Where it rings; None: Auri's own timer, which notifies phones when it ends.
            "device": self.device_name(record.device_id),
            "on_this_device": bool(device_id) and record.device_id == device_id,
        }

    def entity_id(self, record: TimerRecord) -> str:
        return f"sensor.auri_timer_{record.id}"

    # -- following Home Assistant's timers ----------------------------------------------------

    def _on_device(self, record: TimerRecord) -> bool:
        return record.device_id is not None and not record.restoring and record.native_id is not None

    @callback
    def _async_sync(self, _now: datetime | None = None) -> None:
        manager = self._manager()
        now = dt_util.utcnow()
        native = {
            timer_id: info
            for timer_id, info in (manager.timers.items() if manager is not None else ())
            # Delayed commands ("turn off the lights in 5 minutes") ring nowhere.
            if not getattr(info, "conversation_command", None)
        }
        changed = False
        for record in list(self._records.values()):
            if record.device_id is None:
                if not record.paused and record.remaining(now) <= 0:
                    self._finish(record, notify=True)
                    changed = True
            elif record.restoring:
                changed |= self._restore(record, manager, now)
            elif (info := native.get(record.native_id)) is None:
                # It finished on its device, or was cancelled outside Auri (Home Assistant's own agent).
                if not record.paused and record.remaining(now) <= FINISH_SLACK_SECONDS:
                    self._finish(record, notify=False)
                else:
                    self._drop(record)
                changed = True
            else:
                changed |= self._follow(record, info, now)
        known = {record.native_id for record in self._records.values() if record.native_id}
        for info in native.values():
            if info.id not in known and info.device_id:
                self._adopt(info, now)
                changed = True
        changed |= self._clear_stale(manager, now)
        if changed:
            self._save()

    @staticmethod
    def _native_finish(info: TimerInfo, now: datetime) -> datetime:
        left_ns = info.updated_at + info.seconds * 1_000_000_000 - time.monotonic_ns()
        return now + timedelta(seconds=max(0.0, left_ns / 1e9))

    def _take(self, record: TimerRecord, info: TimerInfo, now: datetime) -> None:
        """Copy a Home Assistant timer's state into its record."""
        if info.is_active:
            record.finishes_at, record.seconds_left = self._native_finish(info, now), None
        else:
            record.finishes_at, record.seconds_left = None, float(info.seconds)
        record.name = info.name or None
        record.duration = max(record.duration, int(info.created_seconds), math.ceil(record.remaining(now)))

    def _follow(self, record: TimerRecord, info: TimerInfo, now: datetime) -> bool:
        """Take a change made to the timer outside Auri, if any (returns whether there was one)."""
        if info.is_active == record.paused or (info.name or None) != record.name:
            moved = True
        elif info.is_active:
            moved = abs((self._native_finish(info, now) - record.finishes_at).total_seconds()) > 1.0
        else:
            moved = abs(float(info.seconds) - float(record.seconds_left or 0)) > 1.0
        if moved:
            self._take(record, info, now)
            self._write(record)
        return moved

    def _adopt(self, info: TimerInfo, now: datetime) -> None:
        """A timer Home Assistant started without Auri (its own agent): shown and kept the same way."""
        record = TimerRecord(
            id=new_timer_id(),
            duration=max(1, int(info.created_seconds)),
            device_id=info.device_id,
            native_id=info.id,
            language=info.language or "en",
        )
        self._take(record, info, now)
        self._records[record.id] = record
        self._add(record)

    def _restore(self, record: TimerRecord, manager: TimerManager | None, now: datetime) -> bool:
        """After a restart: start the timer again on its device once the device is back."""
        if manager is None or not manager.is_timer_device(record.device_id):
            if now < self._restore_until:
                return False
            _LOGGER.warning(
                "Dropped the timer %s: its device didn't reconnect within %s of Home Assistant starting",
                record.name or record.id,
                RESTORE_WAIT,
            )
            self._drop(record)
            return True
        self._cancel_on_device(manager, record.device_id, record.native_id, record.name, record.language)
        # One that ran out while Home Assistant was down rings at once.
        seconds = 1 if not record.paused and record.remaining(now) <= 0 else max(1, math.ceil(record.remaining(now)))
        try:
            native_id = manager.start_timer(record.device_id, *_split(seconds), language=record.language, name=record.name)
            if record.paused:
                manager.pause_timer(native_id)
        except HomeAssistantError as err:
            _LOGGER.warning("Couldn't restart the timer %s on its device: %s", record.name or record.id, err)
            self._drop(record)
            return True
        record.native_id, record.restoring = native_id, False
        if record.paused:
            record.seconds_left = float(seconds)
        else:
            record.finishes_at = now + timedelta(seconds=seconds)
        self._write(record)
        return True

    def _forget_on_device(self, record: TimerRecord) -> None:
        """A timer cancelled while its device is still away: cancel the old one there once it's back."""
        if record.device_id and record.native_id:
            self._stale.append(
                {
                    "device_id": record.device_id,
                    "native_id": record.native_id,
                    "name": record.name,
                    "language": record.language,
                    "until": self._restore_until.isoformat(),
                }
            )

    def _clear_stale(self, manager: TimerManager | None, now: datetime) -> bool:
        if not self._stale:
            return False
        kept = []
        for item in self._stale:
            until = dt_util.parse_datetime(str(item.get("until") or ""))
            if manager is not None and manager.is_timer_device(item["device_id"]):
                self._cancel_on_device(manager, item["device_id"], item["native_id"], item.get("name"), item.get("language"))
            elif until is not None and until > now:
                kept.append(item)
        changed = len(kept) != len(self._stale)
        self._stale = kept
        return changed

    @staticmethod
    def _cancel_on_device(
        manager: TimerManager, device_id: str, native_id: str | None, name: str | None, language: str | None
    ) -> None:
        """Tell a device that a timer from before the restart is gone (a satellite shows it until then)."""
        handler = getattr(manager, "handlers", {}).get(device_id)
        if handler is None or not native_id:
            return
        stamp = time.monotonic_ns()
        try:
            handler(
                TimerEventType.CANCELLED,
                TimerInfo(
                    id=native_id,
                    name=name,
                    seconds=0,
                    device_id=device_id,
                    start_hours=None,
                    start_minutes=None,
                    start_seconds=None,
                    created_at=stamp,
                    updated_at=stamp,
                    language=language or "en",
                    is_active=False,
                ),
            )
        except Exception:  # noqa: BLE001 (an old timer left on a satellite only looks wrong)
            _LOGGER.debug("Couldn't clear an old timer from device %s", device_id, exc_info=True)

    # -- sensors and endings ------------------------------------------------------------------

    def _new_entity(self, record: TimerRecord) -> AuriTimerEntity:
        entity = AuriTimerEntity(record, self.entity_id(record), self.device_name)
        self._entities[record.id] = entity
        return entity

    def _add(self, record: TimerRecord) -> None:
        entity = self._new_entity(record)
        if self._add_entities is not None:
            self._add_entities([entity])

    def _write(self, record: TimerRecord) -> None:
        entity = self._entities.get(record.id)
        if entity is not None and entity.hass is not None:
            entity.async_write_ha_state()

    def _drop(self, record: TimerRecord) -> None:
        self._records.pop(record.id, None)
        entity = self._entities.pop(record.id, None)
        if entity is not None and entity.hass is not None:
            self.hass.async_create_task(entity.async_remove(), eager_start=True)

    def _finish(self, record: TimerRecord, *, notify: bool) -> None:
        self._drop(record)
        _LOGGER.debug("Timer %s finished", record.name or record.id)
        self.hass.bus.async_fire(
            EVENT_TIMER_FINISHED,
            {
                "timer_id": record.id,
                "entity_id": self.entity_id(record),
                "name": record.name,
                "duration": record.duration,
                "device_id": record.device_id,
            },
        )
        if notify:
            self.hass.async_create_background_task(
                self._async_notify_phones(record), name="Auri timer notification"
            )

    async def _async_notify_phones(self, record: TimerRecord) -> None:
        """An own timer's finish, pushed as Home Assistant pushes its own timers' to the phone that set them."""
        phones = await self._async_phones(record.user_id)
        if not phones:
            return
        if record.name:
            message = f"{record.name} finished"
        else:
            message = f"{timedelta(seconds=record.duration)} timer finished"
        for device_id in phones:
            try:
                await async_push_to_phone(self.hass, device_id, message)
            except (HomeAssistantError, ImportError) as err:
                _LOGGER.warning("Couldn't send the timer notification to a phone: %s", err)

    async def _async_phones(self, user_id: str | None) -> list[str]:
        """The asker's phones that take notifications, or everyone's (but Auri support's) without any."""
        from .support import async_support_user  # noqa: PLC0415 (support imports the auth models)

        registry = dr.async_get(self.hass)
        phones: dict[str, list[str]] = {}
        for entry in self.hass.config_entries.async_entries("mobile_app"):
            for device in dr.async_entries_for_config_entry(registry, entry.entry_id):
                # Home Assistant takes a phone as a timer device exactly when it can push to it.
                if self._manager() is not None and self._manager().is_timer_device(device.id):
                    phones.setdefault(str(entry.data.get("user_id") or ""), []).append(device.id)
        if user_id and phones.get(user_id):
            return phones[user_id]
        support = await async_support_user(self.hass, dict(self.entry.options))
        staff = support.id if support is not None else None
        return [device_id for owner, devices in phones.items() if owner != staff for device_id in devices]
