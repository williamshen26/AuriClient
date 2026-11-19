"""Alarms ring on voice satellites at a time of day, kept by the client itself (alarms.py).

Uses pytest-homeassistant-custom-component (Linux only). A satellite here is a device with an Assist
satellite entity and, while it's connected, a timer handler, as ESPHome registers them (it removes
the satellite entity, and so the handler, while the satellite is away).
"""
from __future__ import annotations

from datetime import date, datetime, time as dtime, timedelta, timezone
import json
from typing import Any
from zoneinfo import ZoneInfo

import pytest

pytest.importorskip("pytest_homeassistant_custom_component.common")

from pytest_homeassistant_custom_component.common import (  # noqa: E402
    MockConfigEntry,
    MockEntityPlatform,
    async_capture_events,
    async_fire_time_changed,
    async_mock_service,
)

from homeassistant.components import conversation, intent  # noqa: E402
from homeassistant.components.homeassistant.const import DATA_EXPOSED_ENTITIES  # noqa: E402
from homeassistant.components.homeassistant.exposed_entities import async_should_expose  # noqa: E402
from homeassistant.components.intent import TimerEventType, TimerManager  # noqa: E402
from homeassistant.components.intent.const import TIMER_DATA  # noqa: E402
from homeassistant.core import Context, HomeAssistant  # noqa: E402
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er  # noqa: E402
from homeassistant.setup import async_setup_component  # noqa: E402

from custom_components.hey_auri_client.alarms import (  # noqa: E402
    AlarmRecord,
    AuriAlarms,
    first_date,
    next_ring,
    parse_clock_time,
    parse_days,
)
from custom_components.hey_auri_client.const import (  # noqa: E402
    CONF_BOOKING_ID,
    CONF_CHECK_OUT,
    CONF_CLIENT_ID,
    CONF_SHARED_SECRET,
    DATA_ALARMS,
    DOMAIN,
    EVENT_ALARM_RINGING,
    RUNTIME_ALARMS,
)
from custom_components.hey_auri_client.conversation.custom_services.alarm_services import AlarmToolService  # noqa: E402
from custom_components.hey_auri_client.conversation.custom_services.timer_services import RUNTIME_TIMERS  # noqa: E402
from custom_components.hey_auri_client.conversation.helpers import build_context_snapshot  # noqa: E402
from custom_components.hey_auri_client.conversation.local_tool_executor import LocalToolExecutor  # noqa: E402
from custom_components.hey_auri_client.memory.identity import TurnMeta  # noqa: E402
from custom_components.hey_auri_client.timers import AuriTimers  # noqa: E402
from custom_components.hey_auri_client.updates import AutoUpdater  # noqa: E402

# Clocks still change here (British Columbia's no longer do: America/Vancouver stays on UTC-7).
LA = "America/Los_Angeles"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


# -- the clock arithmetic, on its own -------------------------------------------------------------


def _alarm(**fields: Any) -> AlarmRecord:
    return AlarmRecord(id="a1", time=fields.pop("time", dtime(7, 0)), **fields)


def _utc(text: str) -> datetime:
    return datetime.fromisoformat(text).astimezone(timezone.utc)


def test_times_of_day() -> None:
    assert parse_clock_time("07:00") == dtime(7, 0)
    assert parse_clock_time("7") == dtime(7, 0)
    assert parse_clock_time("0730") == dtime(7, 30)
    assert parse_clock_time("19:05") == dtime(19, 5)
    assert parse_clock_time("7:30 PM") == dtime(19, 30)
    assert parse_clock_time("12 a.m.") == dtime(0, 0)
    assert parse_clock_time("12pm") == dtime(12, 0)
    for wrong in ("25:00", "7:75", "seven", "13 pm", ""):
        with pytest.raises(ValueError):
            parse_clock_time(wrong)


def test_repeat_days() -> None:
    assert parse_days(["mon", "wed", "fri"]) == (0, 2, 4)
    assert parse_days("weekdays") == (0, 1, 2, 3, 4)
    assert parse_days(["weekends"]) == (5, 6)
    assert parse_days("every day") == (0, 1, 2, 3, 4, 5, 6)
    assert parse_days("Monday, tues and Thursday") == (0, 1, 3)
    assert parse_days("once") == ()
    assert parse_days(None) == ()
    with pytest.raises(ValueError):
        parse_days(["funday"])


