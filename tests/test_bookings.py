"""Reading booking calendars and choosing the stay for Guest Settings (no Home Assistant)."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
import zoneinfo

import pytest

from custom_components.hey_auri_client.bookings.calendar import (
    Booking,
    CalendarError,
    bookings_only_feed,
    parse_bookings,
    valid_calendar_url,
)
from custom_components.hey_auri_client.bookings.schedule import (
    CurrentStay,
    apply,
    choose,
    current_stay,
    following,
    stay_active,
    stay_state,
)
from custom_components.hey_auri_client.const import (
    CONF_BOOKING_CALENDAR_SOURCE,
    CONF_BOOKING_ID,
    CONF_CHECK_IN,
    CONF_CHECK_OUT,
    CONF_GUEST_COUNT,
    CONF_GUEST_NAME,
    CONF_GUEST_PHONE_NUMBER,
    CONF_NOTES,
    CONF_WIFI_INSTRUCTION,
)

GRACE = timedelta(hours=1)
CHECK_IN = time(15)
CHECK_OUT = time(11)


def zone(key: str = "America/Los_Angeles") -> zoneinfo.ZoneInfo:
    """A time zone; skips the test where no tz database is installed (Windows without tzdata)."""
    try:
        return zoneinfo.ZoneInfo(key)
    except zoneinfo.ZoneInfoNotFoundError:
        pytest.skip("no time zone database here (pip install tzdata)")


# ── calendar files ──────────────────────────────────────────────────────────


def calendar(*events: list[str]) -> str:
    lines = [
        "BEGIN:VCALENDAR",
        "PRODID;X-RICAL-TZSOURCE=TZINFO:-//Airbnb Inc//Hosting Calendar 0.8.8//EN",
        "CALSCALE:GREGORIAN",
        "VERSION:2.0",
    ]
    for event in events:
        lines += ["BEGIN:VEVENT", *event, "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def airbnb_reservation(start: str, end: str, code: str) -> list[str]:
    """As Airbnb exports it: no DTSTAMP, no name, the code in the reservation link."""
    return [
        f"DTEND;VALUE=DATE:{end}",
        f"DTSTART;VALUE=DATE:{start}",
        f"UID:{code.lower()}-1418fb94e984@airbnb.com",
        f"DESCRIPTION:Reservation URL: https://www.airbnb.com/hosting/reservations/details/{code}"
        "\\nPhone Number (Last 4 Digits): 1234",
        "SUMMARY:Reserved",
    ]


def airbnb_block(start: str, end: str) -> list[str]:
    return [f"DTEND;VALUE=DATE:{end}", f"DTSTART;VALUE=DATE:{start}", f"UID:block-{start}@airbnb.com", "SUMMARY:Airbnb (Not available)"]


def test_airbnb_reservations_without_the_blocked_dates():
    pytest.importorskip("ical")
    text = calendar(
        airbnb_reservation("20261025", "20261028", "HMBBBBBBBB"),
        airbnb_block("20261101", "20261103"),
        airbnb_reservation("20261020", "20261025", "HMAAAAAAAA"),
    )
    assert parse_bookings(text) == [
        Booking(code="HMAAAAAAAA", start=date(2026, 10, 20), end=date(2026, 10, 25)),
        Booking(code="HMBBBBBBBB", start=date(2026, 10, 25), end=date(2026, 10, 28)),
    ]


def test_a_calendar_that_names_its_guests():
    pytest.importorskip("ical")
    text = calendar(
        [
            "DTSTART;VALUE=DATE:20261020",
            "DTEND;VALUE=DATE:20261025",
            "DTSTAMP:20261001T000000Z",
            "UID:vrbo-4411@vrbo.com",
            "SUMMARY:Reserved - Sarah Lee",
            "DESCRIPTION:Guests: 3",
        ],
        [
            "DTSTART:20261101T230000Z",
            "DTEND:20261103T180000Z",
            "DTSTAMP:20261001T000000Z",
            "UID:" + "x" * 80 + "@pms.example",
            "SUMMARY:Tom Baker",
        ],
        ["DTSTART;VALUE=DATE:20261110", "DTEND;VALUE=DATE:20261112", "DTSTAMP:20261001T000000Z", "UID:owner@pms.example", "SUMMARY:Blocked"],
    )
    sarah, tom = parse_bookings(text)
    assert (sarah.code, sarah.guest_name, sarah.guest_count) == ("vrbo-4411", "Sarah Lee", 3)
    assert tom.guest_name == "Tom Baker" and tom.guest_count is None
    # Timed events keep their times; a long UID becomes a short ID, the same on every read.
    assert tom.start == datetime(2026, 11, 1, 23, 0, tzinfo=timezone.utc)
    assert tom.code.startswith("event-") and parse_bookings(text)[1].code == tom.code


def test_booking_com_titles_every_booking_closed():
    pytest.importorskip("ical")
    text = calendar(["DTSTART;VALUE=DATE:20261020", "DTEND;VALUE=DATE:20261022", "DTSTAMP:20261001T000000Z", "UID:bk-1@booking.com", "SUMMARY:CLOSED - Not available"])
    assert parse_bookings(text) == []
    [booking] = parse_bookings(text, bookings_only=True)
    assert booking.code == "bk-1" and booking.guest_name is None


def test_cancelled_events_and_broken_files():
    pytest.importorskip("ical")
    cancelled = airbnb_reservation("20261020", "20261025", "HMAAAAAAAA") + ["STATUS:CANCELLED"]
    assert parse_bookings(calendar(cancelled)) == []
    with pytest.raises(CalendarError):
        parse_bookings("<html>Sign in to Airbnb</html>")


def test_calendar_links():
    assert valid_calendar_url("https://www.airbnb.ca/calendar/ical/123.ics?s=abc")
    assert valid_calendar_url("webcal://example.com/feed.ics")
    assert not valid_calendar_url("www.airbnb.com/calendar/ical/123.ics")
    assert not valid_calendar_url("ftp://example.com/feed.ics")
    assert bookings_only_feed("https://admin.booking.com/hotel/hoteladmin/ical.html?t=1")
    assert not bookings_only_feed("https://www.airbnb.com/calendar/ical/1.ics")


# ── choosing the stay ───────────────────────────────────────────────────────

A = Booking("HMAAAAAAAA", date(2026, 10, 20), date(2026, 10, 25))
B = Booking("HMBBBBBBBB", date(2026, 10, 25), date(2026, 10, 28))  # back to back with A
C = Booking("HMCCCCCCCC", date(2026, 11, 5), date(2026, 11, 8))


def at(tz, text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=tz)


def held(booking_id: str, check_in: str, check_out: str) -> dict:
    return {CONF_BOOKING_ID: booking_id, CONF_CHECK_IN: check_in, CONF_CHECK_OUT: check_out}


def decide(bookings, options, now, *, protected=True, source=None):
    tz = now.tzinfo
    decision = choose(
        bookings, now=now, tz=tz, check_in_time=CHECK_IN, check_out_time=CHECK_OUT, grace=GRACE,
        current=current_stay(options, tz), protected=protected,
    )
    return decision, apply(options, decision, tz=tz, source=source)


def test_the_next_booking_fills_in_and_clears_the_last_guests_details():
    tz = zone()
    options = {CONF_GUEST_NAME: "Old guest", CONF_GUEST_COUNT: 2, CONF_GUEST_PHONE_NUMBER: "+16045551234",
               CONF_NOTES: "old", CONF_WIFI_INSTRUCTION: "Network: Cabin"}
    decision, updated = decide([A, B], options, at(tz, "2026-10-19 09:00"))
    assert decision.action == "switch"
    assert updated == {
        CONF_BOOKING_ID: "HMAAAAAAAA",
        CONF_CHECK_IN: "2026-10-20 15:00:00",
        CONF_CHECK_OUT: "2026-10-25 11:00:00",
        CONF_WIFI_INSTRUCTION: "Network: Cabin",  # property details are the host's, not the booking's
    }


def test_back_to_back_stays_change_over_an_hour_after_check_out():
    tz = zone()
    options = held("HMAAAAAAAA", "2026-10-20 15:00:00", "2026-10-25 11:00:00")
    # The last guests may still be there: their stay stays.
    for now in ("2026-10-25 10:00", "2026-10-25 11:59"):
        decision, updated = decide([A, B], options, at(tz, now))
        assert decision.action == "update" and updated == options
    # Even when Airbnb has already dropped it from the calendar.
    assert decide([B], options, at(tz, "2026-10-25 11:30"))[0].action == "keep"
    # After the erase, the next guests' booking takes over.
    decision, updated = decide([A, B], options, at(tz, "2026-10-25 12:00:30"))
    assert decision.action == "switch" and updated[CONF_BOOKING_ID] == "HMBBBBBBBB"
    assert updated[CONF_CHECK_IN] == "2026-10-25 15:00:00"


def test_a_same_day_booking_is_entered_straight_away():
    tz = zone()
    today = Booking("HMDDDDDDDD", date(2026, 10, 30), date(2026, 11, 1))
    decision, updated = decide([today], {}, at(tz, "2026-10-30 16:00"))
    assert decision.action == "switch" and updated[CONF_BOOKING_ID] == "HMDDDDDDDD"


def test_an_extension_moves_check_out_within_the_same_booking():
    tz = zone()
    options = held("HMAAAAAAAA", "2026-10-20 13:00:00", "2026-10-25 11:00:00")
    options[CONF_GUEST_NAME] = "Sarah"
    extended = Booking("HMAAAAAAAA", date(2026, 10, 20), date(2026, 10, 27))
    decision, updated = decide([extended], options, at(tz, "2026-10-22 09:00"))
    assert decision.action == "update"
    # Times come from the usual times for every booking; the guest's name stays.
    assert updated[CONF_CHECK_IN] == "2026-10-20 15:00:00"
    assert updated[CONF_CHECK_OUT] == "2026-10-27 11:00:00"
    assert updated[CONF_GUEST_NAME] == "Sarah"


def test_a_new_link_starts_afresh():
    tz = zone()
    under_way = {**held("HMAAAAAAAA", "2026-10-20 15:00:00", "2026-10-25 11:00:00"),
                 CONF_GUEST_NAME: "Sarah", CONF_BOOKING_CALENDAR_SOURCE: "old-link", CONF_WIFI_INSTRUCTION: "Network: Cabin"}
    now = at(tz, "2026-10-22 09:00")
    # Written by another link, a stay under way gives way at once to the new calendar's...
    decision, updated = decide([C], under_way, now, protected=False, source="new-link")
    assert decision.action == "switch"
    assert (updated[CONF_BOOKING_ID], updated[CONF_BOOKING_CALENDAR_SOURCE]) == ("HMCCCCCCCC", "new-link")
    assert CONF_GUEST_NAME not in updated
    # ...and is cleared when the new calendar has nothing coming.
    decision, updated = decide([], under_way, now, protected=False, source="new-link")
    assert decision.action == "clear"
    assert updated == {CONF_WIFI_INSTRUCTION: "Network: Cabin"}
    # A stay this link wrote, or one typed by hand, keeps its place while under way.
    for bookings in ([], [C]):
        assert decide(bookings, under_way, now)[0].action == "keep"


def test_a_booking_cancelled_before_check_in_is_replaced():
    tz = zone()
    options = held("HMAAAAAAAA", "2026-10-20 15:00:00", "2026-10-25 11:00:00")
    decision, updated = decide([C], options, at(tz, "2026-10-18 09:00"))
    assert decision.action == "switch" and updated[CONF_BOOKING_ID] == "HMCCCCCCCC"
    # After check-in, a stay missing from the calendar stays (Airbnb lists future dates only).
    decision, updated = decide([C], options, at(tz, "2026-10-22 09:00"))
    assert decision.action == "keep" and updated == options


def test_nothing_coming_leaves_guest_settings_alone():
    tz = zone()
    options = held("HMAAAAAAAA", "2026-10-20 15:00:00", "2026-10-25 11:00:00")
    decision, updated = decide([], options, at(tz, "2026-10-26 09:00"))
    assert decision.action == "none" and updated == options


def test_check_in_and_out_follow_daylight_saving():
    tz = zone()
    over_the_change = Booking("HMEEEEEEEE", date(2026, 10, 30), date(2026, 11, 2))  # clocks go back Nov 1
    decision, updated = decide([over_the_change], {}, at(tz, "2026-10-29 09:00"))
    assert updated[CONF_CHECK_IN] == "2026-10-30 15:00:00" and updated[CONF_CHECK_OUT] == "2026-11-02 11:00:00"
    assert decision.stay.check_in.utcoffset() == timedelta(hours=-7)
    assert decision.stay.check_out.utcoffset() == timedelta(hours=-8)


def test_a_calendar_with_times_and_names_fills_in_what_it_knows():
    tz = zone()
    timed = Booking("pms-9", datetime(2026, 11, 1, 23, 0, tzinfo=timezone.utc),
                    datetime(2026, 11, 3, 18, 0, tzinfo=timezone.utc), guest_name="Tom Baker", guest_count=2)
    decision, updated = decide([timed], {}, at(tz, "2026-10-29 09:00"))
    assert updated[CONF_CHECK_IN] == "2026-11-01 15:00:00"  # 23:00 UTC is 15:00 in Los Angeles
    assert updated[CONF_CHECK_OUT] == "2026-11-03 10:00:00"
    assert (updated[CONF_GUEST_NAME], updated[CONF_GUEST_COUNT]) == ("Tom Baker", 2)
    # Within the same booking, what the host typed wins; the calendar only fills gaps.
    typed = {**updated, CONF_GUEST_NAME: "Tom (and Ana)"}
    _decision, again = decide([timed], typed, at(tz, "2026-10-30 09:00"))
    assert again[CONF_GUEST_NAME] == "Tom (and Ana)"


def test_the_booking_after_the_one_held():
    tz = zone()
    now = at(tz, "2026-10-22 09:00")
    upcoming = following([A, B, C], after_code="HMAAAAAAAA", after_check_in=at(tz, "2026-10-20 15:00"), now=now,
                         tz=tz, check_in_time=CHECK_IN, check_out_time=CHECK_OUT)
    assert upcoming.booking.code == "HMBBBBBBBB"
    assert following([A], after_code="HMAAAAAAAA", after_check_in=None, now=now, tz=tz,
                     check_in_time=CHECK_IN, check_out_time=CHECK_OUT) is None


def test_a_stay_is_under_way_from_check_in_until_its_guest_memory_is_erased():
    tz = zone()
    options = held("HMAAAAAAAA", "2026-10-20 15:00:00", "2026-10-25 11:00:00")
    for now, active in (
        ("2026-10-20 14:59", False),
        ("2026-10-20 15:00", True),
        ("2026-10-25 11:30", True),  # guests leaving a little late
        ("2026-10-25 12:00", False),
    ):
        assert stay_active(options, at(tz, now), tz, GRACE) is active, now
    assert stay_active({}, at(tz, "2026-10-22 09:00"), tz, GRACE) is False
    assert stay_active({CONF_BOOKING_ID: "HMAAAAAAAA"}, at(tz, "2026-10-22 09:00"), tz, GRACE) is False


def test_the_stay_state_and_when_it_changes():
    tz = zone()
    options = held("HMAAAAAAAA", "2026-10-20 15:00:00", "2026-10-25 11:00:00")
    assert stay_state({}, at(tz, "2026-10-19 09:00"), tz) == ("none", None)
    assert stay_state(options, at(tz, "2026-10-19 09:00"), tz) == ("upcoming", at(tz, "2026-10-20 15:00"))
    assert stay_state(options, at(tz, "2026-10-22 09:00"), tz) == ("staying", at(tz, "2026-10-25 11:00"))
    assert stay_state(options, at(tz, "2026-10-25 11:00"), tz) == ("checked_out", None)
    assert current_stay(options, tz) == CurrentStay("HMAAAAAAAA", at(tz, "2026-10-20 15:00"), at(tz, "2026-10-25 11:00"))
