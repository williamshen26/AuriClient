"""Versioned schema for the memory stores ("auri_memory", and "auri_guest_memory" for guests).

Both schemas get the same tables. Migrations run at startup inside one
transaction holding an advisory lock, so two Home Assistant instances can't
migrate the same database at once. A database newer than this code is
refused rather than written to.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .db import MEMORY_SCHEMA, SCHEMA_TOKEN, MemoryDbError

EMBEDDING_DIMENSIONS = 768

# The statements name the schema with SCHEMA_TOKEN; _migrate fills it in.
_S = SCHEMA_TOKEN

MIGRATIONS: list[tuple[int, list[str]]] = [
    (
        1,
        [
            f"""CREATE TABLE {_S}.meta (
                key text PRIMARY KEY,
                value jsonb NOT NULL,
                updated_at timestamptz NOT NULL DEFAULT now())""",
            f"""CREATE TABLE {_S}.mem_runs (
                id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                scope text NOT NULL,
                job text NOT NULL CHECK (job IN ('nightly', 'legacy_import', 'reembed', 'exam', 'purge')),
                run_date date,
                attempt smallint NOT NULL DEFAULT 1,
                started_at timestamptz NOT NULL DEFAULT now(),
                finished_at timestamptz,
                status text NOT NULL DEFAULT 'running'
                    CHECK (status IN ('running', 'succeeded', 'failed', 'skipped')),
                stage text,
                stats jsonb NOT NULL DEFAULT '{{}}'::jsonb,
                error text)""",
            f"CREATE UNIQUE INDEX mem_runs_one_running_uq ON {_S}.mem_runs (scope, job, run_date) WHERE status = 'running'",
            f"CREATE INDEX mem_runs_lookup_idx ON {_S}.mem_runs (scope, job, run_date, status)",
            # Archive of owner-mode turns, already redacted when written.
            f"""CREATE TABLE {_S}.chat_messages (
                id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                scope text NOT NULL,
                conversation_id text NOT NULL,
                exchange_id uuid NOT NULL,
                role text NOT NULL CHECK (role IN ('user', 'assistant')),
                content text NOT NULL,
                kind text NOT NULL DEFAULT 'chat'
                    CHECK (kind IN ('chat', 'device_control', 'memory_command', 'automatic')),
                speaker_source text NOT NULL DEFAULT 'unknown'
                    CHECK (speaker_source IN ('ha_user', 'stated', 'unknown', 'assistant', 'automation')),
                ha_user_id text,
                person_entity_id text,
                speaker_name text,
                device_id text,
                area text,
                language text,
                tool_names text[] NOT NULL DEFAULT '{{}}',
                meta jsonb NOT NULL DEFAULT '{{}}'::jsonb,
                created_at timestamptz NOT NULL DEFAULT now())""",
            f"CREATE INDEX chat_messages_scope_time_idx ON {_S}.chat_messages (scope, created_at)",
            f"CREATE INDEX chat_messages_exchange_idx ON {_S}.chat_messages (exchange_id)",
            f"""CREATE TABLE {_S}.mem_entities (
                id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                scope text NOT NULL,
                kind text NOT NULL
                    CHECK (kind IN ('household', 'person', 'pet', 'place', 'org', 'project', 'topic', 'item', 'device')),
                name text NOT NULL,
                aliases text[] NOT NULL DEFAULT '{{}}',
                parent_id bigint REFERENCES {_S}.mem_entities(id) ON DELETE SET NULL,
                ha_person_entity_id text,
                ha_user_id text,
                created_at timestamptz NOT NULL DEFAULT now(),
                updated_at timestamptz NOT NULL DEFAULT now())""",
            f"CREATE UNIQUE INDEX mem_entities_name_uq ON {_S}.mem_entities (scope, kind, lower(name))",
            f"""CREATE UNIQUE INDEX mem_entities_person_uq ON {_S}.mem_entities (scope, ha_person_entity_id)
                WHERE ha_person_entity_id IS NOT NULL""",
            f"CREATE INDEX mem_entities_aliases_gin ON {_S}.mem_entities USING gin (aliases)",
            f"""CREATE TABLE {_S}.mem_memories (
                id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                scope text NOT NULL,
                kind text NOT NULL
                    CHECK (kind IN ('fact', 'preference', 'decision', 'commitment', 'about_assistant', 'digest', 'exchange')),
                content text NOT NULL CHECK (length(content) <= 8000),
                embedding vector({EMBEDDING_DIMENSIONS}),
                embedding_model text,
                source text NOT NULL CHECK (source IN ('nightly', 'chat', 'import')),
                source_ref date,
                run_id bigint REFERENCES {_S}.mem_runs(id) ON DELETE SET NULL,
                conversation_id text,
                exchange_id uuid,
                speaker_source text,
                speaker_person_entity_id text,
                speaker_name text,
                occurred_at timestamptz,
                recorded_at timestamptz NOT NULL DEFAULT now(),
                due_at timestamptz,
                status text NOT NULL DEFAULT 'current'
                    CHECK (status IN ('current', 'superseded', 'expired', 'retracted')),
                status_changed_at timestamptz,
                status_reason text,
                superseded_by bigint REFERENCES {_S}.mem_memories(id) ON DELETE SET NULL,
                importance smallint NOT NULL DEFAULT 3 CHECK (importance BETWEEN 1 AND 5),
                sensitivity text NOT NULL DEFAULT 'normal' CHECK (sensitivity IN ('normal', 'private', 'secret')),
                access_count integer NOT NULL DEFAULT 0,
                last_accessed timestamptz,
                confirmed_count integer NOT NULL DEFAULT 0,
                last_confirmed_at timestamptz,
                expires_at timestamptz,
                content_hash text NOT NULL,
                search_text tsvector GENERATED ALWAYS AS (to_tsvector('english'::regconfig, content)) STORED)""",
            f"""CREATE UNIQUE INDEX mem_memories_current_hash_uq ON {_S}.mem_memories (scope, kind, content_hash)
                WHERE status = 'current'""",
            f"""CREATE UNIQUE INDEX mem_memories_current_digest_uq ON {_S}.mem_memories (scope, source_ref)
                WHERE kind = 'digest' AND status = 'current'""",
            f"CREATE INDEX mem_memories_kind_status_idx ON {_S}.mem_memories (scope, kind, status)",
            f"CREATE INDEX mem_memories_occurred_idx ON {_S}.mem_memories (scope, occurred_at)",
            f"CREATE INDEX mem_memories_source_ref_idx ON {_S}.mem_memories (scope, source_ref)",
            f"CREATE INDEX mem_memories_search_gin ON {_S}.mem_memories USING gin (search_text)",
            f"""CREATE INDEX mem_memories_expires_idx ON {_S}.mem_memories (expires_at)
                WHERE expires_at IS NOT NULL AND status = 'current'""",
            f"""CREATE INDEX mem_memories_embedding_hnsw ON {_S}.mem_memories
                USING hnsw (embedding vector_cosine_ops)
                WHERE status = 'current' AND embedding IS NOT NULL""",
            f"""CREATE TABLE {_S}.mem_edges (
                id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                from_memory bigint NOT NULL REFERENCES {_S}.mem_memories(id) ON DELETE CASCADE,
                relation text NOT NULL CHECK (relation IN ('about', 'supersedes', 'because_of')),
                to_memory bigint REFERENCES {_S}.mem_memories(id) ON DELETE CASCADE,
                to_entity bigint REFERENCES {_S}.mem_entities(id) ON DELETE CASCADE,
                created_at timestamptz NOT NULL DEFAULT now(),
                CHECK ((to_memory IS NULL) <> (to_entity IS NULL)))""",
            f"""CREATE UNIQUE INDEX mem_edges_uq ON {_S}.mem_edges
                (from_memory, relation, COALESCE(to_memory, 0), COALESCE(to_entity, 0))""",
            f"CREATE INDEX mem_edges_to_entity_idx ON {_S}.mem_edges (to_entity, relation) WHERE to_entity IS NOT NULL",
            f"CREATE INDEX mem_edges_to_memory_idx ON {_S}.mem_edges (to_memory, relation) WHERE to_memory IS NOT NULL",
        ],
    ),
]

LATEST_SCHEMA_VERSION = MIGRATIONS[-1][0]


class SchemaTooNewError(MemoryDbError):
    """The database was migrated by a newer version of the integration."""

    def __init__(self, found: int) -> None:
        super().__init__(
            "schema_too_new",
            f"The memory database is at schema version {found}, newer than this integration "
            f"supports ({LATEST_SCHEMA_VERSION}). Update the Hey Auri Client integration.",
        )
        self.found = found


def apply_migrations(engine, *, app_version: str, schema: str = MEMORY_SCHEMA) -> int:
    """Bring one schema up to date; return the resulting version."""
    with engine.begin() as conn:
        _lock(conn, schema)
        return _migrate(conn, schema=schema, app_version=app_version)


def reset_schema(
    engine,
    *,
    schema: str,
    app_version: str,
    seed: Callable[[Any], None] | None = None,
) -> int:
    """Drop a guest schema with everything in it and create it again, empty.

    One transaction: the schema is either fully replaced (and seeded) or left
    as it was. The owner's schema is never dropped.
    """
    if schema == MEMORY_SCHEMA:
        raise ValueError("the owner's memory schema is never reset")
    from sqlalchemy import text  # pylint: disable=import-outside-toplevel

    with engine.begin() as conn:
        _lock(conn, schema)
        conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        version = _migrate(conn, schema=schema, app_version=app_version)
        if seed is not None:
            seed(conn)
    return version


def drop_schema(engine, *, schema: str) -> None:
    """Remove a guest schema with everything in it (guest memory switched off). Never the owner's."""
    if schema == MEMORY_SCHEMA:
        raise ValueError("the owner's memory schema is never dropped")
    from sqlalchemy import text  # pylint: disable=import-outside-toplevel

    with engine.begin() as conn:
        _lock(conn, schema)
        conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))


