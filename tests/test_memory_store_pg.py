"""MemoryStore against a real PostgreSQL + pgvector server (skipped when none is reachable)."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import math
import random

import pytest

from custom_components.hey_auri_client.memory.db import MemoryDbError, test_connection as dry_run_connection
from custom_components.hey_auri_client.memory.migrations import LATEST_SCHEMA_VERSION, apply_migrations
from custom_components.hey_auri_client.memory.store import MemoryStore, NewMemory, new_exchange_id

NOW = datetime.now(timezone.utc)


def _unit(seed: int, dims: int = 768) -> list[float]:
    rng = random.Random(seed)
    values = [rng.uniform(-1, 1) for _ in range(dims)]
    norm = math.sqrt(sum(v * v for v in values))
    return [v / norm for v in values]


def _blend(a: list[float], b: list[float], weight: float) -> list[float]:
    mixed = [weight * x + (1 - weight) * y for x, y in zip(a, b)]
    norm = math.sqrt(sum(v * v for v in mixed))
    return [v / norm for v in mixed]


@pytest.fixture
def store(pg_engine):
    store = MemoryStore(pg_engine, iterative_scan=True)
    store.ensure_household()
    return store


def _remember(store: MemoryStore, content: str, **kwargs) -> int:
    memory_id, _ = store.insert_memory(NewMemory(content=content, source=kwargs.pop("source", "chat"), **{"kind": "fact", **kwargs}))
    return memory_id


def test_migrations_are_idempotent(pg_engine):
    assert apply_migrations(pg_engine, app_version="test") == LATEST_SCHEMA_VERSION
    assert apply_migrations(pg_engine, app_version="test") == LATEST_SCHEMA_VERSION


def test_dry_run_connection_reports_versions_and_changes_nothing(pg_config, pg_engine):
    import sqlalchemy

    with pg_engine.begin() as conn:
        conn.execute(sqlalchemy.text("DROP SCHEMA auri_memory CASCADE"))
    info = dry_run_connection(pg_config())
    assert info["server_version_num"] >= 130000
    assert info["pgvector_version"]
    with pg_engine.connect() as conn:
        exists = conn.execute(
            sqlalchemy.text("SELECT 1 FROM information_schema.schemata WHERE schema_name = 'auri_memory'")
        ).first()
    assert exists is None


def test_dry_run_rejects_bad_password(pg_config, pg_engine):
    with pytest.raises(MemoryDbError) as raised:
        dry_run_connection(pg_config(password="wrong"))
    assert raised.value.code == "invalid_auth"


def test_dry_run_rejects_unreachable_server(pg_config, pg_engine):
    with pytest.raises(MemoryDbError) as raised:
        dry_run_connection(pg_config(port=1))
    assert raised.value.code == "cannot_connect"


@pytest.fixture
def limited_config(pg_config, pg_engine):
    """A database owned by a user who isn't a superuser, without pgvector enabled."""
    import sqlalchemy

    def run(statement: str) -> None:
        with pg_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.execute(sqlalchemy.text(statement))

    run("DROP DATABASE IF EXISTS auri_limited")
    run("DROP ROLE IF EXISTS auri_limited")
    run("CREATE ROLE auri_limited LOGIN PASSWORD 'limited-test'")
    run("CREATE DATABASE auri_limited OWNER auri_limited")
    yield pg_config(username="auri_limited", password="limited-test", database="auri_limited")
    run("DROP DATABASE IF EXISTS auri_limited WITH (FORCE)")
    run("DROP ROLE IF EXISTS auri_limited")


def test_a_user_who_cant_enable_pgvector_gets_its_own_error(limited_config):
    from sqlalchemy.exc import DBAPIError

    from custom_components.hey_auri_client.memory.db import build_engine, classify_setup_error

    with pytest.raises(MemoryDbError) as raised:
        dry_run_connection(limited_config)
    assert raised.value.code == "insufficient_privilege"

    # The same problem at startup (rights lost after setup) gets the same code.
    engine = build_engine(limited_config, pooled=False)
    try:
        with pytest.raises(DBAPIError) as failed:
            apply_migrations(engine, app_version="test")
    finally:
        engine.dispose()
    assert classify_setup_error(failed.value).code == "insufficient_privilege"


