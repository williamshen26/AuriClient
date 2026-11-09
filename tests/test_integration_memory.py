"""Memory wired into Home Assistant: options flow, agent turns, tools, turn logging and the memory exam.

Uses pytest-homeassistant-custom-component (Linux only: Home Assistant's
test harness doesn't run on Windows) and the test PostgreSQL server.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import dataclasses
from datetime import timedelta
import json
import math
import re
from typing import Any
from unittest.mock import AsyncMock, patch
import zlib

import pytest

pytest.importorskip("pytest_homeassistant_custom_component.common")

from pytest_homeassistant_custom_component.common import MockConfigEntry, MockUser, async_fire_time_changed  # noqa: E402

from homeassistant.components import conversation  # noqa: E402
from homeassistant.core import Context, HomeAssistant  # noqa: E402
from homeassistant.data_entry_flow import FlowResultType  # noqa: E402
from homeassistant.exceptions import HomeAssistantError  # noqa: E402
from homeassistant.helpers import issue_registry as ir  # noqa: E402
from homeassistant.setup import async_setup_component  # noqa: E402
from homeassistant.util import dt as dt_util  # noqa: E402

from custom_components.hey_auri_client.const import (  # noqa: E402
    CONF_CLIENT_ID,
    CONF_MEMORY_DB_HOST,
    CONF_MEMORY_DB_NAME,
    CONF_MEMORY_DB_PASSWORD,
    CONF_MEMORY_DB_PORT,
    CONF_MEMORY_DB_SSLMODE,
    CONF_MEMORY_DB_USER,
    CONF_MEMORY_ENABLED,
    CONF_MEMORY_LOGIN_PEOPLE,
    CONF_MEMORY_LOGINS_ASKED,
    CONF_MEMORY_SHARE_HOUSEHOLD,
    CONF_MEMORY_VAULT_PATH,
    CONF_SHARED_SECRET,
    DATA_MEMORY,
    DOMAIN,
    MEMORY_EXAM_FILE,
)
from custom_components.hey_auri_client.conversation.agent_class import ThinOpenAIAgent  # noqa: E402
from custom_components.hey_auri_client.memory import exam, logins, names, space as memory_space, turn_log  # noqa: E402
from custom_components.hey_auri_client.memory.recall import match_subjects  # noqa: E402
from custom_components.hey_auri_client.memory.api import MemoryApiError  # noqa: E402
from custom_components.hey_auri_client.memory.db import EXAM_MEMORY_SCHEMA  # noqa: E402
from custom_components.hey_auri_client.memory.identity import Speaker  # noqa: E402
from custom_components.hey_auri_client.memory.runtime import MemoryRuntime, MemoryState  # noqa: E402
from custom_components.hey_auri_client.memory.store import NewMemory  # noqa: E402
from custom_components.hey_auri_client.saas_client import SaaSTurnResponse  # noqa: E402

_WORD = re.compile(r"[a-z0-9']+")


def fake_vector(text: str) -> list[float]:
    vector = [0.0] * 768
    for word in _WORD.findall(text.lower()):
        # crc32, not hash(): Python seeds hash() per run, so which words collide would change between runs.
        vector[zlib.crc32(word.encode()) % 768] += 1.0
    norm = math.sqrt(sum(v * v for v in vector)) or 1.0
    return [v / norm for v in vector]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture
def memory_options(pg_config, pg_engine):
    cfg = pg_config()
    return {
        CONF_MEMORY_ENABLED: True,
        CONF_MEMORY_DB_HOST: cfg.host,
        CONF_MEMORY_DB_PORT: cfg.port,
        CONF_MEMORY_DB_NAME: cfg.database,
        CONF_MEMORY_DB_USER: cfg.username,
        CONF_MEMORY_DB_PASSWORD: cfg.password,
        CONF_MEMORY_DB_SSLMODE: "disable",
    }


def _entry(options: dict[str, Any] | None = None) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN, data={CONF_CLIENT_ID: "client", CONF_SHARED_SECRET: "secret"}, options=options or {}
    )


# ── options flow ────────────────────────────────────────────────────────────


async def _open_memory_step(hass: HomeAssistant, entry: MockConfigEntry) -> dict[str, Any]:
    with patch("homeassistant.config_entries.async_process_deps_reqs", AsyncMock()):
        result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU and "memory" in result["menu_options"]
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "memory"})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "memory"
    return result


async def test_memory_options_validate_and_keep_the_password(hass: HomeAssistant, memory_options, tmp_path) -> None:
    entry = _entry({"stt_language": "en"})
    entry.add_to_hass(hass)

    result = await _open_memory_step(hass, entry)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_MEMORY_ENABLED: True})
    assert result["errors"] == {"base": "memory_settings_missing"}

    wrong = {**memory_options, CONF_MEMORY_DB_PASSWORD: "not-the-password"}
    result = await hass.config_entries.options.async_configure(result["flow_id"], wrong)
    assert result["errors"] == {"base": "invalid_auth"}

    public = tmp_path / "www" / "vault"
    public.mkdir(parents=True)
    with patch.object(hass.config, "config_dir", str(tmp_path)):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {**memory_options, CONF_MEMORY_VAULT_PATH: str(public)}
        )
    assert result["errors"] == {CONF_MEMORY_VAULT_PATH: "vault_public"}

    result = await hass.config_entries.options.async_configure(result["flow_id"], memory_options)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_MEMORY_DB_PASSWORD] == memory_options[CONF_MEMORY_DB_PASSWORD]
    assert entry.options["stt_language"] == "en"  # other options are kept

    # Editing again with the password left empty keeps the saved one.
    result = await _open_memory_step(hass, entry)
    without_password = {k: v for k, v in memory_options.items() if k != CONF_MEMORY_DB_PASSWORD}
    result = await hass.config_entries.options.async_configure(result["flow_id"], without_password)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_MEMORY_DB_PASSWORD] == memory_options[CONF_MEMORY_DB_PASSWORD]


async def test_memory_can_be_saved_switched_off_without_a_database(hass: HomeAssistant) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    result = await _open_memory_step(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_MEMORY_ENABLED: False, CONF_MEMORY_DB_HOST: "nowhere.invalid"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_who_uses_each_login(hass: HomeAssistant) -> None:
    will = await hass.auth.async_create_user("Will")
    admin = await hass.auth.async_create_user("Admin")
    mqtt = await hass.auth.async_create_user("MQTT-user")
    hass.states.async_set("person.will", "home", {"friendly_name": "Will", "user_id": will.id})
    hass.states.async_set("person.mia", "home", {"friendly_name": "Mia"})
    hass.states.async_set("person.mqtt_user", "unknown", {"friendly_name": "MQTT-user", "user_id": mqtt.id})
    entry = _entry()
    entry.add_to_hass(hass)

    async def open_form():
        with patch("homeassistant.config_entries.async_process_deps_reqs", AsyncMock()):
            result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "logins"})
        assert result["type"] is FlowResultType.FORM and result["step_id"] == "logins"
        return result, {str(field): select for field, select in result["data_schema"].schema.items()}

    result, fields = await open_form()
    assert {"Admin", "MQTT-user", "Will"} <= set(fields)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"Admin": "person.will", "Will": "shared", "MQTT-user": "not_a_person"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_MEMORY_LOGIN_PEOPLE] == {
        admin.id: "person.will",
        will.id: "shared",
        mqtt.id: "not_a_person",
    }
    assert {admin.id, will.id, mqtt.id} <= set(entry.options[CONF_MEMORY_LOGINS_ASKED])

    # A login that isn't a person's: its person is no longer offered for the others.
    _result, fields = await open_form()
    assert "person.mqtt_user" not in {option["value"] for option in fields["Admin"].config["options"]}


def test_names_that_look_like_a_service():
    for name in ("Admin", "HA admin", "Administrator", "MQTT-user", "mqttuser", "Zigbee2MQTT", "Node-RED", "nodered",
                 "Frigate", "ESPHome", "Kitchen tablet", "Wall panel", "Kiosk", "Guest", "Test"):
        assert logins.looks_like_a_service(name), name
    for name in ("Will", "Sofia", "Lillian", "Grandma Rose", "Admir", "Tessa", "Tabitha", "Dashiell", None):
        assert not logins.looks_like_a_service(name), name
    assert [logins.names_text(labels) for labels in ([], ["Admin"], ["Admin", "MQTT"], ["Admin", "Frigate", "MQTT"])] == [
        "", "Admin", "Admin and MQTT", "Admin, Frigate and MQTT",
    ]


def _people(hass: HomeAssistant, *users) -> None:
    """A person for each login, as Home Assistant makes one with every login."""
    for user in users:
        hass.states.async_set(
            f"person.{user.name.lower().replace('-', '_')}", "unknown", {"friendly_name": user.name, "user_id": user.id}
        )


async def test_setting_up_memory_asks_who_uses_each_login(hass: HomeAssistant, memory_options) -> None:
    will = await hass.auth.async_create_user("Will")
    admin = await hass.auth.async_create_user("Admin")
    mqtt = await hass.auth.async_create_user("MQTT-user")
    _people(hass, will, admin, mqtt)
    entry = _entry()
    entry.add_to_hass(hass)

    result = await _open_memory_step(hass, entry)
    result = await hass.config_entries.options.async_configure(result["flow_id"], memory_options)
    # Turning memory on asks one more question, and nothing is saved before it's answered.
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "logins"
    assert CONF_MEMORY_ENABLED not in entry.options
    defaults = {str(field): field.default() for field in result["data_schema"].schema}
    assert (defaults["Admin"], defaults["MQTT-user"], defaults["Will"]) == ("not_a_person", "not_a_person", "linked")
    assert "Not a person, because the name looks like a service's or a device's:** Admin and MQTT-user." in (
        result["description_placeholders"]["preselected"]
    )

    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_MEMORY_ENABLED] is True
    assert entry.options[CONF_MEMORY_DB_PASSWORD] == memory_options[CONF_MEMORY_DB_PASSWORD]
    assert entry.options[CONF_MEMORY_LOGIN_PEOPLE] == {admin.id: "not_a_person", mqtt.id: "not_a_person"}

    # Asked once: saving the memory settings again goes straight through.
    result = await _open_memory_step(hass, entry)
    result = await hass.config_entries.options.async_configure(result["flow_id"], memory_options)
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_a_repairs_notice_asks_about_logins_that_look_like_a_service(hass: HomeAssistant, hass_client) -> None:
    will = await hass.auth.async_create_user("Will")
    admin = await hass.auth.async_create_user("Admin")
    mqtt = await hass.auth.async_create_user("MQTT-user")
    _people(hass, will, admin, mqtt)
    # Memory was on before the question was asked at setup.
    entry = _entry({CONF_MEMORY_ENABLED: True})
    entry.add_to_hass(hass)
    issue_id = f"service_logins_{entry.entry_id}"
    issues = ir.async_get(hass)

    await logins.async_update_logins_issue(hass, entry)
    issue = issues.async_get_issue(DOMAIN, issue_id)
    assert issue.is_fixable and issue.translation_placeholders == {"logins": "Admin and MQTT-user"}

    # Its fix is the question, with those two set to Not a person.
    assert await async_setup_component(hass, "repairs", {})
    hass.config.components.add(DOMAIN)  # Repairs only uses the fixes of integrations that are loaded
    client = await hass_client()
    response = await client.post("/api/repairs/issues/fix", json={"handler": DOMAIN, "issue_id": issue_id})
    flow = await response.json()
    assert flow["step_id"] == "logins" and flow["description_placeholders"] == {"logins": "Admin and MQTT-user"}
    defaults = {field["name"]: field.get("default") for field in flow["data_schema"]}
    assert (defaults["Admin"], defaults["MQTT-user"], defaults["Will"]) == ("not_a_person", "not_a_person", "linked")
    response = await client.post(f"/api/repairs/issues/fix/{flow['flow_id']}", json={})
    assert (await response.json())["type"] == "create_entry"
    assert entry.options[CONF_MEMORY_LOGIN_PEOPLE] == {admin.id: "not_a_person", mqtt.id: "not_a_person"}
    assert issues.async_get_issue(DOMAIN, issue_id) is None

    # Asked once: a login set back to its own person afterwards stays that way.
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, CONF_MEMORY_LOGIN_PEOPLE: {mqtt.id: "not_a_person"}}
    )
    await logins.async_update_logins_issue(hass, entry)
    assert issues.async_get_issue(DOMAIN, issue_id) is None

    # A login added later that looks like a service's brings the notice back, for that one.
    frigate = await hass.auth.async_create_user("Frigate")
    _people(hass, frigate)
    await logins.async_update_logins_issue(hass, entry)
    assert issues.async_get_issue(DOMAIN, issue_id).translation_placeholders == {"logins": "Frigate"}

    # With memory off, nothing lists the household: no notice.
    hass.config_entries.async_update_entry(entry, options={**entry.options, CONF_MEMORY_ENABLED: False})
    await logins.async_update_logins_issue(hass, entry)
    assert issues.async_get_issue(DOMAIN, issue_id) is None


async def test_the_owners_own_login_is_kept_when_every_login_looks_like_a_service(hass: HomeAssistant) -> None:
    # Whoever set up Home Assistant called their own login Admin; the MQTT broker uses the other one.
    admin = MockUser(name="Admin", is_owner=True).add_to_hass(hass)
    mqtt = await hass.auth.async_create_user("MQTT-user")
    _people(hass, admin, mqtt)
    entry = _entry({CONF_MEMORY_ENABLED: True})
    entry.add_to_hass(hass)

    await logins.async_update_logins_issue(hass, entry)
    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"service_logins_{entry.entry_id}")
    assert issue.translation_placeholders == {"logins": "MQTT-user"}


async def test_a_repairs_notice_asks_about_names_spelled_two_ways(hass: HomeAssistant, memory_setup, hass_client) -> None:
    _agent, runtime, _client, _will = memory_setup
    store = runtime.store

    def seed() -> list[int]:
        import sqlalchemy

        zoey, zoe, cleo, clio = store.resolve_entities([("Zoey", "pet"), ("Zoe", "pet"), ("Cleo", "pet"), ("Clio", "pet")])
        store.insert_memory(NewMemory(kind="fact", content="Zoe is 12 years old.", source="chat", about_entity_ids=[zoe]))
        # Will also stored as a topic, as a person renamed in Home Assistant can leave behind.
        with store.engine.begin() as conn:
            topic = conn.execute(sqlalchemy.text(
                "INSERT INTO auri_memory.mem_entities (scope, kind, name) VALUES ('owner', 'topic', 'will') RETURNING id"
            )).scalar()
        store.insert_memory(NewMemory(kind="fact", content="Will runs on Sundays.", source="chat", about_entity_ids=[topic]))
        return [zoey, zoe, cleo, clio]

    zoey, zoe, _cleo, _clio = await hass.async_add_executor_job(seed)
    await runtime.owner.async_refresh_entities()
    entry = runtime.entry
    hass.data.setdefault(DOMAIN, {}).setdefault(entry.entry_id, {})[DATA_MEMORY] = runtime
    issue_id = f"similar_names_{entry.entry_id}"
    issues = ir.async_get(hass)

    await names.async_update_names_issue(hass, entry, runtime)
    issue = issues.async_get_issue(DOMAIN, issue_id)
    assert issue.is_fixable and issue.translation_placeholders == {"names": "Cleo and Clio; Zoe and Zoey"}
    # Will stored again under the same name wasn't asked about: it's one person now.
    [runs] = _db(runtime, "SELECT e.to_entity FROM auri_memory.mem_edges e JOIN auri_memory.mem_memories m "
                          "ON m.id = e.from_memory WHERE m.content = 'Will runs on Sundays.'")
    assert runs["to_entity"] == runtime.owner.person_ids["person.will"]
    assert [entity.name for entity in runtime.owner.entities if entity.name.lower() == "will"] == ["Will"]

    # The fix: Zoe and Zoey are one cat, spelled Zoey; Cleo and Clio are two dogs.
    assert await async_setup_component(hass, "repairs", {})
    hass.config.components.add(DOMAIN)
    client = await hass_client()
    response = await client.post("/api/repairs/issues/fix", json={"handler": DOMAIN, "issue_id": issue_id})
    flow = await response.json()
    assert flow["step_id"] == "names" and flow["description_placeholders"] == {"names": "Cleo and Clio; Zoe and Zoey"}
    assert {field["name"]: field.get("default") for field in flow["data_schema"]} == {
        "Cleo and Clio": "unsure", "Zoe and Zoey": "unsure",
    }
    response = await client.post(
        f"/api/repairs/issues/fix/{flow['flow_id']}", json={"Zoe and Zoey": f"same:{zoey}", "Cleo and Clio": "different"}
    )
    assert (await response.json())["type"] == "create_entry"
    assert issues.async_get_issue(DOMAIN, issue_id) is None

    pets = [(entity.name, entity.aliases) for entity in runtime.owner.entities if entity.kind == "pet"]
    assert pets == [("Cleo", ()), ("Clio", ()), ("Zoey", ("Zoe",))]
    assert [m["name"] for m in runtime.owner.household_roster() if m["kind"] == "pet"] == ["Cleo", "Clio", "Zoey"]
    # A question that says Zoe finds what's known about Zoey, the age said as Zoe included.
    assert match_subjects("How old is Zoe?", runtime.owner.entities) == [zoey]
    [age] = _db(runtime, "SELECT e.to_entity FROM auri_memory.mem_edges e JOIN auri_memory.mem_memories m "
                         "ON m.id = e.from_memory WHERE m.content = 'Zoe is 12 years old.'")
    assert age["to_entity"] == zoey and zoe not in {entity.id for entity in runtime.owner.entities}

    # Asked once: Cleo and Clio don't come back, after a nightly run either.
    await runtime.async_run_nightly()
    assert issues.async_get_issue(DOMAIN, issue_id) is None

    # A name the nightly run learns brings it back, for that pair; with memory not working, it goes.
    await hass.async_add_executor_job(lambda: store.resolve_entities([("Rex", "pet"), ("Rexy", "pet")]))
    await runtime.async_run_nightly()
    assert issues.async_get_issue(DOMAIN, issue_id).translation_placeholders == {"names": "Rex and Rexy"}
    runtime.state = MemoryState.UNAVAILABLE
    await names.async_update_names_issue(hass, entry, runtime)
    assert issues.async_get_issue(DOMAIN, issue_id) is None
    runtime.state = MemoryState.ENABLED


# ── agent turns ─────────────────────────────────────────────────────────────


class FakeClient:
    """Scripted backend: returns the queued responses and answers /memory/embed."""

    def __init__(self) -> None:
        self.responses: list[SaaSTurnResponse] = []
        self.payloads: list[dict[str, Any]] = []

    async def send_user_turn(self, payload: dict[str, Any]) -> SaaSTurnResponse:
        self.payloads.append(payload)
        return self.responses.pop(0)

    async def send_tool_results(self, payload: dict[str, Any]) -> SaaSTurnResponse:
        self.payloads.append(payload)
        return self.responses.pop(0)

    async def post_json(self, path: str, payload: dict[str, Any], **_kwargs) -> dict[str, Any]:
        assert path == "/memory/embed"
        return {"model": "fake-embed", "dimensions": 768, "embeddings": [fake_vector(t) for t in payload["texts"]]}


def tool_call(name: str, arguments: dict[str, Any], *, embed: str | None = None) -> dict[str, Any]:
    function: dict[str, Any] = {"name": name, "arguments": json.dumps(arguments)}
    if embed is not None:
        function["memory_embedding"] = {
            "model": "fake-embed", "dimensions": 768, "task_type": "RETRIEVAL_QUERY", "vector": fake_vector(embed),
        }
    return {"id": f"call-{name}", "type": "function", "function": function}


def tool_calls(
    conversation_id: str, calls: list[dict[str, Any]], *, mode: str = "owner", allowed: bool | None = None
) -> SaaSTurnResponse:
    return SaaSTurnResponse(
        response_type="tool_calls", conversation_id=conversation_id, turn_id="turn-1", tool_calls=calls,
        mode=mode, memory_allowed=mode == "owner" if allowed is None else allowed,
    )


def final(conversation_id: str, text: str, *, mode: str = "owner", allowed: bool | None = None) -> SaaSTurnResponse:
    return SaaSTurnResponse(
        response_type="final", conversation_id=conversation_id, content=text, mode=mode,
        memory_allowed=mode == "owner" if allowed is None else allowed,
    )


@pytest.fixture
async def memory_setup(hass: HomeAssistant, memory_options, monkeypatch):
    monkeypatch.setattr(turn_log, "_BATCH_WINDOW_SECONDS", 0)
    await async_setup_component(hass, "homeassistant", {})
    will = await hass.auth.async_create_user("Will")
    hass.states.async_set("person.will", "home", {"friendly_name": "Will", "user_id": will.id})
    hass.states.async_set("person.mia", "home", {"friendly_name": "Mia"})

    entry = _entry(memory_options)
    entry.add_to_hass(hass)
    client = FakeClient()
    runtime = MemoryRuntime(hass, entry, client, app_version="test")
    await runtime.async_start()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert runtime.state is MemoryState.ENABLED

    agent = ThinOpenAIAgent(hass, entry)
    agent.client = client
    agent.attach_memory(runtime)
    yield agent, runtime, client, will
    await runtime.async_shutdown()


def _input(text: str, conversation_id: str, *, user_id: str | None = None, device_id: str | None = None):
    return conversation.ConversationInput(
        text=text, context=Context(user_id=user_id), conversation_id=conversation_id,
        device_id=device_id, satellite_id=None, language="en", agent_id="hey_auri_client",
    )


def _db(runtime: MemoryRuntime, sql: str) -> list[dict[str, Any]]:
    import sqlalchemy

    with runtime.store.engine.connect() as conn:
        return [dict(row) for row in conn.execute(sqlalchemy.text(sql)).mappings()]


async def _settle(hass: HomeAssistant) -> None:
    await hass.async_block_till_done(wait_background_tasks=True)


# ── notes by meaning ────────────────────────────────────────────────────────

_OFFICE_LIGHT = "The office light is usually set to 30% brightness."
_RECYCLING = "The recycling goes out on Tuesday nights."
_LAPTOP_QUESTION = "怎么让笔记本电脑上网？"


async def _seed_for_meaning(hass: HomeAssistant, runtime: MemoryRuntime, *memories: NewMemory) -> None:
    await hass.async_add_executor_job(lambda: [runtime.store.insert_memory(memory) for memory in memories])
    await runtime.owner.async_refresh_entities()
    await runtime.owner.async_backfill_embeddings()


def _embeds_as(text: str) -> AsyncMock:
    """An embedding call that comes back as close as can be to this text."""
    return AsyncMock(return_value=([fake_vector(text)], "fake-embed@768"))


async def test_a_message_in_another_language_gets_notes_by_meaning(hass: HomeAssistant, memory_setup) -> None:
    _agent, runtime, _client, _will = memory_setup
    await _seed_for_meaning(
        hass,
        runtime,
        NewMemory(kind="preference", content=_OFFICE_LIGHT, source="chat"),
        NewMemory(kind="fact", content=_RECYCLING, source="chat"),
    )

    embed = _embeds_as(_OFFICE_LIGHT)
    with patch.object(runtime.api, "embed", embed):
        block = await runtime.owner.async_turn_context("把灯调到我喜欢的亮度。", Speaker(), "m1")
    embed.assert_awaited_once_with(["把灯调到我喜欢的亮度。"], task_type="RETRIEVAL_QUERY", scope="owner")
    assert _OFFICE_LIGHT in block["notes"]
    assert _RECYCLING not in block["notes"]  # below the floor

    # Nothing close means no notes, not the closest there is.
    with patch.object(runtime.api, "embed", _embeds_as("completely different words about nothing here")):
        block = await runtime.owner.async_turn_context(_LAPTOP_QUESTION, Speaker(), "m2")
    assert block["notes"] == ""


async def test_notes_by_meaning_are_only_for_messages_that_need_them(hass: HomeAssistant, memory_setup) -> None:
    _agent, runtime, _client, _will = memory_setup
    embed = _embeds_as(_OFFICE_LIGHT)
    with patch.object(runtime.api, "embed", embed):
        # On a hub set to English: Latin letters are English, and a short reply needs nothing.
        for text in ("What time is the dentist?", "Open the garage door", "Apaga las luces", "好的"):
            await runtime.owner.async_turn_context(text, Speaker(), "m3")
        embed.assert_not_awaited()

        # On a hub set to another language, English still needs no search, but the words have to show it.
        hass.config_entries.async_update_entry(runtime.entry, options={**runtime.entry.options, "stt_language": "zh"})
        await runtime.owner.async_turn_context("What time is the dentist?", Speaker(), "m3")
        embed.assert_not_awaited()
        await runtime.owner.async_turn_context("Apaga las luces de la sala", Speaker(), "m3")
        embed.assert_awaited_once()


@pytest.mark.parametrize("failure", ["slow", "error", "over_budget"])
async def test_a_turn_goes_on_without_notes_by_meaning(hass: HomeAssistant, memory_setup, monkeypatch, failure) -> None:
    _agent, runtime, _client, _will = memory_setup

    def seed() -> None:
        mia = runtime.store.resolve_entities([("Mia", "person")])[0]
        runtime.store.insert_memory(NewMemory(kind="fact", content="Mia has piano on Thursdays.", source="chat",
                                              importance=4, about_entity_ids=[mia]))

    await hass.async_add_executor_job(seed)
    await runtime.owner.async_refresh_entities()
    monkeypatch.setattr(memory_space, "MEANING_NOTES_BUDGET_SECONDS", 0.05)

    async def slow(*_args, **_kwargs):
        await asyncio.sleep(30)

    embed = {
        "slow": AsyncMock(side_effect=slow),
        "error": AsyncMock(side_effect=MemoryApiError("The cloud is down", status_code=502)),
        "over_budget": AsyncMock(side_effect=MemoryApiError("Over budget", status_code=429)),
    }[failure]
    with patch.object(runtime.api, "embed", embed):
        async with asyncio.timeout(5):
            block = await runtime.owner.async_turn_context("Mia的钢琴课是星期几？", Speaker(), "m4")
    embed.assert_awaited_once()
    # Today's notes are still there: the name was enough to find this one.
    assert "Mia has piano on Thursdays." in block["notes"]


async def test_notes_by_meaning_keep_private_memories_private(hass: HomeAssistant, memory_setup) -> None:
    _agent, runtime, _client, _will = memory_setup
    private = "Mia is seeing a counsellor at school."

    def seed() -> int:
        mia = runtime.store.resolve_entities([("Mia", "person")])[0]
        runtime.store.insert_memory(NewMemory(kind="fact", content=private, source="chat", sensitivity="private",
                                              about_entity_ids=[mia]))
        return mia

    mia = await hass.async_add_executor_job(seed)
    await runtime.owner.async_refresh_entities()
    await runtime.owner.async_backfill_embeddings()
    question = "学校里最近有什么事吗？"

    with patch.object(runtime.api, "embed", _embeds_as(private)):
        block = await runtime.owner.async_turn_context(question, Speaker(), "m5")
        assert private not in block["notes"]

        rows = await runtime.owner._async_meaning_recall(question, mia)
        assert [row.content for row in rows] == [private]  # the person it's about gets it

        runtime.config = dataclasses.replace(runtime.config, share_household=True)
        block = await runtime.owner.async_turn_context(question, Speaker(), "m5")
        assert private in block["notes"]


async def test_notes_by_meaning_leave_out_what_the_profile_holds(hass: HomeAssistant, memory_setup) -> None:
    _agent, runtime, _client, _will = memory_setup
    allergy = "The household keeps no peanuts at home because of an allergy."
    await _seed_for_meaning(hass, runtime, NewMemory(kind="fact", content=allergy, source="chat", importance=5))

    with patch.object(runtime.api, "embed", _embeds_as(allergy)):
        block = await runtime.owner.async_turn_context("家里有什么不能吃的吗？", Speaker(), "m6")
    assert allergy in block["profile"]
    assert allergy not in block["notes"]


@pytest.mark.parametrize(
    "message",
    ["我们出门一般用什么交通方式？", "Do we usually prefer driving a car when leaving the house?"],
    ids=["by_meaning", "by_keyword"],
)
async def test_a_memory_the_profile_has_no_room_for_can_be_a_note(hass: HomeAssistant, memory_setup, message) -> None:
    _agent, runtime, _client, _will = memory_setup
    driving = "When leaving the house, the household prefers driving a car unless it is a short walk."
    # The profile fetches 12 memories, most important and newest first, and shows what fits in its characters.
    longer = [
        NewMemory(kind="fact", source="chat", importance=5,
                  content=f"Memory {number} is long enough to use up its share of the profile, the way real ones do.")
        for number in range(11)
    ]
    await _seed_for_meaning(hass, runtime, *longer, NewMemory(kind="fact", content=driving, source="chat", importance=4))

    with patch.object(runtime.api, "embed", _embeds_as(driving)):
        block = await runtime.owner.async_turn_context(message, Speaker(), "m7")
    assert longer[-1].content in block["profile"]
    assert driving not in block["profile"]
    assert driving in block["notes"]


async def test_owner_turn_remembers_recalls_and_is_archived(hass: HomeAssistant, memory_setup) -> None:
    agent, runtime, client, will = memory_setup
    client.responses = [
        tool_calls("c1", [tool_call(
            "remember_memory",
            # A smuggled _turn must not count: identity comes from Home Assistant.
            {"content": "Mia's favorite color is blue", "kind": "preference", "about": ["Mia"], "_turn": "guest"},
            embed="Mia's favorite color is blue",
        )]),
        final("c1", "Got it, Mia's favorite color is blue."),
    ]

    result = await agent.async_process(_input("Remember that Mia's favorite color is blue", "c1", user_id=will.id))
    await _settle(hass)

    assert result.response.speech["plain"]["speech"] == "Got it, Mia's favorite color is blue."
    memory = client.payloads[0]["context"]["memory"]
    assert memory["status"] == "enabled" and memory["speaker"] == {"name": "Will", "source": "ha_user"}
    assert {"name": "Mia", "kind": "person"} in memory["household"]
    assert "user_preference_keys" not in client.payloads[0]["context"]
    tool_result = client.payloads[1]["tool_results"][0]["result"]
    assert tool_result["success"] is True and tool_result["about"] == ["Mia"]

    [fact] = _db(runtime, """SELECT m.content, m.source, m.speaker_name, m.embedding_model,
                                    ARRAY(SELECT en.name FROM auri_memory.mem_edges e JOIN auri_memory.mem_entities en
                                          ON en.id = e.to_entity WHERE e.from_memory = m.id) AS about
                             FROM auri_memory.mem_memories m""")
    assert fact == {"content": "Mia's favorite color is blue", "source": "chat", "speaker_name": "Will",
                    "embedding_model": "fake-embed@768", "about": ["Mia"]}
    archived = _db(runtime, "SELECT role, kind, speaker_name, tool_names FROM auri_memory.chat_messages ORDER BY id")
    assert archived == [
        {"role": "user", "kind": "memory_command", "speaker_name": "Will", "tool_names": ["remember_memory"]},
        {"role": "assistant", "kind": "memory_command", "speaker_name": None, "tool_names": ["remember_memory"]},
    ]

    # A later turn gets the memory as notes, without any tool call.
    client.responses = [final("c2", "Blue.")]
    await agent.async_process(_input("What is Mia's favorite color these days?", "c2", user_id=will.id))
    assert "Mia's favorite color is blue" in client.payloads[-1]["context"]["memory"]["notes"]

    client.responses = [
        tool_calls("c3", [tool_call("recall_memory", {"query": "Mia favorite color"}, embed="Mia favorite color")]),
        final("c3", "Blue."),
    ]
    await agent.async_process(_input("Do you remember Mia's color?", "c3", user_id=will.id))
    recalled = client.payloads[-1]["tool_results"][0]["result"]
    assert recalled["count"] == 1 and "blue" in recalled["results"]


async def test_a_correction_retires_the_note_it_was_shown(hass: HomeAssistant, memory_setup) -> None:
    agent, runtime, client, will = memory_setup
    store = runtime.store

    def seed():
        [mia] = store.resolve_entities([("Mia", "person")])
        old, _ = store.insert_memory(NewMemory(kind="fact", content="Mia's piano lesson is on Tuesdays.", source="chat",
                                               importance=4, about_entity_ids=[mia]))
        unrelated, _ = store.insert_memory(NewMemory(kind="fact", content="The recycling goes out on Tuesday nights.",
                                                     source="chat", importance=2))
        return old, unrelated

    old_id, unrelated_id = await hass.async_add_executor_job(seed)
    correction = {"content": "Mia's piano lesson is on Thursdays at 4 pm.", "kind": "fact", "about": ["Mia"],
                  # The unrelated note exists but was never shown in this conversation.
                  "replaces": [old_id, f"#{unrelated_id}"]}
    client.responses = [tool_calls("p1", [tool_call("remember_memory", correction)]), final("p1", "Updated.")]

    await agent.async_process(_input("Mia's piano lesson moved to Thursdays at 4", "p1", user_id=will.id))
    await _settle(hass)

    assert f"[#{old_id} ·" in client.payloads[0]["context"]["memory"]["notes"]
    result = client.payloads[1]["tool_results"][0]["result"]
    assert result["replaced"] == ["Mia's piano lesson is on Tuesdays."]
    assert result["not_replaced"] == [unrelated_id]
    statuses = {row["id"]: row["status"] for row in _db(runtime, "SELECT id, status FROM auri_memory.mem_memories")}
    assert statuses[old_id] == "superseded" and statuses[unrelated_id] == "current"

    # From the next message on, the old day only shows up as history.
    client.responses = [final("p2", "Thursdays at 4.")]
    await agent.async_process(_input("When is Mia's piano lesson now?", "p2", user_id=will.id))
    notes = client.payloads[-1]["context"]["memory"]["notes"]
    assert "Mia's piano lesson is on Thursdays at 4 pm." in notes
    assert '(replaced: "Mia\'s piano lesson is on Tuesdays."' in notes


async def test_appointments_are_saved_with_their_actual_date(hass: HomeAssistant, memory_setup) -> None:
    agent, runtime, client, will = memory_setup
    due = dt_util.now().date() + timedelta(days=2)
    weekday = due.strftime("%A")
    attempts = [
        {"content": f"Will has a piano lesson this {weekday}.", "kind": "commitment", "due_date": due.isoformat()},
        {"content": f"Will has a piano lesson on {weekday}.", "kind": "commitment"},
        {"content": f"Will has a piano lesson on {weekday}, {due.isoformat()}.", "kind": "commitment", "due_date": due.isoformat()},
    ]
    client.responses = [tool_calls("d1", [tool_call("remember_memory", attempt)]) for attempt in attempts]
    client.responses.append(final("d1", "Saved."))

    await agent.async_process(_input(f"Remember I have a piano lesson this {weekday}", "d1", user_id=will.id))

    first, second, third = (payload["tool_results"][0]["result"] for payload in client.payloads[1:4])
    assert f"this {weekday}" in first["retry"] and "today is" in first["retry"]
    assert "due_date" in second["retry"]
    assert third["success"] is True
    [saved] = _db(runtime, "SELECT content, due_at FROM auri_memory.mem_memories WHERE kind = 'commitment'")
    assert dt_util.as_local(saved["due_at"]).date() == due

    client.responses = [final("d2", "Your piano lesson.")]
    await agent.async_process(_input(f"What do I have on {weekday}?", "d2", user_id=will.id))
    memory = client.payloads[-1]["context"]["memory"]
    assert f"due {due.strftime('%a')} {due.isoformat()} ·" in memory["profile"] + memory["notes"]


async def test_a_routine_is_saved_as_a_fact_and_comes_up_for_its_day(hass: HomeAssistant, memory_setup) -> None:
    agent, runtime, client, will = memory_setup
    as_commitment = {"content": "Will has a piano lesson every Thursday at 3 pm.", "kind": "commitment",
                     "due_date": (dt_util.now().date() + timedelta(days=2)).isoformat()}
    as_fact = {"content": "Will has a piano lesson every Thursday at 3 pm.", "kind": "fact"}
    client.responses = [
        tool_calls("r1", [tool_call("remember_memory", as_commitment)]),
        tool_calls("r1", [tool_call("remember_memory", as_fact)]),
        final("r1", "Got it."),
    ]

    await agent.async_process(_input("Remember I have piano lesson every Thursday at 3", "r1", user_id=will.id))

    first, second = (payload["tool_results"][0]["result"] for payload in client.payloads[1:3])
    assert "every Thursday" in first["retry"] and "fact or preference" in first["retry"]
    assert second["success"] is True
    [saved] = _db(runtime, "SELECT kind, due_at, expires_at FROM auri_memory.mem_memories")
    assert saved == {"kind": "fact", "due_at": None, "expires_at": None}

    client.responses = [final("r2", "Piano at 3.")]
    await agent.async_process(_input("What do I have this Thursday?", "r2", user_id=will.id))
    memory = client.payloads[-1]["context"]["memory"]
    assert "Will has a piano lesson every Thursday at 3 pm." in memory["profile"] + memory["notes"]


async def test_guest_mode_gets_no_memory_and_isnt_archived(hass: HomeAssistant, memory_setup) -> None:
    agent, runtime, client, will = memory_setup
    client.responses = [
        tool_calls("g1", [tool_call("remember_memory", {"content": "The guest likes jazz", "kind": "preference"})],
                   mode="guest"),
        final("g1", "Okay.", mode="guest"),
    ]

    await agent.async_process(_input("Remember I like jazz", "g1", user_id=will.id))
    await _settle(hass)

    assert client.payloads[1]["tool_results"][0]["result"]["error"] == "memory_unavailable"
    assert _db(runtime, "SELECT count(*) AS n FROM auri_memory.mem_memories")[0]["n"] == 0
    assert _db(runtime, "SELECT count(*) AS n FROM auri_memory.chat_messages")[0]["n"] == 0

    # Once the account is in guest mode, the next turn carries no notes at all.
    client.responses = [final("g2", "Hi.", mode="guest")]
    await agent.async_process(_input("What do you remember about Mia?", "g2", user_id=will.id))
    assert "notes" not in client.payloads[-1]["context"]["memory"]


async def test_satellite_speaker_is_asked_once_then_remembered(hass: HomeAssistant, memory_setup) -> None:
    agent, runtime, client, _ = memory_setup
    remember = {"content": "The speaker likes oat milk", "kind": "preference"}
    client.responses = [tool_calls("s1", [tool_call("remember_memory", remember)]), final("s1", "Who is this for?")]
    await agent.async_process(_input("Remember I like oat milk", "s1", device_id="satellite-1"))
    first = client.payloads[1]["tool_results"][0]["result"]
    assert first["needs_person"] is True and "Mia" in first["household_members"]

    client.responses = [
        tool_calls("s1", [tool_call("remember_memory", {**remember, "content": "Mia likes oat milk", "about": ["Mia"]})]),
        final("s1", "Saved for Mia."),
    ]
    await agent.async_process(_input("It's Mia", "s1", device_id="satellite-1"))
    assert client.payloads[-1]["tool_results"][0]["result"]["success"] is True

    # Mia said who she is, so her next personal fact is saved without asking.
    client.responses = [
        tool_calls("s1", [tool_call("remember_memory", {"content": "The speaker drinks tea at night", "kind": "preference"})]),
        final("s1", "Noted."),
    ]
    await agent.async_process(_input("Also I drink tea at night", "s1", device_id="satellite-1"))
    saved = client.payloads[-1]["tool_results"][0]["result"]
    assert saved["saved"] == "Mia drinks tea at night" and saved["about"] == ["Mia"]
    assert client.payloads[-2]["context"]["memory"]["stated_speaker"] == "Mia"

    await _settle(hass)
    speakers = {row["speaker_name"] for row in _db(runtime, "SELECT speaker_name FROM auri_memory.chat_messages WHERE role = 'user'")}
    assert speakers == {None, "Mia"}


async def test_someone_who_isnt_a_home_assistant_person(hass: HomeAssistant, memory_setup) -> None:
    agent, runtime, client, will = memory_setup
    # Signed in: saved about her without asking, though Home Assistant doesn't know her.
    client.responses = [
        tool_calls("l1", [tool_call("remember_memory", {"content": "Lillian likes candy", "kind": "preference", "about": ["Lillian"]})]),
        final("l1", "Got it."),
    ]
    await agent.async_process(_input("Remember Lillian likes candy", "l1", user_id=will.id))
    assert client.payloads[-1]["tool_results"][0]["result"]["about"] == ["Lillian"]
    [lillian] = _db(runtime, "SELECT id, kind FROM auri_memory.mem_entities WHERE name = 'Lillian'")
    assert lillian["kind"] == "topic"

    # On a satellite she answers "Who should I remember this for?": now she's a person, and the speaker.
    remember = {"content": "The speaker likes chocolate", "kind": "preference"}
    client.responses = [tool_calls("s2", [tool_call("remember_memory", remember)]), final("s2", "Who is this for?")]
    await agent.async_process(_input("Remember I like chocolate", "s2", device_id="satellite-1"))
    assert client.payloads[-1]["tool_results"][0]["result"]["needs_person"] is True
    client.responses = [
        tool_calls("s2", [tool_call("remember_memory", {**remember, "content": "Lillian likes chocolate", "about": ["Lillian"]})]),
        final("s2", "Saved for Lillian."),
    ]
    await agent.async_process(_input("It's Lillian", "s2", device_id="satellite-1"))
    assert client.payloads[-1]["tool_results"][0]["result"]["about"] == ["Lillian"]
    assert _db(runtime, "SELECT id, kind FROM auri_memory.mem_entities WHERE name = 'Lillian'") == [
        {"id": lillian["id"], "kind": "person"}
    ]

    # Only the answer names the speaker: a fact about Mia afterwards doesn't make Mia the speaker.
    for text, arguments in (
        ("Remember Mia likes cats", {"content": "Mia likes cats", "kind": "preference", "about": ["Mia"]}),
        ("Also I drink tea at night", {"content": "The speaker drinks tea at night", "kind": "preference"}),
    ):
        client.responses = [tool_calls("s2", [tool_call("remember_memory", arguments)]), final("s2", "Noted.")]
        await agent.async_process(_input(text, "s2", device_id="satellite-1"))
    saved = client.payloads[-1]["tool_results"][0]["result"]
    assert saved["saved"] == "Lillian drinks tea at night" and saved["about"] == ["Lillian"]
    assert client.payloads[-2]["context"]["memory"]["stated_speaker"] == "Lillian"


async def test_a_login_used_by_someone_else(hass: HomeAssistant, memory_setup) -> None:
    agent, runtime, client, will = memory_setup
    admin = await hass.auth.async_create_user("Admin")
    [mia] = await hass.async_add_executor_job(runtime.store.resolve_entities, [("Mia", "person")])
    await hass.async_add_executor_job(
        runtime.store.insert_memory,
        NewMemory(kind="fact", content="Mia is lactose intolerant.", source="chat", importance=5, about_entity_ids=[mia]),
    )

    client.responses = [final("l1", "Morning.")]
    await agent.async_process(_input("Good morning", "l1", user_id=admin.id))
    memory = client.payloads[-1]["context"]["memory"]
    assert memory["speaker"] == {"name": "Admin", "source": "ha_user"} and "lactose" not in memory["profile"]

    # Mia signs in as admin; Will's own login now sits on a shared wall tablet.
    hass.config_entries.async_update_entry(
        agent.entry, options={**agent.entry.options, CONF_MEMORY_LOGIN_PEOPLE: {admin.id: "person.mia", will.id: "shared"}}
    )
    client.responses = [final("l2", "Morning."), final("l3", "Morning.")]
    await agent.async_process(_input("Good morning", "l2", user_id=admin.id))
    memory = client.payloads[-1]["context"]["memory"]
    assert memory["speaker"] == {"name": "Mia", "source": "ha_user"} and "Mia is lactose intolerant." in memory["profile"]

    await agent.async_process(_input("Good morning", "l3", user_id=will.id))
    assert client.payloads[-1]["context"]["memory"]["speaker"] == {"name": None, "source": "unknown"}


async def test_a_login_that_isnt_a_person(hass: HomeAssistant, memory_setup) -> None:
    agent, runtime, client, will = memory_setup
    mqtt = await hass.auth.async_create_user("MQTT-user")
    hass.states.async_set("person.mqtt_user", "unknown", {"friendly_name": "MQTT-user", "user_id": mqtt.id})

    client.responses = [final("n1", "Morning.")]
    await agent.async_process(_input("Good morning", "n1", user_id=will.id))
    assert {"name": "MQTT-user", "kind": "person"} in client.payloads[-1]["context"]["memory"]["household"]

    hass.config_entries.async_update_entry(
        agent.entry, options={**agent.entry.options, CONF_MEMORY_LOGIN_PEOPLE: {mqtt.id: "not_a_person"}}
    )
    client.responses = [final("n2", "Morning."), final("n3", "Morning.")]
    await agent.async_process(_input("Good morning", "n2", user_id=will.id))
    household = client.payloads[-1]["context"]["memory"]["household"]
    assert {member["name"] for member in household} == {"Will", "Mia"}
    assert "MQTT-user" not in runtime.household_names()

    # Nobody talks through it, but if something does, it's nobody in particular.
    await agent.async_process(_input("Good morning", "n3", user_id=mqtt.id))
    assert client.payloads[-1]["context"]["memory"]["speaker"] == {"name": None, "source": "unknown"}


_GUEST = {
    "guest_memory_enabled": True,
    "booking_id": "B-1",
    "guest_name": "Sarah Lee",
    "guest_count": 1,
    "check_in": "2026-01-01 15:00:00",
    "check_out": "2099-01-01 11:00:00",
}


async def _set_guest_options(hass: HomeAssistant, agent, runtime: MemoryRuntime, **changes: Any) -> None:
    """Save Guest options as the options flow would; the integration's update listener then tells the runtime."""
    hass.config_entries.async_update_entry(agent.entry, options={**agent.entry.options, **changes})
    await runtime.async_options_updated(dict(agent.entry.options))


