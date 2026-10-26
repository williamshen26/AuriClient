"""The booking calendar wired into Home Assistant: the sync, its timers, the action, the form and the sensor.

Uses pytest-homeassistant-custom-component (Linux only: Home Assistant's
test harness doesn't run on Windows). The calendar is served by aioclient_mock.
"""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("pytest_homeassistant_custom_component.common")
pytest.importorskip("ical")

from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed  # noqa: E402

from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.data_entry_flow import FlowResultType  # noqa: E402
from homeassistant.exceptions import HomeAssistantError  # noqa: E402

from custom_components.hey_auri_client.bookings.sync import BookingSync  # noqa: E402
from custom_components.hey_auri_client.const import (  # noqa: E402
    CONF_BOOKING_CALENDAR_SOURCE,
    CONF_BOOKING_CALENDAR_URL,
    CONF_BOOKING_ID,
    CONF_CHECK_IN,
    CONF_CHECK_OUT,
    CONF_CLIENT_ID,
    CONF_GUEST_NAME,
    CONF_NOTES,
    CONF_SHARED_SECRET,
    CONF_USUAL_CHECK_IN_TIME,
    DATA_BOOKINGS,
    DOMAIN,
)
from custom_components.hey_auri_client.conversation.services import async_setup_services  # noqa: E402
from custom_components.hey_auri_client.entities.guest_stay import AuriGuestStaySensor  # noqa: E402

URL = "https://www.airbnb.com/calendar/ical/41234.ics?s=0123456789abcdef"
ENTRY_DATA = {CONF_CLIENT_ID: "client", CONF_SHARED_SECRET: "secret"}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


def reservation(start: str, end: str, code: str) -> list[str]:
    return [
        f"DTEND;VALUE=DATE:{end}",
        f"DTSTART;VALUE=DATE:{start}",
        f"UID:{code.lower()}@airbnb.com",
        f"DESCRIPTION:Reservation URL: https://www.airbnb.com/hosting/reservations/details/{code}"
        "\\nPhone Number (Last 4 Digits): 1234",
        "SUMMARY:Reserved",
    ]


def calendar(*events: list[str]) -> str:
    lines = ["BEGIN:VCALENDAR", "PRODID:-//Airbnb Inc//Hosting Calendar 0.8.8//EN", "VERSION:2.0"]
    for event in events:
        lines += ["BEGIN:VEVENT", *event, "END:VEVENT"]
    return "\r\n".join([*lines, "END:VCALENDAR"]) + "\r\n"


A = reservation("20261020", "20261025", "HMAAAAAAAA")
B = reservation("20261025", "20261028", "HMBBBBBBBB")  # the next guests, the day the first leave


def serve(aioclient_mock, *events, status: int = 200) -> None:
    aioclient_mock.clear_requests()
    if status == 200:
        aioclient_mock.get(URL, text=calendar(*events), headers={"ETag": '"v1"'})
    else:
        aioclient_mock.get(URL, status=status)


@pytest.fixture
async def booking_sync(hass: HomeAssistant, freezer):
    await hass.config.async_set_time_zone("America/Los_Angeles")
    freezer.move_to("2026-10-19 09:00:00-07:00")
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=ENTRY_DATA,
        options={CONF_BOOKING_CALENDAR_URL: URL, CONF_GUEST_NAME: "Old guest", CONF_NOTES: "Late check-out"},
    )
    entry.add_to_hass(hass)
    sync = BookingSync(hass, entry)
    yield entry, sync
    await sync.async_stop()


async def test_the_calendar_fills_in_guest_settings(hass: HomeAssistant, booking_sync, aioclient_mock) -> None:
    entry, sync = booking_sync
    serve(aioclient_mock, B, A)

    result = await sync.async_sync()

    assert result["action"] == "switch" and result["changed"] and result["next_booking"] == "HMBBBBBBBB"
    assert entry.options[CONF_BOOKING_ID] == "HMAAAAAAAA"
    assert (entry.options[CONF_CHECK_IN], entry.options[CONF_CHECK_OUT]) == ("2026-10-20 15:00:00", "2026-10-25 11:00:00")
    # The last guests' details are gone; the link stays.
    assert CONF_GUEST_NAME not in entry.options and CONF_NOTES not in entry.options
    assert entry.options[CONF_BOOKING_CALENDAR_URL] == URL

    # Unchanged since the last read (the request carries its ETag): nothing is written.
    serve(aioclient_mock, status=304)
    with patch.object(hass.config_entries, "async_update_entry", wraps=hass.config_entries.async_update_entry) as update:
        result = await sync.async_sync()
    assert result["action"] == "update" and not result["changed"] and not update.called
    assert aioclient_mock.mock_calls[-1][3]["If-None-Match"] == '"v1"'


