"""Timers ring on the device that asked, show on the dashboard, and survive restarts (timers.py).

Uses pytest-homeassistant-custom-component (Linux only). A satellite here is a device with a timer
handler registered, as ESPHome registers one for each voice satellite (and the mobile app for each
phone that takes notifications).
"""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

pytest.importorskip("pytest_homeassistant_custom_component.common")

from pytest_homeassistant_custom_component.common import (  # noqa: E402
    MockConfigEntry,
    MockEntityPlatform,
    async_capture_events,
    async_fire_time_changed,
    async_mock_service,
)

from homeassistant.components import intent  # noqa: E402
from homeassistant.components.intent import TimerEventType, TimerManager  # noqa: E402
from homeassistant.components.intent.const import TIMER_DATA  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.helpers import device_registry as dr, issue_registry as ir  # noqa: E402
from homeassistant.setup import async_setup_component  # noqa: E402

from custom_components.hey_auri_client.const import (  # noqa: E402
    CONF_CLIENT_ID,
    CONF_SHARED_SECRET,
    DOMAIN,
    EVENT_TIMER_FINISHED,
    OLD_TIMER_FINISHED_EVENT,
)
from custom_components.hey_auri_client.conversation.custom_services.timer_services import (  # noqa: E402
    RUNTIME_TIMERS,
    TimerToolService,
    get_auri_timers_native,
    start_auri_timer_native,
)
from custom_components.hey_auri_client.conversation.local_tool_executor import LocalToolExecutor  # noqa: E402
from custom_components.hey_auri_client.memory.identity import TurnMeta  # noqa: E402
from custom_components.hey_auri_client.repairs import async_create_fix_flow  # noqa: E402
from custom_components.hey_auri_client.timer_automation import (  # noqa: E402
    OLD_TIMER_AUTOMATION_ISSUE,
    async_update_old_timer_automation_issue,
)
from custom_components.hey_auri_client.timers import (  # noqa: E402
    PHONE_PUSH_DATA,
    RESTORE_WAIT,
    AuriTimers,
    async_push_to_phone,
    parse_duration,
)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


class Device:
    """A device Home Assistant can ring timers on, keeping the timer events it was sent."""

    def __init__(self, hass: HomeAssistant, name: str, *, domain: str = "esphome", entry: MockConfigEntry | None = None) -> None:
        self.hass = hass
        if entry is None:
            entry = MockConfigEntry(domain=domain)
            entry.add_to_hass(hass)
        self.entry = entry
        self.id = dr.async_get(hass).async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={(domain, name)}, name=name
        ).id
        self.events: list[tuple[TimerEventType, str, int, bool, str | None]] = []
        self._unregister = None

    def connect(self) -> None:
        def handler(event_type: TimerEventType, info: intent.TimerInfo) -> None:
            self.events.append((event_type, info.id, info.seconds_left, info.is_active, info.name))

        self._unregister = intent.async_register_timer_handler(self.hass, self.id, handler)

    def kinds(self) -> list[TimerEventType]:
        return [event[0] for event in self.events]


class Hub:
    """Auri's timers with their sensors, as the integration's sensor platform sets them up."""

    def __init__(self, hass: HomeAssistant, entry: MockConfigEntry) -> None:
        self.hass, self.entry = hass, entry
        self.timers: AuriTimers | None = None
        self.platform: MockEntityPlatform | None = None

    async def start(self) -> AuriTimers:
        platform = self.platform = MockEntityPlatform(self.hass, domain="sensor", platform_name=DOMAIN)

        def add(entities: list, update_before_add: bool = False) -> None:
            self.hass.async_create_task(platform.async_add_entities(entities))

        self.timers = AuriTimers(self.hass, self.entry)
        self.hass.data.setdefault(DOMAIN, {}).setdefault("runtime", {})[RUNTIME_TIMERS] = self.timers
        await self.timers.async_start(add)
        await self.hass.async_block_till_done()
        return self.timers

    async def restart(self) -> AuriTimers:
        """Home Assistant restarting: its own timers are gone, and every device reconnects later."""
        await self.timers.async_stop()
        await self.platform.async_reset()
        self.hass.data[TIMER_DATA] = TimerManager(self.hass)
        return await self.start()

    def states(self) -> dict[str, Any]:
        return {state.attributes.get("timer_id"): state for state in self.hass.states.async_all("sensor")}

    async def advance(self, freezer, seconds: float) -> None:
        """Time going by: Home Assistant's timers end, then Auri's once-a-second look sees it."""
        freezer.tick(timedelta(seconds=seconds))
        async_fire_time_changed(self.hass)
        await self.hass.async_block_till_done()
        self.timers._async_sync()
        await self.hass.async_block_till_done()