def test_duplicate_memories_are_detected(store):
    first, duplicate = store.insert_memory(NewMemory(kind="fact", content="Mia is allergic to peanuts.", source="chat"))
    second, duplicate_again = store.insert_memory(NewMemory(kind="fact", content="  mia is allergic to peanuts ", source="nightly"))
    assert (duplicate, duplicate_again) == (False, True)
    assert first == second


def test_supersede_keeps_history_and_subjects(store):
    mia = store.resolve_entities([("Mia", "person")])[0]
    old = _remember(store, "Mia prefers almond milk in her coffee.", kind="preference", about_entity_ids=[mia])
    with store.engine.begin() as conn:
        new, _ = store.insert_memory(NewMemory(kind="preference", content="Mia prefers oat milk in her coffee.", source="nightly"), conn)
        assert store.supersede(conn, [old], new) == [old]

    rows = store.auto_recall(terms="mia or milk", subject_ids=[mia], hint_days=[], adjacent_days=[], speaker_entity_id=None)
    assert [row.id for row in rows] == [new]
    assert rows[0].about == ["Mia"]
    assert rows[0].replaced[0][0] == "Mia prefers almond milk in her coffee."


def test_supersede_only_retires_facts(store):
    fact_id = _remember(store, "Mia's piano lesson is on Tuesdays.")
    digest_id, _ = store.insert_memory(NewMemory(kind="digest", content="## Summary\nA day.", source="nightly", source_ref=date(2026, 10, 1)))
    new_id = _remember(store, "Mia's piano lesson is on Thursdays.")
    with store.engine.begin() as conn:
        assert store.supersede(conn, [fact_id, digest_id, 999999], new_id) == [fact_id]
    assert [row.id for row in store.get_memories([fact_id, digest_id, new_id])] == sorted([digest_id, new_id])


def test_recall_rows_carry_the_due_date(store):
    due = NOW + timedelta(days=2)
    [will] = store.resolve_entities([("Will", "person")])
    lesson_id, _ = store.insert_memory(NewMemory(
        kind="commitment", content="Will has a piano lesson on Thursday.", source="chat", importance=4,
        about_entity_ids=[will], due_at=due, expires_at=due + timedelta(days=14), embedding=_unit(7), embedding_model="m@768",
    ))
    found = {
        "profile": store.profile(speaker_entity_id=will, household_entity_id=None),
        "auto": store.auto_recall(terms="piano", subject_ids=[], hint_days=[], adjacent_days=[], speaker_entity_id=will),
        "tool": store.semantic_recall(query_vector=_unit(7), embedding_model="m@768", terms="", subject_ids=[],
                                      hint_days=[], adjacent_days=[], range_start=None, range_end=None),
        "by id": store.get_memories([lesson_id]),
    }
    for name, rows in found.items():
        [row] = [row for row in rows if row.id == lesson_id]
        assert row.due == due, name


def test_retract_hides_a_memory_but_keeps_it(store):
    memory_id = _remember(store, "Leo's locker code is near the gym.", importance=3)
    assert store.retract([memory_id], reason="forget") == [(memory_id, "fact", "Leo's locker code is near the gym.")]
    assert store.retract([memory_id], reason="forget") == []
    assert store.auto_recall(terms="leo or locker", subject_ids=[], hint_days=[], adjacent_days=[], speaker_entity_id=None) == []
    assert store.get_memories([memory_id]) == []


def test_profile_shows_private_only_to_the_person_it_is_about(store):
    will, mia = store.resolve_entities([("Will", "person"), ("Mia", "person")])
    household = store.ensure_household()
    _remember(store, "Mia is allergic to peanuts.", importance=5, sensitivity="private", about_entity_ids=[mia])
    _remember(store, "Will likes the bedroom at 19 degrees at night.", kind="preference", importance=4, about_entity_ids=[will])
    _remember(store, "The spare key is in the blue pot by the door.", importance=5, about_entity_ids=[household])
    _remember(store, "Will once tried karaoke.", importance=2, about_entity_ids=[will])
    _remember(store, "Auri should keep answers short.", kind="about_assistant", importance=3)

    def contents(speaker):
        return {row.content for row in store.profile(speaker_entity_id=speaker, household_entity_id=household)}

    assert contents(mia) == {
        "Mia is allergic to peanuts.",
        "The spare key is in the blue pot by the door.",
        "Auri should keep answers short.",
    }
    assert contents(will) == {
        "Will likes the bedroom at 19 degrees at night.",
        "The spare key is in the blue pot by the door.",
        "Auri should keep answers short.",
    }
    assert "Mia is allergic to peanuts." not in contents(None)