async def test_the_next_booking_takes_over_an_hour_after_check_out(
    hass: HomeAssistant, booking_sync, aioclient_mock, freezer
) -> None:
    entry, sync = booking_sync
    serve(aioclient_mock, A, B)
    await sync.async_sync()

    # The first guests may still be there half an hour after check-out.
    freezer.move_to("2026-10-25 11:30:00-07:00")
    await sync.async_sync()
    assert entry.options[CONF_BOOKING_ID] == "HMAAAAAAAA"

    # The check scheduled just after guest memory's erase moves to the next booking.
    freezer.move_to("2026-10-25 12:00:31-07:00")
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert entry.options[CONF_BOOKING_ID] == "HMBBBBBBBB"
    assert entry.options[CONF_CHECK_IN] == "2026-10-25 15:00:00"


async def test_an_unreadable_calendar_keeps_the_stay_and_tells_the_host(
    hass: HomeAssistant, booking_sync, aioclient_mock, freezer
) -> None:
    entry, sync = booking_sync
    serve(aioclient_mock, A)
    await sync.async_sync()

    serve(aioclient_mock, status=404)  # the host reset the link on Airbnb
    with patch("custom_components.hey_auri_client.bookings.sync.persistent_notification") as notification:
        result = await sync.async_sync()
        assert "HTTP 404" in result["error"] and URL not in result["error"]
        assert entry.options[CONF_BOOKING_ID] == "HMAAAAAAAA"
        notification.async_create.assert_not_called()

        freezer.tick(timedelta(hours=6))
        await sync.async_sync()
        notification.async_create.assert_called_once()
        assert URL not in notification.async_create.call_args.args[1]

        serve(aioclient_mock, A)
        result = await sync.async_sync()
        notification.async_dismiss.assert_called_once()
        assert "error" not in result


async def test_a_new_link_starts_afresh(hass: HomeAssistant, booking_sync, aioclient_mock, freezer) -> None:
    entry, sync = booking_sync
    serve(aioclient_mock, A, B)
    await sync.async_sync()
    freezer.move_to("2026-10-21 09:00:00-07:00")  # HMAAAAAAAA is under way

    async def switch_link(link: str, *events) -> dict:
        aioclient_mock.clear_requests()
        aioclient_mock.get(link, text=calendar(*events))
        hass.config_entries.async_update_entry(entry, options={**entry.options, CONF_BOOKING_CALENDAR_URL: link})
        await sync.async_options_updated(entry.options)  # what the integration's update listener does
        return await sync.async_sync()

    # Another listing's link: its calendar replaces the stay at once.
    result = await switch_link("https://example.com/other.ics", reservation("20261030", "20261102", "HMCCCCCCCC"))
    assert result["action"] == "switch" and entry.options[CONF_BOOKING_ID] == "HMCCCCCCCC"

    # A link whose calendar has nothing coming clears the last link's stay.
    result = await switch_link("https://example.com/empty.ics")
    assert result["action"] == "clear"
    assert not {CONF_BOOKING_ID, CONF_CHECK_IN, CONF_CHECK_OUT} & set(entry.options)


async def _open_options_step(hass: HomeAssistant, entry: MockConfigEntry, step: str) -> dict:
    with patch("homeassistant.config_entries.async_process_deps_reqs", AsyncMock()):
        result = await hass.config_entries.options.async_init(entry.entry_id)
    return await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": step})


def _fields(result: dict) -> set[str]:
    return {str(key) for key in result["data_schema"].schema}