@pytest.fixture
async def hub(hass: HomeAssistant):
    await async_setup_component(hass, "homeassistant", {})
    await async_setup_component(hass, "intent", {})
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_CLIENT_ID: "client", CONF_SHARED_SECRET: "secret"})
    entry.add_to_hass(hass)
    hub = Hub(hass, entry)
    await hub.start()
    yield hub
    await hub.timers.async_stop()


def turn(device_id: str | None = None, user_id: str | None = None) -> TurnMeta:
    return TurnMeta(mode="owner", memory_allowed=False, conversation_id="c1", device_id=device_id, user_id=user_id, language="en")


async def call(hass: HomeAssistant, tool: str, *, device_id: str | None = None, user_id: str | None = None, **arguments: Any) -> dict:
    """A tool call as Auri's backend sends it, through the executor (which adds the turn)."""
    import json

    executor = LocalToolExecutor(hass)
    result = await executor.execute_tool_call(
        {"id": "t1", "function": {"name": tool, "arguments": json.dumps(arguments)}},
        conversation_id="c1",
        turn=turn(device_id, user_id),
    )
    await hass.async_block_till_done()
    return result["result"]


def test_durations() -> None:
    assert parse_duration("300") == 300
    assert parse_duration(90) == 90
    assert parse_duration("05:00") == 300  # MM:SS, as people say "five minutes"
    assert parse_duration("1:30:00") == 5400
    assert parse_duration("PT2M") == 120
    assert parse_duration(timedelta(minutes=1)) == 60
    for wrong in ("5 minutes", "", None, 0, "-30", True):
        with pytest.raises(ValueError):
            parse_duration(wrong)


async def test_a_timer_starts_on_the_satellite_that_asked(hass: HomeAssistant, hub: Hub) -> None:
    kitchen = Device(hass, "Kitchen satellite")
    kitchen.connect()

    result = await call(hass, "start_timer", device_id=kitchen.id, duration="300", name="Pasta")

    assert result["success"] is True
    timer = result["timer"]
    assert timer["device"] == "Kitchen satellite" and timer["on_this_device"] is True
    assert timer["name"] == "Pasta" and timer["duration"] == 300 and timer["status"] == "active"
    # Home Assistant's own timer, on the satellite: it counts down on its LED ring and rings there.
    assert kitchen.events == [(TimerEventType.STARTED, kitchen.events[0][1], 300, True, "Pasta")]
    state = hub.states()[timer["timer_id"]]
    assert state.entity_id == f"sensor.auri_timer_{timer['timer_id']}"
    assert state.state == "active" and state.name == "Pasta"
    assert state.attributes["duration"] == 300 and state.attributes["remaining"] == pytest.approx(300, abs=1)


async def test_a_satellite_that_cant_ring_gets_auris_own_timer(hass: HomeAssistant, hub: Hub) -> None:
    quiet = Device(hass, "Old satellite")  # no timer handler: its firmware has no timers

    result = await call(hass, "start_timer", device_id=quiet.id, duration="60")

    assert result["timer"]["device"] is None and quiet.events == []
    assert hub.states()[result["timer"]["timer_id"]].state == "active"


