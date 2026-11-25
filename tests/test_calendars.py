"""The Home and Guest Calendars: chosen by the mode, changed and removed through the calendar entity,
and the Guest Calendar emptied once per stay. On real Local Calendars (Home Assistant's test harness).

Uses pytest-homeassistant-custom-component (Linux only: Home Assistant's test harness doesn't run on Windows).
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

pytest.importorskip("pytest_homeassistant_custom_component.common")

from datetime import timedelta  # noqa: E402

from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed  # noqa: E402

from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.helpers import entity_registry as er  # noqa: E402
from homeassistant.setup import async_setup_component  # noqa: E402

from custom_components.hey_auri_client.calendars import GUEST, HOME, AuriCalendars  # noqa: E402
from custom_components.hey_auri_client.const import (  # noqa: E402
    CONF_BOOKING_ID,
    CONF_CHECK_IN,
    CONF_CHECK_OUT,
    CONF_CLIENT_ID,
    CONF_SHARED_SECRET,
    DOMAIN,
    RUNTIME_CALENDARS,
)
from custom_components.hey_auri_client.conversation.custom_services.calendar_services import (  # noqa: E402
    CalendarToolService,
    build_rrule,
    describe_rrule,
)
from custom_components.hey_auri_client.conversation.custom_services.shopping_list_services import (  # noqa: E402
    ShoppingListToolService,
)
from custom_components.hey_auri_client.conversation.local_tool_executor import LocalToolExecutor  # noqa: E402
from custom_components.hey_auri_client.entities.mode import AuriModeSensor  # noqa: E402
from custom_components.hey_auri_client.saas_client import SaaSClient  # noqa: E402

NOW = "2026-11-18T10:00:00-07:00"  # a Wednesday, in America/Phoenix (no daylight saving time)
OWNER = SimpleNamespace(mode="owner")
GUEST_TURN = SimpleNamespace(mode="guest")
HOME_ENTITY = "calendar.home_calendar"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture
async def home(hass: HomeAssistant, freezer, tmp_path):
    # Local Calendar keeps each calendar in a real .ics file under .storage.
    hass.config.config_dir = str(tmp_path)
    (tmp_path / ".storage").mkdir()
    freezer.move_to(NOW)
    await hass.config.async_set_time_zone("America/Phoenix")
    await async_setup_component(hass, "homeassistant", {})
    # The household's list: Home Assistant's Shopping list, as its onboarding adds it.
    shopping = MockConfigEntry(domain="shopping_list")
    shopping.add_to_hass(hass)
    await hass.config_entries.async_setup(shopping.entry_id)
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_CLIENT_ID: "client", CONF_SHARED_SECRET: "secret"})
    entry.add_to_hass(hass)
    calendars = AuriCalendars(hass, entry, None)
    hass.data.setdefault(DOMAIN, {}).setdefault("runtime", {})[RUNTIME_CALENDARS] = calendars
    await calendars.async_start()
    await hass.async_block_till_done()
    tools = CalendarToolService(hass)
    shopping_tools = ShoppingListToolService(hass)

    async def call(name: str, turn=OWNER, **arguments):
        return await getattr(tools, name)({"entity_id": HOME_ENTITY, **arguments, "_turn": turn})

    async def shop(name: str, turn=OWNER, **arguments):
        # The model's entity_id, which the client ignores.
        return await getattr(shopping_tools, name)({"entity_id": "todo.shopping_list", **arguments, "_turn": turn})

    async def hosting(**options) -> None:
        hass.config_entries.async_update_entry(entry, options=options)
        calendars.note_account_mode("auto")
        await hass.async_block_till_done(wait_background_tasks=True)  # the guest calendar and list are set up in tasks
        await calendars._async_refresh()  # noqa: SLF001

    yield SimpleNamespace(entry=entry, calendars=calendars, call=call, shop=shop, hosting=hosting, freezer=freezer)
    calendars.async_stop()


async def _read(home, turn=OWNER, start="2026-11-01", end="2027-01-31", **arguments) -> list[dict]:
    answer = await home.call("get_calendar_events", turn, start_time=start, end_time=end, **arguments)
    assert answer["success"], answer
    return answer["events"]


def _event(title: str, start: str, end: str, **extra) -> dict:
    return {"event_title": title, "event_description": "", "event_start_time": start, "event_end_time": end, **extra}


# ── the two calendars ────────────────────────────────────────────────────────


async def test_the_home_calendar_is_set_up_and_the_guest_one_once_the_home_hosts(hass: HomeAssistant, home) -> None:
    assert home.calendars.entity_for(HOME) == HOME_ENTITY
    assert home.calendars.entity_for(GUEST) is None  # a household that doesn't host gets no extra calendar

    home.calendars.note_account_mode("auto")
    await hass.async_block_till_done(wait_background_tasks=True)  # the guest calendar is set up in a background task
    assert home.calendars.entity_for(GUEST) == "calendar.guest_calendar"

    # Found by its Local Calendar entry, whatever its entity is called; set up only once.
    er.async_get(hass).async_update_entity(HOME_ENTITY, new_entity_id="calendar.family")
    assert home.calendars.entity_for(HOME) == "calendar.family"
    again = AuriCalendars(hass, home.entry, None)
    await again.async_start()
    again.async_stop()
    assert len(hass.config_entries.async_entries("local_calendar")) == 2


async def test_a_stay_makes_a_home_that_hosts(hass: HomeAssistant, home) -> None:
    hass.config_entries.async_update_entry(home.entry, options={CONF_CHECK_IN: "2026-11-20T15:00:00", CONF_CHECK_OUT: "2026-11-22T11:00:00"})
    await home.calendars._async_refresh()  # noqa: SLF001
    await hass.async_block_till_done()
    assert home.calendars.entity_for(GUEST) == "calendar.guest_calendar"


async def test_the_calendar_follows_the_mode(hass: HomeAssistant, home) -> None:
    calendars = home.calendars
    assert calendars.role_for_turn("guest", "home") == GUEST  # guests only ever their own
    assert calendars.role_for_turn("owner", None) == HOME
    assert calendars.role_for_turn("owner", "guest") == GUEST  # the household, naming it
    assert calendars.mode() == "owner"  # the setting not known yet: owner, as the backend does
    calendars.note_account_mode("guest")
    assert calendars.mode() == "guest"

    calendars.note_account_mode("auto")
    hass.states.async_set("person.sam", "home", {"friendly_name": "Sam"})
    assert calendars.mode() == "owner"  # no stay, someone from the household home
    hass.states.async_set("person.sam", "not_home", {"friendly_name": "Sam"})
    assert calendars.mode() == "guest"  # nobody home: a cleaner between stays isn't the household
    assert calendars.role_for_turn(None) == GUEST  # an older backend's turn: the mode now
    hass.states.async_set("person.sam", "home", {"friendly_name": "Sam"})
    hass.config_entries.async_update_entry(home.entry, options={CONF_CHECK_IN: "2026-11-17T15:00:00", CONF_CHECK_OUT: "2026-11-20T11:00:00"})
    assert calendars.mode() == "guest"  # a stay under way


async def test_the_mode_sensor_shows_the_mode_and_both_calendars(hass: HomeAssistant, home) -> None:
    changes: list[int] = []
    home.calendars.async_add_listener(lambda: changes.append(1))
    home.calendars.note_account_mode("guest")
    # The Guest Shopping List is set up in a background task: wait for it too, or a slow runner sees no list yet.
    await hass.async_block_till_done(wait_background_tasks=True)
    assert changes

    sensor = AuriModeSensor(home.entry, home.calendars)
    assert sensor.native_value == "guest"
    assert sensor.extra_state_attributes == {
        "account_mode": "guest", "home_calendar": HOME_ENTITY, "guest_calendar": "calendar.guest_calendar",
        "home_shopping_list": "todo.shopping_list", "guest_shopping_list": "todo.guest_shopping_list",
    }


async def test_turns_and_the_startup_check_bring_the_account_mode(hass: HomeAssistant, aioclient_mock) -> None:
    seen: list[str] = []
    client = SaaSClient(hass, endpoint="https://api.example", timeout=5, client_id="c", shared_secret="s")
    client.on_account_mode = seen.append
    aioclient_mock.post("https://api.example/conversation", json={"type": "warm", "account_mode": "guest"})
    await client.async_check_account()
    aioclient_mock.clear_requests()
    aioclient_mock.post("https://api.example/conversation", json={
        "type": "final", "conversation_id": "c1", "content": "OK", "mode": "owner", "account_mode": "auto"})
    await client.send_user_turn({"type": "user_turn", "conversation_id": "c1", "message": {"content": "hi"}, "context": {}})
    assert seen == ["guest", "auto"]


async def test_the_hub_asks_for_the_account_mode_at_start_and_every_five_minutes(hass: HomeAssistant, home) -> None:
    # Nothing tells the hub when the setting changes on hey-auri.com: without asking, a home set to
    # Guest by hand kept showing the Home Calendar until someone spoke to Auri.
    answers = iter(["owner", "owner", "guest"])
    asked: list[str] = []

    async def check_account() -> None:
        mode = next(answers)
        asked.append(mode)
        calendars.note_account_mode(mode)

    home.calendars.async_stop()
    calendars = AuriCalendars(hass, home.entry, None, check_account=check_account)
    await calendars.async_start()
    await hass.async_block_till_done(wait_background_tasks=True)  # the first account check runs in a background task
    sensor = AuriModeSensor(home.entry, calendars)
    assert asked == ["owner"] and sensor.native_value == "owner"

    async def later(minutes: int) -> None:
        home.freezer.tick(timedelta(minutes=minutes))
        async_fire_time_changed(hass)
        await hass.async_block_till_done(wait_background_tasks=True)  # a guest answer sets up the guest calendar in a task

    await later(4)
    assert asked == ["owner"]  # not yet
    await later(1)
    assert asked == ["owner", "owner"]
    await later(5)
    assert asked == ["owner", "owner", "guest"]
    assert sensor.native_value == "guest"
    assert sensor.extra_state_attributes["guest_calendar"] == "calendar.guest_calendar"  # set up for the guests
    calendars.async_stop()


def test_the_account_mode_comes_with_the_backends_answers(hass: HomeAssistant) -> None:
    seen: list[str] = []
    client = SaaSClient(hass, endpoint="https://api.example", timeout=5, client_id="c", shared_secret="s")
    client.on_account_mode = seen.append
    client._note_account_mode({"type": "final", "account_mode": "auto"})  # noqa: SLF001
    client._note_account_mode({"type": "warm"})  # noqa: SLF001  (an older backend)
    client._note_account_mode({"account_mode": "vacation"})  # noqa: SLF001
    assert seen == ["auto"]


# ── events ───────────────────────────────────────────────────────────────────


async def test_reading_adding_moving_and_removing_an_event(home) -> None:
    added = await home.call("add_calendar_event", **_event("Dentist", "2026-11-20T15:00:00", "2026-11-20T16:00:00",
                                                        event_location="Main St"))
    assert added.get("success") and added["calendar"] == "home", added
    (event,) = await _read(home)
    assert (event["title"], event["start"], event["location"]) == ("Dentist", "2026-11-20T15:00:00-07:00", "Main St")
    assert "repeats" not in event

    moved = await home.call("change_calendar_event", event_id=event["event_id"], event_start_time="2026-11-20T16:30:00")
    assert moved["success"]
    assert (moved["changed"]["start"], moved["changed"]["end"]) == ("2026-11-20T16:30:00-07:00", "2026-11-20T17:30:00-07:00")
    # An id changed under it is forgotten: read again.
    assert "retry" in await home.call("remove_calendar_event", event_id=event["event_id"])

    (event,) = await _read(home)
    removed = await home.call("remove_calendar_event", event_id=event["event_id"])
    assert removed["success"] and removed["removed"]["title"] == "Dentist"
    assert await _read(home) == []


async def test_a_weekly_event_one_occurrence_or_from_one_on(home) -> None:
    await home.call("add_calendar_event", **_event("Piano", "2026-11-19T16:00:00", "2026-11-19T17:00:00",
                                                   repeat="weekly", repeat_days=["thu"]))
    events = await _read(home, end="2026-12-04")
    assert [e["start"][:10] for e in events] == ["2026-11-19", "2026-11-26", "2026-12-03"]
    assert events[0]["repeats"] == "every week on Thursday"
    assert [e["day"] for e in events] == ["Thursday"] * 3  # named, so the reply says the right day

    # Which ones: asked for, not guessed.
    asked = await home.call("change_calendar_event", event_id=events[1]["event_id"], event_start_time="2026-11-26T17:00:00")
    assert "which" in asked["retry"]
    assert (await home.call("change_calendar_event", event_id=events[1]["event_id"], which="this",
                            event_start_time="2026-11-26T17:00:00"))["success"]
    events = await _read(home, end="2026-12-04")
    assert [e["start"][11:16] for e in events] == ["16:00", "17:00", "16:00"]

    # No more lessons from the third one on.
    assert (await home.call("remove_calendar_event", event_id=events[2]["event_id"], which="following"))["success"]
    assert [e["start"][:10] for e in await _read(home)] == ["2026-11-19", "2026-11-26"]


async def test_all_of_a_repeating_event_moves_together(home) -> None:
    await home.call("add_calendar_event", **_event("Yoga", "2026-11-23T09:00:00", "2026-11-23T10:00:00", repeat="weekly"))
    events = await _read(home, end="2026-12-08")
    assert [e["start"][:10] for e in events] == ["2026-11-23", "2026-11-30", "2026-12-07"]

    # "Move yoga to 10 every week", said about the second one: the first moves too.
    changed = await home.call("change_calendar_event", event_id=events[1]["event_id"], which="all",
                              event_start_time="2026-11-30T10:00:00", event_title="Morning yoga")
    assert changed["success"]
    events = await _read(home, end="2026-12-08")
    assert [(e["start"][5:16], e["end"][11:16], e["title"]) for e in events] == [
        ("11-23T10:00", "11:00", "Morning yoga"), ("11-30T10:00", "11:00", "Morning yoga"), ("12-07T10:00", "11:00", "Morning yoga"),
    ]
    # And all of it removed.
    assert (await home.call("remove_calendar_event", event_id=events[0]["event_id"], which="all"))["success"]
    assert await _read(home) == []


async def test_an_all_day_event(home) -> None:
    added = await home.call("add_calendar_event", **_event("Emma's birthday", "2026-12-14", "2026-12-14"))
    assert added["success"] and added["added"]["all_day"]
    (event,) = await _read(home, start="2026-12-14", end="2026-12-14")
    assert (event["all_day"], event["start"], event["end"]) == (True, "2026-12-14", "2026-12-14")
    assert "retry" in await home.call("add_calendar_event", **_event("Trip", "2026-12-20", "2026-12-21T10:00:00"))


def test_repeats_in_rules_and_in_words() -> None:
    assert build_rrule("weekly", ["thu", "tue"], "2027-06-30", False) == "FREQ=WEEKLY;BYDAY=TH,TU;UNTIL=20270630T235959"
    assert build_rrule("yearly", None, "2030-01-01", True) == "FREQ=YEARLY;UNTIL=20300101"
    assert build_rrule(None, ["mon"], None, False) is None
    with pytest.raises(ValueError):
        build_rrule("daily", ["mon"], None, False)
    with pytest.raises(ValueError):
        build_rrule("hourly", None, None, False)
    assert describe_rrule("FREQ=WEEKLY;BYDAY=TU,TH;UNTIL=20270630T235959") == "every week on Tuesday, Thursday until 2027-06-30"
    assert describe_rrule("FREQ=DAILY;INTERVAL=2;COUNT=5") == "every 2 days, 5 times"


# ── guests ───────────────────────────────────────────────────────────────────


async def test_guests_have_only_their_own_calendar(home) -> None:
    await home.hosting()
    await home.call("add_calendar_event", OWNER, **_event("Family dinner", "2026-11-20T18:00:00", "2026-11-20T20:00:00"))

    guest_view = await home.call("get_calendar_events", GUEST_TURN, start_time="2026-11-01", end_time="2027-01-31", calendar="home")
    assert guest_view["calendar"] == "guest" and guest_view["events"] == []
    added = await home.call("add_calendar_event", GUEST_TURN, calendar="home", **_event("Wine tour", "2026-11-19T13:00:00", "2026-11-19T17:00:00"))
    assert added["calendar"] == "guest"

    household = await _read(home)
    assert [e["title"] for e in household] == ["Family dinner"]
    assert [e["title"] for e in await _read(home, calendar="guest")] == ["Wine tour"]
    # A guest can't touch the household's event, even with its id.
    refused = await home.call("remove_calendar_event", GUEST_TURN, event_id=household[0]["event_id"])
    assert refused["success"] is False
    # The household can plan for the guests.
    cleaner = await home.call("add_calendar_event", OWNER, calendar="guest", **_event("Cleaner", "2026-11-21T11:00:00", "2026-11-21T12:00:00"))
    assert cleaner["calendar"] == "guest"


async def test_the_tool_executor_gives_calendar_tools_the_turn(hass: HomeAssistant, home) -> None:
    await home.hosting()
    hass.states.async_set("person.sam", "home", {"friendly_name": "Sam"})
    assert home.calendars.mode() == "owner"  # so only the turn itself can make it the guests' calendar
    executor = LocalToolExecutor(hass)
    call = {"id": "c1", "function": {"name": "get_calendar_events", "arguments": json.dumps(
        {"entity_id": HOME_ENTITY, "start_time": "2026-11-18T00:00:00", "end_time": "2026-11-19T00:00:00"})}}
    answer = await executor.execute_tool_call(call, conversation_id="conv", turn=SimpleNamespace(mode="guest", user_is_admin=None))
    payload = answer.get("result") if isinstance(answer.get("result"), dict) else answer
    assert "guest" in json.dumps(payload)


async def test_the_guest_calendar_is_emptied_once_per_stay(home) -> None:
    await home.hosting(**{CONF_BOOKING_ID: "B1", CONF_CHECK_IN: "2026-11-17T15:00:00", CONF_CHECK_OUT: "2026-11-19T11:00:00"})
    await home.call("add_calendar_event", GUEST_TURN, **_event("Wine tour", "2026-11-18T13:00:00", "2026-11-18T17:00:00"))
    await home.call("add_calendar_event", GUEST_TURN, **_event("Yoga", "2026-11-19T08:00:00", "2026-11-19T09:00:00", repeat="daily"))

    async def titles() -> set[str]:
        return {e["title"] for e in await _read(home, calendar="guest")}

    assert await titles() == {"Wine tour", "Yoga"}
    # Check-out at 11:00: still there at 11:30, gone with the stay's guest memory at 12:00.
    home.freezer.move_to("2026-11-19T11:30:00-07:00")
    await home.calendars._async_refresh()  # noqa: SLF001
    assert await titles() == {"Wine tour", "Yoga"}
    home.freezer.move_to("2026-11-19T12:01:00-07:00")
    await home.calendars._async_refresh()  # noqa: SLF001
    assert await titles() == set()

    # The household plans for the next guests: kept, since this stay was emptied already.
    await home.call("add_calendar_event", OWNER, calendar="guest", **_event("Pool cleaning", "2026-11-21T09:00:00", "2026-11-21T10:00:00"))
    await home.hosting(**{CONF_BOOKING_ID: "B2", CONF_CHECK_IN: "2026-11-21T15:00:00", CONF_CHECK_OUT: "2026-11-23T11:00:00"})
    assert await titles() == {"Pool cleaning"}

    # A stay replaced before it ended: its calendar is emptied at the takeover.
    await home.hosting(**{CONF_BOOKING_ID: "B3", CONF_CHECK_IN: "2026-11-24T15:00:00", CONF_CHECK_OUT: "2026-11-26T11:00:00"})
    assert await titles() == set()


async def test_what_the_household_plans_before_the_first_booking_stays(home) -> None:
    """A home with no stay has nobody's events to empty when the first booking arrives."""
    await home.hosting()
    await home.call("add_calendar_event", OWNER, calendar="guest", **_event("Welcome basket", "2026-11-21T15:00:00", "2026-11-21T15:30:00"))
    await home.hosting(**{CONF_BOOKING_ID: "B1", CONF_CHECK_IN: "2026-11-21T15:00:00", CONF_CHECK_OUT: "2026-11-23T11:00:00"})
    assert [e["title"] for e in await _read(home, calendar="guest")] == ["Welcome basket"]


