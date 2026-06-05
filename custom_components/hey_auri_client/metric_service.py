"""InfluxDB-backed request latency metrics for SaaS client calls."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import logging
from typing import Any

from influxdb import InfluxDBClient

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_METRICS_DATABASE,
    CONF_METRICS_ENABLED,
    CONF_METRICS_HOST,
    CONF_METRICS_MEASUREMENT,
    CONF_METRICS_PASSWORD,
    CONF_METRICS_PORT,
    CONF_METRICS_SSL,
    CONF_METRICS_USERNAME,
    DEFAULT_METRICS_MEASUREMENT,
    DEFAULT_METRICS_PORT,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class InfluxLatencyMetricsConfig:
    """Runtime configuration for request latency metrics."""

    enabled: bool
    host: str
    port: int
    database: str
    username: str
    password: str
    measurement: str = DEFAULT_METRICS_MEASUREMENT
    ssl: bool = False


class RequestLatencyMetricService:
    """Writes SaaS request latency points into InfluxDB 1.x."""

    def __init__(self, hass: HomeAssistant, config: InfluxLatencyMetricsConfig) -> None:
        self._hass = hass
        self._config = config

    @classmethod
    def from_options(
        cls,
        hass: HomeAssistant,
        entry: ConfigEntry,
    ) -> RequestLatencyMetricService:
        """Build service from config entry options."""
        options = entry.options
        enabled = bool(options.get(CONF_METRICS_ENABLED, False))

        config = InfluxLatencyMetricsConfig(
            enabled=enabled,
            host=str(options.get(CONF_METRICS_HOST, "")).strip(),
            port=_parse_int(
                options.get(CONF_METRICS_PORT),
                DEFAULT_METRICS_PORT,
            ),
            database=str(options.get(CONF_METRICS_DATABASE, "")).strip(),
            username=str(options.get(CONF_METRICS_USERNAME, "")).strip(),
            password=str(options.get(CONF_METRICS_PASSWORD, "")).strip(),
            measurement=str(
                options.get(
                    CONF_METRICS_MEASUREMENT,
                    DEFAULT_METRICS_MEASUREMENT,
                )
            ).strip()
            or DEFAULT_METRICS_MEASUREMENT,
            ssl=_parse_bool(
                options.get(CONF_METRICS_SSL),
                default=False,
            ),
        )

        if config.enabled and not all(
            [config.host, config.database, config.username, config.password]
        ):
            _LOGGER.warning(
                "InfluxDB latency metrics enabled but missing one of host/database/username/password; disabling"
            )
            config.enabled = False

        return cls(hass, config)

    @property
    def enabled(self) -> bool:
        """Whether metrics writes are enabled."""
        return self._config.enabled

    async def async_record_request_latency(
        self,
        *,
        client_id: str,
        path: str,
        measurement_key: str,
        latency_ms: int,
        success: bool,
        status_code: int | None,
        error_type: str | None,
    ) -> None:
        """Record one SaaS request latency datapoint."""
        if not self._config.enabled:
            return

        await self._hass.async_add_executor_job(
            self._write_latency_point,
            client_id,
            path,
            measurement_key,
            latency_ms,
            success,
            status_code,
            error_type,
        )

    def _write_latency_point(
        self,
        client_id: str,
        path: str,
        measurement_key: str,
        latency_ms: int,
        success: bool,
        status_code: int | None,
        error_type: str | None,
    ) -> None:
        """Write one point to InfluxDB (executor thread)."""
        point = {
            "measurement": self._config.measurement,
            "time": datetime.now(UTC).isoformat(),
            "tags": {
                "client_id": client_id,
                "path": path,
                "measurement_key": measurement_key,
            },
            "fields": {
                "latency_ms": int(latency_ms),
                "success": 1 if success else 0,
            },
        }

        if status_code is not None:
            point["fields"]["status_code"] = int(status_code)
        if error_type:
            point["fields"]["error_type"] = str(error_type)

        client = InfluxDBClient(
            host=self._config.host,
            port=self._config.port,
            username=self._config.username,
            password=self._config.password,
            database=self._config.database,
            ssl=self._config.ssl,
        )
        try:
            client.switch_database(self._config.database)
            ok = client.write_points([point], time_precision="ms")
            if not ok:
                _LOGGER.warning(
                    "InfluxDB latency metric write returned False (path=%s client_id=%s)",
                    path,
                    client_id,
                )
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Failed to write InfluxDB latency metric")
        finally:
            client.close()


def _parse_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _parse_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