async def test_changes_go_to_the_device_and_the_dashboard(hass: HomeAssistant, hub: Hub, freezer) -> None:
    kitchen = Device(hass, "Kitchen satellite")
    kitchen.connect()
    timer_id = (await call(hass, "start_timer", device_id=kitchen.id, duration="300"))["timer"]["timer_id"]

    freezer.tick(timedelta(seconds=100))
    paused = await call(hass, "pause_timer", device_id=kitchen.id)
    assert paused["timer"]["status"] == "paused" and paused["timer"]["remaining"] == 200
    assert kitchen.events[-1][0] == TimerEventType.UPDATED and kitchen.events[-1][3] is False
    state = hub.states()[timer_id]
    assert state.state == "paused" and state.attributes["remaining"] == 200

    freezer.tick(timedelta(seconds=50))  # paused: no time goes by on it
    resumed = await call(hass, "resume_timer", device_id=kitchen.id)
    assert resumed["timer"]["status"] == "active" and resumed["timer"]["remaining"] == 200
    assert hub.states()[timer_id].state == "active"

    longer = await call(hass, "change_timer", device_id=kitchen.id, add="05:00")
    assert longer["timer"]["remaining"] == 500 and longer["timer"]["duration"] == 500
    assert kitchen.events[-1][0] == TimerEventType.UPDATED and kitchen.events[-1][2] == 500
    shorter = await call(hass, "change_timer", device_id=kitchen.id, remove="60")
    assert shorter["timer"]["remaining"] == 440

    cancelled = await call(hass, "cancel_timer", device_id=kitchen.id)
    assert cancelled["success"] is True and kitchen.kinds()[-1] == TimerEventType.CANCELLED
    assert hub.states() == {}
    assert (await call(hass, "list_timers"))["timers"] == []


async def test_the_bar_counts_down_from_when_the_state_last_changed(hass: HomeAssistant, hub: Hub, freezer) -> None:
    """The timer bar takes remaining as of the state's last change, as for Home Assistant's timers."""
    kitchen = Device(hass, "Kitchen satellite")
    kitchen.connect()
    timer_id = (await call(hass, "start_timer", device_id=kitchen.id, duration="300"))["timer"]["timer_id"]
    started = hub.states()[timer_id]

    freezer.tick(timedelta(seconds=100))
    await call(hass, "change_timer", device_id=kitchen.id, add="60")

    state = hub.states()[timer_id]
    assert state.state == "active" and state.last_changed == started.last_changed
    # The bar shows remaining - (now - last_changed): 360 - 100 = 260 seconds left.
    assert state.attributes["remaining"] == pytest.approx(360, abs=1)


async def test_which_timer(hass: HomeAssistant, hub: Hub) -> None:
    kitchen = Device(hass, "Kitchen satellite")
    kitchen.connect()
    await call(hass, "start_timer", device_id=kitchen.id, duration="600", name="Pasta")
    await call(hass, "start_timer", device_id=kitchen.id, duration="300", name="Eggs")

    unsure = await call(hass, "cancel_timer", device_id=kitchen.id)
    assert unsure["success"] is False and "Ask the user which one" in unsure["error"]
    assert {timer["name"] for timer in unsure["timers"]} == {"Pasta", "Eggs"}
    assert (await call(hass, "cancel_timer", name="laundry"))["success"] is False  # no such name
    assert (await call(hass, "list_timers"))["timers"][0]["name"] == "Eggs"  # the one ending first
    # "Pause the timer" with one already paused means the other one.
    await call(hass, "pause_timer", name="eggs")
    assert (await call(hass, "pause_timer", device_id=kitchen.id))["timer"]["name"] == "Pasta"

    assert (await call(hass, "cancel_timer", name="PASTA"))["cancelled"][0]["name"] == "Pasta"
    everything = await call(hass, "cancel_timer", all=True)
    assert [timer["name"] for timer in everything["cancelled"]] == ["Eggs"] and hub.states() == {}