async def test_every_thursday_must_start_on_a_thursday(home) -> None:
    """The model once added "every Thursday" starting on Friday the 20th: the client sends it back to fix."""
    wrong = await home.call("add_calendar_event", **_event("Piano", "2026-11-20T16:00:00", "2026-11-20T17:00:00",
                                                           repeat="weekly", repeat_days=["thu"]))
    assert "Friday" in wrong["retry"] and "Thursday" in wrong["retry"]
    assert await _read(home) == []


# ── the shopping lists ───────────────────────────────────────────────────────


async def _items(home, turn=OWNER, **arguments) -> list[str]:
    answer = await home.shop("get_shopping_list", turn, status=["needs_action", "completed"], **arguments)
    assert answer["success"], answer
    return sorted(item["summary"] for list_ in answer.values() if isinstance(list_, dict) for item in list_.get("items", []))


async def test_the_guest_shopping_list_is_set_up_once_the_home_hosts(hass: HomeAssistant, home) -> None:
    assert home.calendars.list_for(HOME) == "todo.shopping_list"
    assert home.calendars.list_for(GUEST) is None  # a household that doesn't host gets no extra list
    await home.hosting()
    assert home.calendars.list_for(GUEST) == "todo.guest_shopping_list"
    # Found by its Local To-do entry, whatever its entity is called; set up only once.
    er.async_get(hass).async_update_entity("todo.guest_shopping_list", new_entity_id="todo.visitors")
    assert home.calendars.list_for(GUEST) == "todo.visitors"
    await home.calendars._async_refresh()  # noqa: SLF001
    assert len(hass.config_entries.async_entries("local_todo")) == 1


