"""Constants for the thin frontend integration prototype."""

from __future__ import annotations

DOMAIN = "hey_auri_client"
DEFAULT_NAME = "Hey Auri Client"

API_ENDPOINT = "https://api.hey-auri.com"
CONF_CLIENT_ID = "client_id"
CONF_SHARED_SECRET = "shared_secret"
CONF_REQUEST_TIMEOUT = "request_timeout"
DEFAULT_REQUEST_TIMEOUT = 30

CONF_METRICS_ENABLED = "metrics_enabled"
CONF_METRICS_HOST = "metrics_host"
CONF_METRICS_PORT = "metrics_port"
CONF_METRICS_DATABASE = "metrics_database"
CONF_METRICS_USERNAME = "metrics_username"
CONF_METRICS_PASSWORD = "metrics_password"
CONF_METRICS_MEASUREMENT = "metrics_measurement"
CONF_METRICS_SSL = "metrics_ssl"

DEFAULT_METRICS_PORT = 8086
DEFAULT_METRICS_MEASUREMENT = "saas_request_latency"

ATTR_DURATION = "duration"
ATTR_REMAINING = "remaining"
ATTR_TIMER_ID = "timer_id"
ATTR_ENTITY_ID = "entity_id"
ATTR_SATELLITE_SPEAKER = "satellite_speaker"

SERVICE_START_AURI_TIMER = "start_auri_timer"
SERVICE_GET_AURI_TIMERS = "get_auri_timers"

EVENT_AURI_TIMER_FINISHED = f"{DOMAIN}.auri_timer_finished"
EVENT_CONVERSATION_FINISHED = f"{DOMAIN}.conversation_finished"

DATA_AGENT = "agent"
DATA_FRONTEND_CLIENT = "frontend_client"

ROOT_RUNTIME = {
    "timers": {},
    "async_add_entities": None,
}