def test_a_household_that_shares_sees_everyones_important_memories(store):
    will, mia, biscuit, lillian = store.resolve_entities(
        [("Will", "person"), ("Mia", "person"), ("Biscuit", "pet"), ("Lillian", "person")]
    )
    household = store.ensure_household()
    _remember(store, "Mia is lactose intolerant.", importance=5, sensitivity="private", about_entity_ids=[mia])
    _remember(store, "Will is allergic to shellfish.", importance=5, sensitivity="private", about_entity_ids=[will])
    _remember(store, "Biscuit can't eat chicken.", importance=5, about_entity_ids=[biscuit])
    _remember(store, "Will likes the bedroom at 19 degrees at night.", kind="preference", importance=4, about_entity_ids=[will])
    _remember(store, "Lillian likes candy.", kind="preference", importance=4, about_entity_ids=[lillian])

    def contents(speaker, shared):
        rows = store.profile(
            speaker_entity_id=speaker, household_entity_id=household, member_ids=[will, mia, biscuit], shared=shared
        )
        return [row.content for row in rows]

    # A voice satellite: nothing about any one person, unless the household shares.
    assert contents(None, False) == []
    everyone = contents(None, True)
    assert set(everyone) == {
        "Mia is lactose intolerant.",
        "Will is allergic to shellfish.",
        "Biscuit can't eat chicken.",
        "Will likes the bedroom at 19 degrees at night.",
    }
    assert everyone[-1] == "Will likes the bedroom at 19 degrees at night."  # most important first
    assert contents(mia, True)[0] == "Mia is lactose intolerant."  # the speaker's own first among equals

    def found(shared):
        rows = store.auto_recall(
            terms="lactose", subject_ids=[], hint_days=[], adjacent_days=[], speaker_entity_id=None, shared=shared
        )
        return [row.content for row in rows]

    assert found(False) == [] and found(True) == ["Mia is lactose intolerant."]


def test_auto_recall_by_keyword_subject_and_day(store):
    mia = store.resolve_entities([("Mia", "person")])[0]
    yesterday = date.today() - timedelta(days=1)
    _remember(store, "Mia is allergic to peanuts.", importance=5, about_entity_ids=[mia])
    _remember(store, "The passports are in the top drawer of the office desk.", importance=5)
    digest_id = _remember(
        store, "## Summary\nWill planned the spring trip to Lisbon.", kind="digest", source="nightly",
        source_ref=yesterday, occurred_at=NOW - timedelta(days=1),
    )

    def recall(text_terms="", subjects=(), days=()):
        return [row.content for row in store.auto_recall(
            terms=text_terms, subject_ids=list(subjects), hint_days=list(days), adjacent_days=[], speaker_entity_id=None
        )]

    assert recall("passports") == ["The passports are in the top drawer of the office desk."]
    assert recall("peanut or allergy") == ["Mia is allergic to peanuts."]
    assert recall(subjects=[mia]) == ["Mia is allergic to peanuts."]
    assert recall(days=[yesterday])[0].startswith("## Summary")
    assert recall("weather or forecast") == []
    assert digest_id


def test_semantic_recall_finds_meaning_and_respects_filters(store):
    milk = _unit(1)
    store.insert_memory(NewMemory(kind="preference", content="Mia prefers oat milk in her coffee.", source="chat",
                                  embedding=milk, embedding_model="gemini-embedding-001@768"))
    store.insert_memory(NewMemory(kind="fact", content="The car is due for an oil change.", source="chat",
                                  embedding=_unit(2), embedding_model="gemini-embedding-001@768"))
    store.insert_memory(NewMemory(kind="fact", content="Old-model vector row.", source="chat",
                                  embedding=milk, embedding_model="some-other-model"))

    query = _blend(milk, _unit(3), 0.9)
    rows = store.semantic_recall(
        query_vector=query, embedding_model="gemini-embedding-001@768", terms="", subject_ids=[],
        hint_days=[], adjacent_days=[], range_start=None, range_end=None,
    )
    assert [row.content for row in rows] == ["Mia prefers oat milk in her coffee."]

    nothing = store.semantic_recall(
        query_vector=_unit(99), embedding_model="gemini-embedding-001@768", terms="", subject_ids=[],
        hint_days=[], adjacent_days=[], range_start=None, range_end=None,
    )
    assert nothing == []

    keyword_only = store.semantic_recall(
        query_vector=None, embedding_model=None, terms="oil or change", subject_ids=[],
        hint_days=[], adjacent_days=[], range_start=None, range_end=None,
    )
    assert [row.content for row in keyword_only] == ["The car is due for an oil change."]