async def test_with_a_link_the_guest_form_shows_the_calendars_booking(
    hass: HomeAssistant, booking_sync, aioclient_mock
) -> None:
    entry, sync = booking_sync
    serve(aioclient_mock, A, B)
    await sync.async_sync()

    result = await _open_options_step(hass, entry, "guest")
    assert not {CONF_BOOKING_ID, CONF_CHECK_IN, CONF_CHECK_OUT, CONF_BOOKING_CALENDAR_URL} & _fields(result)
    assert CONF_GUEST_NAME in _fields(result)
    assert "booking HMAAAAAAAA, check-in Tue 2026-10-20 15:00" in result["description_placeholders"]["calendar_booking"]

    # Clearing the link on its own page (an emptied field isn't sent) hands the stay back to the host...
    result = await _open_options_step(hass, entry, "booking_calendar")
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_BOOKING_CALENDAR_URL] == "" and CONF_BOOKING_CALENDAR_SOURCE not in entry.options
    assert entry.options[CONF_BOOKING_ID] == "HMAAAAAAAA"

    # ...and the Guest form shows the booking fields again.
    result = await _open_options_step(hass, entry, "guest")
    assert {CONF_BOOKING_ID, CONF_CHECK_IN, CONF_CHECK_OUT} <= _fields(result)
    assert "Booking calendar" in result["description_placeholders"]["calendar_booking"]


async def test_checking_the_calendar_on_demand(hass: HomeAssistant, booking_sync, aioclient_mock) -> None:
    entry, sync = booking_sync
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {DATA_BOOKINGS: sync}
    await async_setup_services(hass, {})
    serve(aioclient_mock, A, B)

    result = await hass.services.async_call(DOMAIN, "sync_bookings", {}, blocking=True, return_response=True)
    assert result["action"] == "switch" and result["bookings"] == 2

    hass.config_entries.async_update_entry(entry, options={**entry.options, CONF_BOOKING_CALENDAR_URL: ""})
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(DOMAIN, "sync_bookings", {}, blocking=True, return_response=True)


async def test_the_guest_stay_sensor(hass: HomeAssistant, booking_sync, aioclient_mock, freezer) -> None:
    entry, sync = booking_sync
    sensor = AuriGuestStaySensor(entry, sync)
    sensor.hass = hass
    assert sensor.native_value == "none"

    serve(aioclient_mock, A, B)
    await sync.async_sync()
    assert sensor.native_value == "upcoming"
    attributes = sensor.extra_state_attributes
    assert attributes["booking_id"] == "HMAAAAAAAA" and attributes["next_booking"] == "HMBBBBBBBB"
    assert attributes["calendar"] == "on" and attributes["check_out"] == "2026-10-25T11:00:00-07:00"

    freezer.move_to("2026-10-21 09:00:00-07:00")
    assert sensor.native_value == "staying"
    freezer.move_to("2026-10-25 11:15:00-07:00")
    assert sensor.native_value == "checked_out"


async def test_a_new_calendar_link_is_checked_before_it_is_saved(hass: HomeAssistant, aioclient_mock) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, options={})
    entry.add_to_hass(hass)
    aioclient_mock.get("https://example.com/missing.ics", status=404)
    aioclient_mock.get("https://example.com/page.ics", text="<html>Sign in to continue</html>")
    aioclient_mock.get(URL, text=calendar(A))

    result = await _open_options_step(hass, entry, "booking_calendar")
    assert _fields(result) == {CONF_BOOKING_CALENDAR_URL, CONF_USUAL_CHECK_IN_TIME, "usual_check_out_time"}
    flow_id = result["flow_id"]
    for link, error in (
        ("www.airbnb.com/calendar/ical/41234.ics", "calendar_invalid_link"),
        ("https://example.com/missing.ics", "calendar_unreachable"),
        ("https://example.com/page.ics", "calendar_not_ical"),
    ):
        result = await hass.config_entries.options.async_configure(flow_id, {CONF_BOOKING_CALENDAR_URL: link})
        assert result["errors"] == {CONF_BOOKING_CALENDAR_URL: error}

    result = await hass.config_entries.options.async_configure(
        flow_id, {CONF_BOOKING_CALENDAR_URL: f"  {URL}  ", CONF_USUAL_CHECK_IN_TIME: "16:00:00"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_BOOKING_CALENDAR_URL] == URL
    assert entry.options[CONF_USUAL_CHECK_IN_TIME] == "16:00:00"
