"""Hey Auri's off-site backup location (backup.py): Home Assistant's backups, uploaded straight to Hey Auri's storage.

Uses pytest-homeassistant-custom-component (Linux only) and Home Assistant's own backup integration: a real
backup goes through Home Assistant's backup manager to the Hey Auri location. Hey Auri's API and S3 are fakes
here; AuriService's tests/test_backup_handler.py has the API's side.

The contract tests at the end guard what the agent relies on in Home Assistant (the agent interface, the
backup details' format, the backup settings), so a Home Assistant update that changes them fails here first.
"""
from __future__ import annotations

import dataclasses
import hashlib
import inspect
import json
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

pytest.importorskip("pytest_homeassistant_custom_component.common")
pytest.importorskip("securetar")  # Home Assistant's backup integration needs it

import aiohttp  # noqa: E402
from pytest_homeassistant_custom_component.common import MockConfigEntry  # noqa: E402

from homeassistant.components.backup import (  # noqa: E402
    AgentBackup,
    BackupAgent,
    BackupAgentError,
    BackupNotFound,
    OnProgressCallback,
    async_get_manager,
)
from homeassistant.components.backup import config as backup_config  # noqa: E402
from homeassistant.config_entries import ConfigEntryState  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.setup import async_setup_component  # noqa: E402

from custom_components.hey_auri_client import backup as offsite  # noqa: E402
from custom_components.hey_auri_client.const import CONF_CLIENT_ID, CONF_SHARED_SECRET, DATA_AGENT, DOMAIN  # noqa: E402
from custom_components.hey_auri_client.exceptions import SaaSRequestError  # noqa: E402

KEY = "ABCD-EFGH-IJKL-MNOP-QRST-UVWX-YZ12"
# The same vector as AuriService's tests/test_support_logins.py: the hub and the staff vault fingerprint alike.
KEY_ID = "429c806425bcbf4d"
PART = 1000  # bytes in a part here (100 MiB on hubs)
FOLDER = "hubs/cli_test"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


# ── Fakes ────────────────────────────────────────────────────────────────────


class FakeResponse:
    def __init__(self, status: int, headers: dict[str, str] | None = None, body: bytes = b"") -> None:
        self.status = status
        self.headers = headers or {}
        self._body = body
        self.released = False
        self.content = SimpleNamespace(iter_chunked=self._chunks)

    async def read(self) -> bytes:
        return b""

    async def _chunks(self, size: int):
        for start in range(0, len(self._body), size):
            yield self._body[start:start + size]

    def release(self) -> None:
        self.released = True

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class FakeS3:
    """S3 as a hub reaches it through Hey Auri's links: PUT a part, GET a backup."""

    def __init__(self) -> None:
        self.parts: dict[tuple[str, int], bytes] = {}
        self.objects: dict[str, bytes] = {}
        self.refused_links: set[str] = set()
        self.expire_next_link = False
        self.faults: list[Any] = []  # what the next PUTs get: a status, or an exception
        self.wrong_etag = False
        self.puts: list[str] = []

    def put(self, url: str, *, data: bytes, timeout: Any) -> FakeResponse:
        self.puts.append(url)
        if self.expire_next_link:
            self.expire_next_link = False
            self.refused_links.add(url)
        if url in self.refused_links:
            return FakeResponse(403)
        if self.faults:
            fault = self.faults.pop(0)
            if isinstance(fault, Exception):
                raise fault
            return FakeResponse(fault)
        query = parse_qs(urlsplit(url).query)
        self.parts[(query["upload"][0], int(query["part"][0]))] = bytes(data)
        etag = "0" * 32 if self.wrong_etag else hashlib.md5(data).hexdigest()  # noqa: S324
        return FakeResponse(200, {"ETag": f'"{etag}"'})

    async def get(self, url: str, *, timeout: Any) -> FakeResponse:
        key = urlsplit(url).path.lstrip("/")
        if key not in self.objects:
            return FakeResponse(404)
        return FakeResponse(200, body=self.objects[key])


