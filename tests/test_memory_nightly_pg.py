"""The nightly job end to end against PostgreSQL, with a scripted stand-in for /memory/*."""
from __future__ import annotations

import asyncio
from datetime import date, datetime, time, timedelta
import math
import os
import re
from types import SimpleNamespace
from typing import Any
import zoneinfo

import pytest

pytest.importorskip("homeassistant")

from homeassistant.util import dt as dt_util  # noqa: E402

from custom_components.hey_auri_client.const import EVENT_MEMORY_RUN_FINISHED  # noqa: E402
from custom_components.hey_auri_client.memory.api import MemoryApiError, embedding_tag  # noqa: E402
from custom_components.hey_auri_client.memory.nightly import NightlyJob, parse_speaker  # noqa: E402
from custom_components.hey_auri_client.memory.recall import normalize_statement  # noqa: E402
from custom_components.hey_auri_client.memory.store import MemoryStore, NewMemory, new_exchange_id  # noqa: E402
from custom_components.hey_auri_client.memory.transcript import current_day, day_window  # noqa: E402

TZ = zoneinfo.ZoneInfo("America/Vancouver")
TAG = embedding_tag("fake-embed", 768)
_WORD = re.compile(r"[a-z0-9']+")


def fake_vector(text: str) -> list[float]:
    """Bag-of-words vector: statements sharing words are close, like a real embedding."""
    vector = [0.0] * 768
    for word in _WORD.findall(text.lower()):
        vector[hash(word) % 768] += 1.0
    norm = math.sqrt(sum(v * v for v in vector)) or 1.0
    return [v / norm for v in vector]


def default_decision(item: dict[str, Any]) -> dict[str, Any]:
    """Skip what's already known, update when the statement says "now", else add."""
    for existing in item["existing"]:
        if normalize_statement(existing["statement"]) == normalize_statement(item["statement"]):
            return {"idx": item["idx"], "action": "skip", "replaces": [existing["id"]], "statement": ""}
    if " now " in f" {item['statement']} ":
        return {
            "idx": item["idx"],
            "action": "update",
            "replaces": [item["existing"][0]["id"]],
            "statement": item["statement"],
            "embedding": fake_vector(item["statement"]),
        }
    return {"idx": item["idx"], "action": "add", "replaces": [], "statement": item["statement"]}


class FakeApi:
    def __init__(self, candidates: list[dict[str, Any]], *, decide=default_decision) -> None:
        self.candidates = candidates
        self.decide = decide
        self.calls: list[tuple[str, Any]] = []
        self.fail: dict[str, Exception] = {}
        self.extract_limit: int | None = None

    def _maybe_fail(self, stage: str) -> None:
        if stage in self.fail:
            raise self.fail[stage]

    async def embed(self, texts, *, task_type):
        self.calls.append(("embed", len(texts)))
        self._maybe_fail("embed")
        return [fake_vector(text) for text in texts], TAG

    async def digest(self, **fields):
        stage = fields["stage"]
        self.calls.append(("digest", stage))
        self._maybe_fail(f"digest_{stage}")
        usage = {"prompt_tokens": 100, "completion_tokens": 20}
        if stage == "scrub":
            return {"digest": f"## Summary\n- A cleaned digest for {fields['day']}.", "empty": False, "usage": usage}
        if stage == "merge":
            return {"digest": "## Summary\n- " + " / ".join(p[:40] for p in fields["partials"]), "empty": False, "usage": usage}
        return {"digest": f"## Summary\n- The {stage} digest for {fields['day']}.", "empty": False, "usage": usage}

    async def extract(self, **fields):
        self.calls.append(("extract", len(fields["transcript"])))
        self._maybe_fail("extract")
        if self.extract_limit is not None and len(fields["transcript"]) > self.extract_limit:
            raise MemoryApiError("too slow", status_code=504)
        return {
            "candidates": [dict(c, embedding=fake_vector(c["statement"])) for c in self.candidates],
            "embedding_model": "fake-embed",
            "dimensions": 768,
            "usage": {"prompt_tokens": 50, "completion_tokens": 10},
        }

    async def reconcile(self, **fields):
        self.calls.append(("reconcile", len(fields["items"])))
        self._maybe_fail("reconcile")
        return {
            "decisions": [self.decide(item) for item in fields["items"]],
            "embedding_model": "fake-embed",
            "dimensions": 768,
            "usage": {"prompt_tokens": 30, "completion_tokens": 5},
        }