def test_a_one_time_alarm_rings_today_if_its_time_is_still_to_come_else_tomorrow() -> None:
    tz = ZoneInfo(LA)
    now = _utc("2026-11-18T22:00:00-08:00")  # Wednesday evening
    assert first_date(dtime(7, 0), now, tz) == date(2026, 11, 19)
    assert first_date(dtime(23, 0), now, tz) == date(2026, 11, 18)
    assert next_ring(_alarm(date=date(2026, 11, 19)), now, tz) == _utc("2026-11-19T07:00:00-08:00")
    assert next_ring(_alarm(date=date(2026, 11, 18)), now, tz) is None  # that was this morning
    assert next_ring(_alarm(date=date(2026, 11, 19), enabled=False), now, tz) is None


def test_a_repeating_alarm_rings_on_its_next_day_not_skipped() -> None:
    tz = ZoneInfo(LA)
    friday_morning = _utc("2026-11-20T08:00:00-08:00")
    weekdays = _alarm(days=(0, 1, 2, 3, 4))
    assert next_ring(weekdays, friday_morning, tz) == _utc("2026-11-23T07:00:00-08:00")
    weekdays.skip_date = date(2026, 11, 23)
    assert next_ring(weekdays, friday_morning, tz) == _utc("2026-11-24T07:00:00-08:00")
    # A weekly alarm whose next day is skipped rings the week after.
    mondays = _alarm(days=(0,), skip_date=date(2026, 11, 23))
    assert next_ring(mondays, friday_morning, tz) == _utc("2026-11-30T07:00:00-08:00")
    # A snooze comes first.
    weekdays.snooze_until = friday_morning + timedelta(minutes=9)
    assert next_ring(weekdays, friday_morning, tz) == friday_morning + timedelta(minutes=9)


def test_alarms_on_the_nights_the_clocks_change() -> None:
    tz = ZoneInfo(LA)
    # 02:30 doesn't exist on 14 March 2027: it rings at 03:30 (10:30 UTC).
    daily = _alarm(time=dtime(2, 30), days=(0, 1, 2, 3, 4, 5, 6))
    assert next_ring(daily, _utc("2027-03-13T12:00:00-08:00"), tz) == datetime(2027, 3, 14, 10, 30, tzinfo=timezone.utc)
    # 01:30 happens twice on 7 November 2027: it rings the first time only.
    daily.time = dtime(1, 30)
    first = next_ring(daily, _utc("2027-11-06T12:00:00-07:00"), tz)
    assert first == datetime(2027, 11, 7, 8, 30, tzinfo=timezone.utc)
    assert next_ring(daily, first + timedelta(minutes=1), tz) == datetime(2027, 11, 8, 9, 30, tzinfo=timezone.utc)


# -- in Home Assistant ----------------------------------------------------------------------------


class Satellite:
    """A voice satellite: a device with an Assist satellite entity, and a timer handler while connected."""

    def __init__(self, hass: HomeAssistant, name: str, *, area: str | None = None, aliases: tuple[str, ...] = ()) -> None:
        self.hass = hass
        self.entry = MockConfigEntry(domain="esphome")
        self.entry.add_to_hass(hass)
        devices = dr.async_get(hass)
        self.id = devices.async_get_or_create(
            config_entry_id=self.entry.entry_id, identifiers={("esphome", name)}, name=name
        ).id
        if area:
            areas = ar.async_get(hass)
            room = areas.async_get_area_by_name(area) or areas.async_create(area, aliases=set(aliases))
            devices.async_update_device(self.id, area_id=room.id)
        er.async_get(hass).async_get_or_create(
            "assist_satellite", "esphome", f"{name}-satellite", device_id=self.id, config_entry=self.entry
        )
        self.events: list[tuple[TimerEventType, int]] = []
        self._unregister = None
        self.connect()

    def connect(self) -> None:
        def handler(event_type: TimerEventType, info: intent.TimerInfo) -> None:
            self.events.append((event_type, info.created_seconds))

        self._unregister = intent.async_register_timer_handler(self.hass, self.id, handler)

    def disconnect(self) -> None:
        if self._unregister is not None:
            self._unregister()
            self._unregister = None

    def rings(self) -> int:
        """How many times it was rung: each ring is a 1-second timer started on it."""
        return sum(1 for kind, seconds in self.events if kind == TimerEventType.STARTED and seconds == 1)

    def add_ring_switch(self) -> str:
        """The firmware's Timer Ringing switch, ringing."""
        entry = er.async_get(self.hass).async_get_or_create(
            "switch", "esphome", f"{self.id}-switch-timer_ringing", device_id=self.id,
            config_entry=self.entry, original_name="Timer Ringing",
        )
        self.hass.states.async_set(entry.entity_id, "on")
        return entry.entity_id


