"""PostgreSQL connection handling for the memory store.

Uses SQLAlchemy (part of Home Assistant Core) with psycopg2 (preinstalled in
the Home Assistant images), so the integration adds no requirements. Both
are imported lazily: Core-in-venv installs without psycopg2 get a clear
error instead of a failed integration load. Everything here is blocking and
must run in an executor thread.
"""
from __future__ import annotations

from typing import Any

from .config import MemoryConfig

MEMORY_SCHEMA = "auri_memory"
MIN_SERVER_VERSION_NUM = 130000  # PostgreSQL 13
APPLICATION_NAME = "hey_auri_client"

# SQLSTATE codes we turn into specific setup errors.
_SQLSTATE_INSUFFICIENT_PRIVILEGE = "42501"
_SQLSTATE_UNDEFINED_FILE = "58P01"
_SQLSTATE_FEATURE_NOT_SUPPORTED = "0A000"


class MemoryDbError(Exception):
    """A setup or connection problem with a stable code for the config flow."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _require_drivers() -> None:
    try:
        import psycopg2  # noqa: F401  pylint: disable=import-outside-toplevel,unused-import
        import sqlalchemy  # noqa: F401  pylint: disable=import-outside-toplevel,unused-import
    except ImportError as err:
        raise MemoryDbError(
            "psycopg2_missing",
            "The PostgreSQL driver (psycopg2) isn't installed in this Home Assistant environment.",
        ) from err


def build_engine(cfg: MemoryConfig, *, vector_schema: str = "public", pooled: bool = True):
    """Create a SQLAlchemy engine for the memory database.

    search_path covers the memory schema and the schema holding the pgvector
    type, so CAST(:v AS vector) resolves however the extension was installed.
    """
    _require_drivers()
    from sqlalchemy import create_engine  # pylint: disable=import-outside-toplevel
    from sqlalchemy.engine import URL  # pylint: disable=import-outside-toplevel
    from sqlalchemy.pool import NullPool  # pylint: disable=import-outside-toplevel

    url = URL.create(
        "postgresql+psycopg2",
        username=cfg.username,
        password=cfg.password or None,
        host=cfg.host,
        port=cfg.port,
        database=cfg.database,
    )
    search_path = ",".join(dict.fromkeys([MEMORY_SCHEMA, vector_schema, "public"]))
    connect_args = {
        "connect_timeout": 5,
        "application_name": APPLICATION_NAME,
        "sslmode": cfg.sslmode,
        "options": f"-c statement_timeout=5000 -c search_path={search_path}",
    }
    if not pooled:
        return create_engine(url, poolclass=NullPool, connect_args=connect_args)
    return create_engine(
        url,
        pool_size=3,
        max_overflow=2,
        pool_pre_ping=True,
        pool_recycle=1800,
        pool_timeout=5,
        connect_args=connect_args,
    )


def classify_connection_error(err: Exception) -> MemoryDbError:
    """Map a driver error raised while connecting to a config-flow error code."""
    message = str(getattr(err, "orig", None) or err)
    lowered = message.lower()
    if "password authentication failed" in lowered or "no password supplied" in lowered or (
        "role" in lowered and "does not exist" in lowered
    ):
        return MemoryDbError("invalid_auth", "The database rejected the username or password.")
    if "database" in lowered and "does not exist" in lowered:
        return MemoryDbError("cannot_connect", "That database doesn't exist on the server.")
    return MemoryDbError("cannot_connect", "Can't reach the PostgreSQL server with these settings.")


def _sqlstate(err: Exception) -> str | None:
    return getattr(getattr(err, "orig", None), "pgcode", None)


def classify_setup_error(err: Exception) -> MemoryDbError:
    """Map a driver error from enabling pgvector or creating the schema to a setup error code."""
    if _sqlstate(err) == _SQLSTATE_INSUFFICIENT_PRIVILEGE:
        return MemoryDbError(
            "insufficient_privilege",
            f"This database user can't enable pgvector or create the {MEMORY_SCHEMA} schema. "
            "Have an administrator run: CREATE EXTENSION vector;",
        )
    if _sqlstate(err) in {_SQLSTATE_UNDEFINED_FILE, _SQLSTATE_FEATURE_NOT_SUPPORTED} or "vector" in str(err).lower():
        return MemoryDbError("pgvector_missing", "This PostgreSQL server doesn't have the pgvector extension installed.")
    return classify_connection_error(err)


def detect_vector_extension(conn) -> tuple[str, str] | None:
    """Return (schema, version) of the installed pgvector extension, or None."""
    from sqlalchemy import text  # pylint: disable=import-outside-toplevel

    row = conn.execute(
        text(
            "SELECT n.nspname, e.extversion FROM pg_extension e "
            "JOIN pg_namespace n ON n.oid = e.extnamespace WHERE e.extname = 'vector'"
        )
    ).first()
    return (row[0], row[1]) if row else None


def test_connection(cfg: MemoryConfig) -> dict[str, Any]:
    """Dry-run the setup: connect, check versions and privileges, change nothing.

    CREATE EXTENSION and CREATE SCHEMA run inside a transaction that is
    always rolled back (DDL is transactional in PostgreSQL), which proves
    the account can do them without leaving anything behind.
    """
    _require_drivers()
    from sqlalchemy import text  # pylint: disable=import-outside-toplevel
    from sqlalchemy.exc import DBAPIError, OperationalError  # pylint: disable=import-outside-toplevel

    engine = build_engine(cfg, pooled=False)
    try:
        try:
            conn = engine.connect()
        except OperationalError as err:
            raise classify_connection_error(err) from err

        with conn:
            # Everything runs in one transaction that is always rolled back
            # (SQLAlchemy would otherwise auto-begin on the first statement).
            transaction = conn.begin()
            try:
                server_version = int(conn.execute(text("SHOW server_version_num")).scalar() or 0)
                if server_version < MIN_SERVER_VERSION_NUM:
                    raise MemoryDbError("pg_version_unsupported", "PostgreSQL 13 or newer is required.")

                try:
                    conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
                except DBAPIError as err:
                    raise classify_setup_error(err) from err
                vector = detect_vector_extension(conn)
                try:
                    conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {MEMORY_SCHEMA}"))
                except DBAPIError as err:
                    if _sqlstate(err) == _SQLSTATE_INSUFFICIENT_PRIVILEGE:
                        raise MemoryDbError(
                            "insufficient_privilege",
                            f"This user can't create the {MEMORY_SCHEMA} schema in this database.",
                        ) from err
                    raise
            finally:
                transaction.rollback()

        return {
            "server_version_num": server_version,
            "pgvector_version": vector[1] if vector else None,
        }
    except MemoryDbError:
        raise
    except OperationalError as err:
        raise classify_connection_error(err) from err
    finally:
        engine.dispose()