async def test_guests_have_only_their_own_shopping_list(home) -> None:
    await home.hosting()
    added = await home.shop("add_shopping_list_item", OWNER, item="Milk")
    assert added == {"success": True, "list": "home"}
    # A guest gets their own list, even asking for the household's and naming its entity.
    assert (await home.shop("add_shopping_list_item", GUEST_TURN, item="Sunscreen", list="home"))["list"] == "guest"
    assert await _items(home, GUEST_TURN, list="home") == ["Sunscreen"]
    assert await _items(home) == ["Milk"]
    # The household can add to the guests' list by naming it.
    assert (await home.shop("add_shopping_list_item", OWNER, item="Towels", list="guest"))["list"] == "guest"
    assert await _items(home, OWNER, list="guest") == ["Sunscreen", "Towels"]
    # A guest can't tick off the household's items.
    milk_uid = (await home.shop("get_shopping_list", OWNER, status=["needs_action"]))["todo.shopping_list"]["items"][0]["uid"]
    refused = await home.shop("mark_shopping_list_item_complete", GUEST_TURN, uid=milk_uid)
    assert "retry" in refused
    assert await _items(home) == ["Milk"]


async def test_the_tool_executor_gives_shopping_list_tools_the_turn(hass: HomeAssistant, home) -> None:
    await home.hosting()
    hass.states.async_set("person.sam", "home", {"friendly_name": "Sam"})
    assert home.calendars.mode() == "owner"  # so only the turn itself can make it the guests' list
    executor = LocalToolExecutor(hass)
    call = {"id": "c1", "function": {"name": "add_shopping_list_item", "arguments": json.dumps(
        {"entity_id": "todo.shopping_list", "item": "Ice"})}}
    answer = await executor.execute_tool_call(call, conversation_id="conv", turn=SimpleNamespace(mode="guest", user_is_admin=None))
    assert "guest" in json.dumps(answer)
    assert await _items(home) == []