class Home:
    """Auri's timers and alarms with their entities, as the sensor and switch platforms set them up."""

    def __init__(self, hass: HomeAssistant, entry: MockConfigEntry) -> None:
        self.hass, self.entry = hass, entry
        self.satellites: list[Satellite] = []
        self.platforms: list[MockEntityPlatform] = []
        self.tools = AlarmToolService(hass)

    def satellite(self, name: str, **room: Any) -> Satellite:
        satellite = Satellite(self.hass, name, **room)
        self.satellites.append(satellite)
        return satellite

    async def start(self) -> AuriAlarms:
        hass = self.hass
        self.platforms = [
            MockEntityPlatform(hass, domain="sensor", platform_name=DOMAIN),
            MockEntityPlatform(hass, domain="switch", platform_name=DOMAIN),
        ]

        def adder(platform: MockEntityPlatform):
            def add(entities: list, update_before_add: bool = False) -> None:
                hass.async_create_task(platform.async_add_entities(entities))

            return add

        self.timers = AuriTimers(hass, self.entry)
        self.alarms = AuriAlarms(hass, self.entry, self.timers)
        runtime = hass.data.setdefault(DOMAIN, {}).setdefault("runtime", {})
        runtime[RUNTIME_TIMERS], runtime[RUNTIME_ALARMS] = self.timers, self.alarms
        hass.data[DOMAIN].setdefault(self.entry.entry_id, {})[DATA_ALARMS] = self.alarms
        await self.timers.async_start(adder(self.platforms[0]))
        await self.alarms.async_start(adder(self.platforms[1]))
        await hass.async_block_till_done()
        return self.alarms

    async def stop(self) -> None:
        await self.alarms.async_stop()
        await self.timers.async_stop()
        for platform in self.platforms:
            await platform.async_reset()

    async def restart(self, freezer, *, down_for: timedelta) -> None:
        """Home Assistant off for a while: its own timers are gone, and the satellites come back later."""
        await self.stop()
        self.hass.data[TIMER_DATA] = TimerManager(self.hass)
        for satellite in self.satellites:
            satellite._unregister = None
        freezer.tick(down_for)
        await self.start()

    async def advance(self, freezer, delta: timedelta) -> None:
        freezer.tick(delta)
        async_fire_time_changed(self.hass)
        await self.hass.async_block_till_done()

    async def advance_to(self, freezer, local: str) -> None:
        await self.advance(freezer, datetime.fromisoformat(local) - datetime.now(timezone.utc))

    async def call(self, tool: str, turn: TurnMeta | None = None, **arguments: Any) -> dict[str, Any]:
        result = await getattr(self.tools, tool)({**arguments, "_turn": turn})
        await self.hass.async_block_till_done()
        return result

    def timer_sensors(self) -> list[str]:
        return [state.entity_id for state in self.hass.states.async_all("sensor") if state.entity_id.startswith("sensor.auri_timer_")]


def turn(device_id: str | None = None, *, mode: str = "owner", user_id: str | None = None) -> TurnMeta:
    return TurnMeta(
        mode=mode, memory_allowed=False, conversation_id="c1", device_id=device_id, user_id=user_id, language="en"
    )


@pytest.fixture
async def home(hass: HomeAssistant, freezer):
    await hass.config.async_set_time_zone(LA)
    freezer.move_to("2026-11-18T22:00:00-08:00")  # Wednesday evening
    await async_setup_component(hass, "homeassistant", {})
    await async_setup_component(hass, "intent", {})
    # The switch actions, for the dashboard's on/off toggle.
    await async_setup_component(hass, "switch", {})
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_CLIENT_ID: "client", CONF_SHARED_SECRET: "secret"})
    entry.add_to_hass(hass)
    home = Home(hass, entry)
    await home.start()
    yield home
    await home.stop()