def _guest_facts(runtime: MemoryRuntime) -> set[str]:
    return {
        row["content"]
        for row in _db(runtime, "SELECT content FROM auri_guest_memory.mem_memories WHERE status = 'current' AND kind = 'fact'")
    }


async def test_guests_and_owners_never_share_memories(hass: HomeAssistant, memory_setup) -> None:
    agent, runtime, client, will = memory_setup
    [mia] = await hass.async_add_executor_job(runtime.store.resolve_entities, [("Mia", "person")])
    await hass.async_add_executor_job(
        runtime.store.insert_memory,
        NewMemory(kind="fact", content="Mia is lactose intolerant.", source="chat", importance=5, about_entity_ids=[mia]),
    )
    await _set_guest_options(hass, agent, runtime, **_GUEST)
    assert "Sarah Lee booked this stay." in _guest_facts(runtime) and len(_guest_facts(runtime)) == 3

    # The first reply says the account is in guest mode; from then on each turn gets the guests' memory only.
    client.responses = [final("t1", "Hi.", mode="guest", allowed=True), final("t2", "Welcome.", mode="guest", allowed=True)]
    await agent.async_process(_input("Hello there", "t1", device_id="satellite-1"))
    await agent.async_process(_input("Good morning", "t2", device_id="satellite-1"))
    memory = client.payloads[-1]["context"]["memory"]
    assert memory["scope"] == "guest" and memory["party_size"] == 1
    assert memory["household"] == [{"name": "Sarah Lee", "kind": "person"}]
    assert "Sarah Lee booked this stay." in memory["profile"] and "lactose" not in memory["profile"]

    # A guest's "remember": the only guest is who's speaking, and it goes to the guests' memory.
    client.responses = [
        tool_calls("t3", [tool_call("remember_memory", {"content": "The speaker parked in spot 12", "kind": "fact"})],
                   mode="guest", allowed=True),
        final("t3", "Got it.", mode="guest", allowed=True),
    ]
    await agent.async_process(_input("Remember I parked in spot 12", "t3", device_id="satellite-1"))
    saved = client.payloads[-1]["tool_results"][0]["result"]
    assert saved["saved"] == "Sarah Lee parked in spot 12" and saved["about"] == ["Sarah Lee"]

    # And a guest's search never reaches the owner's memory.
    client.responses = [
        tool_calls("t4", [tool_call("recall_memory", {"query": "lactose intolerant milk"})], mode="guest", allowed=True),
        final("t4", "I have no record of that.", mode="guest", allowed=True),
    ]
    await agent.async_process(_input("Is anyone lactose intolerant?", "t4", device_id="satellite-1"))
    assert client.payloads[-1]["tool_results"][0]["result"]["count"] == 0
    assert "lactose" not in client.payloads[-2]["context"]["memory"]["notes"]

    await _settle(hass)
    assert _db(runtime, "SELECT count(*) AS n FROM auri_memory.mem_memories WHERE content LIKE '%spot 12%'")[0]["n"] == 0
    assert _db(runtime, "SELECT count(*) AS n FROM auri_memory.chat_messages")[0]["n"] == 0
    assert _db(runtime, "SELECT count(*) AS n FROM auri_guest_memory.chat_messages WHERE role = 'user'")[0]["n"] == 4

    # Back in owner mode: the household's memory again, and nothing of the guests'.
    client.responses = [final("t5", "Hi."), final("t6", "Morning.")]
    await agent.async_process(_input("Hello", "t5", user_id=will.id))
    await agent.async_process(_input("Good morning", "t6", user_id=will.id))
    memory = client.payloads[-1]["context"]["memory"]
    assert memory["scope"] == "owner" and "Sarah" not in memory["profile"]
    assert all(member["name"] != "Sarah Lee" for member in memory["household"])


