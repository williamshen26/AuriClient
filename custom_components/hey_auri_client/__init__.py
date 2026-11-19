"""Thin frontend Home Assistant integration prototype."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components import persistent_notification
import homeassistant.components.conversation as ha_conversation
from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv, issue_registry as ir
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType
from homeassistant.loader import async_get_integration

from .alarms import AuriAlarms
from .calendars import AuriCalendars
from .bookings.sync import BookingSync
from .conversation.agent_class import ThinOpenAIAgent
from .cache import (
    dump_media_player_sources,
    set_media_player_macs,
    set_media_player_sources,
    upsert_media_player_sources,
)
from .const import (
    DATA_AGENT,
    DATA_ALARMS,
    DATA_CALENDARS,
    DATA_BOOKINGS,
    DATA_MEMORY,
    DATA_SKILL_GUESS,
    DATA_SECURITY_REPORT,
    DATA_SUPPORT,
    DATA_TIMERS,
    DATA_UPDATER,
    DOMAIN,
    LEGACY_USER_DATA_DIR,
    MEDIA_PLAYER_MACS_STORAGE_KEY,
    MEDIA_PLAYER_MACS_STORAGE_VERSION,
    ROOT_RUNTIME,
    RUNTIME_ALARMS,
    RUNTIME_CALENDARS,
)
from .conversation.custom_services.timer_services import RUNTIME_TIMERS
from .conversation.services import async_setup_services
from .frontend import async_register_frontend_resources
from .memory.legacy import relocate_legacy_user_data
from .memory.logins import async_delete_logins_issue, async_update_logins_issue
from .memory.names import async_delete_names_issue, async_update_names_issue
from .memory.runtime import MemoryRuntime
from .security import (
    SECURITY_CHECK_INTERVAL,
    SecurityReporter,
    async_delete_security_issues,
    async_security_summary,
    async_update_security_issues,
)
from .support import SupportSessions
from .timer_automation import (
    EVENT_AUTOMATION_RELOADED,
    OLD_TIMER_AUTOMATION_ISSUE,
    async_update_old_timer_automation_issue,
)
from .timers import AuriTimers
from .updates import AutoUpdater

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
PLATFORMS = ["stt", "sensor", "tts", "button", "switch"]
MEDIA_PLAYER_SOURCES_STORAGE_VERSION = 1
MEDIA_PLAYER_SOURCES_STORAGE_KEY = f"{DOMAIN}_media_player_sources"


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the thin frontend integration."""
    await async_setup_services(hass, config)

    runtime = hass.data.setdefault(DOMAIN, {}).setdefault("runtime", dict(ROOT_RUNTIME))

    if runtime.get("frontend_resources_registered") is None:
        await async_register_frontend_resources(hass)
        runtime["frontend_resources_registered"] = True

    store: Store[dict[str, list[str]]] = runtime.setdefault(
        "media_player_sources_store",
        Store(
            hass,
            MEDIA_PLAYER_SOURCES_STORAGE_VERSION,
            MEDIA_PLAYER_SOURCES_STORAGE_KEY,
        ),
    )

    if runtime.get("media_player_sources_loaded") is None:
        persisted_sources = await store.async_load()
        if isinstance(persisted_sources, dict):
            sanitized: dict[str, list[str]] = {}
            for entity_id, source_list in persisted_sources.items():
                if not isinstance(entity_id, str) or not isinstance(source_list, list):
                    continue
                sanitized[entity_id] = [str(source) for source in source_list]
            set_media_player_sources(sanitized)
        runtime["media_player_sources_loaded"] = True

    macs_store: Store[dict[str, list[str]]] = runtime.setdefault(
        "media_player_macs_store",
        Store(
            hass,
            MEDIA_PLAYER_MACS_STORAGE_VERSION,
            MEDIA_PLAYER_MACS_STORAGE_KEY,
        ),
    )

    if runtime.get("media_player_macs_loaded") is None:
        persisted_macs = await macs_store.async_load()
        if isinstance(persisted_macs, dict):
            sanitized_macs: dict[str, list[str]] = {}
            for entity_id, mac_list in persisted_macs.items():
                if not isinstance(entity_id, str) or not isinstance(mac_list, list):
                    continue
                sanitized_macs[entity_id] = [str(mac) for mac in mac_list]
            set_media_player_macs(sanitized_macs)
        runtime["media_player_macs_loaded"] = True

    @callback
    def _persist_media_player_sources() -> dict[str, list[str]]:
        return dump_media_player_sources()

    if runtime.get("media_player_sources_listener") is None:
        @callback
        def _cache_media_player_sources(event: Any) -> None:
            new_state = event.data.get("new_state")
            if new_state is None:
                return
            if not new_state.entity_id.startswith("media_player."):
                return
            if new_state.state != "on":
                return

            source_list = new_state.attributes.get("source_list")
            if isinstance(source_list, list):
                changed = upsert_media_player_sources(
                    new_state.entity_id,
                    [str(source) for source in source_list],
                )
                if changed:
                    store.async_delay_save(_persist_media_player_sources, 5)

        runtime["media_player_sources_listener"] = hass.bus.async_listen(
            EVENT_STATE_CHANGED,
            _cache_media_player_sources,
        )

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the thin frontend integration from a config entry."""
    await _async_relocate_legacy_user_data(hass)

    agent = ThinOpenAIAgent(hass, entry)
    integration = await async_get_integration(hass, DOMAIN)
    memory = MemoryRuntime(hass, entry, agent.client, app_version=str(integration.version or "unknown"))
    agent.attach_memory(memory)

    bookings = BookingSync(hass, entry)
    # The account Auri support signs in with (Auri support in the options): its sign-ins are
    # announced, and each ends after 12 hours.
    support = SupportSessions(hass, entry)
    reporter = SecurityReporter(agent.client)
    # Hubs Auri set up install the Hey Auri Client version a manager approved, at night.
    updater = AutoUpdater(hass, entry, agent.client)
    # Timers ring on the device that asked, show on the dashboard and survive restarts.
    timers = AuriTimers(hass, entry)
    # Alarms ring on satellites at a time of day (Home Assistant has no alarm clock); kept here.
    alarms = AuriAlarms(hass, entry, timers)
    # The Home and Guest Calendars, chosen by the account's mode, which every backend answer carries.
    calendars = AuriCalendars(hass, entry, bookings, check_account=agent.client.async_check_account)
    agent.client.on_account_mode = calendars.note_account_mode
    # The household's alarms don't ring while Auri is in guest mode, and their switches say so.
    alarms.mode = calendars.mode
    entry.async_on_unload(calendars.async_add_listener(alarms.mode_changed))
    client_version = str(integration.version or "unknown")

    data = hass.data.setdefault(DOMAIN, {}).setdefault(entry.entry_id, {})
    data[DATA_AGENT] = agent
    data[DATA_MEMORY] = memory
    data[DATA_SKILL_GUESS] = agent.skill_guesses
    data[DATA_BOOKINGS] = bookings
    data[DATA_SUPPORT] = support
    data[DATA_SECURITY_REPORT] = reporter
    data[DATA_UPDATER] = updater
    data[DATA_TIMERS] = timers
    data[DATA_ALARMS] = alarms
    data[DATA_CALENDARS] = calendars
    runtime = hass.data[DOMAIN].setdefault("runtime", dict(ROOT_RUNTIME))
    runtime[RUNTIME_TIMERS] = timers
    runtime[RUNTIME_ALARMS] = alarms
    runtime[RUNTIME_CALENDARS] = calendars

    # Connects in the background; conversations work (without memory) meanwhile.
    await memory.async_start()
    await bookings.async_start()
    await support.async_start()
    entry.async_on_unload(support.async_stop)
    await updater.async_start()
    entry.async_on_unload(updater.async_stop)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    async def _async_start_calendars(hass: HomeAssistant) -> None:
        # Once Home Assistant runs: setting up a Local Calendar loads that integration. It also asks
        # for the account's mode setting, then and every few minutes, for the calendar the dashboard shows.
        await calendars.async_start()

    entry.async_on_unload(async_at_started(hass, _async_start_calendars))
    entry.async_on_unload(calendars.async_stop)

    async def _async_check_logins(hass: HomeAssistant) -> None:
        await async_update_logins_issue(hass, entry)

    # Once the persons are loaded: a Repairs notice for logins that look like a service's.
    entry.async_on_unload(async_at_started(hass, _async_check_logins))

    async def _async_check_security(*_: Any) -> None:
        await async_update_security_issues(hass)
        await _async_report_security(hass, entry, client_version)

    # With remote access: notices for accounts without two-factor and for unlimited failed
    # logins. Hourly too, since turning two-factor on fires no event. The same checks go to
    # Hey Auri as counts (the dashboard's Hub security) when they change.
    entry.async_on_unload(async_at_started(hass, _async_check_security))
    entry.async_on_unload(async_track_time_interval(hass, _async_check_security, SECURITY_CHECK_INTERVAL))

    async def _async_check_timer_automation(*_: Any) -> None:
        await async_update_old_timer_automation_issue(hass)

    # Timers ring by themselves now: a notice offers to delete the automation that rang them,
    # and goes once it's deleted (here or in the automation editor).
    entry.async_on_unload(async_at_started(hass, _async_check_timer_automation))
    entry.async_on_unload(hass.bus.async_listen(EVENT_AUTOMATION_RELOADED, _async_check_timer_automation))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    ha_conversation.async_set_agent(hass, entry, agent)
    return True


async def _async_report_security(hass: HomeAssistant, entry: ConfigEntry, client_version: str) -> None:
    """Send the hub's security summary to Hey Auri if it changed. Never raises."""
    data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    reporter: SecurityReporter | None = data.get(DATA_SECURITY_REPORT)
    if reporter is None:
        return
    try:
        summary = await async_security_summary(hass, data.get(DATA_SUPPORT), client_version, data.get(DATA_UPDATER))
    except Exception:  # noqa: BLE001 (a report must never break the integration)
        _LOGGER.exception("Couldn't put together the hub security summary")
        return
    await reporter.async_report(summary)


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Apply memory and booking changes. Options also change at runtime (set_language, the booking calendar)."""
    data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    memory: MemoryRuntime | None = data.get(DATA_MEMORY)
    if memory is not None:
        await memory.async_options_updated(dict(entry.options))
    bookings: BookingSync | None = data.get(DATA_BOOKINGS)
    if bookings is not None:
        await bookings.async_options_updated(dict(entry.options))
    # Answering Who uses each login, or turning memory off, removes the notice.
    await async_update_logins_issue(hass, entry)
    # And memory off removes the one about names spelled two ways.
    await async_update_names_issue(hass, entry, memory)
    # Another account chosen under Auri support: from now on, its sign-ins are announced.
    support: SupportSessions | None = data.get(DATA_SUPPORT)
    if support is not None:
        await support.async_check()
    # The night job's hour may have moved, and the update check goes the hour before it.
    updater: AutoUpdater | None = data.get(DATA_UPDATER)
    if updater is not None:
        updater.async_schedule()


async def _async_relocate_legacy_user_data(hass: HomeAssistant) -> None:
    """Move files the old preference tool wrote to www/, which Home Assistant serves without login."""
    try:
        moved = await hass.async_add_executor_job(
            relocate_legacy_user_data, hass.config.config_dir, LEGACY_USER_DATA_DIR
        )
    except OSError as err:
        _LOGGER.warning("Couldn't move old Auri preference files out of www/user_data: %s", err)
        return
    if moved:
        persistent_notification.async_create(
            hass,
            f"Moved {len(moved)} file(s) saved by Auri's old preference tool out of `www/user_data`, "
            f"which Home Assistant serves without a login, to `.storage/{LEGACY_USER_DATA_DIR}`. "
            "Set up Memory in the Hey Auri Client options and they'll be imported into Auri's memory.",
            title="Hey Auri",
            notification_id=f"{DOMAIN}_legacy_user_data",
        )


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload the thin frontend integration."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False

    data = hass.data[DOMAIN].pop(entry.entry_id, None) or {}
    timers: AuriTimers | None = data.get(DATA_TIMERS)
    if timers is not None:
        await timers.async_stop()
        runtime = hass.data[DOMAIN].get("runtime") or {}
        if runtime.get(RUNTIME_TIMERS) is timers:
            runtime.pop(RUNTIME_TIMERS)
    alarms: AuriAlarms | None = data.get(DATA_ALARMS)
    if alarms is not None:
        await alarms.async_stop()
        runtime = hass.data[DOMAIN].get("runtime") or {}
        if runtime.get(RUNTIME_ALARMS) is alarms:
            runtime.pop(RUNTIME_ALARMS)
    calendars: AuriCalendars | None = data.get(DATA_CALENDARS)
    runtime = hass.data[DOMAIN].get("runtime") or {}
    if calendars is not None and runtime.get(RUNTIME_CALENDARS) is calendars:
        runtime.pop(RUNTIME_CALENDARS)
    ha_conversation.async_unset_agent(hass, entry)
    bookings: BookingSync | None = data.get(DATA_BOOKINGS)
    if bookings is not None:
        await bookings.async_stop()
    memory: MemoryRuntime | None = data.get(DATA_MEMORY)
    if memory is not None:
        await memory.async_shutdown()
    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove the integration's Repairs notices with it."""
    async_delete_logins_issue(hass, entry.entry_id)
    async_delete_names_issue(hass, entry.entry_id)
    async_delete_security_issues(hass)
    ir.async_delete_issue(hass, DOMAIN, OLD_TIMER_AUTOMATION_ISSUE)