async def test_an_alarm_rings_on_the_satellite_it_was_set_at(hass: HomeAssistant, home: Home, freezer) -> None:
    bedroom = home.satellite("Bedroom satellite", area="Bedroom")
    rang = async_capture_events(hass, EVENT_ALARM_RINGING)
    # As on a hub where new devices are exposed to Assist (switches are, by default).
    hass.data[DATA_EXPOSED_ENTITIES].async_set_expose_new_entities(conversation.DOMAIN, True)

    result = await home.call("set_alarm", turn(bedroom.id), time="07:00")

    alarm = result["alarm"]
    assert result["success"] is True
    assert alarm["next_ring"] == "2026-11-19T07:00:00-08:00"
    assert alarm["next_ring_day"] == "tomorrow (Thursday)"
    assert (alarm["repeats"], alarm["rings_in"], alarm["on_this_device"]) == ("once", ["Bedroom"], True)
    switch = f"switch.auri_alarm_{alarm['alarm_id']}"
    state = hass.states.get(switch)
    assert state.state == "on"
    assert state.attributes["summary"] == "07:00 · Once · Bedroom"
    # Not for voice assistants: "turn off all the switches" mustn't silence alarms.
    assert not async_should_expose(hass, conversation.DOMAIN, switch)

    await home.advance_to(freezer, "2026-11-19T06:59:59-08:00")
    assert bedroom.rings() == 0
    await home.advance(freezer, timedelta(seconds=1))
    assert bedroom.rings() == 1
    assert rang[0].data["alarm_id"] == alarm["alarm_id"]
    # The satellite's own ring: the 1-second timer ends there, and isn't one of Auri's timers.
    await home.advance(freezer, timedelta(seconds=3))
    assert (TimerEventType.FINISHED, 1) in bedroom.events
    assert home.timer_sensors() == []

    # A one-time alarm goes once its snooze window is over.
    await home.advance(freezer, timedelta(minutes=31))
    assert hass.states.get(switch) is None
    assert er.async_get(hass).async_get(switch) is None
    assert (await home.call("list_alarms", turn(bedroom.id)))["alarms"] == []


async def test_set_where_no_satellite_asked_auri_asks_which_room(home: Home) -> None:
    home.satellite("Bedroom satellite", area="Bedroom")
    home.satellite("Kitchen satellite", area="Kitchen")

    result = await home.call("set_alarm", turn(None), time="07:00")
    assert result["needs_room"] is True
    assert result["rooms"] == ["Bedroom", "Kitchen"]
    assert home.alarms.alarms() == []

    result = await home.call("set_alarm", turn(None), time="07:00", room="the bedroom")
    assert result["alarm"]["rings_in"] == ["Bedroom"]


async def test_with_only_one_satellite_it_rings_there(home: Home) -> None:
    home.satellite("Living room satellite", area="Living room")
    result = await home.call("set_alarm", turn(None), time="07:00")
    assert result["alarm"]["rings_in"] == ["Living room"]


async def test_no_satellite_no_alarm(home: Home) -> None:
    result = await home.call("set_alarm", turn(None), time="07:00")
    assert result["success"] is False
    assert "voice satellite" in result["error"]


async def test_an_alarm_for_another_room_rings_every_satellite_there(home: Home, freezer) -> None:
    bedroom = home.satellite("Bedroom satellite", area="Bedroom")
    mia = home.satellite("Kids satellite", area="Kids room", aliases=("Mia's room",))
    mia_too = home.satellite("Kids satellite 2", area="Kids room")

    result = await home.call("set_alarm", turn(bedroom.id), time="07:00", room="Mia's room")
    assert result["alarm"]["rings_in"] == ["Kids room"]
    assert result["alarm"]["on_this_device"] is False

    await home.advance_to(freezer, "2026-11-19T07:00:00-08:00")
    assert (bedroom.rings(), mia.rings(), mia_too.rings()) == (0, 1, 1)


async def test_a_room_that_could_be_two_rooms_is_asked_about(home: Home) -> None:
    master = home.satellite("Master satellite", area="Master bedroom")
    home.satellite("Guest satellite", area="Guest bedroom")
    result = await home.call("set_alarm", turn(master.id), time="07:00", room="bedroom")
    assert result["needs_room"] is True
    assert result["rooms"] == ["Guest bedroom", "Master bedroom"]
    result = await home.call("set_alarm", turn(master.id), time="07:00", room="guest bedroom")
    assert result["alarm"]["rings_in"] == ["Guest bedroom"]