def test_date_ranges_find_commitments_by_their_due_day(store):
    utc = timezone.utc
    tuesday, thursday = date(2026, 10, 13), date(2026, 10, 15)
    model = "gemini-embedding-001@768"
    store.insert_memory(NewMemory(  # mentioned on Tuesday, due on Thursday
        kind="commitment", content="Will has a piano lesson on Thursday, 2026-10-15.", source="chat",
        occurred_at=datetime(2026, 10, 13, 15, 2, tzinfo=utc), due_at=datetime(2026, 10, 15, tzinfo=utc),
        embedding=_unit(11), embedding_model=model))
    store.insert_memory(NewMemory(
        kind="fact", content="The plumber fixed the sink.", source="chat",
        occurred_at=datetime(2026, 10, 15, 10, tzinfo=utc), embedding=_unit(12), embedding_model=model))
    store.insert_memory(NewMemory(
        kind="preference", content="Mia likes jazz.", source="chat",
        occurred_at=datetime(2026, 10, 13, 9, tzinfo=utc), embedding=_unit(13), embedding_model=model))

    def contents(day: date, vector) -> set[str]:
        start = datetime.combine(day, datetime.min.time(), tzinfo=utc)
        rows = store.semantic_recall(
            query_vector=vector, embedding_model=model if vector else None, terms="", subject_ids=[],
            hint_days=[], adjacent_days=[],
            # Memory days start at the 04:00 boundary; due dates are calendar days.
            range_start=start + timedelta(hours=4), range_end=start + timedelta(days=1, hours=4),
            due_start=start, due_end=start + timedelta(days=1),
        )
        return {row.content for row in rows}

    for vector in (_unit(99), None):  # the meaning search and the keyword fallback
        assert contents(thursday, vector) == {"Will has a piano lesson on Thursday, 2026-10-15.", "The plumber fixed the sink."}
        assert contents(tuesday, vector) == {"Will has a piano lesson on Thursday, 2026-10-15.", "Mia likes jazz."}


def test_neighbors_for_reconcile(store):
    base = _unit(5)
    close_id, _ = store.insert_memory(NewMemory(kind="preference", content="Will drinks his coffee black.", source="chat",
                                                embedding=base, embedding_model="m@768"))
    store.insert_memory(NewMemory(kind="fact", content="Unrelated fact.", source="chat", embedding=_unit(6), embedding_model="m@768"))
    neighbors = store.neighbors(_blend(base, _unit(7), 0.95), "m@768", limit=5)
    assert neighbors[0]["id"] == close_id and neighbors[0]["sim"] > 0.9


def test_housekeeping_expires_and_prunes(store):
    expired = _remember(store, "Earlier: turned on lights.", kind="exchange", expires_at=NOW - timedelta(minutes=1))
    _remember(store, "Will will call the plumber.", kind="commitment", expires_at=NOW + timedelta(days=5))
    store.insert_messages([{
        "conversation_id": "c1", "exchange_id": new_exchange_id(), "role": "user", "content": "old",
        "created_at": NOW - timedelta(days=400),
    }])
    result = store.housekeeping(archive_retention_days=365)
    assert result == {"expired": 1, "archive_pruned": 1}
    assert store.get_memories([expired]) == []


def test_runs_are_unique_while_running(store):
    day = date(2026, 10, 12)
    run_id = store.start_run("nightly", day)
    assert run_id is not None
    assert store.start_run("nightly", day) is None
    store.finish_run(run_id, status="succeeded", stats={"facts_added": 2})
    assert store.start_run("nightly", day) is not None
    assert store.run_history("nightly", day) == {"succeeded": 1, "running": 1}