class FakeHass:
    def __init__(self, config_dir: str) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.bus = SimpleNamespace(async_fire=lambda event, data=None: self.events.append((event, data)))
        self.config = SimpleNamespace(
            time_zone="America/Vancouver",
            config_dir=config_dir,
            is_allowed_path=lambda path: True,
        )
        persons = [
            SimpleNamespace(entity_id="person.mia", name="Mia", attributes={"friendly_name": "Mia", "user_id": "u-mia"}),
            SimpleNamespace(entity_id="person.will", name="Will", attributes={"friendly_name": "Will", "user_id": "u-will"}),
        ]
        self.states = SimpleNamespace(async_all=lambda domain=None: persons)

    async def async_add_executor_job(self, func, *args):
        return func(*args)


class FakeRuntime:
    def __init__(self, hass: FakeHass, store: MemoryStore, config, api: FakeApi) -> None:
        self.hass = hass
        self.store = store
        self.config = config
        self.api = api
        self.embedding_tag = TAG
        self.entities = store.list_entities()
        self.backfills = 0

    def household_roster(self):
        return [{"name": "Mia", "kind": "person"}, {"name": "Will", "kind": "person"}]

    def note_embedding_tag(self, tag: str) -> None:
        self.embedding_tag = tag

    async def async_backfill_embeddings(self) -> int:
        self.backfills += 1
        return 0


CANDIDATES = [
    {
        "statement": "Mia is allergic to peanuts.",
        "type": "fact",
        "subjects": [{"name": "Mia", "kind": "person"}],
        "speaker": "Will",
        "importance": 5,
        "occurred_on": "",
        "sensitivity": "private",
    },
    {
        "statement": "Will prefers oat milk in his coffee.",
        "type": "preference",
        "subjects": [{"name": "Will", "kind": "person"}],
        "speaker": "Unknown speaker (Kitchen)",
        "importance": 3,
    },
]


@pytest.fixture(autouse=True)
def local_time_zone():
    previous = dt_util.get_default_time_zone()
    dt_util.set_default_time_zone(TZ)
    yield
    dt_util.set_default_time_zone(previous)


@pytest.fixture
def store(pg_engine):
    store = MemoryStore(pg_engine, iterative_scan=True)
    store.ensure_household()
    store.sync_persons(
        [
            {"entity_id": "person.mia", "name": "Mia", "user_id": "u-mia"},
            {"entity_id": "person.will", "name": "Will", "user_id": "u-will"},
        ]
    )
    return store


@pytest.fixture
def make_job(store, pg_config, tmp_path):
    def _make(candidates=CANDIDATES, *, vault_path: str = "", **api_kwargs):
        hass = FakeHass(str(tmp_path / "config"))
        os.makedirs(hass.config.config_dir, exist_ok=True)
        api = FakeApi(list(candidates), **api_kwargs)
        runtime = FakeRuntime(hass, store, pg_config(vault_path=vault_path), api)
        return NightlyJob(runtime), runtime, api, hass

    return _make


def yesterday() -> date:
    return current_day(dt_util.now(), 4) - timedelta(days=1)


def seed_day(store: MemoryStore, day: date, turns: list[tuple[str, str]], *, speaker: str = "Will") -> None:
    start = day_window(day, TZ, 4)[0] + timedelta(hours=6)
    rows = []
    for index, (said, reply) in enumerate(turns):
        exchange = new_exchange_id()
        at = dt_util.as_utc(start + timedelta(minutes=10 * index))
        common = {"conversation_id": f"conv-{day}", "exchange_id": exchange, "kind": "chat"}
        rows.append({**common, "role": "user", "content": said, "speaker_source": "ha_user",
                     "speaker_name": speaker, "person_entity_id": f"person.{speaker.lower()}", "created_at": at})
        rows.append({**common, "role": "assistant", "content": reply, "speaker_source": "assistant",
                     "created_at": at + timedelta(seconds=2)})
    store.insert_messages(rows)