async def test_a_weekday_alarm_skips_a_day_and_turns_off_and_on(hass: HomeAssistant, home: Home, freezer) -> None:
    bedroom = home.satellite("Bedroom satellite", area="Bedroom")
    alarm = (await home.call("set_alarm", turn(bedroom.id), time="06:30", days=["weekdays"]))["alarm"]
    assert (alarm["repeats"], alarm["next_ring"]) == ("weekdays", "2026-11-19T06:30:00-08:00")

    skipped = (await home.call("change_alarm", turn(bedroom.id), skip_next=True))["alarm"]
    assert skipped["skipping"] == "2026-11-19"
    assert skipped["next_ring"] == "2026-11-20T06:30:00-08:00"
    assert hass.states.get(f"switch.auri_alarm_{alarm['alarm_id']}").attributes["summary"] == "06:30 · Weekdays · Bedroom · skips Thu"

    await home.advance_to(freezer, "2026-11-19T07:00:00-08:00")
    assert bedroom.rings() == 0
    await home.advance_to(freezer, "2026-11-20T06:30:00-08:00")
    assert bedroom.rings() == 1
    # Not at the weekend.
    await home.advance_to(freezer, "2026-11-22T12:00:00-08:00")
    assert bedroom.rings() == 1

    # Off from the dashboard: Monday is quiet. On again: Tuesday rings.
    switch = f"switch.auri_alarm_{alarm['alarm_id']}"
    await hass.services.async_call("switch", "turn_off", {"entity_id": switch}, blocking=True)
    assert hass.states.get(switch).state == "off"
    await home.advance_to(freezer, "2026-11-23T07:00:00-08:00")
    assert bedroom.rings() == 1
    await hass.services.async_call("switch", "turn_on", {"entity_id": switch}, blocking=True)
    await home.advance_to(freezer, "2026-11-24T06:30:00-08:00")
    assert bedroom.rings() == 2


async def test_snooze_rings_again_and_stop_ends_it(hass: HomeAssistant, home: Home, freezer) -> None:
    bedroom = home.satellite("Bedroom satellite", area="Bedroom")
    assert (await home.call("snooze_alarm", turn(bedroom.id)))["success"] is False  # nothing went off yet
    await home.call("set_alarm", turn(bedroom.id), time="07:00")
    await home.advance_to(freezer, "2026-11-19T07:00:00-08:00")
    assert bedroom.rings() == 1

    # Snoozed from another room while it still rings: the ring stops now.
    ringing = bedroom.add_ring_switch()
    turned_off = async_mock_service(hass, "switch", "turn_off")
    snoozed = await home.call("snooze_alarm", turn(None))
    assert (snoozed["minutes"], snoozed["alarm"]["snoozed_until"]) == (9, "07:09")
    assert turned_off[0].data["entity_id"] == ringing
    await home.advance(freezer, timedelta(minutes=9))
    assert bedroom.rings() == 2

    # "Snooze for 5 minutes", at the satellite.
    snoozed = await home.call("snooze_alarm", turn(bedroom.id), minutes=5)
    assert snoozed["alarm"]["snoozed_until"] == "07:14"
    # "I'm up": no more snoozes, and a one-time alarm is done.
    stopped = await home.call("stop_alarm", turn(bedroom.id))
    assert stopped["success"] is True
    await home.advance(freezer, timedelta(minutes=10))
    assert bedroom.rings() == 2
    assert home.alarms.alarms() == []


async def test_stopping_a_repeating_alarm_keeps_it_for_its_next_day(home: Home, freezer) -> None:
    bedroom = home.satellite("Bedroom satellite", area="Bedroom")
    await home.call("set_alarm", turn(bedroom.id), time="07:00", days="daily")
    await home.advance_to(freezer, "2026-11-19T07:00:00-08:00")
    await home.call("snooze_alarm", turn(bedroom.id))
    stopped = await home.call("stop_alarm", turn(bedroom.id))
    assert stopped["next_ring"] == "2026-11-20T07:00:00-08:00"
    await home.advance(freezer, timedelta(minutes=10))
    assert bedroom.rings() == 1
    await home.advance_to(freezer, "2026-11-20T07:00:00-08:00")
    assert bedroom.rings() == 2