async def test_the_guest_shopping_list_is_emptied_with_the_guest_calendar(home) -> None:
    await home.hosting()
    # Before the first booking: what the household puts there stays.
    await home.shop("add_shopping_list_item", OWNER, item="Welcome wine", list="guest")
    await home.hosting(**{CONF_BOOKING_ID: "B1", CONF_CHECK_IN: "2026-11-17T15:00:00", CONF_CHECK_OUT: "2026-11-19T11:00:00"})
    assert await _items(home, GUEST_TURN) == ["Welcome wine"]
    await home.shop("add_shopping_list_item", GUEST_TURN, item="Sunscreen")
    await home.shop("add_shopping_list_item", OWNER, item="Milk")

    home.freezer.move_to("2026-11-19T11:30:00-07:00")
    await home.calendars._async_refresh()  # noqa: SLF001
    assert await _items(home, GUEST_TURN) == ["Sunscreen", "Welcome wine"]
    home.freezer.move_to("2026-11-19T12:01:00-07:00")
    await home.calendars._async_refresh()  # noqa: SLF001
    assert await _items(home, GUEST_TURN) == []
    assert await _items(home) == ["Milk"]  # the household's list is never touched

    # A stay replaced before it ended: its list is emptied at the takeover.
    await home.hosting(**{CONF_BOOKING_ID: "B2", CONF_CHECK_IN: "2026-11-21T15:00:00", CONF_CHECK_OUT: "2026-11-23T11:00:00"})
    await home.shop("add_shopping_list_item", GUEST_TURN, item="Coffee")
    await home.hosting(**{CONF_BOOKING_ID: "B3", CONF_CHECK_IN: "2026-11-24T15:00:00", CONF_CHECK_OUT: "2026-11-26T11:00:00"})
    assert await _items(home, GUEST_TURN) == []