def rows(store: MemoryStore, sql: str, **params) -> list[dict[str, Any]]:
    import sqlalchemy

    with store.engine.connect() as conn:
        return [dict(row) for row in conn.execute(sqlalchemy.text(sql), {"scope": "owner", **params}).mappings()]


DAY_TURNS = [
    ("Mia can't have peanuts, she's allergic, so keep that in mind for snacks", "Noted, no peanuts for Mia."),
    ("I always want oat milk in my coffee from now on", "Oat milk it is."),
]


def test_parse_speaker():
    assert parse_speaker("Will") == ("Will", False)
    assert parse_speaker("Mia (stated)") == ("Mia", True)
    assert parse_speaker("Unknown speaker (Kitchen)") == (None, False)
    assert parse_speaker("Assistant") == (None, False)
    assert parse_speaker("") == (None, False)


def test_day_becomes_digest_exchanges_and_facts(store, make_job):
    day = yesterday()
    seed_day(store, day, DAY_TURNS)
    job, runtime, api, hass = make_job()

    result = asyncio.run(job.async_run_day(day))

    assert result["status"] == "succeeded"
    assert (result["digest"], result["exchanges"], result["facts_added"]) == (1, 2, 2)
    assert ("digest", "draft") in api.calls and ("digest", "review") in api.calls

    digests = rows(store, "SELECT id, content, embedding_model FROM auri_memory.mem_memories "
                          "WHERE kind = 'digest' AND status = 'current' AND source_ref = :day", day=day)
    assert len(digests) == 1 and "review digest" in digests[0]["content"] and digests[0]["embedding_model"] == TAG

    exchanges = rows(store, "SELECT occurred_at, expires_at FROM auri_memory.mem_memories WHERE kind = 'exchange'")
    assert len(exchanges) == 2
    assert all(row["expires_at"] - row["occurred_at"] == timedelta(days=7) for row in exchanges)

    facts = rows(
        store,
        """SELECT m.content, m.sensitivity, m.speaker_name, m.speaker_source, m.speaker_person_entity_id,
                  ARRAY(SELECT en.name FROM auri_memory.mem_edges e JOIN auri_memory.mem_entities en
                        ON en.id = e.to_entity WHERE e.from_memory = m.id AND e.relation = 'about') AS about
           FROM auri_memory.mem_memories m WHERE m.kind IN ('fact', 'preference') ORDER BY m.id""",
    )
    assert [fact["content"] for fact in facts] == [c["statement"] for c in CANDIDATES]
    peanuts, oat_milk = facts
    assert peanuts["about"] == ["Mia"] and peanuts["sensitivity"] == "private"
    assert (peanuts["speaker_name"], peanuts["speaker_source"], peanuts["speaker_person_entity_id"]) == (
        "Will", "ha_user", "person.will",
    )
    # Nobody knows who said it, so it isn't attributed to anyone.
    assert (oat_milk["speaker_name"], oat_milk["speaker_source"]) == (None, "unknown")

    run = rows(store, "SELECT status, stats FROM auri_memory.mem_runs WHERE job = 'nightly' AND run_date = :day", day=day)
    assert run[0]["status"] == "succeeded"
    assert run[0]["stats"]["usage"]["prompt_tokens"] > 0
    assert hass.events[-1][0] == EVENT_MEMORY_RUN_FINISHED and hass.events[-1][1]["status"] == "succeeded"
    assert runtime.backfills == 1