async def test_guest_memory_starts_again_and_is_erased(hass: HomeAssistant, memory_setup) -> None:
    agent, runtime, _, _ = memory_setup
    await _set_guest_options(hass, agent, runtime, **_GUEST)
    await hass.async_add_executor_job(
        runtime.guest.store.insert_memory, NewMemory(kind="fact", content="The guests parked in spot 12.", source="chat")
    )
    assert "The guests parked in spot 12." in _guest_facts(runtime)

    # Saving Guest options for the same stay keeps what the guests said.
    await _set_guest_options(hass, agent, runtime, notes="Celebrating an anniversary")
    assert "The guests parked in spot 12." in _guest_facts(runtime)

    # The host's reset, ticked in the Guest options form.
    with patch("homeassistant.config_entries.async_process_deps_reqs", AsyncMock()):
        result = await hass.config_entries.options.async_init(agent.entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "guest"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"reset_guest_memory": True})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert "guest_memory_reset_at" in agent.entry.options and "reset_guest_memory" not in agent.entry.options
    await runtime.async_options_updated(dict(agent.entry.options))
    assert _guest_facts(runtime) == {
        "Sarah Lee booked this stay.",
        "1 guest is staying.",
        next(fact for fact in _guest_facts(runtime) if fact.startswith("The stay runs from")),
        "The host's note about this stay: Celebrating an anniversary",
    }

    # A new booking starts again with its own guest.
    await _set_guest_options(hass, agent, runtime, booking_id="B-2", guest_name="Tom", notes="")
    assert "Tom booked this stay." in _guest_facts(runtime) and "Sarah Lee booked this stay." not in _guest_facts(runtime)

    # After check-out it's erased, and stays empty and closed.
    await _set_guest_options(hass, agent, runtime, check_out="2020-01-01 11:00:00")
    assert _guest_facts(runtime) == set() and runtime.status_attributes()["guest_memory_erased"]
    await _set_guest_options(hass, agent, runtime, notes="")
    assert _guest_facts(runtime) == set()
    assert runtime.status_attributes()["guest_memory"] == "closed" and not runtime.guest.enabled
    # Ticking reset after the stay has ended doesn't open it again.
    await _set_guest_options(hass, agent, runtime, guest_memory_reset_at="2026-10-23T12:00:00+00:00")
    assert _guest_facts(runtime) == set() and not runtime.guest.enabled

    # Switching guest memory off removes it altogether; the owner's memory stays.
    await _set_guest_options(hass, agent, runtime, guest_memory_enabled=False)
    assert _db(runtime, "SELECT count(*) AS n FROM pg_namespace WHERE nspname = 'auri_guest_memory'")[0]["n"] == 0
    assert runtime.status_attributes()["guest_memory"] == "off" and runtime.store is not None