async def test_a_timer_that_finishes_on_its_device(hass: HomeAssistant, hub: Hub, freezer) -> None:
    """The satellite rang by itself: the dashboard drops it, and the new event goes out, not the old."""
    kitchen = Device(hass, "Kitchen satellite")
    kitchen.connect()
    finished = async_capture_events(hass, EVENT_TIMER_FINISHED)
    old = async_capture_events(hass, OLD_TIMER_FINISHED_EVENT)
    timer_id = (await call(hass, "start_timer", device_id=kitchen.id, duration="30", name="Tea"))["timer"]["timer_id"]

    await hub.advance(freezer, 31)

    assert kitchen.kinds() == [TimerEventType.STARTED, TimerEventType.FINISHED]
    assert hub.states() == {}
    assert [event.data for event in finished] == [
        {"timer_id": timer_id, "entity_id": f"sensor.auri_timer_{timer_id}", "name": "Tea", "duration": 30, "device_id": kitchen.id}
    ]
    assert old == []


async def test_home_assistants_own_timers_are_shown_too(hass: HomeAssistant, hub: Hub) -> None:
    """A timer set through Home Assistant's own agent, or cancelled there."""
    kitchen = Device(hass, "Kitchen satellite")
    kitchen.connect()
    manager: TimerManager = hass.data[TIMER_DATA]

    native_id = manager.start_timer(kitchen.id, None, 5, None, language="en", name="Laundry")
    hub.timers._async_sync()
    await hass.async_block_till_done()
    (state,) = hub.states().values()
    assert state.name == "Laundry" and state.attributes["duration"] == 300

    manager.pause_timer(native_id)
    hub.timers._async_sync()
    await hass.async_block_till_done()
    assert next(iter(hub.states().values())).state == "paused"

    manager.cancel_timer(native_id)
    hub.timers._async_sync()
    await hass.async_block_till_done()
    assert hub.states() == {}


async def test_timers_survive_a_restart(hass: HomeAssistant, hub: Hub, freezer) -> None:
    kitchen = Device(hass, "Kitchen satellite")
    kitchen.connect()
    running = (await call(hass, "start_timer", device_id=kitchen.id, duration="300", name="Pasta"))["timer"]
    paused = (await call(hass, "start_timer", device_id=kitchen.id, duration="600", name="Bread"))["timer"]
    await call(hass, "pause_timer", name="bread")
    old_ids = {event[1] for event in kitchen.events}

    freezer.tick(timedelta(seconds=100))
    await hub.restart()
    kitchen.events.clear()

    # Until the satellite reconnects, the dashboard keeps both, counting down as before.
    states = hub.states()
    assert states[running["timer_id"]].state == "active" and states[paused["timer_id"]].state == "paused"
    listed = {timer["name"]: timer for timer in (await call(hass, "list_timers"))["timers"]}
    assert listed["Pasta"]["remaining"] == 200 and listed["Bread"]["remaining"] == 600

    freezer.tick(timedelta(seconds=10))
    kitchen.connect()
    hub.timers._async_sync()
    await hass.async_block_till_done()

    # The satellite still shows the timers it had: those are cancelled there, then started again.
    cancelled = {event[1] for event in kitchen.events if event[0] == TimerEventType.CANCELLED}
    assert cancelled == old_ids
    started = {event[4]: event for event in kitchen.events if event[0] == TimerEventType.STARTED}
    assert started["Pasta"][2] == 190 and started["Bread"][2] == 600
    assert kitchen.events[-1][0] == TimerEventType.UPDATED and kitchen.events[-1][3] is False  # Bread paused
    states = hub.states()
    assert set(states) == {running["timer_id"], paused["timer_id"]}  # the same sensors
    assert states[paused["timer_id"]].state == "paused"
    manager: TimerManager = hass.data[TIMER_DATA]
    assert sorted(timer.name for timer in manager.timers.values()) == ["Bread", "Pasta"]