def test_rerun_supersedes_the_digest_and_adds_no_duplicates(store, make_job):
    day = yesterday()
    seed_day(store, day, DAY_TURNS)
    job, _, _, _ = make_job()
    asyncio.run(job.async_run_day(day))
    first_digest = rows(store, "SELECT id FROM auri_memory.mem_memories WHERE kind = 'digest'")[0]["id"]

    assert asyncio.run(job.async_run_day(day)) == {"status": "skipped", "reason": "already_processed"}
    result = asyncio.run(job.async_run_day(day, force=True))

    assert result["status"] == "succeeded"
    assert (result["exchanges"], result["facts_added"], result["facts_confirmed"]) == (0, 0, 2)
    digests = rows(store, "SELECT id, status, superseded_by, status_reason FROM auri_memory.mem_memories "
                          "WHERE kind = 'digest' ORDER BY id")
    assert [d["status"] for d in digests] == ["superseded", "current"]
    assert digests[0]["superseded_by"] == digests[1]["id"] and digests[0]["status_reason"] == "rerun"
    assert digests[0]["id"] == first_digest
    assert rows(store, "SELECT count(*) AS n FROM auri_memory.mem_memories WHERE kind IN ('fact', 'preference')")[0]["n"] == 2
    confirmed = rows(store, "SELECT confirmed_count FROM auri_memory.mem_memories WHERE kind IN ('fact', 'preference')")
    assert all(row["confirmed_count"] == 1 for row in confirmed)
    assert rows(store, "SELECT count(*) AS n FROM auri_memory.mem_memories WHERE kind = 'exchange'")[0]["n"] == 2


def test_a_changed_fact_supersedes_the_old_one(store, make_job):
    day = yesterday()
    seed_day(store, day, [("Mia's favorite color is now blue, not green anymore", "Blue it is.")])
    [mia] = store.resolve_entities([("Mia", "person")])
    old_id, _ = store.insert_memory(
        NewMemory(kind="preference", content="Mia's favorite color is green.", source="chat",
                  about_entity_ids=[mia], embedding=fake_vector("Mia's favorite color is green."), embedding_model=TAG)
    )
    job, _, api, _ = make_job(
        [{"statement": "Mia's favorite color is now blue.", "type": "preference", "subjects": [], "speaker": "Mia"}]
    )

    result = asyncio.run(job.async_run_day(day))

    assert result["facts_updated"] == 1 and ("reconcile", 1) in api.calls
    current = rows(store, """SELECT id, content, status, embedding_model FROM auri_memory.mem_memories
                             WHERE kind = 'preference' ORDER BY id""")
    assert [(row["content"], row["status"]) for row in current] == [
        ("Mia's favorite color is green.", "superseded"),
        ("Mia's favorite color is now blue.", "current"),
    ]
    new_id = current[1]["id"]
    assert current[1]["embedding_model"] == TAG
    edges = rows(store, "SELECT relation, to_memory, to_entity FROM auri_memory.mem_edges WHERE from_memory = :id", id=new_id)
    assert {"relation": "supersedes", "to_memory": old_id, "to_entity": None} in edges
    # The old memory's subject carries over even though the candidate named none.
    assert {"relation": "about", "to_memory": None, "to_entity": mia} in edges


def test_an_update_matching_a_stored_sentence_still_retires_the_old_one(store, make_job):
    """Monday said Tuesday; this morning's explicit "remember" already says Thursday."""
    day = yesterday()
    seed_day(store, day, [("Remember that Mia's piano lesson moved to Thursdays at 4", "Got it.")])
    [mia] = store.resolve_entities([("Mia", "person")])
    old_id, _ = store.insert_memory(
        NewMemory(kind="fact", content="Mia's piano lesson is on Tuesdays.", source="chat", about_entity_ids=[mia],
                  embedding=fake_vector("Mia's piano lesson is on Tuesdays."), embedding_model=TAG)
    )
    current = "Mia's piano lesson is on Thursdays at 4 pm."
    current_id, _ = store.insert_memory(
        NewMemory(kind="fact", content=current, source="chat", about_entity_ids=[mia],
                  embedding=fake_vector(current), embedding_model=TAG)
    )

    def merge_into_the_stored_sentence(item):
        return {"idx": item["idx"], "action": "update", "replaces": [old_id], "statement": current}

    job, _, _, _ = make_job(
        [{"statement": "Mia's piano lesson moved to Thursdays at 4 pm.", "type": "fact", "subjects": [], "speaker": "Will"}],
        decide=merge_into_the_stored_sentence,
    )

    result = asyncio.run(job.async_run_day(day))

    assert (result["facts_updated"], result["facts_added"]) == (1, 0)
    facts = {row["id"]: row for row in rows(store, "SELECT id, status, superseded_by, confirmed_count FROM auri_memory.mem_memories WHERE kind = 'fact'")}
    assert facts[old_id]["status"] == "superseded" and facts[old_id]["superseded_by"] == current_id
    assert facts[current_id]["status"] == "current" and facts[current_id]["confirmed_count"] == 1
    assert len(facts) == 2  # no third copy