async def test_a_satellite_that_is_away_rings_once_it_is_back(home: Home, freezer) -> None:
    bedroom = home.satellite("Bedroom satellite", area="Bedroom")
    await home.call("set_alarm", turn(bedroom.id), time="07:00", days="daily")

    bedroom.disconnect()
    await home.advance_to(freezer, "2026-11-19T07:00:00-08:00")
    assert bedroom.rings() == 0
    await home.advance(freezer, timedelta(minutes=5))
    bedroom.connect()
    await home.advance(freezer, timedelta(seconds=10))
    assert bedroom.rings() == 1

    # Away for longer than 15 minutes: that morning's ring is skipped.
    bedroom.disconnect()
    await home.advance_to(freezer, "2026-11-20T07:00:00-08:00")
    await home.advance(freezer, timedelta(minutes=20))
    bedroom.connect()
    await home.advance(freezer, timedelta(seconds=10))
    assert bedroom.rings() == 1
    assert home.alarms.alarms()[0].pending == {}


async def test_alarms_survive_restarts_and_ring_once_home_assistant_is_back(home: Home, freezer) -> None:
    bedroom = home.satellite("Bedroom satellite", area="Bedroom")
    await home.call("set_alarm", turn(bedroom.id), time="07:00", days="daily")

    # Restarting from 06:55 to 07:05 (the nightly update, say): it rings once the satellite is back.
    await home.advance_to(freezer, "2026-11-19T06:55:00-08:00")
    await home.restart(freezer, down_for=timedelta(minutes=10))
    assert bedroom.rings() == 0
    bedroom.connect()
    await home.advance(freezer, timedelta(seconds=10))
    assert bedroom.rings() == 1
    assert home.alarms.describe(home.alarms.alarms()[0])["next_ring"] == "2026-11-20T07:00:00-08:00"

    # Off from 06:55 to 07:30: too late, it's skipped; the next day rings as usual.
    await home.advance_to(freezer, "2026-11-20T06:55:00-08:00")
    await home.restart(freezer, down_for=timedelta(minutes=35))
    bedroom.connect()
    await home.advance(freezer, timedelta(seconds=10))
    assert bedroom.rings() == 1
    await home.advance_to(freezer, "2026-11-21T07:00:00-08:00")
    assert bedroom.rings() == 2


async def test_a_new_time_zone_moves_every_alarm(hass: HomeAssistant, home: Home) -> None:
    bedroom = home.satellite("Bedroom satellite", area="Bedroom")
    alarm = (await home.call("set_alarm", turn(bedroom.id), time="07:00", days="daily"))["alarm"]
    assert alarm["next_ring"] == "2026-11-19T07:00:00-08:00"
    await hass.config.async_update(time_zone="America/New_York")
    await hass.async_block_till_done()
    listed = (await home.call("list_alarms", turn(bedroom.id)))["alarms"]
    assert listed[0]["next_ring"] == "2026-11-19T07:00:00-05:00"


async def test_guests_see_only_their_own_alarms_and_they_go_at_check_out(hass: HomeAssistant, home: Home, freezer) -> None:
    hass.config_entries.async_update_entry(
        home.entry, options={CONF_BOOKING_ID: "B1", CONF_CHECK_OUT: "2026-11-20T11:00:00"}
    )
    bedroom = home.satellite("Bedroom satellite", area="Bedroom")
    owners = (await home.call("set_alarm", turn(bedroom.id), time="06:00", days="weekdays"))["alarm"]
    guests = (await home.call("set_alarm", turn(bedroom.id, mode="guest"), time="08:00", days="daily"))["alarm"]

    as_guest = (await home.call("list_alarms", turn(bedroom.id, mode="guest")))["alarms"]
    assert [a["alarm_id"] for a in as_guest] == [guests["alarm_id"]]
    as_owner = (await home.call("list_alarms", turn(bedroom.id)))["alarms"]
    assert {a["alarm_id"] for a in as_owner} == {owners["alarm_id"], guests["alarm_id"]}
    refused = await home.call("cancel_alarm", turn(bedroom.id, mode="guest"), alarm_id=owners["alarm_id"])
    assert refused["success"] is False

    await home.advance_to(freezer, "2026-11-20T11:00:10-08:00")
    assert [a.id for a in home.alarms.alarms()] == [owners["alarm_id"]]


