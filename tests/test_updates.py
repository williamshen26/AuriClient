"""Hey Auri Client updates itself on hubs Auri set up: the approved version, at night, then a restart.

Uses pytest-homeassistant-custom-component (Linux only).
"""
from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import pytest

pytest.importorskip("pytest_homeassistant_custom_component.common")

from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service  # noqa: E402

from homeassistant.core import HomeAssistant, ServiceCall  # noqa: E402

from custom_components.hey_auri_client.const import CONF_CLIENT_ID, CONF_SHARED_SECRET, DOMAIN  # noqa: E402
from custom_components.hey_auri_client.memory.config import CONF_MEMORY_DAY_BOUNDARY_HOUR  # noqa: E402
from custom_components.hey_auri_client import updates  # noqa: E402
from custom_components.hey_auri_client.updates import (  # noqa: E402
    FAILED_NIGHTS_BEFORE_GIVING_UP,
    UPDATE_PATH,
    AutoUpdater,
    update_hour,
)

RELEASES = "https://github.com/williamshen26/AuriClient/releases/tag/"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


class Backend:
    """Hey Auri's answer to "which version should I run?"."""

    def __init__(self, version: str | None) -> None:
        self.version = version
        self.down = False
        self.asked = 0

    async def post_json(self, path: str, payload: dict, **_: Any) -> dict:
        assert path == UPDATE_PATH
        self.asked += 1
        if self.down:
            raise ConnectionError("no route to host")
        return {"ring": "general" if self.version else "off", "version": self.version}


class Marketplace:
    """The Marketplace's update entity for Hey Auri Client, and its install action."""

    def __init__(self, hass: HomeAssistant, installed: str, *, entity_id: str = "update.hey_auri_client") -> None:
        self.hass, self.entity_id = hass, entity_id
        self.installs: list[dict] = []
        self.fail = False
        self.writes_version = True
        self.on_install = lambda: None
        self.set(installed)

        async def install(call: ServiceCall) -> None:
            self.on_install()
            self.installs.append(dict(call.data))
            if self.fail:
                raise RuntimeError("GitHub rate limit")
            if self.writes_version:
                self.set(call.data["version"])

        hass.services.async_register("update", "install", install)

    def set(self, installed: str) -> None:
        self.hass.states.async_set(
            self.entity_id, "off", {"installed_version": installed, "latest_version": installed, "release_url": RELEASES + installed}
        )