def test_a_routine_is_never_stored_as_a_commitment(store, make_job):
    day = yesterday()
    seed_day(store, day, [("I have piano lessons every Thursday at 4 now", "Got it.")])
    job, _, _, _ = make_job([
        {"statement": "Will has a piano lesson every Thursday at 4 pm.", "type": "commitment",
         "subjects": [{"name": "Will", "kind": "person"}], "speaker": "Will", "importance": 4,
         "due_on": (day + timedelta(days=2)).isoformat()},
        {"statement": "Will has a dentist appointment on 2026-12-01.", "type": "commitment",
         "subjects": [{"name": "Will", "kind": "person"}], "speaker": "Will", "due_on": "2026-12-01"},
    ])

    asyncio.run(job.async_run_day(day))

    stored = {row["content"]: row for row in rows(store, "SELECT content, kind, due_at, expires_at FROM auri_memory.mem_memories "
                                                         "WHERE kind IN ('fact', 'commitment')")}
    routine = stored["Will has a piano lesson every Thursday at 4 pm."]
    assert (routine["kind"], routine["due_at"], routine["expires_at"]) == ("fact", None, None)
    dentist = stored["Will has a dentist appointment on 2026-12-01."]
    assert dentist["kind"] == "commitment" and dentist["due_at"] is not None


def test_someone_first_saved_as_a_topic_becomes_a_person(store, make_job):
    day = yesterday()
    seed_day(store, day, [("Lillian loves candy, by the way", "Good to know.")])
    [lillian] = store.resolve_entities([("Lillian", "topic")])
    job, _, _, _ = make_job([
        {"statement": "Lillian loves candy.", "type": "preference", "subjects": [{"name": "Lillian", "kind": "person"}],
         "speaker": "Will", "importance": 3},
    ])

    asyncio.run(job.async_run_day(day))

    assert rows(store, "SELECT id, kind FROM auri_memory.mem_entities WHERE name = 'Lillian'") == [
        {"id": lillian, "kind": "person"}
    ]


def test_a_fact_already_known_still_tells_what_its_subject_is(store, make_job):
    day = yesterday()
    seed_day(store, day, [("Remember our dog Biscuit is allergic to chicken", "Got it.")])
    # Saved with "remember..." during the day, when Biscuit was only a name.
    [biscuit] = store.resolve_entities([("Biscuit", "topic")])
    store.insert_memory(
        NewMemory(kind="fact", content="Biscuit is allergic to chicken.", source="chat", about_entity_ids=[biscuit],
                  embedding=fake_vector("Biscuit is allergic to chicken."), embedding_model=TAG)
    )
    job, _, _, _ = make_job([
        {"statement": "Biscuit is allergic to chicken.", "type": "fact", "speaker": "Will", "importance": 5,
         "subjects": [{"name": "Biscuit", "kind": "pet"}, {"name": "Pepper", "kind": "pet"}]},
    ])

    result = asyncio.run(job.async_run_day(day))

    assert result["facts_confirmed"] == 1 and result["facts_added"] == 0
    kinds = {row["name"]: row["kind"] for row in rows(store, "SELECT name, kind FROM auri_memory.mem_entities")}
    assert kinds["Biscuit"] == "pet" and "Pepper" not in kinds