def _local(moment) -> str:
    """A Guest Settings date and time (local, as the form stores it)."""
    return dt_util.as_local(moment).strftime("%Y-%m-%d %H:%M:%S")


async def _guest_turns(hass: HomeAssistant, agent, client, *texts: str) -> None:
    client.responses = [final(f"g{index}", "OK.", mode="guest", allowed=True) for index, _ in enumerate(texts)]
    for index, text in enumerate(texts):
        await agent.async_process(_input(text, f"g{index}", device_id="satellite-1"))
    await _settle(hass)


async def test_guest_memory_is_erased_an_hour_after_check_out(hass: HomeAssistant, memory_setup, freezer) -> None:
    agent, runtime, client, _ = memory_setup
    check_out = dt_util.now().replace(microsecond=0) + timedelta(minutes=10)
    await _set_guest_options(hass, agent, runtime, **{**_GUEST, "check_out": _local(check_out)})
    await hass.async_add_executor_job(
        runtime.guest.store.insert_memory, NewMemory(kind="fact", content="The guests parked in spot 12.", source="chat")
    )

    # Guests leaving a little late still have their memory, and what they say is kept.
    freezer.move_to(check_out + timedelta(minutes=30))
    async_fire_time_changed(hass)
    await _guest_turns(hass, agent, client, "Hello", "Where did we park?")
    assert "The guests parked in spot 12." in _guest_facts(runtime) and runtime.guest.enabled
    assert client.payloads[-1]["context"]["memory"]["status"] == "enabled"

    # An hour after check-out the timer erases it.
    assert runtime._erase_unsub is not None  # noqa: SLF001
    freezer.move_to(check_out + timedelta(hours=1, seconds=1))
    async_fire_time_changed(hass)
    await _settle(hass)
    assert runtime._erase_unsub is None  # noqa: SLF001  (the timer fired)
    assert _guest_facts(runtime) == set()
    assert _db(runtime, "SELECT count(*) AS n FROM auri_guest_memory.chat_messages")[0]["n"] == 0
    assert runtime.status_attributes()["guest_memory"] == "closed"

    # Closed until the next stay: no memory for guests, and nothing they say is kept.
    await _guest_turns(hass, agent, client, "Hello again", "Remember we parked in spot 9")
    assert client.payloads[-1]["context"]["memory"]["status"] == "not_configured"
    assert _db(runtime, "SELECT count(*) AS n FROM auri_guest_memory.chat_messages")[0]["n"] == 0

    # The next booking opens it again, starting from its own guest details.
    next_check_out = check_out + timedelta(days=3)
    await _set_guest_options(hass, agent, runtime, booking_id="B-2", guest_name="Tom", check_out=_local(next_check_out))
    assert "Tom booked this stay." in _guest_facts(runtime) and runtime.guest.enabled


