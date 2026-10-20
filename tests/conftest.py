"""Fixtures for the integration's tests.

Run from the AuriClient repo root:
    python -m pytest tests
The pure memory tests need neither Home Assistant nor a database. Tests
marked with Home Assistant imports are skipped when it isn't installed
(pip install pytest-homeassistant-custom-component). Postgres-backed tests
need a pgvector server; point AURI_TEST_PG_* at one (defaults match
`docker run -p 127.0.0.1:55432:5432 -e POSTGRES_USER=auri
-e POSTGRES_PASSWORD=auri-test -e POSTGRES_DB=auri pgvector/pgvector:pg17`).
They are skipped when it isn't reachable.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import sys
import types

import pytest

_INTEGRATION = Path(__file__).resolve().parents[1] / "custom_components" / "hey_auri_client"
HAS_HOMEASSISTANT = importlib.util.find_spec("homeassistant") is not None

# Without Home Assistant, import custom_components.hey_auri_client.memory.*
# without executing the integration's __init__ (which imports it).
if not HAS_HOMEASSISTANT and "custom_components.hey_auri_client" not in sys.modules:
    _namespace = types.ModuleType("custom_components")
    _namespace.__path__ = [str(_INTEGRATION.parent)]
    _package = types.ModuleType("custom_components.hey_auri_client")
    _package.__path__ = [str(_INTEGRATION)]
    sys.modules.setdefault("custom_components", _namespace)
    sys.modules["custom_components.hey_auri_client"] = _package

from custom_components.hey_auri_client.memory.config import MemoryConfig  # noqa: E402


def pytest_collection_modifyitems(config, items):
    """Skip tests that need the hass fixture when Home Assistant's test plugin isn't loaded."""
    if config.pluginmanager.has_plugin("homeassistant"):
        return
    skip = pytest.mark.skip(
        reason="needs pytest-homeassistant-custom-component's plugin (it doesn't load on Windows)"
    )
    for item in items:
        if "hass" in getattr(item, "fixturenames", ()):
            item.add_marker(skip)


def _pg_config(**overrides) -> MemoryConfig:
    values = {
        "enabled": True,
        "host": os.environ.get("AURI_TEST_PG_HOST", "127.0.0.1"),
        "port": int(os.environ.get("AURI_TEST_PG_PORT", "55432")),
        "database": os.environ.get("AURI_TEST_PG_DB", "auri"),
        "username": os.environ.get("AURI_TEST_PG_USER", "auri"),
        "password": os.environ.get("AURI_TEST_PG_PASSWORD", "auri-test"),
        "sslmode": "disable",
        "vault_path": "",
        "vault_include_private": False,
        "day_boundary_hour": 4,
        "archive_retention_days": 365,
    }
    values.update(overrides)
    return MemoryConfig(**values)


@pytest.fixture
def pg_config():
    return _pg_config


@pytest.fixture
def pg_engine():
    """An engine on a freshly migrated, empty auri_memory schema."""
    sqlalchemy = pytest.importorskip("sqlalchemy")
    pytest.importorskip("psycopg2")
    from custom_components.hey_auri_client.memory.db import build_engine
    from custom_components.hey_auri_client.memory.migrations import apply_migrations

    engine = build_engine(_pg_config())
    try:
        with engine.begin() as conn:
            conn.execute(sqlalchemy.text("DROP SCHEMA IF EXISTS auri_memory CASCADE"))
    except sqlalchemy.exc.OperationalError:
        engine.dispose()
        pytest.skip("No test PostgreSQL server reachable")
    apply_migrations(engine, app_version="test")
    yield engine
    engine.dispose()