async def test_a_timer_that_ran_out_during_the_restart_rings_at_once(hass: HomeAssistant, hub: Hub, freezer) -> None:
    kitchen = Device(hass, "Kitchen satellite")
    kitchen.connect()
    await call(hass, "start_timer", device_id=kitchen.id, duration="60")

    freezer.tick(timedelta(seconds=120))
    await hub.restart()
    kitchen.events.clear()
    kitchen.connect()
    hub.timers._async_sync()
    await hass.async_block_till_done()

    assert [event[0] for event in kitchen.events] == [TimerEventType.CANCELLED, TimerEventType.STARTED]
    assert kitchen.events[1][2] == 1
    await hub.advance(freezer, 2)
    assert kitchen.kinds()[-1] == TimerEventType.FINISHED and hub.states() == {}


async def test_a_device_that_never_comes_back(hass: HomeAssistant, hub: Hub, freezer) -> None:
    kitchen = Device(hass, "Kitchen satellite")
    kitchen.connect()
    await call(hass, "start_timer", device_id=kitchen.id, duration="3600")
    await hub.restart()

    await hub.advance(freezer, (RESTORE_WAIT + timedelta(seconds=1)).total_seconds())

    assert hub.states() == {}


async def test_cancelled_while_the_satellite_is_still_away(hass: HomeAssistant, hub: Hub) -> None:
    kitchen = Device(hass, "Kitchen satellite")
    kitchen.connect()
    await call(hass, "start_timer", device_id=kitchen.id, duration="300")
    old_id = kitchen.events[0][1]
    await hub.restart()
    kitchen.events.clear()

    assert (await call(hass, "cancel_timer"))["success"] is True
    await hub.restart()  # kept across another restart
    kitchen.connect()
    hub.timers._async_sync()
    await hass.async_block_till_done()

    # Once back, the satellite is told the old one is gone, and nothing starts again.
    assert [(event[0], event[1]) for event in kitchen.events] == [(TimerEventType.CANCELLED, old_id)]
    assert hub.states() == {}


async def test_auris_own_timer_notifies_the_askers_phones(hass: HomeAssistant, hub: Hub, freezer) -> None:
    """From the web page (no device): the asker's phones, then everyone's but Auri support's."""
    owner = await hass.auth.async_create_user("Owner")
    partner = await hass.auth.async_create_user("Partner")
    support = await hass.auth.async_create_user("Support")
    phones = {}
    for user, name in ((owner, "Owner's phone"), (partner, "Partner's phone"), (support, "Support's phone")):
        entry = MockConfigEntry(domain="mobile_app", data={"user_id": user.id})
        entry.add_to_hass(hass)
        phone = Device(hass, name, domain="mobile_app", entry=entry)
        phone.connect()  # takes notifications, so Home Assistant rings its own timers there
        phones[name] = phone.id
    hass.config_entries.async_update_entry(hub.entry, options={"support_user_id": support.id})
    sent: list[tuple[str, str]] = []

    async def push(hass: HomeAssistant, device_id: str, message: str) -> None:
        sent.append((device_id, message))

    with patch("custom_components.hey_auri_client.timers.async_push_to_phone", push):
        await call(hass, "start_timer", user_id=owner.id, duration="60", name="Pizza")
        assert (await call(hass, "list_timers"))["timers"][0]["device"] is None
        await hub.advance(freezer, 61)
        assert sent == [(phones["Owner's phone"], "Pizza finished")]

        sent.clear()
        await call(hass, "start_timer", duration="90")  # an automation: nobody asked
        await hub.advance(freezer, 91)
        assert sorted(device_id for device_id, _ in sent) == sorted([phones["Owner's phone"], phones["Partner's phone"]])
        assert {message for _, message in sent} == {"0:01:30 timer finished"}
    assert hub.states() == {}