async def test_a_missed_guest_erase_happens_at_the_next_check(hass: HomeAssistant, memory_setup, freezer) -> None:
    agent, runtime, _, _ = memory_setup
    check_out = dt_util.now().replace(microsecond=0) + timedelta(minutes=10)
    await _set_guest_options(hass, agent, runtime, **{**_GUEST, "check_out": _local(check_out)})
    # As if the database was down when the erase time came.
    runtime._erase_unsub()  # noqa: SLF001
    runtime._erase_unsub = None  # noqa: SLF001

    freezer.move_to(check_out + timedelta(hours=1, minutes=1))
    # Closed from the erase time on, even before it's erased: nothing more is recalled or kept.
    assert not runtime.guest.enabled and "Sarah Lee booked this stay." in _guest_facts(runtime)

    # With no erase timer left, the 15-minute check erases it.
    async_fire_time_changed(hass)
    await _settle(hass)
    assert _guest_facts(runtime) == set() and runtime.status_attributes()["guest_memory"] == "closed"


async def test_the_guests_nightly_run_and_an_erase_take_turns(hass: HomeAssistant, memory_setup) -> None:
    agent, runtime, _, _ = memory_setup
    await _set_guest_options(hass, agent, runtime, **_GUEST)
    seen: list[tuple[str, bool]] = []

    async def fake_run(space, **_kwargs):
        seen.append((space.mode, runtime._guest_lock.locked()))  # noqa: SLF001
        return {"status": "skipped"}

    with patch.object(runtime, "_async_run_space_nightly", side_effect=fake_run):
        await runtime.async_run_nightly()
    assert seen == [("owner", False), ("guest", True)]


