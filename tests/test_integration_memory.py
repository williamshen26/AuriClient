"""Memory wired into Home Assistant: options flow, agent turns, tools and turn logging.

Uses pytest-homeassistant-custom-component (Linux only: Home Assistant's
test harness doesn't run on Windows) and the test PostgreSQL server.
"""
from __future__ import annotations

from datetime import timedelta
import json
import math
import re
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("pytest_homeassistant_custom_component.common")

from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed  # noqa: E402

from homeassistant.components import conversation  # noqa: E402
from homeassistant.core import Context, HomeAssistant  # noqa: E402
from homeassistant.data_entry_flow import FlowResultType  # noqa: E402
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
    CONF_MEMORY_SHARE_HOUSEHOLD,
    CONF_MEMORY_VAULT_PATH,
    CONF_SHARED_SECRET,
    DOMAIN,
)
from custom_components.hey_auri_client.conversation.agent_class import ThinOpenAIAgent  # noqa: E402
from custom_components.hey_auri_client.memory import turn_log  # noqa: E402
from custom_components.hey_auri_client.memory.runtime import MemoryRuntime, MemoryState  # noqa: E402
from custom_components.hey_auri_client.memory.store import NewMemory  # noqa: E402
from custom_components.hey_auri_client.saas_client import SaaSTurnResponse  # noqa: E402

_WORD = re.compile(r"[a-z0-9']+")


def fake_vector(text: str) -> list[float]:
    vector = [0.0] * 768
    for word in _WORD.findall(text.lower()):
        vector[hash(word) % 768] += 1.0
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

    # A login that isn't a person's: its person is no longer offered for the others.
    _result, fields = await open_form()
    assert "person.mqtt_user" not in {option["value"] for option in fields["Admin"].config["options"]}


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