def test_a_cloud_failure_mid_run_writes_nothing_and_is_retried(store, make_job):
    day = yesterday()
    seed_day(store, day, DAY_TURNS)
    [mia] = store.resolve_entities([("Mia", "person")])
    store.insert_memory(NewMemory(kind="fact", content="Mia is allergic to tree nuts.", source="chat",
                                  about_entity_ids=[mia], embedding=fake_vector("Mia is allergic to tree nuts."),
                                  embedding_model=TAG))
    job, _, api, hass = make_job()
    api.fail["reconcile"] = MemoryApiError("bad gateway", status_code=502)

    result = asyncio.run(job.async_run_day(day))

    assert result == {"status": "failed", "reason": "cloud_error"}
    assert rows(store, "SELECT count(*) AS n FROM auri_memory.mem_memories WHERE source_ref = :day", day=day)[0]["n"] == 0
    run = rows(store, "SELECT status, error FROM auri_memory.mem_runs WHERE run_date = :day", day=day)
    assert run[0]["status"] == "failed" and "cloud_error" in run[0]["error"]
    assert hass.events[-1][1]["status"] == "failed"

    del api.fail["reconcile"]
    assert asyncio.run(job.async_run_day(day))["status"] == "succeeded"
    attempts = rows(store, "SELECT attempt, status FROM auri_memory.mem_runs WHERE run_date = :day ORDER BY id", day=day)
    assert [(a["attempt"], a["status"]) for a in attempts] == [(1, "failed"), (2, "succeeded")]


def test_a_failure_while_writing_rolls_the_whole_day_back(store, make_job, monkeypatch):
    day = yesterday()
    seed_day(store, day, DAY_TURNS)
    job, _, _, _ = make_job()

    def broken_confirm(conn, ids):
        raise RuntimeError("disk full")

    asyncio.run(job.async_run_day(day))
    monkeypatch.setattr(store, "confirm", broken_confirm)
    result = asyncio.run(job.async_run_day(day, force=True))

    assert result == {"status": "failed", "reason": "error"}
    # The re-run's digest and the retiring of the first one were rolled back together.
    digests = rows(store, "SELECT status FROM auri_memory.mem_memories WHERE kind = 'digest'")
    assert digests == [{"status": "current"}]
    statuses = rows(store, "SELECT status FROM auri_memory.mem_runs WHERE run_date = :day ORDER BY id", day=day)
    assert [s["status"] for s in statuses] == ["succeeded", "failed"]


def test_something_forgotten_isnt_learned_again(store, make_job):
    day = yesterday()
    seed_day(store, day, DAY_TURNS)
    forgotten_id, _ = store.insert_memory(
        NewMemory(kind="fact", content="Mia is allergic to peanuts.", source="chat",
                  embedding=fake_vector("Mia is allergic to peanuts."), embedding_model=TAG)
    )
    store.retract([forgotten_id], reason="forget")
    job, _, _, _ = make_job()

    result = asyncio.run(job.async_run_day(day))

    assert result["facts_added"] == 1
    run = rows(store, "SELECT stats FROM auri_memory.mem_runs WHERE run_date = :day", day=day)
    assert run[0]["stats"]["dropped_forgotten"] == 1
    assert rows(store, "SELECT count(*) AS n FROM auri_memory.mem_memories "
                       "WHERE content = 'Mia is allergic to peanuts.' AND status = 'current'")[0]["n"] == 0


def test_catch_up_runs_missed_days_oldest_first_and_gives_up_after_three_failures(store, make_job):
    newest = yesterday()
    older = newest - timedelta(days=2)
    hopeless = newest - timedelta(days=3)
    for day in (newest, older, hopeless):
        seed_day(store, day, DAY_TURNS[:1])
    for attempt in (1, 2, 3):
        run_id = store.start_run("nightly", hopeless, attempt=attempt)
        store.finish_run(run_id, status="failed", error="earlier")
    job, runtime, _, hass = make_job()

    result = asyncio.run(job.async_catch_up(days_back=7))

    assert list(result["days"]) == [older.isoformat(), newest.isoformat()]
    assert all(outcome["status"] == "succeeded" for outcome in result["days"].values())
    assert [data["day"] for event, data in hass.events if event == EVENT_MEMORY_RUN_FINISHED] == [
        older.isoformat(), newest.isoformat(),
    ]
    assert "housekeeping" in result and runtime.backfills == 1
    assert asyncio.run(job.async_catch_up(days_back=7))["days"] == {}