async def test_guest_memory_needs_memory_set_up(hass: HomeAssistant) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    with patch("homeassistant.config_entries.async_process_deps_reqs", AsyncMock()):
        result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "guest"})
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"guest_memory_enabled": True, "check_in": "2026-01-01 15:00:00", "check_out": "2099-01-01 11:00:00"},
    )
    assert result["errors"] == {"guest_memory_enabled": "guest_memory_needs_memory"}


async def test_a_household_that_shares_its_memories(hass: HomeAssistant, memory_setup) -> None:
    agent, runtime, client, _ = memory_setup
    [mia] = await hass.async_add_executor_job(runtime.store.resolve_entities, [("Mia", "person")])
    await hass.async_add_executor_job(
        runtime.store.insert_memory,
        NewMemory(kind="fact", content="Mia is lactose intolerant.", source="nightly", importance=5,
                  sensitivity="private", about_entity_ids=[mia]),
    )
    client.responses = [final("h1", "Sure.")]
    await agent.async_process(_input("Can I have milk for breakfast?", "h1", device_id="satellite-1"))
    assert "lactose" not in client.payloads[-1]["context"]["memory"]["profile"]

    store = runtime.store
    hass.config_entries.async_update_entry(agent.entry, options={**agent.entry.options, CONF_MEMORY_SHARE_HOUSEHOLD: True})
    await runtime.async_options_updated(dict(agent.entry.options))  # what the integration's update listener does
    assert runtime.store is store  # applied without restarting memory

    client.responses = [final("h2", "Better not.")]
    await agent.async_process(_input("Can I have milk for breakfast?", "h2", device_id="satellite-1"))
    profile = client.payloads[-1]["context"]["memory"]["profile"]
    assert "Mia is lactose intolerant." in profile and "private" not in profile

    client.responses = [
        tool_calls("h3", [tool_call("recall_memory", {"query": "Mia milk lactose"})]),
        final("h3", "Mia is lactose intolerant."),
    ]
    await agent.async_process(_input("What can't Mia drink?", "h3", device_id="satellite-1"))
    result = client.payloads[-1]["tool_results"][0]["result"]
    assert "Mia is lactose intolerant." in result["results"] and "private" not in result["results"]
    assert "Private notes" not in result["guidance"]