def test_sync_persons_links_existing_names(store):
    mia_from_nightly = store.resolve_entities([("Mia", "person")])[0]
    mapping = store.sync_persons([
        {"entity_id": "person.mia", "name": "Mia", "user_id": "u-mia"},
        {"entity_id": "person.will", "name": "Will", "user_id": None},
    ])
    assert mapping["person.mia"] == mia_from_nightly
    renamed = store.sync_persons([{"entity_id": "person.will", "name": "William", "user_id": None}])
    assert renamed["person.will"] == mapping["person.will"]
    names = {entity.name for entity in store.list_entities()}
    assert {"Mia", "William", "household"} <= names


def test_a_topic_becomes_a_person_or_pet_once_known_as_one(store):
    import sqlalchemy

    lillian, rex, sam = store.resolve_entities([("Lillian", "topic"), ("Rex", "pet"), ("Sam", "topic")])
    store.resolve_entities([("Biscuit", "topic"), ("garden", "topic")])
    # Named as a person: by the nightly run, or answering "Who should I remember this for?".
    assert store.resolve_entities([("lillian", "person"), ("Rex", "person")]) == [lillian, rex]
    # A fact the nightly run already had: known topics become a person or pet, nothing is created.
    with store.engine.begin() as conn:
        store.learn_kinds(conn, [("Biscuit", "pet"), ("garden", "place"), ("Pepper", "pet")])
    # Added to Home Assistant.
    assert store.sync_persons([{"entity_id": "person.sam", "name": "Sam", "user_id": None}]) == {"person.sam": sam}
    kinds = {entity.name: entity.kind for entity in store.list_entities()}
    assert {name: kinds.get(name) for name in ("Lillian", "Rex", "Sam", "Biscuit", "garden", "Pepper")} == {
        "Lillian": "person", "Rex": "pet", "Sam": "person", "Biscuit": "pet", "garden": "topic", "Pepper": None,
    }

    # A topic found by alias stays a topic when a person already has its name.
    [mia] = store.resolve_entities([("Mia", "person")])
    with store.engine.begin() as conn:
        topic = conn.execute(sqlalchemy.text(
            "INSERT INTO auri_memory.mem_entities (scope, kind, name, aliases) "
            "VALUES ('owner', 'topic', 'Mia', ARRAY['Mimi']) RETURNING id"
        )).scalar()
    assert store.resolve_entities([("Mimi", "person")]) == [topic]
    assert {(e.id, e.kind) for e in store.list_entities() if e.name == "Mia"} == {(mia, "person"), (topic, "topic")}


def test_resolve_entities_by_alias_and_household(store):
    import sqlalchemy

    mia = store.resolve_entities([("Mia", "person")])[0]
    with store.engine.begin() as conn:
        conn.execute(sqlalchemy.text("UPDATE auri_memory.mem_entities SET aliases = ARRAY['Mimi'] WHERE id = :id"), {"id": mia})
    assert store.resolve_entities([("mimi", "topic")]) == [mia]
    assert store.resolve_entities([("the family", "topic")]) == [store.ensure_household()]


def test_archive_queries(store):
    exchange = new_exchange_id()
    store.insert_messages([
        {"conversation_id": "c1", "exchange_id": exchange, "role": "user", "content": "Remember Mia hates olives",
         "speaker_name": "Will", "speaker_source": "ha_user", "kind": "memory_command", "tool_names": ["remember_memory"]},
        {"conversation_id": "c1", "exchange_id": exchange, "role": "assistant", "content": "Got it.", "speaker_source": "assistant",
         "kind": "memory_command"},
    ])
    rows = store.messages_between(NOW - timedelta(minutes=5), NOW + timedelta(minutes=5))
    assert [row["role"] for row in rows] == ["user", "assistant"]
    assert rows[0]["tool_names"] == ["remember_memory"]
    today = store.todays_exchanges(since=NOW - timedelta(hours=1))
    assert today[0].content == "Will: Remember Mia hates olives / Assistant: Got it."
    days = store.days_with_messages(since=NOW - timedelta(days=1), time_zone="UTC", boundary_hour=0)
    assert days == [NOW.date()]


def test_embedding_backfill(store):
    memory_id = _remember(store, "Imported preference without a vector.", source="import")
    assert store.missing_embeddings("m@768") == [(memory_id, "Imported preference without a vector.")]
    store.set_embeddings([(memory_id, _unit(8))], "m@768")
    assert store.missing_embeddings("m@768") == []
    assert store.missing_embeddings("newer-model@768") == [(memory_id, "Imported preference without a vector.")]