def test_catch_up_stops_when_over_budget(store, make_job):
    newest = yesterday()
    for day in (newest, newest - timedelta(days=1)):
        seed_day(store, day, DAY_TURNS[:1])
    job, _, api, _ = make_job()
    api.fail["digest_draft"] = MemoryApiError("limit", status_code=429)

    result = asyncio.run(job.async_catch_up(days_back=7))

    assert result["stopped"] == "over_budget"
    assert len(result["days"]) == 1
    assert "scrubbed_digests" not in result


def test_long_days_are_split_to_fit_each_request(store, make_job):
    day = yesterday()
    long_turns = [(f"Story part {i}: " + "words " * 260, "Okay.") for i in range(100)]
    seed_day(store, day, long_turns)
    job, _, api, _ = make_job(CANDIDATES[:1])
    api.extract_limit = 40_000  # chunks above this "time out" and must be halved

    result = asyncio.run(job.async_run_day(day))

    assert result["status"] == "succeeded"
    digest_calls = [stage for kind, stage in api.calls if kind == "digest"]
    assert digest_calls.count("draft") == 2 and digest_calls.count("merge") == 1 and "review" not in digest_calls
    extract_sizes = [size for kind, size in api.calls if kind == "extract"]
    too_big = [size for size in extract_sizes if size > 40_000]
    # Each chunk that "timed out" was retried as two halves.
    assert too_big and len(extract_sizes) - len(too_big) >= 2 * len(too_big)
    # The same candidate from every chunk is only stored once.
    assert result["facts_added"] == 1


def test_forgotten_text_is_scrubbed_from_digests(store, make_job, tmp_path):
    day = yesterday()
    vault = tmp_path / "vault"
    vault.mkdir()
    seed_day(store, day, DAY_TURNS)
    job, _, api, _ = make_job(vault_path=str(vault))
    asyncio.run(job.async_run_day(day))
    note = vault / "Auri" / "Daily" / f"{day.isoformat()}.md"
    assert "review digest" in note.read_text(encoding="utf-8")

    [fact] = rows(store, "SELECT id FROM auri_memory.mem_memories WHERE content = 'Mia is allergic to peanuts.'")
    [digest] = rows(store, "SELECT id FROM auri_memory.mem_memories WHERE kind = 'digest'")
    store.retract([fact["id"]], reason="forget")
    store.retract([digest["id"]], reason=f"scrub_pending:{fact['id']}")

    assert asyncio.run(job._async_scrub_digests()) == 1  # noqa: SLF001

    digests = rows(store, "SELECT id, content, status, status_reason FROM auri_memory.mem_memories "
                          "WHERE kind = 'digest' ORDER BY id")
    assert [(d["status"], d["status_reason"]) for d in digests] == [("retracted", "scrubbed"), ("current", None)]
    assert "cleaned digest" in digests[1]["content"]
    # The replacement never links to the original, whose text holds what was forgotten.
    assert rows(store, "SELECT count(*) AS n FROM auri_memory.mem_edges WHERE relation = 'supersedes'")[0]["n"] == 0
    assert "cleaned digest" in note.read_text(encoding="utf-8")
    assert asyncio.run(job._async_scrub_digests()) == 0  # noqa: SLF001


def test_vault_copy_is_written_and_a_public_folder_is_refused(store, make_job, tmp_path):
    day = yesterday()
    seed_day(store, day, DAY_TURNS)
    vault = tmp_path / "vault"
    vault.mkdir()
    job, _, _, _ = make_job(vault_path=str(vault))

    asyncio.run(job.async_run_day(day))

    memory_note = (vault / "Auri" / "Memory.md").read_text(encoding="utf-8")
    assert "Will prefers oat milk in his coffee." in memory_note
    # Private memories stay out of the vault unless that option is on.
    assert "peanuts" not in memory_note
    assert (vault / "Auri" / "Daily" / f"{day.isoformat()}.md").exists()

    public = tmp_path / "config" / "www" / "vault"
    public.mkdir(parents=True)
    job, _, _, _ = make_job(vault_path=str(public))
    asyncio.run(job.async_run_day(day, force=True))
    assert not (public / "Auri").exists()