async def test_the_push_is_home_assistants_own_timer_push(hass: HomeAssistant) -> None:
    """The same time-sensitive push Home Assistant sends for its own timers (mobile_app/timers.py).

    Needs the mobile app integration's own packages, which the Home Assistant image has and CI doesn't.
    """
    device_action = pytest.importorskip("homeassistant.components.mobile_app.device_action")
    sent: list[dict] = []

    async def call_action(hass: HomeAssistant, config: dict, variables: dict, context: Any) -> None:
        sent.append(config)

    with patch.object(device_action, "async_call_action_from_config", call_action):
        await async_push_to_phone(hass, "phone-1", "Pizza finished")

    assert sent == [{"device_id": "phone-1", "message": "Pizza finished", "data": PHONE_PUSH_DATA}]
    assert PHONE_PUSH_DATA["push"] == {"interruption-level": "time-sensitive"}
    assert PHONE_PUSH_DATA["channel"] == "Timers" and PHONE_PUSH_DATA["importance"] == "high"


async def test_own_timers_survive_a_restart(hass: HomeAssistant, hub: Hub, freezer) -> None:
    await call(hass, "start_timer", duration="300")
    freezer.tick(timedelta(seconds=100))
    await hub.restart()

    (timer,) = (await call(hass, "list_timers"))["timers"]
    assert timer["remaining"] == 200 and timer["status"] == "active"


async def test_the_actions_for_automations(hass: HomeAssistant, hub: Hub) -> None:
    kitchen = Device(hass, "Kitchen satellite")
    kitchen.connect()

    started = await start_auri_timer_native(hass, 120, device_id=kitchen.id, name="Oven")

    assert started["device"] == "Kitchen satellite" and started["entity_id"].startswith("sensor.auri_timer_")
    assert [timer["name"] for timer in (await get_auri_timers_native(hass))["timers"]] == ["Oven"]
    assert kitchen.kinds() == [TimerEventType.STARTED]


async def test_an_older_backend_still_sending_satellite_speaker(hass: HomeAssistant, hub: Hub) -> None:
    """The satellite comes from the turn; the model's satellite_speaker is ignored."""
    kitchen = Device(hass, "Kitchen satellite")
    kitchen.connect()

    result = await TimerToolService(hass).start_timer(
        {"duration": "60", "satellite_speaker": "satellite_speaker.somewhere_else", "_turn": turn(kitchen.id)}
    )

    assert result["timer"]["device"] == "Kitchen satellite"


async def test_the_old_timer_automation_notice(hass: HomeAssistant, hub: Hub, tmp_path: Path) -> None:
    hass.config.config_dir = str(tmp_path)
    Path(hass.config.path("automations.yaml")).write_text(
        "- id: '1700000000001'\n"
        "  alias: Timer finished (DO NOT DELETE)\n"
        "  triggers:\n"
        "  - trigger: event\n"
        "    event_type: hey_auri_client.auri_timer_finished\n"
        "  actions:\n"
        "  - action: notify.notify\n"
        "    data:\n"
        "      message: Timer finished\n"
        "- id: '1700000000002'\n"
        "  alias: Porch light at sunset\n"
        "  triggers:\n"
        "  - trigger: sun\n"
        "    event: sunset\n"
        "  actions:\n"
        "  - action: light.turn_on\n"
        "    target:\n"
        "      entity_id: light.porch\n",
        encoding="utf-8",
    )
    async_mock_service(hass, "automation", "reload")

    await async_update_old_timer_automation_issue(hass)
    issue = ir.async_get(hass).async_get_issue(DOMAIN, OLD_TIMER_AUTOMATION_ISSUE)
    assert issue is not None and issue.is_fixable
    assert issue.translation_placeholders == {"automations": "“Timer finished (DO NOT DELETE)”"}

    flow = await async_create_fix_flow(hass, OLD_TIMER_AUTOMATION_ISSUE, None)
    flow.hass = hass
    form = await flow.async_step_init()
    assert form["type"] == "form" and form["step_id"] == "confirm"
    done = await flow.async_step_confirm({})
    assert done["type"] == "create_entry"

    left = Path(hass.config.path("automations.yaml")).read_text(encoding="utf-8")
    assert "Porch light at sunset" in left and "auri_timer_finished" not in left
    assert ir.async_get(hass).async_get_issue(DOMAIN, OLD_TIMER_AUTOMATION_ISSUE) is None
