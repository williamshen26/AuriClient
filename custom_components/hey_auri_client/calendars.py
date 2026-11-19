"""The home's two calendars: the household's Home Calendar and the guests' Guest Calendar.

Both are Home Assistant Local Calendars, which can add, change and remove events and keep repeating
ones (Local Calendar only for now; other calendars may come later). Hey Auri Client sets up whichever
it needs and is missing: the Home Calendar at once, the Guest Calendar once the home hosts guests (the
account is set to guest or Automatic, or a stay is set). Each is found by its Local Calendar entry, so
renaming its entity doesn't lose it.

The mode decides which calendar Auri and the dashboard use, by the backend's rule for the account's
setting: owner, the Home Calendar; guest, the Guest Calendar; Automatic, owner only when no stay is
under way and someone from the household is home (bookings/automatic_mode.py), guest otherwise. The
backend sends the account's setting with every answer (account_mode); a turn uses the mode the backend
decided for it. The "Auri Mode" sensor shows the mode between turns, for the dashboard.

The Guest Calendar is emptied once per stay, so every guest starts with a clean calendar: when the
stay's guest memory is erased (an hour after check-out), or when a new stay takes over before that.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import timedelta
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import SOURCE_USER, ConfigEntry
from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .bookings.automatic_mode import stay_context
from .bookings.schedule import stay_state
from .const import DOMAIN
from .memory.guest import stay_key, stay_over

if TYPE_CHECKING:
    from .bookings.sync import BookingSync

_LOGGER = logging.getLogger(__name__)

HOME = "home"
GUEST = "guest"
# Each calendar's name, and the Local Calendar storage key its name gives (Local Calendar's slugify).
CALENDARS = {HOME: ("Home Calendar", "home_calendar"), GUEST: ("Guest Calendar", "guest_calendar")}
LOCAL_CALENDAR = "local_calendar"
ACCOUNT_MODES = frozenset({"owner", "guest", "auto"})
STORAGE_VERSION = 1
# How often the stay, the time and the mode are looked at (presence changes are followed at once).
TICK = timedelta(minutes=1)
# Emptying the Guest Calendar: every event from this long ago to this far ahead.
_CLEAR_BACK = timedelta(days=5 * 365)
_CLEAR_AHEAD = timedelta(days=10 * 365)
# memory.guest.stay_key for a home with no stay set (no booking ID, no check-in).
NO_STAY = "check_in:"


def calendar_entity(hass: HomeAssistant, entity_id: str | None) -> Any:
    """The calendar entity object (as Home Assistant's calendar websocket commands get it), or None."""
    if not entity_id:
        return None
    try:
        from homeassistant.components.calendar import DATA_COMPONENT  # noqa: PLC0415
    except ImportError:
        return None
    component = hass.data.get(DATA_COMPONENT)
    return component.get_entity(entity_id) if component is not None else None


class AuriCalendars:
    """Finds and sets up the two calendars, follows the account's mode, and empties the Guest Calendar."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, bookings: BookingSync | None) -> None:
        self.hass = hass
        self.entry = entry
        self.bookings = bookings
        self.account_mode: str | None = None  # the account's setting, as the backend last said
        self._marker: dict[str, Any] = {}  # {"stay": the stay the Guest Calendar is for, "cleared": bool}
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, f"{DOMAIN}_calendars_{entry.entry_id}")
        self._listeners: list[Callable[[], None]] = []
        self._unsubs: list[CALLBACK_TYPE] = []
        self._locks = {HOME: asyncio.Lock(), GUEST: asyncio.Lock()}
        self._refreshing = asyncio.Lock()  # the minute's check and a change's never overlap
        self._shown_mode: str | None = None
        self._started = False

    # ── lifecycle ─────────────────────────────────────────────────────────────

    async def async_start(self) -> None:
        saved = await self._store.async_load() or {}
        mode = saved.get("account_mode")
        self.account_mode = mode if mode in ACCOUNT_MODES else None
        self._marker = saved.get("marker") if isinstance(saved.get("marker"), dict) else {}
        self._started = True
        await self.async_ensure(HOME)
        if self._hosting():
            await self.async_ensure(GUEST)
        if self.bookings is not None:
            self._unsubs.append(self.bookings.async_add_listener(self._changed))
        self._unsubs.append(async_track_time_interval(self.hass, self._async_tick, TICK))
        self._unsubs.append(
            self.hass.bus.async_listen(EVENT_STATE_CHANGED, self._presence_changed, event_filter=_is_person)
        )
        await self._async_check_stay()
        self._shown_mode = self.mode()
        self._notify()

    @callback
    def async_stop(self) -> None:
        while self._unsubs:
            self._unsubs.pop()()
        self._started = False

    @callback
    def async_add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Called when the mode or a calendar changes (the Auri Mode sensor)."""
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener) if listener in self._listeners else None

    @callback
    def _notify(self) -> None:
        for listener in list(self._listeners):
            listener()

    # ── the account's mode ───────────────────────────────────────────────────

    @callback
    def note_account_mode(self, mode: Any) -> None:
        """The account's setting, from a backend answer (SaaSClient.on_account_mode)."""
        if mode not in ACCOUNT_MODES or mode == self.account_mode:
            return
        self.account_mode = mode
        self._save()
        if self._started and self._hosting():
            self.entry.async_create_background_task(self.hass, self.async_ensure(GUEST), "hey_auri_client guest calendar")
        self._changed()

    def mode(self) -> str:
        """owner or guest now, by the backend's rule for the account's setting (owner until it's known)."""
        mode = self.account_mode or "owner"
        if mode == "auto":
            stay = stay_context(self.hass, self.entry.options)
            return "owner" if stay.get("active") is False and stay.get("household_home") is True else "guest"
        return mode

    def role_for_turn(self, turn_mode: str | None, requested: Any = None) -> str:
        """The calendar a turn uses: guests only theirs; the household its own, or the guests' when named."""
        mode = turn_mode if turn_mode in ("owner", "guest") else self.mode()
        if mode == "guest":
            return GUEST
        return GUEST if requested == GUEST else HOME

    def allowed_roles(self, turn_mode: str | None) -> set[str]:
        """The calendars a turn may change: a guest turn only the Guest Calendar."""
        mode = turn_mode if turn_mode in ("owner", "guest") else self.mode()
        return {GUEST} if mode == "guest" else {HOME, GUEST}

    def _hosting(self) -> bool:
        """Whether the home hosts guests: the account is set to guest or Automatic, or a stay is set."""
        if self.account_mode in ("guest", "auto"):
            return True
        state, _ = stay_state(self.entry.options, dt_util.now(), dt_util.get_default_time_zone())
        return state != "none"

    # ── the two Local Calendars ──────────────────────────────────────────────

    def _local_entry(self, role: str) -> ConfigEntry | None:
        key = CALENDARS[role][1]
        return next(
            (entry for entry in self.hass.config_entries.async_entries(LOCAL_CALENDAR) if entry.data.get("storage_key") == key),
            None,
        )

    def entity_for(self, role: str) -> str | None:
        """The calendar's entity id, found by its Local Calendar entry (None when it isn't set up)."""
        entry = self._local_entry(role)
        if entry is None:
            return None
        registry = er.async_get(self.hass)
        return next(
            (item.entity_id for item in er.async_entries_for_config_entry(registry, entry.entry_id) if item.domain == "calendar"),
            None,
        )

    async def async_ensure(self, role: str) -> str | None:
        """The calendar's entity id, setting up its Local Calendar first when it's missing. Never raises."""
        async with self._locks[role]:
            if self._local_entry(role) is None:
                name = CALENDARS[role][0]
                try:
                    await self.hass.config_entries.flow.async_init(
                        LOCAL_CALENDAR, context={"source": SOURCE_USER}, data={"calendar_name": name}
                    )
                    _LOGGER.info("Set up the %s (Local Calendar)", name)
                except Exception as err:  # noqa: BLE001
                    _LOGGER.warning("Couldn't set up the %s: %s", name, err)
                    return None
                self._notify()
            return self.entity_for(role)

    # ── emptying the Guest Calendar, once per stay ───────────────────────────

    @callback
    def _changed(self) -> None:
        if self._started:
            self.entry.async_create_background_task(self.hass, self._async_refresh(), "hey_auri_client calendars")

    @callback
    def _presence_changed(self, _event: Event) -> None:
        self._show_mode_if_changed()

    @callback
    def _show_mode_if_changed(self) -> None:
        mode = self.mode()
        if mode != self._shown_mode:
            self._shown_mode = mode
            self._notify()

    async def _async_tick(self, _now: Any) -> None:
        await self._async_refresh()

    async def _async_refresh(self) -> None:
        async with self._refreshing:
            if self._hosting() and self._local_entry(GUEST) is None:
                await self.async_ensure(GUEST)
            await self._async_check_stay()
        self._show_mode_if_changed()

    async def _async_check_stay(self) -> None:
        """Empty the Guest Calendar when its stay ends, or when a new stay takes over before that.

        A home with no stay yet has nobody's events to empty: what the household puts there before
        the first booking stays.
        """
        options = self.entry.options
        key = stay_key(options)
        previous = self._marker.get("stay")
        if previous != key:
            if previous and previous != NO_STAY and not self._marker.get("cleared"):
                await self._async_clear_guest("a new stay took over")
            self._marker = {"stay": key, "cleared": False}
            self._save()
        if not self._marker.get("cleared") and stay_over(options):
            await self._async_clear_guest("the stay ended")
            self._marker["cleared"] = True
            self._save()

    async def _async_clear_guest(self, why: str) -> None:
        entity = calendar_entity(self.hass, self.entity_for(GUEST))
        if entity is None:
            return
        now = dt_util.now()
        try:
            events = await entity.async_get_events(self.hass, now - _CLEAR_BACK, now + _CLEAR_AHEAD)
            uids = list(dict.fromkeys(event.uid for event in events if event.uid))
            for uid in uids:  # each repeating series once: no occurrence given removes all of it
                await entity.async_delete_event(uid)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Couldn't empty the Guest Calendar (%s): %s", why, err)
            return
        _LOGGER.info("Emptied the Guest Calendar (%s): %s event(s)", why, len(uids))

    @callback
    def _save(self) -> None:
        self._store.async_delay_save(lambda: {"account_mode": self.account_mode, "marker": self._marker}, 1)


@callback
def _is_person(event_data: Any) -> bool:
    return str(event_data.get("entity_id") or "").startswith("person.")