async def test_exam_and_automation_turns(hass: HomeAssistant, memory_setup) -> None:
    agent, runtime, client, will = memory_setup
    client.responses = [final("auri-exam-1", "Blue.")]
    await agent.async_process(_input("What is Mia's favorite color?", "auri-exam-1", user_id=will.id))
    client.responses = [final("a1", "Lights are off.")]
    await agent.async_process(_input("Turn off the lights at night", "a1"))
    await _settle(hass)

    archived = _db(runtime, "SELECT conversation_id, kind FROM auri_memory.chat_messages WHERE role = 'user'")
    # The exam isn't archived; an automation's turn is, marked so the nightly job skips it.
    assert archived == [{"conversation_id": "a1", "kind": "automatic"}]


# ── the memory exam ─────────────────────────────────────────────────────────


@contextmanager
def _exam_through(agent: ThinOpenAIAgent, config_dir, converse=None):
    """Ask the exam's questions through this agent (or converse), reading its files from config_dir."""

    async def through_agent(_hass, text, conversation_id, _context, **_kwargs):
        return await agent.async_process(_input(text, conversation_id))

    with (
        patch.object(agent.hass.config, "config_dir", str(config_dir)),
        patch.object(exam.conversation, "async_converse", converse or through_agent),
    ):
        yield


def _graded(*grades: str) -> AsyncMock:
    """The backend's /memory/grade, grading the answers in turn."""
    return AsyncMock(return_value={"grades": [{"idx": idx, "grade": grade} for idx, grade in enumerate(grades)], "usage": {}})


def _schema_exists(runtime: MemoryRuntime, schema: str) -> bool:
    return bool(_db(runtime, f"SELECT count(*) AS n FROM pg_namespace WHERE nspname = '{schema}'")[0]["n"])