def _lock(conn, schema: str) -> None:
    from sqlalchemy import text  # pylint: disable=import-outside-toplevel

    conn.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": f"{schema}:migrations"})


def _migrate(conn, *, schema: str, app_version: str) -> int:
    from sqlalchemy import text  # pylint: disable=import-outside-toplevel

    def sql(statement: str):
        return text(statement.replace(SCHEMA_TOKEN, schema))

    # Extension before schema: while auri_memory doesn't exist yet, a fresh
    # install lands in the usual schema (public) rather than ours, which
    # comes first in the connection's search_path.
    conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    conn.execute(sql(f"CREATE SCHEMA IF NOT EXISTS {_S}"))
    conn.execute(
        sql(
            f"""CREATE TABLE IF NOT EXISTS {_S}.schema_migrations (
                version int PRIMARY KEY,
                applied_at timestamptz NOT NULL DEFAULT now(),
                app_version text NOT NULL)"""
        )
    )
    current = int(conn.execute(sql(f"SELECT COALESCE(MAX(version), 0) FROM {_S}.schema_migrations")).scalar())
    if current > LATEST_SCHEMA_VERSION:
        raise SchemaTooNewError(current)

    for version, statements in MIGRATIONS:
        if version <= current:
            continue
        for statement in statements:
            conn.execute(sql(statement))
        conn.execute(
            sql(f"INSERT INTO {_S}.schema_migrations (version, app_version) VALUES (:version, :app_version)"),
            {"version": version, "app_version": app_version},
        )
        current = version
    return current
