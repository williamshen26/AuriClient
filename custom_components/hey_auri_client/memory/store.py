"""Synchronous data access for the memory store. Every method blocks: call it in an executor.

Rows in owner scope are never deleted, only given a new status (superseded,
expired, retracted), so the history of what Auri believed stays visible. The
exceptions are archive retention for chat_messages and the guest-scope purge.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import json
import logging
from typing import Any, Iterable, Sequence
import uuid

from .db import MEMORY_SCHEMA
from .recall import RecallRow, SubjectEntity, content_hash

_LOGGER = logging.getLogger(__name__)
_S = MEMORY_SCHEMA

OWNER_SCOPE = "owner"
FACT_KINDS: tuple[str, ...] = ("fact", "preference", "decision", "commitment", "about_assistant")
ALL_KINDS: tuple[str, ...] = (*FACT_KINDS, "digest", "exchange")
HOUSEHOLD_NAME = "household"
EXCHANGE_TTL = timedelta(days=7)
COMMITMENT_GRACE = timedelta(days=14)

# Columns every recall query returns, plus the names of the subjects a memory is about.
_ROW_COLUMNS = f"""m.id, m.kind, m.content, COALESCE(m.occurred_at, m.recorded_at) AS at,
    m.importance, m.sensitivity, m.due_at,
    ARRAY(SELECT en.name FROM {_S}.mem_edges ed JOIN {_S}.mem_entities en ON en.id = ed.to_entity
          WHERE ed.from_memory = m.id AND ed.relation = 'about' AND en.kind <> 'household'
          ORDER BY en.name) AS about"""
# The same columns, as named by the recall queries' final SELECT over their scored rows.
_SCORED_COLUMNS = "id, kind, content, at, importance, sensitivity, due_at, about"


def vector_literal(values: Sequence[float]) -> str:
    """pgvector text form, passed with CAST(:v AS vector) so no pgvector package is needed."""
    return "[" + ",".join(f"{float(value):.6f}" for value in values) + "]"


def _text():
    from sqlalchemy import text  # pylint: disable=import-outside-toplevel

    return text


@dataclass(slots=True)
class NewMemory:
    """A memory to insert."""

    kind: str
    content: str
    source: str
    importance: int = 3
    sensitivity: str = "normal"
    about_entity_ids: Sequence[int] = ()
    embedding: Sequence[float] | None = None
    embedding_model: str | None = None
    occurred_at: datetime | None = None
    due_at: datetime | None = None
    expires_at: datetime | None = None
    source_ref: date | None = None
    run_id: int | None = None
    conversation_id: str | None = None
    exchange_id: str | None = None
    speaker_source: str | None = None
    speaker_person_entity_id: str | None = None
    speaker_name: str | None = None


class MemoryStore:
    """Data access for one scope ("owner" now; "guest:<booking_id>" later)."""

    def __init__(self, engine, *, scope: str = OWNER_SCOPE, iterative_scan: bool = False) -> None:
        self._engine = engine
        self.scope = scope
        # pgvector >= 0.8 can keep scanning the HNSW index when filters drop
        # rows, so a filtered nearest-neighbour search still returns enough.
        self._iterative_scan = iterative_scan

    @property
    def engine(self):
        return self._engine

    def ping(self) -> None:
        with self._engine.connect() as conn:
            conn.execute(_text()("SELECT 1"))

    # ── meta ────────────────────────────────────────────────────────────────

    def get_meta(self, key: str) -> Any:
        with self._engine.connect() as conn:
            value = conn.execute(
                _text()(f"SELECT value FROM {_S}.meta WHERE key = :key"), {"key": key}
            ).scalar()
        return value

    def set_meta(self, key: str, value: Any) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                _text()(
                    f"""INSERT INTO {_S}.meta (key, value) VALUES (:key, CAST(:value AS jsonb))
                        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()"""
                ),
                {"key": key, "value": json.dumps(value)},
            )

    # ── entities ────────────────────────────────────────────────────────────

    def ensure_household(self, conn=None) -> int:
        """Id of the scope's single household entity (memories about the whole home)."""
        if conn is None:
            with self._engine.begin() as own:
                return self.ensure_household(own)
        return int(
            conn.execute(
                _text()(
                    f"""INSERT INTO {_S}.mem_entities (scope, kind, name) VALUES (:scope, 'household', :name)
                        ON CONFLICT (scope, kind, (lower(name))) DO UPDATE SET updated_at = now()
                        RETURNING id"""
                ),
                {"scope": self.scope, "name": HOUSEHOLD_NAME},
            ).scalar()
        )

    def sync_persons(self, persons: Iterable[dict[str, Any]]) -> dict[str, int]:
        """Upsert Home Assistant persons as person entities; return {person entity_id: id}.

        A person the nightly job already created by name (no HA link yet) is
        linked rather than duplicated, and so is a topic of that name: someone
        mentioned before they were added to Home Assistant.
        """
        text = _text()
        result: dict[str, int] = {}
        with self._engine.begin() as conn:
            for person in persons:
                entity_id = str(person.get("entity_id") or "").strip()
                name = str(person.get("name") or "").strip()
                if not entity_id or not name:
                    continue
                user_id = person.get("user_id") or None
                row = conn.execute(
                    text(
                        f"""SELECT id, name FROM {_S}.mem_entities
                            WHERE scope = :scope AND ha_person_entity_id = :entity_id"""
                    ),
                    {"scope": self.scope, "entity_id": entity_id},
                ).first()
                if row is None:
                    # Or one known by name only; a topic only when no person has
                    # that name, so it can become this person.
                    row = conn.execute(
                        text(
                            f"""SELECT id, name FROM {_S}.mem_entities e
                                WHERE scope = :scope AND lower(name) = lower(:name) AND ha_person_entity_id IS NULL
                                  AND (kind = 'person' OR (kind = 'topic' AND NOT EXISTS (
                                      SELECT 1 FROM {_S}.mem_entities p WHERE p.scope = e.scope
                                      AND p.kind = 'person' AND lower(p.name) = lower(e.name))))
                                ORDER BY (kind = 'person') DESC LIMIT 1"""
                        ),
                        {"scope": self.scope, "name": name},
                    ).first()
                if row is None:
                    entity_pk = conn.execute(
                        text(
                            f"""INSERT INTO {_S}.mem_entities (scope, kind, name, ha_person_entity_id, ha_user_id)
                                VALUES (:scope, 'person', :name, :entity_id, :user_id)
                                ON CONFLICT (scope, kind, (lower(name))) DO UPDATE SET updated_at = now()
                                RETURNING id"""
                        ),
                        {"scope": self.scope, "name": name, "entity_id": entity_id, "user_id": user_id},
                    ).scalar()
                else:
                    entity_pk = row[0]
                    taken = conn.execute(
                        text(
                            f"""SELECT 1 FROM {_S}.mem_entities WHERE scope = :scope AND kind = 'person'
                                AND lower(name) = lower(:name) AND id <> :id"""
                        ),
                        {"scope": self.scope, "name": name, "id": entity_pk},
                    ).first()
                    conn.execute(
                        text(
                            f"""UPDATE {_S}.mem_entities SET ha_person_entity_id = :entity_id,
                                ha_user_id = :user_id, kind = 'person',
                                name = CASE WHEN :rename THEN :name ELSE name END,
                                updated_at = now() WHERE id = :id"""
                        ),
                        {"entity_id": entity_id, "user_id": user_id, "name": name, "rename": taken is None, "id": entity_pk},
                    )
                result[entity_id] = int(entity_pk)
        return result

    def list_entities(self) -> list[SubjectEntity]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                _text()(
                    f"""SELECT id, kind, name, aliases FROM {_S}.mem_entities
                        WHERE scope = :scope ORDER BY kind, name"""
                ),
                {"scope": self.scope},
            ).all()
        return [SubjectEntity(id=row[0], kind=row[1], name=row[2], aliases=tuple(row[3] or ())) for row in rows]

    def resolve_entities(self, subjects: Iterable[tuple[str, str]], conn=None) -> list[int]:
        """Find or create entities for (name, kind) pairs; kind "household" is the home itself.

        Lookup is by name or alias across kinds (a person named in a fact is
        the same Mia whatever kind the extractor guessed). Unknown names are
        created with the given kind, and a name only known as a topic becomes
        the person or pet it's given as.
        """
        if conn is None:
            with self._engine.begin() as own:
                return self.resolve_entities(subjects, own)

        ids: list[int] = []
        for name, kind in subjects:
            clean = " ".join(str(name or "").split())[:60]
            if not clean:
                continue
            if kind == "household" or clean.lower() in {HOUSEHOLD_NAME, "the household", "everyone", "family", "the family"}:
                entity_pk = self.ensure_household(conn)
            elif (found := self._find_entity(conn, clean)) is not None:
                entity_pk = found[0]
                self._learn_kind(conn, *found, kind)
            else:
                entity_pk = conn.execute(
                    _text()(
                        f"""INSERT INTO {_S}.mem_entities (scope, kind, name) VALUES (:scope, :kind, :name)
                            ON CONFLICT (scope, kind, (lower(name))) DO UPDATE SET updated_at = now()
                            RETURNING id"""
                    ),
                    {"scope": self.scope, "kind": kind if kind in _ENTITY_KINDS else "topic", "name": clean},
                ).scalar()
            if int(entity_pk) not in ids:
                ids.append(int(entity_pk))
        return ids

    def learn_kinds(self, conn, subjects: Iterable[tuple[str, str]]) -> None:
        """Known topics become the person or pet they're given as; no entity is created."""
        for name, kind in subjects:
            clean = " ".join(str(name or "").split())[:60]
            if clean and kind in _LEARNED_KINDS and (found := self._find_entity(conn, clean)) is not None:
                self._learn_kind(conn, *found, kind)

    def _find_entity(self, conn, name: str) -> tuple[int, str] | None:
        """(id, kind) of the entity a name or alias refers to, a person first."""
        row = conn.execute(
            _text()(
                f"""SELECT id, kind FROM {_S}.mem_entities
                    WHERE scope = :scope AND kind <> 'household'
                      AND (lower(name) = lower(:name)
                           OR lower(:name) = ANY (SELECT lower(alias) FROM unnest(aliases) AS alias))
                    ORDER BY (kind = 'person') DESC, id LIMIT 1"""
            ),
            {"scope": self.scope, "name": name},
        ).first()
        return (int(row[0]), str(row[1])) if row else None

    def _learn_kind(self, conn, entity_pk: int, current: str, kind: str) -> None:
        if current != "topic" or kind not in _LEARNED_KINDS:
            return
        # Unless one of that kind already has the name (this topic was found by alias).
        conn.execute(
            _text()(
                f"""UPDATE {_S}.mem_entities e SET kind = :kind, updated_at = now()
                    WHERE e.id = :id AND NOT EXISTS (
                        SELECT 1 FROM {_S}.mem_entities p WHERE p.scope = e.scope
                        AND p.kind = :kind AND lower(p.name) = lower(e.name))"""
            ),
            {"id": entity_pk, "kind": kind},
        )

    # ── conversation archive ────────────────────────────────────────────────

    def insert_messages(self, rows: Sequence[dict[str, Any]]) -> int:
        """Insert chat_messages rows (already redacted). Returns the number written."""
        if not rows:
            return 0
        params = [
            {
                "scope": row.get("scope") or self.scope,
                "conversation_id": row["conversation_id"],
                "exchange_id": row["exchange_id"],
                "role": row["role"],
                "content": row["content"],
                "kind": row.get("kind") or "chat",
                "speaker_source": row.get("speaker_source") or "unknown",
                "ha_user_id": row.get("ha_user_id"),
                "person_entity_id": row.get("person_entity_id"),
                "speaker_name": row.get("speaker_name"),
                "device_id": row.get("device_id"),
                "area": row.get("area"),
                "language": row.get("language"),
                "tool_names": list(row.get("tool_names") or []),
                "meta": json.dumps(row.get("meta") or {}),
                "created_at": row.get("created_at") or datetime.now(timezone.utc),
            }
            for row in rows
        ]
        with self._engine.begin() as conn:
            conn.execute(
                _text()(
                    f"""INSERT INTO {_S}.chat_messages
                        (scope, conversation_id, exchange_id, role, content, kind, speaker_source, ha_user_id,
                         person_entity_id, speaker_name, device_id, area, language, tool_names, meta, created_at)
                        VALUES (:scope, :conversation_id, CAST(:exchange_id AS uuid), :role, :content, :kind,
                                :speaker_source, :ha_user_id, :person_entity_id, :speaker_name, :device_id, :area,
                                :language, CAST(:tool_names AS text[]), CAST(:meta AS jsonb), :created_at)"""
                ),
                params,
            )
        return len(params)

    def messages_between(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        """Archive rows in [start, end), oldest first."""
        with self._engine.connect() as conn:
            rows = conn.execute(
                _text()(
                    f"""SELECT id, conversation_id, exchange_id::text, role, content, kind, speaker_source,
                               speaker_name, area, tool_names, created_at
                        FROM {_S}.chat_messages
                        WHERE scope = :scope AND created_at >= :start AND created_at < :end
                        ORDER BY created_at, id"""
                ),
                {"scope": self.scope, "start": start, "end": end},
            ).mappings().all()
        return [dict(row) for row in rows]

    def days_with_messages(self, *, since: datetime, time_zone: str, boundary_hour: int) -> list[date]:
        """Local days (shifted by the day boundary) that have archived turns since ``since``."""
        with self._engine.connect() as conn:
            rows = conn.execute(
                _text()(
                    f"""SELECT DISTINCT ((created_at AT TIME ZONE :tz) - make_interval(hours => :boundary))::date AS day
                        FROM {_S}.chat_messages
                        WHERE scope = :scope AND created_at >= :since AND kind <> 'automatic'
                        ORDER BY day"""
                ),
                {"scope": self.scope, "since": since, "tz": time_zone, "boundary": boundary_hour},
            ).all()
        return [row[0] for row in rows]

    def todays_exchanges(self, *, since: datetime, limit: int = 6) -> list[RecallRow]:
        """Recent turns not yet processed by the nightly job, as exchange notes (newest first)."""
        with self._engine.connect() as conn:
            rows = conn.execute(
                _text()(
                    f"""SELECT MIN(id) AS id, exchange_id,
                               string_agg(CASE WHEN role = 'user' THEN COALESCE(speaker_name, 'Someone') || ': '
                                               ELSE 'Assistant: ' END || content, ' / ' ORDER BY id) AS content,
                               MIN(created_at) AS at
                        FROM {_S}.chat_messages
                        WHERE scope = :scope AND created_at >= :since AND kind IN ('chat', 'memory_command')
                        GROUP BY exchange_id ORDER BY at DESC LIMIT :limit"""
                ),
                {"scope": self.scope, "since": since, "limit": limit},
            ).all()
        return [RecallRow(id=row[0], kind="exchange", content=row[2], at=row[3], from_archive=True) for row in rows]

    # ── memories ────────────────────────────────────────────────────────────

    def insert_memory(self, memory: NewMemory, conn=None) -> tuple[int, bool]:
        """Insert a memory and its about edges; returns (id, duplicate).

        A current memory with the same kind and normalized text counts as a
        duplicate: its id is returned and nothing new is written.
        """
        if conn is None:
            with self._engine.begin() as own:
                return self.insert_memory(memory, own)

        text = _text()
        digest = content_hash(memory.content)
        params = {
            "scope": self.scope,
            "kind": memory.kind,
            "content": memory.content[:8000],
            "embedding": vector_literal(memory.embedding) if memory.embedding else None,
            "embedding_model": memory.embedding_model if memory.embedding else None,
            "source": memory.source,
            "source_ref": memory.source_ref,
            "run_id": memory.run_id,
            "conversation_id": memory.conversation_id,
            "exchange_id": memory.exchange_id,
            "speaker_source": memory.speaker_source,
            "speaker_person_entity_id": memory.speaker_person_entity_id,
            "speaker_name": memory.speaker_name,
            "occurred_at": memory.occurred_at,
            "due_at": memory.due_at,
            "importance": min(max(int(memory.importance), 1), 5),
            "sensitivity": memory.sensitivity,
            "expires_at": memory.expires_at,
            "content_hash": digest,
        }
        new_id = conn.execute(
            text(
                f"""INSERT INTO {_S}.mem_memories
                    (scope, kind, content, embedding, embedding_model, source, source_ref, run_id, conversation_id,
                     exchange_id, speaker_source, speaker_person_entity_id, speaker_name, occurred_at, due_at,
                     importance, sensitivity, expires_at, content_hash)
                    VALUES (:scope, :kind, :content, CAST(:embedding AS vector), :embedding_model, :source,
                            :source_ref, :run_id, :conversation_id, CAST(:exchange_id AS uuid), :speaker_source,
                            :speaker_person_entity_id, :speaker_name, :occurred_at, :due_at, :importance,
                            :sensitivity, :expires_at, :content_hash)
                    ON CONFLICT (scope, kind, content_hash) WHERE status = 'current' DO NOTHING
                    RETURNING id"""
            ),
            params,
        ).scalar()
        if new_id is None:
            existing = conn.execute(
                text(
                    f"""SELECT id FROM {_S}.mem_memories WHERE scope = :scope AND kind = :kind
                        AND content_hash = :content_hash AND status = 'current'"""
                ),
                params,
            ).scalar()
            return int(existing), True

        self.add_about_edges(conn, int(new_id), memory.about_entity_ids)
        return int(new_id), False

    def add_about_edges(self, conn, memory_id: int, entity_ids: Iterable[int]) -> None:
        for entity_pk in dict.fromkeys(entity_ids):
            conn.execute(
                _text()(
                    f"""INSERT INTO {_S}.mem_edges (from_memory, relation, to_entity)
                        VALUES (:memory_id, 'about', :entity_id) ON CONFLICT DO NOTHING"""
                ),
                {"memory_id": memory_id, "entity_id": int(entity_pk)},
            )

    def supersede(
        self, conn, old_ids: Sequence[int], new_id: int, *, kinds: Sequence[str] = FACT_KINDS
    ) -> list[int]:
        """Mark current memories of the given kinds superseded by new_id, link them, and carry over their subjects."""
        if not old_ids:
            return []
        text = _text()
        params = {"scope": self.scope, "old": [int(i) for i in old_ids], "new_id": new_id, "kinds": list(kinds)}
        replaced = [
            int(row[0])
            for row in conn.execute(
                text(
                    f"""UPDATE {_S}.mem_memories SET status = 'superseded', superseded_by = :new_id,
                            status_changed_at = now(), status_reason = 'updated'
                        WHERE scope = :scope AND status = 'current' AND id = ANY (CAST(:old AS bigint[]))
                          AND id <> :new_id AND kind = ANY (CAST(:kinds AS text[]))
                        RETURNING id"""
                ),
                params,
            )
        ]
        if replaced:
            params["old"] = replaced
            conn.execute(
                text(
                    f"""INSERT INTO {_S}.mem_edges (from_memory, relation, to_memory)
                        SELECT :new_id, 'supersedes', old_id FROM unnest(CAST(:old AS bigint[])) AS old_id
                        ON CONFLICT DO NOTHING"""
                ),
                params,
            )
            conn.execute(
                text(
                    f"""INSERT INTO {_S}.mem_edges (from_memory, relation, to_entity)
                        SELECT DISTINCT :new_id, 'about', e.to_entity FROM {_S}.mem_edges e
                        WHERE e.from_memory = ANY (CAST(:old AS bigint[])) AND e.relation = 'about'
                        ON CONFLICT DO NOTHING"""
                ),
                params,
            )
        return replaced

    def retire_current_digest(self, conn, day: date) -> list[int]:
        """Take the day's current digest out of service before a re-run writes its replacement.

        Only one current digest per day is allowed, so the old one must stop
        being current before the new one is inserted; link_superseded() then
        records which replaced which.
        """
        rows = conn.execute(
            _text()(
                f"""UPDATE {_S}.mem_memories SET status = 'superseded', status_changed_at = now(),
                        status_reason = 'rerun'
                    WHERE scope = :scope AND kind = 'digest' AND status = 'current' AND source_ref = :day
                    RETURNING id"""
            ),
            {"scope": self.scope, "day": day},
        ).all()
        return [int(row[0]) for row in rows]

    def current_digest_id(self, conn, day: date) -> int | None:
        return conn.execute(
            _text()(
                f"""SELECT id FROM {_S}.mem_memories
                    WHERE scope = :scope AND kind = 'digest' AND status = 'current' AND source_ref = :day"""
            ),
            {"scope": self.scope, "day": day},
        ).scalar()

    def link_superseded(self, conn, old_ids: Sequence[int], new_id: int) -> None:
        """Point already-superseded memories at their replacement."""
        if not old_ids:
            return
        params = {"old": list(old_ids), "new_id": new_id}
        text = _text()
        conn.execute(
            text(f"UPDATE {_S}.mem_memories SET superseded_by = :new_id WHERE id = ANY (CAST(:old AS bigint[]))"),
            params,
        )
        conn.execute(
            text(
                f"""INSERT INTO {_S}.mem_edges (from_memory, relation, to_memory)
                    SELECT :new_id, 'supersedes', old_id FROM unnest(CAST(:old AS bigint[])) AS old_id
                    ON CONFLICT DO NOTHING"""
            ),
            params,
        )

    def get_contents(self, ids: Sequence[int]) -> dict[int, str]:
        """Content of memories by id, whatever their status."""
        if not ids:
            return {}
        with self._engine.connect() as conn:
            rows = conn.execute(
                _text()(
                    f"SELECT id, content FROM {_S}.mem_memories WHERE scope = :scope AND id = ANY (CAST(:ids AS bigint[]))"
                ),
                {"scope": self.scope, "ids": [int(i) for i in ids]},
            ).all()
        return {int(row[0]): row[1] for row in rows}

    def list_scopes(self) -> list[str]:
        """Every scope with stored data (owner plus any guest bookings)."""
        with self._engine.connect() as conn:
            rows = conn.execute(
                _text()(
                    f"""SELECT scope FROM {_S}.mem_memories UNION SELECT scope FROM {_S}.chat_messages
                        UNION SELECT scope FROM {_S}.mem_entities"""
                )
            ).all()
        return sorted({row[0] for row in rows})

    def retract(self, ids: Sequence[int], *, reason: str) -> list[tuple[int, str, str]]:
        """Retract current memories (never deleted); returns (id, kind, content) of each one changed."""
        if not ids:
            return []
        with self._engine.begin() as conn:
            rows = conn.execute(
                _text()(
                    f"""UPDATE {_S}.mem_memories SET status = 'retracted', status_changed_at = now(),
                            status_reason = :reason
                        WHERE scope = :scope AND status = 'current' AND id = ANY (CAST(:ids AS bigint[]))
                        RETURNING id, kind, content"""
                ),
                {"scope": self.scope, "ids": [int(i) for i in ids], "reason": reason[:200]},
            ).all()
        return [(int(row[0]), row[1], row[2]) for row in rows]

    def retract_similar(
        self,
        vector: Sequence[float],
        embedding_model: str,
        *,
        kinds: Sequence[str],
        min_sim: float,
        reason: str,
    ) -> list[int]:
        """Retract current memories of the given kinds that are close in meaning to vector."""
        with self._engine.begin() as conn:
            rows = conn.execute(
                _text()(
                    f"""UPDATE {_S}.mem_memories SET status = 'retracted', status_changed_at = now(),
                            status_reason = :reason
                        WHERE scope = :scope AND status = 'current' AND kind = ANY (CAST(:kinds AS text[]))
                          AND embedding IS NOT NULL AND embedding_model = :model
                          AND 1 - (embedding <=> CAST(:v AS vector)) >= :min_sim
                        RETURNING id"""
                ),
                {
                    "scope": self.scope,
                    "kinds": list(kinds),
                    "model": embedding_model,
                    "v": vector_literal(vector),
                    "min_sim": min_sim,
                    "reason": reason[:200],
                },
            ).all()
        return [int(row[0]) for row in rows]

    def confirm(self, conn, ids: Sequence[int]) -> None:
        """Note that a nightly run saw these memories stated again."""
        if ids:
            conn.execute(
                _text()(
                    f"""UPDATE {_S}.mem_memories SET confirmed_count = confirmed_count + 1, last_confirmed_at = now()
                        WHERE scope = :scope AND id = ANY (CAST(:ids AS bigint[]))"""
                ),
                {"scope": self.scope, "ids": list(ids)},
            )

    def get_memories(self, ids: Sequence[int]) -> list[RecallRow]:
        if not ids:
            return []
        with self._engine.connect() as conn:
            rows = conn.execute(
                _text()(
                    f"""SELECT {_ROW_COLUMNS}, m.status FROM {_S}.mem_memories m
                        WHERE m.scope = :scope AND m.id = ANY (CAST(:ids AS bigint[]))"""
                ),
                {"scope": self.scope, "ids": [int(i) for i in ids]},
            ).all()
        return [_row(row) for row in rows if row[-1] == "current"]

    # ── recall ──────────────────────────────────────────────────────────────

    def profile(
        self,
        *,
        speaker_entity_id: int | None,
        household_entity_id: int | None,
        member_ids: Sequence[int] = (),
        shared: bool = False,
        limit: int = 12,
    ) -> list[RecallRow]:
        """Always-relevant memories: important facts about the speaker, the household, or nobody in particular.

        shared (a household that shares its memories): important facts about
        every member too (member_ids: its people and pets), private ones
        included. Most important first; the speaker's own first among equals.
        """
        profile_ids = [i for i in (speaker_entity_id, household_entity_id, *(member_ids if shared else ())) if i is not None]
        with self._engine.connect() as conn:
            rows = conn.execute(
                _text()(
                    f"""SELECT {_ROW_COLUMNS} FROM {_S}.mem_memories m
                        WHERE m.scope = :scope AND m.status = 'current' AND m.sensitivity <> 'secret'
                          AND (m.expires_at IS NULL OR m.expires_at > now())
                          AND m.kind IN ('fact', 'preference', 'commitment', 'about_assistant')
                          AND (m.importance >= 4 OR m.kind = 'about_assistant')
                          AND (m.kind = 'about_assistant'
                               OR EXISTS (SELECT 1 FROM {_S}.mem_edges e WHERE e.from_memory = m.id
                                          AND e.relation = 'about' AND e.to_entity = ANY (CAST(:profile_ids AS bigint[])))
                               OR NOT EXISTS (SELECT 1 FROM {_S}.mem_edges e WHERE e.from_memory = m.id
                                              AND e.relation = 'about'))
                          AND (:shared OR m.sensitivity = 'normal'
                               OR EXISTS (SELECT 1 FROM {_S}.mem_edges e WHERE e.from_memory = m.id
                                          AND e.relation = 'about' AND e.to_entity = CAST(:speaker AS bigint)))
                          AND (m.kind <> 'commitment' OR m.due_at IS NULL
                               OR m.due_at BETWEEN now() - interval '1 day' AND now() + interval '7 days')
                        ORDER BY (m.kind = 'about_assistant') DESC, m.importance DESC,
                                 EXISTS (SELECT 1 FROM {_S}.mem_edges e WHERE e.from_memory = m.id
                                         AND e.relation = 'about' AND e.to_entity = CAST(:speaker AS bigint)) DESC,
                                 COALESCE(m.occurred_at, m.recorded_at) DESC
                        LIMIT :limit"""
                ),
                {
                    "scope": self.scope,
                    "profile_ids": profile_ids,
                    "speaker": speaker_entity_id,
                    "shared": shared,
                    "limit": limit,
                },
            ).all()
        return [_row(row) for row in rows]

    def auto_recall(
        self,
        *,
        terms: str,
        subject_ids: Sequence[int],
        hint_days: Sequence[date],
        adjacent_days: Sequence[date],
        speaker_entity_id: int | None,
        shared: bool = False,
        limit: int = 6,
        timeout_ms: int = 250,
    ) -> list[RecallRow]:
        """Keyword + subject + date recall on every message (no embedding needed, so no cloud call).

        Private memories only about the speaker, unless the household shares (shared).
        """
        if not terms and not subject_ids and not hint_days:
            return []
        text = _text()
        with self._engine.begin() as conn:
            conn.execute(text(f"SET LOCAL statement_timeout = {int(timeout_ms)}"))
            rows = conn.execute(
                text(
                    f"""WITH q AS (SELECT websearch_to_tsquery('english', :terms) AS tsq),
                    cand AS (
                        SELECT m.id FROM {_S}.mem_memories m, q
                        WHERE :has_terms AND m.scope = :scope AND m.status = 'current' AND m.search_text @@ q.tsq
                        UNION
                        SELECT e.from_memory FROM {_S}.mem_edges e
                        WHERE e.relation = 'about' AND e.to_entity = ANY (CAST(:subject_ids AS bigint[]))
                        UNION
                        SELECT m.id FROM {_S}.mem_memories m
                        WHERE m.scope = :scope AND m.status = 'current' AND m.kind IN ('digest', 'exchange')
                          AND m.source_ref = ANY (CAST(:hint_days AS date[]))),
                    s AS (
                        SELECT {_ROW_COLUMNS},
                               CASE WHEN :has_terms THEN ts_rank(m.search_text, q.tsq) ELSE 0 END AS kw,
                               EXISTS (SELECT 1 FROM {_S}.mem_edges e WHERE e.from_memory = m.id AND e.relation = 'about'
                                       AND e.to_entity = ANY (CAST(:subject_ids AS bigint[]))) AS about_subject,
                               COALESCE(m.source_ref = ANY (CAST(:hint_days AS date[])), false) AS on_day,
                               COALESCE(m.source_ref = ANY (CAST(:adjacent_days AS date[])), false) AS near_day,
                               EXTRACT(EPOCH FROM now() - COALESCE(m.occurred_at, m.recorded_at)) / 86400.0 AS age_days
                        FROM {_S}.mem_memories m JOIN cand c ON c.id = m.id CROSS JOIN q
                        WHERE m.scope = :scope AND m.status = 'current' AND m.sensitivity <> 'secret'
                          AND (m.expires_at IS NULL OR m.expires_at > now())
                          AND (:shared OR m.sensitivity = 'normal'
                               OR EXISTS (SELECT 1 FROM {_S}.mem_edges e WHERE e.from_memory = m.id
                                          AND e.relation = 'about' AND e.to_entity = CAST(:speaker AS bigint))))
                    SELECT {_SCORED_COLUMNS},
                           LEAST(kw, 0.5) * 0.6 + CASE WHEN about_subject THEN 0.15 ELSE 0 END
                           + CASE WHEN on_day THEN 0.12 WHEN near_day THEN 0.08 ELSE 0 END
                           + (importance - 3) * 0.02 + 0.05 * exp(-GREATEST(age_days, 0) / 30.0) AS score
                    FROM s
                    WHERE kw >= :min_kw OR on_day OR (about_subject AND importance >= 4)
                    ORDER BY score DESC LIMIT :limit"""
                ),
                {
                    "scope": self.scope,
                    "terms": terms or "",
                    "has_terms": bool(terms),
                    "subject_ids": list(subject_ids),
                    "hint_days": list(hint_days),
                    "adjacent_days": list(adjacent_days),
                    "speaker": speaker_entity_id,
                    "shared": shared,
                    "min_kw": AUTO_RECALL_MIN_KEYWORD_RANK,
                    "limit": limit,
                },
            ).all()
        return self._with_history([_row(row) for row in rows])

    def semantic_recall(
        self,
        *,
        query_vector: Sequence[float] | None,
        embedding_model: str | None,
        terms: str,
        subject_ids: Sequence[int],
        hint_days: Sequence[date],
        adjacent_days: Sequence[date],
        range_start: datetime | None,
        range_end: datetime | None,
        due_start: datetime | None = None,
        due_end: datetime | None = None,
        kinds: Sequence[str] = ALL_KINDS,
        min_sim: float = 0.55,
        limit: int = 8,
    ) -> list[RecallRow]:
        """Hybrid recall for the recall_memory tool: meaning + keywords + subjects + days.

        With a date range, a memory matches when it was said in
        [range_start, range_end) (memory days, which start at the day
        boundary) or, for commitments, is due in [due_start, due_end)
        (calendar days). Private memories are returned flagged; the model is
        told they only help the person they are about.
        """
        if query_vector is None:
            return self._keyword_tool_recall(
                terms=terms,
                subject_ids=subject_ids,
                hint_days=hint_days,
                adjacent_days=adjacent_days,
                range_start=range_start,
                range_end=range_end,
                due_start=due_start,
                due_end=due_end,
                kinds=kinds,
                limit=limit,
            )
        text = _text()
        with self._engine.begin() as conn:
            conn.execute(text("SET LOCAL statement_timeout = 2000"))
            if self._iterative_scan:
                conn.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
                conn.execute(text("SET LOCAL hnsw.ef_search = 100"))
            rows = conn.execute(
                text(
                    f"""WITH q AS (SELECT CAST(:qvec AS vector) AS v, websearch_to_tsquery('english', :terms) AS tsq),
                    sem AS (
                        SELECT m.id FROM {_S}.mem_memories m, q
                        WHERE m.status = 'current' AND m.embedding IS NOT NULL AND m.scope = :scope
                          AND m.embedding_model = :emb_model AND m.kind = ANY (CAST(:kinds AS text[]))
                        ORDER BY m.embedding <=> q.v LIMIT 60),
                    kwc AS (
                        SELECT m.id FROM {_S}.mem_memories m, q
                        WHERE :has_terms AND m.scope = :scope AND m.status = 'current'
                          AND m.kind = ANY (CAST(:kinds AS text[])) AND m.search_text @@ q.tsq
                        LIMIT 40),
                    subj AS (
                        SELECT e.from_memory AS id FROM {_S}.mem_edges e
                        JOIN {_S}.mem_memories m ON m.id = e.from_memory
                        WHERE e.relation = 'about' AND e.to_entity = ANY (CAST(:subject_ids AS bigint[]))
                          AND m.scope = :scope AND m.status = 'current' AND m.kind = ANY (CAST(:kinds AS text[]))
                        LIMIT 40),
                    days AS (
                        SELECT m.id FROM {_S}.mem_memories m
                        WHERE m.scope = :scope AND m.status = 'current' AND m.kind = ANY (CAST(:kinds AS text[]))
                          AND m.source_ref = ANY (CAST(:hint_days AS date[]))
                        LIMIT 20),
                    cand AS (SELECT id FROM sem UNION SELECT id FROM kwc UNION SELECT id FROM subj UNION SELECT id FROM days),
                    s AS (
                        SELECT {_ROW_COLUMNS},
                               CASE WHEN m.embedding IS NOT NULL AND m.embedding_model = :emb_model
                                    THEN 1 - (m.embedding <=> q.v) ELSE 0 END AS sim,
                               CASE WHEN :has_terms THEN ts_rank(m.search_text, q.tsq) ELSE 0 END AS kw,
                               EXISTS (SELECT 1 FROM {_S}.mem_edges e WHERE e.from_memory = m.id AND e.relation = 'about'
                                       AND e.to_entity = ANY (CAST(:subject_ids AS bigint[]))) AS about_subject,
                               COALESCE(m.source_ref = ANY (CAST(:hint_days AS date[])), false) AS on_day,
                               COALESCE(m.source_ref = ANY (CAST(:adjacent_days AS date[])), false) AS near_day,
                               (CAST(:range_start AS timestamptz) IS NOT NULL) AS in_range,
                               EXTRACT(EPOCH FROM now() - COALESCE(m.occurred_at, m.recorded_at)) / 86400.0 AS age_days
                        FROM {_S}.mem_memories m JOIN cand USING (id) CROSS JOIN q
                        WHERE m.scope = :scope AND m.status = 'current' AND m.sensitivity <> 'secret'
                          AND (m.expires_at IS NULL OR m.expires_at > now())
                          AND (CAST(:range_start AS timestamptz) IS NULL
                               OR (COALESCE(m.occurred_at, m.recorded_at) >= CAST(:range_start AS timestamptz)
                                   AND COALESCE(m.occurred_at, m.recorded_at) < CAST(:range_end AS timestamptz))
                               OR (m.due_at >= CAST(:due_start AS timestamptz) AND m.due_at < CAST(:due_end AS timestamptz))))
                    SELECT {_SCORED_COLUMNS},
                           sim + LEAST(kw, 0.5) * 0.3 + CASE WHEN about_subject THEN 0.08 ELSE 0 END
                           + CASE WHEN on_day THEN 0.12 WHEN near_day THEN 0.08 ELSE 0 END
                           + (importance - 3) * 0.02 + 0.05 * exp(-GREATEST(age_days, 0) / 30.0) AS score
                    FROM s
                    WHERE on_day OR about_subject OR in_range OR sim >= :min_sim
                       OR (kw >= 0.1 AND sim >= :min_sim - 0.1)
                    ORDER BY score DESC LIMIT :limit"""
                ),
                {
                    "scope": self.scope,
                    "qvec": vector_literal(query_vector),
                    "emb_model": embedding_model,
                    "terms": terms or "",
                    "has_terms": bool(terms),
                    "subject_ids": list(subject_ids),
                    "hint_days": list(hint_days),
                    "adjacent_days": list(adjacent_days),
                    "range_start": range_start,
                    "range_end": range_end,
                    "due_start": due_start,
                    "due_end": due_end,
                    "kinds": list(kinds),
                    "min_sim": min_sim,
                    "limit": limit,
                },
            ).all()
        return self._with_history([_row(row) for row in rows])

    def _keyword_tool_recall(
        self,
        *,
        terms: str,
        subject_ids: Sequence[int],
        hint_days: Sequence[date],
        adjacent_days: Sequence[date],
        range_start: datetime | None,
        range_end: datetime | None,
        due_start: datetime | None,
        due_end: datetime | None,
        kinds: Sequence[str],
        limit: int,
    ) -> list[RecallRow]:
        """The recall tool's fallback when no embedding came with the call."""
        text = _text()
        with self._engine.begin() as conn:
            conn.execute(text("SET LOCAL statement_timeout = 2000"))
            rows = conn.execute(
                text(
                    f"""WITH q AS (SELECT websearch_to_tsquery('english', :terms) AS tsq),
                    s AS (
                        SELECT {_ROW_COLUMNS},
                               CASE WHEN :has_terms THEN ts_rank(m.search_text, q.tsq) ELSE 0 END AS kw,
                               EXISTS (SELECT 1 FROM {_S}.mem_edges e WHERE e.from_memory = m.id AND e.relation = 'about'
                                       AND e.to_entity = ANY (CAST(:subject_ids AS bigint[]))) AS about_subject,
                               COALESCE(m.source_ref = ANY (CAST(:hint_days AS date[])), false) AS on_day,
                               COALESCE(m.source_ref = ANY (CAST(:adjacent_days AS date[])), false) AS near_day,
                               (CAST(:range_start AS timestamptz) IS NOT NULL) AS in_range,
                               EXTRACT(EPOCH FROM now() - COALESCE(m.occurred_at, m.recorded_at)) / 86400.0 AS age_days
                        FROM {_S}.mem_memories m CROSS JOIN q
                        WHERE m.scope = :scope AND m.status = 'current' AND m.sensitivity <> 'secret'
                          AND m.kind = ANY (CAST(:kinds AS text[]))
                          AND (m.expires_at IS NULL OR m.expires_at > now())
                          AND (CAST(:range_start AS timestamptz) IS NULL
                               OR (COALESCE(m.occurred_at, m.recorded_at) >= CAST(:range_start AS timestamptz)
                                   AND COALESCE(m.occurred_at, m.recorded_at) < CAST(:range_end AS timestamptz))
                               OR (m.due_at >= CAST(:due_start AS timestamptz) AND m.due_at < CAST(:due_end AS timestamptz)))
                          AND ((:has_terms AND m.search_text @@ q.tsq)
                               OR EXISTS (SELECT 1 FROM {_S}.mem_edges e WHERE e.from_memory = m.id
                                          AND e.relation = 'about' AND e.to_entity = ANY (CAST(:subject_ids AS bigint[])))
                               OR m.source_ref = ANY (CAST(:hint_days AS date[]))
                               OR CAST(:range_start AS timestamptz) IS NOT NULL))
                    SELECT {_SCORED_COLUMNS},
                           LEAST(kw, 0.5) * 0.6 + CASE WHEN about_subject THEN 0.15 ELSE 0 END
                           + CASE WHEN on_day THEN 0.12 WHEN near_day THEN 0.08 ELSE 0 END
                           + (importance - 3) * 0.02 + 0.05 * exp(-GREATEST(age_days, 0) / 30.0) AS score
                    FROM s ORDER BY score DESC LIMIT :limit"""
                ),
                {
                    "scope": self.scope,
                    "terms": terms or "",
                    "has_terms": bool(terms),
                    "subject_ids": list(subject_ids),
                    "hint_days": list(hint_days),
                    "adjacent_days": list(adjacent_days),
                    "range_start": range_start,
                    "range_end": range_end,
                    "due_start": due_start,
                    "due_end": due_end,
                    "kinds": list(kinds),
                    "limit": limit,
                },
            ).all()
        return self._with_history([_row(row) for row in rows])

    def _with_history(self, rows: list[RecallRow]) -> list[RecallRow]:
        """Attach what each returned fact replaced (newest first).

        Digests re-run for the same day aren't history worth showing, and a
        replaced memory the household asked to forget is never shown.
        """
        by_id = {row.id: row for row in rows if row.kind in FACT_KINDS}
        if not by_id:
            return rows
        with self._engine.connect() as conn:
            history = conn.execute(
                _text()(
                    f"""SELECT e.from_memory, old.content, COALESCE(old.occurred_at, old.recorded_at) AS at
                        FROM {_S}.mem_edges e JOIN {_S}.mem_memories old ON old.id = e.to_memory
                        WHERE e.relation = 'supersedes' AND e.from_memory = ANY (CAST(:ids AS bigint[]))
                          AND old.status <> 'retracted' AND old.sensitivity <> 'secret'
                        ORDER BY at DESC"""
                ),
                {"ids": list(by_id)},
            ).all()
        for memory_id, content, at in history:
            by_id[memory_id].replaced.append((content, at))
        return rows

    def touch(self, ids: Sequence[int]) -> None:
        """Count a recall of these memories (used to see what gets used)."""
        if not ids:
            return
        with self._engine.begin() as conn:
            conn.execute(
                _text()(
                    f"""UPDATE {_S}.mem_memories SET access_count = access_count + 1, last_accessed = now()
                        WHERE scope = :scope AND id = ANY (CAST(:ids AS bigint[]))"""
                ),
                {"scope": self.scope, "ids": [int(i) for i in ids]},
            )

    def neighbors(self, vector: Sequence[float], embedding_model: str, *, limit: int = 5) -> list[dict[str, Any]]:
        """Nearest current facts to a candidate statement (nightly reconcile)."""
        text = _text()
        with self._engine.begin() as conn:
            if self._iterative_scan:
                conn.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
            rows = conn.execute(
                text(
                    f"""SELECT m.id, m.kind, m.content, COALESCE(m.occurred_at, m.recorded_at) AS at,
                               1 - (m.embedding <=> CAST(:v AS vector)) AS sim
                        FROM {_S}.mem_memories m
                        WHERE m.status = 'current' AND m.embedding IS NOT NULL AND m.scope = :scope
                          AND m.embedding_model = :model AND m.kind = ANY (CAST(:kinds AS text[]))
                        ORDER BY m.embedding <=> CAST(:v AS vector) LIMIT :limit"""
                ),
                {"scope": self.scope, "v": vector_literal(vector), "model": embedding_model,
                 "kinds": list(FACT_KINDS), "limit": limit},
            ).mappings().all()
        return [dict(row) for row in rows]

    def retracted_neighbors(self, vector: Sequence[float], embedding_model: str, *, min_sim: float) -> list[dict[str, Any]]:
        """Retracted facts close to a candidate: the household asked to forget them."""
        with self._engine.connect() as conn:
            rows = conn.execute(
                _text()(
                    f"""SELECT m.id, m.status_changed_at, 1 - (m.embedding <=> CAST(:v AS vector)) AS sim
                        FROM {_S}.mem_memories m
                        WHERE m.scope = :scope AND m.status = 'retracted' AND m.embedding IS NOT NULL
                          AND m.embedding_model = :model
                          AND 1 - (m.embedding <=> CAST(:v AS vector)) >= :min_sim
                        ORDER BY m.embedding <=> CAST(:v AS vector) LIMIT 3"""
                ),
                {"scope": self.scope, "v": vector_literal(vector), "model": embedding_model, "min_sim": min_sim},
            ).mappings().all()
        return [dict(row) for row in rows]

    def pending_scrub_digests(self) -> list[dict[str, Any]]:
        """Digests retracted by a forget that still need a cleaned replacement written."""
        with self._engine.connect() as conn:
            rows = conn.execute(
                _text()(
                    f"""SELECT id, source_ref, content, status_reason FROM {_S}.mem_memories
                        WHERE scope = :scope AND kind = 'digest' AND status = 'retracted'
                          AND status_reason LIKE 'scrub_pending:%' ORDER BY source_ref"""
                ),
                {"scope": self.scope},
            ).mappings().all()
        return [dict(row) for row in rows]

    def mark_digest_scrubbed(self, conn, digest_id: int) -> None:
        conn.execute(
            _text()(f"UPDATE {_S}.mem_memories SET status_reason = 'scrubbed' WHERE id = :id"),
            {"id": digest_id},
        )

    # ── embeddings ──────────────────────────────────────────────────────────

    def missing_embeddings(self, embedding_model: str, *, limit: int = 100) -> list[tuple[int, str]]:
        """Current memories without an embedding from embedding_model (newest first)."""
        with self._engine.connect() as conn:
            rows = conn.execute(
                _text()(
                    f"""SELECT id, content FROM {_S}.mem_memories
                        WHERE scope = :scope AND status = 'current'
                          AND (embedding IS NULL OR embedding_model IS DISTINCT FROM :model)
                        ORDER BY recorded_at DESC LIMIT :limit"""
                ),
                {"scope": self.scope, "model": embedding_model, "limit": limit},
            ).all()
        return [(int(row[0]), row[1]) for row in rows]

    def set_embeddings(self, pairs: Sequence[tuple[int, Sequence[float]]], embedding_model: str) -> None:
        if not pairs:
            return
        with self._engine.begin() as conn:
            conn.execute(
                _text()(
                    f"""UPDATE {_S}.mem_memories SET embedding = CAST(:v AS vector), embedding_model = :model
                        WHERE id = :id AND scope = :scope"""
                ),
                [
                    {"id": int(memory_id), "v": vector_literal(vector), "model": embedding_model, "scope": self.scope}
                    for memory_id, vector in pairs
                ],
            )

    # ── runs ────────────────────────────────────────────────────────────────

    def start_run(self, job: str, run_date: date | None, *, attempt: int = 1) -> int | None:
        """Insert a running run row; None when the same job is already running for that day."""
        from sqlalchemy.exc import IntegrityError  # pylint: disable=import-outside-toplevel

        try:
            with self._engine.begin() as conn:
                return int(
                    conn.execute(
                        _text()(
                            f"""INSERT INTO {_S}.mem_runs (scope, job, run_date, attempt)
                                VALUES (:scope, :job, :run_date, :attempt) RETURNING id"""
                        ),
                        {"scope": self.scope, "job": job, "run_date": run_date, "attempt": attempt},
                    ).scalar()
                )
        except IntegrityError:
            return None

    def finish_run(self, run_id: int, *, status: str, stats: dict[str, Any] | None = None, error: str | None = None, conn=None) -> None:
        statement = _text()(
            f"""UPDATE {_S}.mem_runs SET status = :status, finished_at = now(), stage = NULL,
                    stats = CAST(:stats AS jsonb), error = :error WHERE id = :id"""
        )
        params = {"status": status, "stats": json.dumps(stats or {}), "error": (error or None) and error[:2000], "id": run_id}
        if conn is not None:
            conn.execute(statement, params)
            return
        with self._engine.begin() as own:
            own.execute(statement, params)

    def set_run_stage(self, run_id: int, stage: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(_text()(f"UPDATE {_S}.mem_runs SET stage = :stage WHERE id = :id"), {"stage": stage, "id": run_id})

    def fail_stale_runs(self, *, older_than: timedelta = timedelta(hours=1)) -> int:
        """Runs left 'running' by a crash or restart can't still be going: mark them failed."""
        with self._engine.begin() as conn:
            return conn.execute(
                _text()(
                    f"""UPDATE {_S}.mem_runs SET status = 'failed', finished_at = now(), error = 'interrupted'
                        WHERE scope = :scope AND status = 'running' AND started_at < :cutoff"""
                ),
                {"scope": self.scope, "cutoff": datetime.now(timezone.utc) - older_than},
            ).rowcount

    def run_history(self, job: str, run_date: date) -> dict[str, int]:
        """How many times a job has succeeded and failed for a day."""
        with self._engine.connect() as conn:
            rows = conn.execute(
                _text()(
                    f"""SELECT status, count(*) FROM {_S}.mem_runs
                        WHERE scope = :scope AND job = :job AND run_date = :run_date GROUP BY status"""
                ),
                {"scope": self.scope, "job": job, "run_date": run_date},
            ).all()
        return {row[0]: int(row[1]) for row in rows}

    def last_run(self, job: str) -> dict[str, Any] | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                _text()(
                    f"""SELECT id, run_date, status, started_at, finished_at, stats, error FROM {_S}.mem_runs
                        WHERE scope = :scope AND job = :job ORDER BY started_at DESC LIMIT 1"""
                ),
                {"scope": self.scope, "job": job},
            ).mappings().first()
        return dict(row) if row else None

    # ── housekeeping ────────────────────────────────────────────────────────

    def housekeeping(self, *, archive_retention_days: int) -> dict[str, int]:
        """Expire exchanges and commitments past their date; prune the archive past retention."""
        text = _text()
        with self._engine.begin() as conn:
            expired = conn.execute(
                text(
                    f"""UPDATE {_S}.mem_memories SET status = 'expired', status_changed_at = now(),
                            status_reason = 'expired'
                        WHERE scope = :scope AND status = 'current' AND expires_at IS NOT NULL AND expires_at <= now()"""
                ),
                {"scope": self.scope},
            ).rowcount
            pruned = 0
            if archive_retention_days > 0:
                pruned = conn.execute(
                    text(
                        f"""DELETE FROM {_S}.chat_messages
                            WHERE scope = :scope AND created_at < now() - make_interval(days => :days)"""
                    ),
                    {"scope": self.scope, "days": archive_retention_days},
                ).rowcount
        return {"expired": int(expired or 0), "archive_pruned": int(pruned or 0)}

    def purge_scope(self) -> dict[str, int]:
        """Hard-delete everything in this scope (guest data after check-out). Never for owner scope."""
        if self.scope == OWNER_SCOPE:
            raise ValueError("The owner scope is never purged")
        text = _text()
        with self._engine.begin() as conn:
            counts = {}
            for table in ("mem_memories", "chat_messages", "mem_entities", "mem_runs"):
                counts[table] = conn.execute(
                    text(f"DELETE FROM {_S}.{table} WHERE scope = :scope"), {"scope": self.scope}
                ).rowcount
        return counts

    # ── stats ───────────────────────────────────────────────────────────────

    def counts(self) -> dict[str, int]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                _text()(
                    f"""SELECT kind, count(*) FROM {_S}.mem_memories
                        WHERE scope = :scope AND status = 'current' GROUP BY kind"""
                ),
                {"scope": self.scope},
            ).all()
        return {row[0]: int(row[1]) for row in rows}

    def current_facts_for_export(self, *, include_private: bool) -> list[RecallRow]:
        """Current long-lived memories for the readable Obsidian copy."""
        with self._engine.connect() as conn:
            rows = conn.execute(
                _text()(
                    f"""SELECT {_ROW_COLUMNS} FROM {_S}.mem_memories m
                        WHERE m.scope = :scope AND m.status = 'current'
                          AND m.kind IN ('fact', 'preference', 'decision', 'commitment', 'about_assistant')
                          AND m.sensitivity <> 'secret' AND (:include_private OR m.sensitivity = 'normal')
                        ORDER BY m.kind, COALESCE(m.occurred_at, m.recorded_at) DESC"""
                ),
                {"scope": self.scope, "include_private": include_private},
            ).all()
        return [_row(row) for row in rows]


_ENTITY_KINDS = frozenset({"person", "pet", "place", "org", "project", "topic", "item", "device"})
# Kinds a topic becomes once it's named as one.
_LEARNED_KINDS = frozenset({"person", "pet"})

# Minimum ts_rank for a keyword-only auto-recall hit (calibrated in tests).
AUTO_RECALL_MIN_KEYWORD_RANK = 0.03


def _row(row: Any) -> RecallRow:
    values = row._mapping  # noqa: SLF001 (SQLAlchemy's public mapping view of a row)
    score = values.get("score")
    return RecallRow(
        id=int(values["id"]),
        kind=values["kind"],
        content=values["content"],
        at=values["at"],
        importance=int(values["importance"]),
        sensitivity=values["sensitivity"],
        about=list(values["about"] or []),
        due=values.get("due_at"),
        score=float(score) if isinstance(score, (int, float)) else 0.0,
    )


def new_exchange_id() -> str:
    return str(uuid.uuid4())