class FakeHeyAuri:
    """Hey Auri's backup routes (AuriService apis/backups), in memory."""

    def __init__(self, s3: FakeS3) -> None:
        self.s3 = s3
        self.details: dict[str, dict[str, Any]] = {}
        self.uploads: dict[str, str] = {}
        self.calls: list[str] = []
        self.faults: list[int] = []  # statuses for the next calls
        self.refuse: tuple[int, str, str] | None = None
        self.links_made = 0

    def _link(self, upload_id: str, number: int) -> str:
        self.links_made += 1
        return f"https://s3.test/{FOLDER}/{self.uploads[upload_id]}.tar?upload={upload_id}&part={number}&v={self.links_made}"

    async def post_json(self, path: str, payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        assert path.startswith("/hub/backups/") and kwargs.get("measurement_key") == "backup"
        action = path.removeprefix("/hub/backups/")
        self.calls.append(action)
        if self.faults:
            status = self.faults.pop(0)
            raise SaaSRequestError(
                f"SaaS request failed: {status}", status_code=status, response_body=json.dumps({"error": "busy", "message": "Busy"})
            )
        if self.refuse is not None:
            status, error, message = self.refuse
            raise SaaSRequestError(
                f"SaaS request failed: {status}", status_code=status, response_body=json.dumps({"error": error, "message": message})
            )
        return getattr(self, f"_{action}")(payload)

    def _start(self, payload: dict[str, Any]) -> dict[str, Any]:
        details = payload["backup"]
        upload_id = f"up{len(self.calls)}"
        self.uploads[upload_id] = details["backup_id"]
        count = max(1, -(-details["size"] // PART))
        return {
            "backup_id": details["backup_id"],
            "upload_id": upload_id,
            "part_bytes": PART,
            "parts": [{"number": number, "url": self._link(upload_id, number)} for number in range(1, count + 1)],
            "expires_in": 3600,
        }

    def _links(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"parts": [{"number": n, "url": self._link(payload["upload_id"], n)} for n in payload["parts"]], "expires_in": 3600}

    def _complete(self, payload: dict[str, Any]) -> dict[str, Any]:
        upload_id = payload["upload_id"]
        backup_id = self.uploads.pop(upload_id)
        data = b""
        for expected, part in enumerate(payload["parts"], start=1):
            stored = self.s3.parts[(upload_id, part["number"])]
            assert part["number"] == expected and part["etag"] == f'"{hashlib.md5(stored).hexdigest()}"'  # noqa: S324
            data += stored
        assert len(data) == payload["bytes"]
        self.s3.objects[f"{FOLDER}/{backup_id}.tar"] = data
        self.details[backup_id] = {**payload["backup"], "size": len(data)}
        return {"backup_id": backup_id, "size": len(data), "removed": []}

    def _abort(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.uploads.pop(payload["upload_id"], None)
        return {"backup_id": payload["backup_id"], "aborted": True}

    def _list(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"backups": list(self.details.values())}

    def _missing(self, backup_id: str) -> SaaSRequestError:
        return SaaSRequestError(
            "SaaS request failed: 404",
            status_code=404,
            response_body=json.dumps({"error": "not_found", "message": "Hey Auri has no backup with that ID for this hub."}),
        )

    def _download(self, payload: dict[str, Any]) -> dict[str, Any]:
        backup_id = payload["backup_id"]
        if backup_id not in self.details:
            raise self._missing(backup_id)
        return {"backup_id": backup_id, "url": f"https://s3.test/{FOLDER}/{backup_id}.tar?sig=1", "size": self.details[backup_id]["size"]}

    def _delete(self, payload: dict[str, Any]) -> dict[str, Any]:
        backup_id = payload["backup_id"]
        if backup_id not in self.details:
            raise self._missing(backup_id)
        del self.details[backup_id]
        self.s3.objects.pop(f"{FOLDER}/{backup_id}.tar", None)
        return {"backup_id": backup_id, "deleted": True}


@pytest.fixture
def world(hass: HomeAssistant, monkeypatch) -> SimpleNamespace:
    s3 = FakeS3()
    api = FakeHeyAuri(s3)
    waits: list[float] = []

    async def no_wait(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(offsite, "async_get_clientsession", lambda hass: s3)
    monkeypatch.setattr(offsite, "_sleep", no_wait)
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_CLIENT_ID: "cli_test", CONF_SHARED_SECRET: "s"})
    entry.add_to_hass(hass)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {DATA_AGENT: SimpleNamespace(client=api)}
    entry.mock_state(hass, ConfigEntryState.LOADED)
    return SimpleNamespace(s3=s3, api=api, entry=entry, waits=waits, agent=offsite.HeyAuriBackupAgent(hass, entry))


def _backup(size: int, *, backup_id: str = "a1b2c3d4", protected: bool = True) -> AgentBackup:
    return AgentBackup.from_dict(
        {
            "addons": [],
            "backup_id": backup_id,
            "date": "2026-11-24T04:52:10.123456-08:00",
            "database_included": True,
            "extra_metadata": {"with_automatic_settings": True},
            "folders": [],
            "homeassistant_included": True,
            "homeassistant_version": "2026.10.1",
            "name": "Automatic backup 2026.10.1",
            "protected": protected,
            "size": size,
        }
    )


def _opener(data: bytes, chunk: int = 300):
    async def open_stream():
        async def chunks():
            for start in range(0, len(data), chunk):
                yield data[start:start + chunk]

        return chunks()

    return open_stream


def _data(size: int) -> bytes:
    return bytes((index * 31 + 7) % 256 for index in range(size))


async def _upload(world: SimpleNamespace, data: bytes, **kwargs: Any) -> list[int]:
    progress: list[int] = []
    backup = kwargs.pop("backup", None) or _backup(len(data), **kwargs)
    await world.agent.async_upload_backup(
        open_stream=_opener(data), backup=backup, on_progress=lambda *, bytes_uploaded, **_: progress.append(bytes_uploaded)
    )
    return progress


# ── Uploading ────────────────────────────────────────────────────────────────


async def test_a_backup_goes_up_in_parts_and_is_listed(hass: HomeAssistant, world) -> None:
    data = _data(2500)

    progress = await _upload(world, data)

    assert progress == [1000, 2000, 2500]
    assert world.s3.objects[f"{FOLDER}/a1b2c3d4.tar"] == data
    assert world.api.calls == ["start", "complete"]
    assert await world.agent.async_list_backups() == [_backup(2500)]
    assert world.agent.agent_id == "hey_auri_client.offsite" and world.agent.name == "Hey Auri"


async def test_chunks_are_cut_into_whole_parts_whatever_their_size(hass: HomeAssistant, world) -> None:
    for chunk in (1, 999, 1000, 1001, 2500, 10_000):
        data = _data(2500)
        sizes = [len(part) async for _, part in offsite._parts(_aiter(data, chunk), PART)]
        assert sizes == [1000, 1000, 500], chunk
        joined = b"".join([part async for _, part in offsite._parts(_aiter(data, chunk), PART)])
        assert joined == data
    assert [part async for part in offsite._parts(_aiter(b"", 300), PART)] == []


async def _aiter(data: bytes, chunk: int):
    for start in range(0, len(data), chunk):
        yield data[start:start + chunk]


async def test_a_stream_longer_than_said_gets_more_links(hass: HomeAssistant, world) -> None:
    data = _data(2600)

    await _upload(world, data, backup=_backup(1500))

    assert world.api.calls == ["start", "links", "complete"]
    assert world.s3.objects[f"{FOLDER}/a1b2c3d4.tar"] == data
    assert world.api.details["a1b2c3d4"]["size"] == 2600


async def test_a_failed_part_is_tried_again(hass: HomeAssistant, world) -> None:
    world.s3.faults = [503, aiohttp.ClientConnectionError("connection reset")]

    await _upload(world, _data(1500))

    assert len(world.s3.puts) == 4 and len(world.waits) == 2  # part 1 three times, part 2 once
    assert world.s3.objects[f"{FOLDER}/a1b2c3d4.tar"] == _data(1500)


async def test_a_link_that_ran_out_is_asked_for_again(hass: HomeAssistant, world) -> None:
    world.s3.expire_next_link = True

    await _upload(world, _data(500))

    assert world.api.calls == ["start", "links", "complete"]
    assert world.waits == []  # a new link straight away
    assert len(world.s3.puts) == 2 and world.s3.puts[0] != world.s3.puts[1]


async def test_bytes_s3_kept_wrong_fail_the_upload_and_drop_it(hass: HomeAssistant, world) -> None:
    world.s3.wrong_etag = True

    with pytest.raises(BackupAgentError, match="after 5 tries: S3 kept different bytes"):
        await _upload(world, _data(1500))

    assert len(world.s3.puts) == offsite.PART_TRIES
    assert world.api.calls == ["start", "abort"] and world.api.uploads == {}
    assert await world.agent.async_list_backups() == []


async def test_an_unencrypted_backup_isnt_sent(hass: HomeAssistant, world) -> None:
    with pytest.raises(BackupAgentError, match="encrypted backups only"):
        await _upload(world, _data(500), protected=False)

    assert world.api.calls == [] and world.s3.puts == []


async def test_hey_auri_refusing_a_backup_says_why(hass: HomeAssistant, world) -> None:
    world.api.refuse = (413, "too_large", "This backup is 12.3 GB; Hey Auri keeps backups up to 10.7 GB.")

    with pytest.raises(BackupAgentError, match="12.3 GB"):
        await _upload(world, _data(500))

    assert world.api.calls == ["start"]  # not tried again: Hey Auri said no


async def test_a_busy_hey_auri_is_asked_again(hass: HomeAssistant, world) -> None:
    world.api.faults = [503, 429]

    await _upload(world, _data(500))

    assert world.api.calls == ["start", "start", "start", "complete"] and len(world.waits) == 2


async def test_an_upload_cut_short_drops_its_parts(hass: HomeAssistant, world) -> None:
    async def broken_stream():
        async def chunks():
            yield _data(1200)
            raise OSError("The disk went away")

        return chunks()

    with pytest.raises(OSError):
        await world.agent.async_upload_backup(open_stream=broken_stream, backup=_backup(3000), on_progress=lambda **_: None)

    assert world.api.calls == ["start", "abort"] and world.api.uploads == {}


# ── Listing, downloading, deleting ───────────────────────────────────────────


async def test_download_streams_the_backup(hass: HomeAssistant, world) -> None:
    data = _data(2500)
    await _upload(world, data)

    stream = await world.agent.async_download_backup("a1b2c3d4")

    assert b"".join([chunk async for chunk in stream]) == data


async def test_backups_hey_auri_doesnt_have(hass: HomeAssistant, world) -> None:
    with pytest.raises(BackupNotFound):
        await world.agent.async_get_backup("nope")
    with pytest.raises(BackupNotFound):
        await world.agent.async_download_backup("nope")
    with pytest.raises(BackupNotFound):
        await world.agent.async_delete_backup("nope")


async def test_the_list_is_kept_five_minutes_and_redone_after_a_change(hass: HomeAssistant, world) -> None:
    await _upload(world, _data(500))
    await world.agent.async_list_backups()
    await world.agent.async_get_backup("a1b2c3d4")
    assert world.api.calls.count("list") == 1

    await world.agent.async_delete_backup("a1b2c3d4")

    assert await world.agent.async_list_backups() == []
    assert world.api.calls.count("list") == 2


async def test_a_list_hey_auri_cant_give_is_an_agent_error(hass: HomeAssistant, world) -> None:
    world.api.refuse = (403, "backup_disabled", "Off-site backups are off for this account.")

    with pytest.raises(BackupAgentError, match="Off-site backups are off for this account"):
        await world.agent.async_list_backups()


# ── With Home Assistant's backups ────────────────────────────────────────────


@pytest.fixture
async def manager(hass: HomeAssistant, world, socket_enabled):
    """Home Assistant's backup integration, with the Hey Auri Client loaded (its HTTP server on a spare port)."""
    assert await async_setup_component(hass, "http", {"http": {"server_port": 18124}})
    hass.config.components.add(DOMAIN)
    assert await async_setup_component(hass, "backup", {})
    await hass.async_block_till_done()
    yield async_get_manager(hass)
    await hass.http.stop()


async def test_home_assistant_backs_up_to_hey_auri_and_deletes_there(hass: HomeAssistant, world, manager) -> None:
    assert manager.backup_agents[offsite.OFFSITE_AGENT_ID].name == "Hey Auri"

    await manager.async_create_backup(
        agent_ids=[offsite.OFFSITE_AGENT_ID],
        include_addons=None,
        include_all_addons=False,
        include_database=False,
        include_folders=None,
        include_homeassistant=True,
        name="Before updating Hey Auri Client",
        password=KEY,
    )
    await hass.async_block_till_done()

    [backup_id] = list(world.api.details)
    details = world.api.details[backup_id]
    assert details["protected"] is True and details["name"] == "Before updating Hey Auri Client"
    assert len(world.s3.objects[f"{FOLDER}/{backup_id}.tar"]) == details["size"]
    backups, errors = await manager.async_get_backups()
    assert errors == {} and offsite.OFFSITE_AGENT_ID in backups[backup_id].agents

    assert await manager.async_delete_backup(backup_id, agent_ids=[offsite.OFFSITE_AGENT_ID]) == {}
    assert world.api.details == {}


async def test_the_location_follows_the_entry(hass: HomeAssistant, world, manager) -> None:
    assert offsite.OFFSITE_AGENT_ID in manager.backup_agents

    world.entry.mock_state(hass, ConfigEntryState.NOT_LOADED)
    offsite.async_notify_backup_listeners(hass)
    await hass.async_block_till_done()

    assert offsite.OFFSITE_AGENT_ID not in manager.backup_agents


async def test_not_offered_in_mainland_china(hass: HomeAssistant, world) -> None:
    hass.config.country = "CN"

    assert await offsite.async_get_backup_agents(hass) == []
    assert offsite.backup_summary(hass)["backup_offsite"] is None

    hass.config.country = "CA"
    assert [agent.agent_id for agent in await offsite.async_get_backup_agents(hass)] == [offsite.OFFSITE_AGENT_ID]


async def test_hey_auri_is_added_to_the_automatic_locations_once(hass: HomeAssistant, world, manager) -> None:
    assert manager.config.data.create_backup.agent_ids == []

    assert await offsite.async_offer_offsite(hass) is True
    assert manager.config.data.create_backup.agent_ids == [offsite.OFFSITE_AGENT_ID]

    # Someone takes it off; after a restart it stays off.
    manager.config.update(create_backup={"agent_ids": ["backup.local"]})
    hass.data.pop(offsite._DATA_OFFERED_ONCE)
    assert await offsite.async_offer_offsite(hass) is True
    assert manager.config.data.create_backup.agent_ids == ["backup.local"]


async def test_nothing_is_added_before_the_location_is_loaded(hass: HomeAssistant, world) -> None:
    assert await offsite.async_offer_offsite(hass) is False  # no backup integration yet


async def test_the_summary_has_the_backup_settings_and_a_key_fingerprint(hass: HomeAssistant, world, manager) -> None:
    data = manager.config.data
    data.schedule = backup_config.BackupSchedule(recurrence=backup_config.ScheduleRecurrence.DAILY)
    data.retention = backup_config.RetentionConfig(copies=7, days=None)
    manager.config.update(create_backup={"agent_ids": [offsite.OFFSITE_AGENT_ID], "password": KEY})

    summary = offsite.backup_summary(hass)

    assert summary == {"backup_schedule": "daily", "backup_copies": 7, "backup_offsite": True, "backup_key_id": KEY_ID}
    assert KEY not in json.dumps(summary)

    data.retention = backup_config.RetentionConfig(copies=None, days=14)  # kept by age
    manager.config.update(create_backup={"agent_ids": ["backup.local"], "password": None})
    assert offsite.backup_summary(hass) == {
        "backup_schedule": "daily", "backup_copies": None, "backup_offsite": False, "backup_key_id": None
    }


async def test_without_backups_the_summary_says_nothing(hass: HomeAssistant) -> None:
    assert offsite.backup_summary(hass) == {
        "backup_schedule": None, "backup_copies": None, "backup_offsite": None, "backup_key_id": None
    }


def test_the_key_fingerprint_matches_hey_auris() -> None:
    assert offsite.backup_key_id(KEY) == KEY_ID
    assert offsite.backup_key_id(f" {KEY}\n") == KEY_ID


# ── Contract: what the agent relies on in Home Assistant ─────────────────────


def test_home_assistants_agent_interface_is_what_the_agent_implements() -> None:
    assert BackupAgent.__abstractmethods__ == {
        "async_download_backup", "async_upload_backup", "async_delete_backup", "async_list_backups", "async_get_backup",
    }
    assert offsite.HeyAuriBackupAgent.__abstractmethods__ == frozenset()
    upload = inspect.signature(BackupAgent.async_upload_backup).parameters
    assert {"open_stream", "backup", "on_progress"} <= set(upload)
    assert "bytes_uploaded" in inspect.signature(OnProgressCallback.__call__).parameters
    assert inspect.signature(BackupAgent.async_download_backup).parameters.keys() >= {"backup_id"}


def test_backup_details_survive_the_trip_to_hey_auri() -> None:
    backup = _backup(123_456)

    assert AgentBackup.from_dict(json.loads(json.dumps(backup.as_dict()))) == backup
    assert backup.as_dict()["backup_id"] == "a1b2c3d4" and backup.as_dict()["protected"] is True


def test_the_backup_settings_the_client_reads() -> None:
    fields = {cls.__name__: {field.name for field in dataclasses.fields(cls)} for cls in (
        backup_config.BackupConfigData, backup_config.CreateBackupConfig, backup_config.RetentionConfig, backup_config.BackupSchedule,
    )}

    assert {"create_backup", "retention", "schedule"} <= fields["BackupConfigData"]
    assert {"agent_ids", "password"} <= fields["CreateBackupConfig"]
    assert "copies" in fields["RetentionConfig"]
    assert "recurrence" in fields["BackupSchedule"]
    assert {member.value for member in backup_config.ScheduleRecurrence} == {"never", "daily", "custom_days"}
    assert "create_backup" in inspect.signature(backup_config.BackupConfig.update).parameters