class Backups:
    """Home Assistant's backup.create_automatic action: blocking until the backup is done."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.made = 0
        self.fail = False
        self.slow = False
        self.cancelled = 0
        # Set by a test: the backup waits for it (an upload to an off-site location still going).
        self.hold: asyncio.Event | None = None
        self.on_backup = lambda: None

        async def create_automatic(_call: ServiceCall) -> None:
            self.on_backup()
            try:
                if self.slow:
                    await asyncio.sleep(1)
                if self.hold is not None:
                    await self.hold.wait()
            except asyncio.CancelledError:
                self.cancelled += 1
                raise
            if self.fail:
                raise RuntimeError("No space left on device")
            self.made += 1

        hass.services.async_register("backup", "create_automatic", create_automatic)


@pytest.fixture
def make(hass: HomeAssistant):
    created: list[AutoUpdater] = []

    async def _make(version: str | None, installed: str | None = "v0.2.6", **options: Any):
        entry = MockConfigEntry(domain=DOMAIN, data={CONF_CLIENT_ID: "c", CONF_SHARED_SECRET: "s"}, options=options)
        entry.add_to_hass(hass)
        backend = Backend(version)
        marketplace = Marketplace(hass, installed) if installed else None
        restarts = async_mock_service(hass, "homeassistant", "restart")
        hass.data["test_backups"] = Backups(hass)
        updater = AutoUpdater(hass, entry, backend)
        await updater.async_start()
        created.append(updater)
        return updater, backend, marketplace, restarts

    yield _make
    for updater in created:
        updater.async_stop()


async def test_a_new_approved_version_is_installed_then_home_assistant_restarts(hass: HomeAssistant, make) -> None:
    updater, _backend, marketplace, restarts = await make("v0.2.7")

    result = await updater.async_check()
    await hass.async_block_till_done()

    assert result == {"result": "installed", "version": "v0.2.7"}
    assert marketplace.installs == [{"entity_id": "update.hey_auri_client", "version": "v0.2.7"}]
    assert len(restarts) == 1
    assert updater.summary() == {"update_status": "current", "update_target": "v0.2.7"}


@pytest.mark.parametrize(("version", "installed", "result"), [(None, "v0.2.6", "off"), ("v0.2.6", "v0.2.6", "current")])
async def test_nothing_to_install_means_no_restart(hass: HomeAssistant, make, version, installed, result) -> None:
    updater, _backend, marketplace, restarts = await make(version, installed)

    assert (await updater.async_check())["result"] == result
    await hass.async_block_till_done()

    assert marketplace.installs == [] and restarts == []
    assert updater.summary()["update_status"] == result


async def test_an_older_approved_version_rolls_back(hass: HomeAssistant, make) -> None:
    updater, _backend, marketplace, restarts = await make("v0.2.5", "v0.2.6")

    assert (await updater.async_check())["result"] == "installed"
    await hass.async_block_till_done()

    assert marketplace.installs[0]["version"] == "v0.2.5" and len(restarts) == 1


async def test_a_conversation_in_progress_waits_for_another_night(hass: HomeAssistant, make) -> None:
    updater, _backend, marketplace, restarts = await make("v0.2.7")
    hass.states.async_set("assist_satellite.kitchen", "responding")

    assert (await updater.async_check())["result"] == "busy"
    await hass.async_block_till_done()

    assert marketplace.installs == [] and restarts == []
    assert updater.summary()["update_status"] == "pending"


async def test_after_three_failed_nights_it_gives_up_until_another_version(hass: HomeAssistant, make) -> None:
    updater, backend, marketplace, restarts = await make("v0.2.7")
    marketplace.fail = True

    results = [(await updater.async_check())["result"] for _ in range(FAILED_NIGHTS_BEFORE_GIVING_UP + 1)]
    await hass.async_block_till_done()

    assert results == ["install_failed"] * FAILED_NIGHTS_BEFORE_GIVING_UP + ["failed"]
    assert len(marketplace.installs) == FAILED_NIGHTS_BEFORE_GIVING_UP and restarts == []
    assert updater.summary()["update_status"] == "failed"

    backend.version = "v0.2.8"  # a new version gets its own tries
    marketplace.fail = False
    assert (await updater.async_check())["result"] == "installed"


async def test_an_install_that_doesnt_take_counts_as_failed(hass: HomeAssistant, make) -> None:
    updater, _backend, marketplace, restarts = await make("v0.2.7")
    marketplace.writes_version = False

    assert (await updater.async_check())["result"] == "install_failed"
    await hass.async_block_till_done()

    assert restarts == []


async def test_without_the_marketplace_entity_it_says_so(hass: HomeAssistant, make, caplog) -> None:
    """A test hub whose code was copied by hand: nothing to update through."""
    updater, _backend, _marketplace, restarts = await make("v0.2.7", installed=None)

    assert (await updater.async_check())["result"] == "no_entity"

    assert restarts == [] and updater.summary()["update_status"] == "no_entity"
    assert "wasn't installed from the Marketplace" in caplog.text


async def test_hey_auri_unreachable_changes_nothing(hass: HomeAssistant, make) -> None:
    updater, backend, marketplace, _restarts = await make("v0.2.7")
    backend.down = True

    assert (await updater.async_check())["result"] == "unreachable"

    assert marketplace.installs == []
    assert updater.summary() == {"update_status": None, "update_target": None}  # never told anything yet


async def test_the_entity_is_found_by_its_repository(hass: HomeAssistant, make) -> None:
    updater, _backend, _marketplace, restarts = await make("v0.2.7", installed=None)
    Marketplace(hass, "v0.2.6", entity_id="update.hey_auri_client_2")

    assert (await updater.async_check())["result"] == "installed"
    await hass.async_block_till_done()

    assert len(restarts) == 1


async def test_what_was_asked_survives_a_restart(hass: HomeAssistant, make) -> None:
    updater, _backend, marketplace, _restarts = await make("v0.2.7")
    marketplace.fail = True
    await updater.async_check()
    await hass.async_block_till_done()

    again = AutoUpdater(hass, updater.entry, updater.client)
    await again.async_start()
    again.async_stop()

    assert again.summary() == {"update_status": "pending", "update_target": "v0.2.7"}


@pytest.mark.parametrize(("options", "hour"), [({}, 3), ({CONF_MEMORY_DAY_BOUNDARY_HOUR: 6}, 5), ({CONF_MEMORY_DAY_BOUNDARY_HOUR: 0}, 23)])
def test_it_runs_the_hour_before_memorys_night_job(options, hour) -> None:
    assert update_hour(options) == hour


# ── a backup first ───────────────────────────────────────────────────────────────────────────────

async def test_home_assistant_is_backed_up_before_the_install(hass: HomeAssistant, make) -> None:
    updater, _backend, marketplace, restarts = await make("v0.2.7")
    order: list[str] = []
    marketplace.on_install = lambda: order.append("install")
    updater.hass.data["test_backups"].on_backup = lambda: order.append("backup")

    assert (await updater.async_check())["result"] == "installed"
    await hass.async_block_till_done()

    assert order == ["backup", "install"] and len(restarts) == 1


@pytest.mark.parametrize("problem", ["fails", "missing", "slow"])
async def test_without_a_finished_backup_nothing_is_installed(hass: HomeAssistant, make, monkeypatch, problem) -> None:
    updater, _backend, marketplace, restarts = await make("v0.2.7")
    backups = updater.hass.data["test_backups"]
    if problem == "fails":
        backups.fail = True
    elif problem == "missing":
        hass.services.async_remove("backup", "create_automatic")
    else:
        backups.slow = True
        monkeypatch.setattr(updates, "BACKUP_TIMEOUT", timedelta(milliseconds=50))

    result = await updater.async_check()
    await hass.async_block_till_done()

    assert result == {"result": "backup_failed", "version": "v0.2.7"}
    assert marketplace.installs == [] and restarts == []
    # Counted like a failed install: after 3 nights staff see it on their list (update_status "failed").
    assert updater.summary() == {"update_status": "pending", "update_target": "v0.2.7"}


async def test_running_out_of_time_never_cancels_the_backup(hass: HomeAssistant, make, monkeypatch) -> None:
    """The backup may be uploading to Hey Auri's off-site location: the updater stops waiting, the backup goes on."""
    updater, _backend, marketplace, restarts = await make("v0.2.7")
    backups = updater.hass.data["test_backups"]
    backups.hold = asyncio.Event()
    monkeypatch.setattr(updates, "BACKUP_TIMEOUT", timedelta(milliseconds=50))

    assert await updater.async_check() == {"result": "backup_failed", "version": "v0.2.7"}
    assert marketplace.installs == [] and restarts == []

    backups.hold.set()  # the upload finishes later
    await hass.async_block_till_done(wait_background_tasks=True)
    assert backups.made == 1 and backups.cancelled == 0


async def test_nothing_to_install_means_no_backup(hass: HomeAssistant, make) -> None:
    updater, _backend, _marketplace, _restarts = await make("v0.2.6")
    assert (await updater.async_check())["result"] == "current"
    assert updater.hass.data["test_backups"].made == 0


async def test_a_conversation_that_starts_during_the_backup_waits_for_another_night(hass: HomeAssistant, make) -> None:
    updater, _backend, marketplace, restarts = await make("v0.2.7")
    updater.hass.data["test_backups"].on_backup = lambda: hass.states.async_set("assist_satellite.kitchen", "listening")

    assert (await updater.async_check())["result"] == "busy"
    assert marketplace.installs == [] and restarts == []
