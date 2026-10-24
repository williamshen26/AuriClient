"""Memory settings parsed from the config entry options."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Any, Mapping

# Option keys (also listed in const.py for the config flow).
CONF_MEMORY_ENABLED = "memory_enabled"
CONF_MEMORY_DB_HOST = "memory_db_host"
CONF_MEMORY_DB_PORT = "memory_db_port"
CONF_MEMORY_DB_NAME = "memory_db_name"
CONF_MEMORY_DB_USER = "memory_db_user"
CONF_MEMORY_DB_PASSWORD = "memory_db_password"
CONF_MEMORY_DB_SSLMODE = "memory_db_sslmode"
CONF_MEMORY_VAULT_PATH = "memory_vault_path"
CONF_MEMORY_VAULT_INCLUDE_PRIVATE = "memory_vault_include_private"
CONF_MEMORY_DAY_BOUNDARY_HOUR = "memory_day_boundary_hour"
CONF_MEMORY_ARCHIVE_RETENTION_DAYS = "memory_archive_retention_days"
CONF_MEMORY_SHARE_HOUSEHOLD = "memory_share_household"
# Who uses each Home Assistant login: {user id: person entity id, or "shared"}.
# Read on every turn, so changing it doesn't restart memory.
CONF_MEMORY_LOGIN_PEOPLE = "memory_login_people"
# Guest Settings: memory for guests in guest mode, and when the host last asked
# to start it again (a timestamp; the checkbox itself isn't stored).
CONF_GUEST_MEMORY_ENABLED = "guest_memory_enabled"
CONF_GUEST_MEMORY_RESET_AT = "guest_memory_reset_at"

MEMORY_OPTION_KEYS = frozenset(
    {
        CONF_MEMORY_ENABLED,
        CONF_MEMORY_DB_HOST,
        CONF_MEMORY_DB_PORT,
        CONF_MEMORY_DB_NAME,
        CONF_MEMORY_DB_USER,
        CONF_MEMORY_DB_PASSWORD,
        CONF_MEMORY_DB_SSLMODE,
        CONF_MEMORY_VAULT_PATH,
        CONF_MEMORY_VAULT_INCLUDE_PRIVATE,
        CONF_MEMORY_DAY_BOUNDARY_HOUR,
        CONF_MEMORY_ARCHIVE_RETENTION_DAYS,
        CONF_MEMORY_SHARE_HOUSEHOLD,
    }
)

DEFAULT_MEMORY_DB_PORT = 5432
DEFAULT_MEMORY_DB_SSLMODE = "prefer"
DEFAULT_MEMORY_DAY_BOUNDARY_HOUR = 4
DEFAULT_MEMORY_ARCHIVE_RETENTION_DAYS = 365
SSL_MODES = ("disable", "allow", "prefer", "require", "verify-ca", "verify-full")


def _parse_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return min(max(number, minimum), maximum)


@dataclass(frozen=True, slots=True)
class MemoryConfig:
    """Connection and behaviour settings for the local memory store."""

    enabled: bool
    host: str
    port: int
    database: str
    username: str
    password: str
    sslmode: str
    vault_path: str
    vault_include_private: bool
    day_boundary_hour: int
    archive_retention_days: int
    # Everyone's memories, private ones too, are used for whoever is speaking.
    share_household: bool = False

    @classmethod
    def from_options(cls, options: Mapping[str, Any]) -> "MemoryConfig":
        sslmode = str(options.get(CONF_MEMORY_DB_SSLMODE) or DEFAULT_MEMORY_DB_SSLMODE).strip()
        return cls(
            enabled=bool(options.get(CONF_MEMORY_ENABLED, False)),
            host=str(options.get(CONF_MEMORY_DB_HOST) or "").strip(),
            port=_parse_int(options.get(CONF_MEMORY_DB_PORT), DEFAULT_MEMORY_DB_PORT, 1, 65535),
            database=str(options.get(CONF_MEMORY_DB_NAME) or "").strip(),
            username=str(options.get(CONF_MEMORY_DB_USER) or "").strip(),
            password=str(options.get(CONF_MEMORY_DB_PASSWORD) or ""),
            sslmode=sslmode if sslmode in SSL_MODES else DEFAULT_MEMORY_DB_SSLMODE,
            vault_path=str(options.get(CONF_MEMORY_VAULT_PATH) or "").strip(),
            vault_include_private=bool(options.get(CONF_MEMORY_VAULT_INCLUDE_PRIVATE, False)),
            day_boundary_hour=_parse_int(
                options.get(CONF_MEMORY_DAY_BOUNDARY_HOUR), DEFAULT_MEMORY_DAY_BOUNDARY_HOUR, 0, 23
            ),
            archive_retention_days=_parse_int(
                options.get(CONF_MEMORY_ARCHIVE_RETENTION_DAYS), DEFAULT_MEMORY_ARCHIVE_RETENTION_DAYS, 0, 3650
            ),
            share_household=bool(options.get(CONF_MEMORY_SHARE_HOUSEHOLD, False)),
        )

    @property
    def configured(self) -> bool:
        """True when memory is switched on and the database settings are filled in."""
        return self.enabled and bool(self.host and self.database and self.username)

    def fingerprint(self) -> str:
        """Stable digest of the settings that need a restart, used to skip reloads when others change.

        share_household is left out: it's read on every turn.
        """
        parts = [
            str(self.enabled),
            self.host,
            str(self.port),
            self.database,
            self.username,
            self.password,
            self.sslmode,
            self.vault_path,
            str(self.vault_include_private),
            str(self.day_boundary_hour),
            str(self.archive_retention_days),
        ]
        return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