async def test_change_and_cancel_by_time_or_name(home: Home) -> None:
    bedroom = home.satellite("Bedroom satellite", area="Bedroom")
    work = (await home.call("set_alarm", turn(bedroom.id), time="07:00", days="weekdays", name="work"))["alarm"]
    await home.call("set_alarm", turn(bedroom.id), time="09:00", days="weekends")

    # Two alarms on this satellite and none named: which one?
    which = await home.call("change_alarm", turn(bedroom.id), time="06:45")
    assert which["success"] is False and len(which["alarms"]) == 2
    assert (await home.call("change_alarm", turn(bedroom.id)))["retry"]

    moved = await home.call("change_alarm", turn(bedroom.id), current_time="07:00", time="06:45")
    assert (moved["alarm"]["alarm_id"], moved["alarm"]["time"]) == (work["alarm_id"], "06:45")
    once = await home.call("change_alarm", turn(bedroom.id), name="work", days="once")
    assert (once["alarm"]["repeats"], once["alarm"]["date"]) == ("once", "2026-11-19")
    assert (await home.call("change_alarm", turn(bedroom.id), name="work", skip_next=True))["success"] is False
    off = await home.call("change_alarm", turn(bedroom.id), name="work", enabled=False)
    assert (off["alarm"]["enabled"], off["alarm"]["next_ring"]) == (False, None)

    # "My 9 o'clock alarm" is the one at 09:00; "7 o'clock" finds 19:00 when there's no 07:00.
    await home.call("set_alarm", turn(bedroom.id), time="19:00")
    assert (await home.call("cancel_alarm", turn(bedroom.id), time="7:00"))["cancelled"][0]["time"] == "19:00"
    cancelled = await home.call("cancel_alarm", turn(bedroom.id), all=True)
    assert len(cancelled["cancelled"]) == 2
    assert home.alarms.alarms() == []


async def test_the_same_alarm_asked_twice_is_set_once(home: Home) -> None:
    bedroom = home.satellite("Bedroom satellite", area="Bedroom")
    first = await home.call("set_alarm", turn(bedroom.id), time="07:00")
    again = await home.call("set_alarm", turn(bedroom.id), time="7:00 AM")
    assert again["already_set"] is True
    assert again["alarm"]["alarm_id"] == first["alarm"]["alarm_id"]
    assert len(home.alarms.alarms()) == 1


async def test_the_turn_says_an_alarm_just_rang_here(hass: HomeAssistant, home: Home, freezer) -> None:
    bedroom = home.satellite("Bedroom satellite", area="Bedroom")
    await home.call("set_alarm", turn(bedroom.id), time="07:00", name="Gym")
    await home.advance_to(freezer, "2026-11-19T07:00:00-08:00")
    await home.advance(freezer, timedelta(minutes=2))

    snapshot = await build_context_snapshot(
        hass,
        conversation.ConversationInput(
            text="snooze", context=Context(), conversation_id="c1", device_id=bedroom.id,
            satellite_id=None, language="en", agent_id="hey_auri_client",
        ),
    )
    assert snapshot["alarm_rang"] == {
        "alarm_id": home.alarms.alarms()[0].id, "name": "Gym", "time": "07:00", "minutes_ago": 2, "snoozed_until": None,
    }
    await home.advance(freezer, timedelta(minutes=30))
    assert home.alarms.rang_here(bedroom.id) is None


async def test_the_executor_hands_alarm_tools_the_turn(home: Home) -> None:
    bedroom = home.satellite("Bedroom satellite", area="Bedroom")
    executor = LocalToolExecutor(home.hass)
    answer = await executor.execute_tool_call(
        {"id": "1", "function": {"name": "set_alarm", "arguments": json.dumps({"time": "07:00", "_turn": "forged"})}},
        conversation_id="c1",
        turn=turn(bedroom.id, mode="guest"),
    )
    assert answer["result"]["success"] is True
    assert home.alarms.alarms()[0].guest is True


async def test_the_nightly_update_waits_when_an_alarm_is_about_to_ring(hass: HomeAssistant, home: Home) -> None:
    class Backend:
        async def post_json(self, path: str, payload: dict, **_: Any) -> dict:
            return {"version": "v0.2.11"}

    hass.states.async_set(
        "update.hey_auri_client", "on",
        {"installed_version": "v0.2.10", "release_url": "https://github.com/williamshen26/AuriClient/releases/tag/v0.2.10"},
    )
    bedroom = home.satellite("Bedroom satellite", area="Bedroom")
    await home.call("set_alarm", turn(bedroom.id), time="22:10")
    updater = AutoUpdater(hass, home.entry, Backend())
    assert (await updater.async_check())["result"] == "busy"
