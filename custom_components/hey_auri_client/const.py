"""Constants for the thin frontend integration prototype."""

from __future__ import annotations

DOMAIN = "hey_auri_client"
DEFAULT_NAME = "Hey Auri Client"

API_ENDPOINT = "https://api.hey-auri.com"
CONF_CLIENT_ID = "client_id"
CONF_SHARED_SECRET = "shared_secret"
CONF_REQUEST_TIMEOUT = "request_timeout"
CONF_STT_LANGUAGE = "stt_language"
CONF_TTS_STREAM_ENDPOINT = "tts_stream_endpoint"
CONF_TTS_LANGUAGE = "tts_language"
CONF_TTS_VOICE = "tts_voice"
CONF_BOOKING_ID = "booking_id"
CONF_GUEST_NAME = "guest_name"
CONF_CHECK_IN = "check_in"
CONF_CHECK_OUT = "check_out"
CONF_GUEST_COUNT = "guest_count"
CONF_NOTES = "notes"
CONF_CHECK_IN_INSTRUCTION = "check_in_instruction"
CONF_CHECK_OUT_INSTRUCTION = "check_out_instruction"
CONF_WIFI_INSTRUCTION = "wifi_instruction"
CONF_PARKING_INSTRUCTION = "parking_instruction"
CONF_HOUSE_RULES = "house_rules"
CONF_TRASH_DISPOSAL_INSTRUCTION = "trash_disposal_instruction"
CONF_HOST_CONTACT_INSTRUCTION = "host_contact_instruction"
CONF_PROPERTY_KNOWLEDGE = "property_knowledge"
CONF_LOCAL_RECOMMENDATIONS = "local_recommendations"
DEFAULT_REQUEST_TIMEOUT = 30
DEFAULT_STT_PIPELINE_ID = "auri-cloud-voice"
DEFAULT_STT_LANGUAGE = "en"
DEFAULT_TTS_LANGUAGE = "en"
DEFAULT_TTS_VOICE = "alloy"
DEFAULT_TTS_STREAM_ENDPOINT = f"{API_ENDPOINT}/tts/stream"
DEFAULT_TTS_SUPPORTED_VOICES = [
    "alloy",
    "ash",
    "ballad",
    "coral",
    "echo",
    "fable",
    "onyx",
    "nova",
    "sage",
    "shimmer",
]
DEFAULT_REALTIME_WS_ENDPOINT = "wss://realtime.hey-auri.com/ws/realtime"

TRANSCRIBE_PATH = "/voice/transcribe"
TTS_STREAM_PATH = "/tts/stream"
WAKE_WORD_COLLISION_WINDOW_SECONDS = 1.0
MIN_REALTIME_AUDIO_BYTES = 24000  # 200 ms of 16 kHz mono 16-bit PCM
MIN_FALLBACK_AUDIO_BYTES = 24000  # 200 ms of 16 kHz mono 16-bit PCM

LATENCY_MEASUREMENT_KEY_CONVERSATION = "conversation"
LATENCY_MEASUREMENT_KEY_AUDIO = "audio"
LATENCY_MEASUREMENT_KEY_AUDIO_STREAM = "audio_stream"
LATENCY_MEASUREMENT_KEY_TTS = "tts"
LATENCY_MEASUREMENT_KEY_TTS_STREAM = "tts_stream"

ERROR_NO_NO_AUDIO_CHUNKS = "RT001"
ERROR_NO_AUDIO_TOO_SHORT = "RT002"
ERROR_NO_NO_UPSTREAM_AUDIO = "RT003"
ERROR_NO_EMPTY_TRANSCRIPT = "RT004"
ERROR_NO_TIMEOUT = "RT005"
ERROR_NO_WS_CLOSED_DURING_STREAM = "RT006"
ERROR_NO_WS_CLOSED_BEFORE_END = "RT007"
ERROR_NO_WS_CLOSED_NO_TRANSCRIPT = "RT008"
ERROR_NO_UPSTREAM_REPORTED_ERROR = "RT009"
ERROR_NO_AIOHTTP_CLIENT = "RT010"
ERROR_NO_CONNECTION = "RT011"
ERROR_NO_AUTHENTICATION = "RT012"
ERROR_NO_UNHANDLED = "RT999"

VOICE_AGENT_ERROR_ACCOUNT_NOT_ACTIVE = "account_not_active"

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
ATTR_NOTE_ID = "note_id"
ATTR_TITLE = "title"
ATTR_MARKDOWN = "markdown"
STICKY_NOTE_UNIQUE_ID_PREFIX = "auri_sticky_note_"

SERVICE_START_AURI_TIMER = "start_auri_timer"
SERVICE_GET_AURI_TIMERS = "get_auri_timers"
SERVICE_CREATE_AURI_STICKY_NOTE = "create_auri_sticky_note"
SERVICE_DELETE_AURI_STICKY_NOTE = "delete_auri_sticky_note"
SERVICE_GET_AURI_STICKY_NOTES = "get_auri_sticky_notes"

EVENT_AURI_TIMER_FINISHED = f"{DOMAIN}.auri_timer_finished"
EVENT_CONVERSATION_FINISHED = f"{DOMAIN}.conversation_finished"

DATA_AGENT = "agent"
DATA_FRONTEND_CLIENT = "frontend_client"

ROOT_RUNTIME = {
    "timers": {},
    "sticky_notes": {},
    "async_add_entities": None,
}