async def test_the_exam_shows_where_each_answers_memory_reached_auri(hass: HomeAssistant, memory_setup, tmp_path) -> None:
    agent, runtime, client, _will = memory_setup
    store = runtime.store

    def seed() -> None:
        will, mia = store.resolve_entities([("Will", "person"), ("Mia", "person")])
        store.insert_memory(NewMemory(kind="fact", content="Mia has piano lessons every Thursday at 16:00.", source="chat",
                                      importance=4, about_entity_ids=[mia]))
        store.insert_memory(NewMemory(kind="fact", content="Will wants to buy a Volvo XC40.", source="chat",
                                      about_entity_ids=[will]))

    await hass.async_add_executor_job(seed)
    await runtime.owner.async_refresh_entities()
    await runtime.owner.async_backfill_embeddings()
    (tmp_path / MEMORY_EXAM_FILE).write_text(
        "- q: What day is Mia's piano lesson?\n  expected: Thursday.\n  memory: Mia piano Thursday\n  outdated: Thursday\n"
        "  case: direct\n"
        "- q: Which car is Will thinking of getting?\n  expected: A Volvo XC40.\n  memory: Volvo\n  outdated: Mia piano\n"
        "- q: What's the Wi-Fi password?\n  expected: HomeNet.\n  memory: wifi password\n"
        "- q: Is it raining?\n  expected: No.\n",
        encoding="utf-8",
    )
    query = "car Will is thinking of getting"
    client.responses = [
        final("x", "Thursday."),
        # The car isn't in the notes, so Auri looks it up.
        tool_calls("x", [tool_call("recall_memory", {"query": query, "about": ["Will"]}, embed=query)]),
        final("x", "A Volvo XC40."),
        final("x", "I don't know it."),
        final("x", "No."),
    ]

    with _exam_through(agent, tmp_path), patch.object(runtime.api, "grade", _graded("correct", "correct", "wrong", "correct")) as grade:
        result = await runtime.async_run_exam()

    found = [(item.get("memory_found_in"), item.get("memory_rank")) for item in result["grades"]]
    assert found == [("notes", 1), ("recall_memory", 1), ("none", None), (None, None)]
    # An outdated memory is looked for the same way (here, stand-ins that are or aren't shown).
    assert [item.get("outdated_found_in") for item in result["grades"]] == ["notes", "none", None, None]
    assert result["outdated"] == {
        "questions": 2, "shown": 1, "found_in": {"profile": 0, "notes": 1, "recall_memory": 0, "none": 1},
        "not_correct_when_shown": 0,
    }
    assert [item["searches"] for item in result["grades"]] == [0, 1, 0, 0]
    assert all(isinstance(item["seconds"], float) and item["seconds"] >= 0 for item in result["grades"])
    # One embedding call per question is timed; the fake backend answers it.
    assert all(isinstance(item["embed_seconds"], float) and item["embed_seconds"] >= 0 for item in result["grades"])

    # The question that searched shows what its search returned, after its grade.
    [volvo] = _db(runtime, "SELECT id FROM auri_memory.mem_memories WHERE content LIKE 'Will wants%'")
    car = result["grades"][1]
    assert list(car)[-1] == "search_results"
    [search] = car["search_results"]
    [returned] = search["memories"]
    assert (search["query"], search["results"], returned["id"]) == (query, 1, volvo["id"])
    assert returned["text"] == "Will wants to buy a Volvo XC40." and search["chars"] > len(returned["text"])

    # Each question also shows what a search by meaning would have added to the notes.
    notes = car["meaning_notes"]
    assert list(car)[-2:] == ["meaning_notes", "search_results"]
    assert (notes["memory_rank"], notes["memories"][0]["id"]) == (1, volvo["id"]) and notes["memory_similarity"] > 0
    assert result["grades"][2]["meaning_notes"]["memory_rank"] is None  # nothing about the Wi-Fi
    assert list(result["grades"][3]["meaning_notes"]) == ["memories"]  # it names no memory
    assert all(isinstance(returned[key], float) for key in ("score", "similarity", "keyword"))
    assert all("search_results" not in item for position, item in enumerate(result["grades"]) if position != 1)

    memory = result["memory"]
    timings = (
        memory.pop("seconds"), memory["by_case"]["direct"].pop("seconds"),
        memory.pop("embed_seconds"), memory["by_case"]["direct"].pop("embed_seconds"),
    )
    assert all(value >= 0 for value in timings)
    assert memory == {
        "questions": 3,
        "found": 0.6667,
        "mrr": 0.6667,
        "searches": 0.33,
        "results_per_search": 1.0,
        "chars_per_search": search["chars"],
        "found_in": {"profile": 0, "notes": 1, "recall_memory": 1, "none": 1},
        "not_correct": {"memory_shown": 0, "memory_not_shown": 1},
        "by_case": {"direct": {"questions": 1, "found": 1.0, "mrr": 1.0, "searches": 0.0}},
    }
    # The turns cover every question, also the one that names no memory.
    turns = result["turns"]
    assert {key: turns[key] for key in ("questions", "searches", "results_per_search", "chars_per_search")} == {
        "questions": 4, "searches": 0.25, "results_per_search": 1.0, "chars_per_search": search["chars"],
    }
    assert turns["seconds"] >= 0 and turns["embed_seconds"] >= 0
    assert "file" not in result and "test_memory" not in result
    # Only the questions and answers are sent to be graded.
    assert {key for item in grade.call_args.args[0] for key in item} == {"q", "expected", "answer"}
    assert len(runtime.owner.recall_trace) == 0

    # Other conversations are never traced.
    client.responses = [final("n1", "Thursday.")]
    await agent.async_process(_input("What day is Mia's piano lesson?", "n1"))
    assert len(runtime.owner.recall_trace) == 0


async def test_an_exam_can_bring_its_own_memories(hass: HomeAssistant, memory_setup, tmp_path) -> None:
    agent, runtime, client, _will = memory_setup
    store = runtime.store
    # The household's real memory answers the same question differently, and the exam must never see it.
    [mia] = await hass.async_add_executor_job(store.resolve_entities, [("Mia", "person")])
    await hass.async_add_executor_job(
        store.insert_memory,
        NewMemory(kind="fact", content="Mia has piano every Tuesday.", source="chat", importance=4, about_entity_ids=[mia]),
    )
    await runtime.owner.async_refresh_entities()
    real_memories = _db(runtime, "SELECT id, content, status FROM auri_memory.mem_memories ORDER BY id")
    (tmp_path / "benchmark.yaml").write_text(
        # Pipp is Pip spelled another way, which the household has said (same).
        "household:\n  people: [Ana, Mia]\n  pets: [Pip, Pipp]\n  same: [[Pip, Pipp]]\n"
        "memories:\n"
        "  - text: Mia has piano every Thursday at 16:00.\n    about: [Mia]\n    importance: 4\n"
        "  - text: Pip is allergic to chicken.\n    about: [Pip]\n"
        "questions:\n"
        "  - q: What day is Mia's piano lesson?\n    expected: Thursday.\n    memory: Mia piano Thursday\n"
        "  - q: What can't our dog eat?\n    expected: Chicken.\n    memory: Pip chicken\n",
        encoding="utf-8",
    )
    query = "what the dog can't eat"
    client.responses = [
        final("x", "Thursday."),
        final("n1", "Tuesday."),
        tool_calls("x", [tool_call("recall_memory", {"query": query, "about": ["Pip"]}, embed=query)]),
        final("x", "Chicken."),
    ]
    normal: dict[str, Any] = {}

    async def converse(_hass, text, conversation_id, _context, **_kwargs):
        result = await agent.async_process(_input(text, conversation_id))
        if not normal:
            # Someone talks to Auri in the middle of the exam: that's the real memory.
            await agent.async_process(_input("What day is Mia's piano lesson?", "n1"))
            normal.update(client.payloads[-1]["context"]["memory"])
        return result

    with _exam_through(agent, tmp_path, converse), patch.object(runtime.api, "grade", _graded("correct", "correct")):
        result = await runtime.async_run_exam("benchmark.yaml")

    asked = client.payloads[0]["context"]["memory"]
    assert "Thursday" in asked["notes"] and "Tuesday" not in asked["notes"]
    assert asked["household"] == [
        {"name": "Ana", "kind": "person"}, {"name": "Mia", "kind": "person"}, {"name": "Pip", "kind": "pet"}
    ]
    assert "Tuesday" in normal["notes"] and "Thursday" not in normal["notes"]
    assert {"name": "Will", "kind": "person"} in normal["household"]
    assert [(item["memory_found_in"], item["memory_rank"]) for item in result["grades"]] == [("notes", 1), ("recall_memory", 1)]
    # The search by meaning looks in the test memory too.
    shown = [memory["text"] for item in result["grades"] for memory in item["meaning_notes"]["memories"]]
    assert "Pip is allergic to chicken." in shown and "Mia has piano every Tuesday." not in shown
    assert result["file"] == "benchmark.yaml" and result["test_memory"] is True
    # The pair Repairs would ask about, which the file's answer made one pet (the household above lists Pip once).
    assert result["similar_names"] == ["Pip and Pipp"]
    # The test memory is gone, and the real memory is as it was.
    assert runtime.exam is None and not _schema_exists(runtime, EXAM_MEMORY_SCHEMA)
    assert _db(runtime, "SELECT id, content, status FROM auri_memory.mem_memories ORDER BY id") == real_memories

    # It's also gone when a question fails.
    async def unplugged(*_args, **_kwargs):
        raise HomeAssistantError("The satellite is unplugged")

    with _exam_through(agent, tmp_path, unplugged), pytest.raises(HomeAssistantError, match="unplugged"):
        await runtime.async_run_exam("benchmark.yaml")
    assert runtime.exam is None and not _schema_exists(runtime, EXAM_MEMORY_SCHEMA)


async def test_the_exam_puts_the_households_language_back(hass: HomeAssistant, memory_setup, tmp_path) -> None:
    agent, runtime, client, _will = memory_setup
    hass.config_entries.async_update_entry(runtime.entry, options={**runtime.entry.options, "stt_language": "zh"})
    (tmp_path / MEMORY_EXAM_FILE).write_text(
        "- q: ¿Cuál es la capital de Francia?\n  expected: Paris.\n- q: 法国的首都是哪里？\n  expected: Paris.\n",
        encoding="utf-8",
    )
    client.responses = [
        tool_calls("x", [tool_call("set_language", {"language": "es"})]),
        final("x", "París."),
        final("x", "巴黎。"),
    ]
    languages_heard: list[str] = []

    async def through_agent(_hass, text, conversation_id, _context, **_kwargs):
        languages_heard.append(runtime.entry.options["stt_language"])
        return await agent.async_process(_input(text, conversation_id))

    with _exam_through(agent, tmp_path, through_agent), patch.object(runtime.api, "grade", _graded("correct", "correct")):
        result = await runtime.async_run_exam()

    # It shows which question changed it, and the next question is asked in the household's language.
    assert [item.get("language_set") for item in result["grades"]] == ["es", None]
    assert languages_heard == ["zh", "zh"]
    assert runtime.entry.options["stt_language"] == "zh"

    # Also when a question fails halfway, or the language was never chosen.
    options = {key: value for key, value in runtime.entry.options.items() if key != "stt_language"}
    hass.config_entries.async_update_entry(runtime.entry, options=options)

    async def changed_it_and_failed(*_args, **_kwargs):
        hass.config_entries.async_update_entry(runtime.entry, options={**options, "stt_language": "es"})
        raise HomeAssistantError("The satellite is unplugged")

    with _exam_through(agent, tmp_path, changed_it_and_failed), pytest.raises(HomeAssistantError, match="unplugged"):
        await runtime.async_run_exam()
    assert "stt_language" not in runtime.entry.options
