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
CONF_GUEST_PHONE_NUMBER = "guest_phone_number"
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
# Shared ceiling for both STT (populates the language dropdown in HA's Assist
# pipeline editor) and TTS (the set of languages get_response_language() can
# resolve to, validated by set_language before persisting) — keeping one list
# means a language you can pick for STT is always one TTS can actually speak
# back. Cartesia's /tts/bytes `language` field only accepts this exact set of
# ISO 639-1 codes. Source: Cartesia /tts/bytes API reference request-schema
# enum (checked 2026-07-14) — update this list if Cartesia adds languages.
SUPPORTED_LANGUAGES = (
    "en", "fr", "de", "es", "pt", "zh", "ja", "hi", "it", "ko",
    "nl", "pl", "ru", "sv", "tr", "tl", "bg", "ro", "ar", "cs",
    "el", "fi", "hr", "ms", "sk", "da", "ta", "uk", "hu", "no",
    "vi", "bn", "th", "he", "ka", "id", "te", "gu", "kn", "ml",
    "mr", "pa",
)
# Short spoken acknowledgment ("OK"/"done") per SUPPORTED_LANGUAGES code, used
# by the fast-path reply in conversation/agent_class.py so a quick device
# action doesn't have to wait on a full LLM-generated confirmation just to
# speak back in the right language. One entry per SUPPORTED_LANGUAGES code --
# keep the two lists in sync. Best-effort translations, not native-speaker
# verified for every language; worth a native-speaker pass before relying on
# it for the less common ones.
FAST_PATH_ACK_PHRASES: dict[str, str] = {
    "en": "OK",
    "fr": "D'accord",
    "de": "Erledigt",
    "es": "Listo",
    "pt": "Pronto",
    "zh": "好的",
    "ja": "了解",
    "hi": "ठीक है",
    "it": "Fatto",
    "ko": "완료",
    "nl": "Gedaan",
    "pl": "Gotowe",
    "ru": "Готово",
    "sv": "Klart",
    "tr": "Tamam",
    "tl": "Sige",
    "bg": "Готово",
    "ro": "Gata",
    "ar": "تم",
    "cs": "Hotovo",
    "el": "Έγινε",
    "fi": "Valmis",
    "hr": "Gotovo",
    "ms": "Selesai",
    "sk": "Hotovo",
    "da": "Klar",
    "ta": "சரி",
    "uk": "Готово",
    "hu": "Kész",
    "no": "Klart",
    "vi": "Xong",
    "bn": "ঠিক আছে",
    "th": "เรียบร้อย",
    "he": "בסדר",
    "ka": "კარგი",
    "id": "Selesai",
    "te": "సరే",
    "gu": "ઠીક છે",
    "kn": "ಸರಿ",
    "ml": "ശരി",
    "mr": "ठीक आहे",
    "pa": "ਠੀਕ ਹੈ",
}
# Persona names, not provider voice names — the backend resolves each to a
# real per-provider/per-language voice (see AuriService cartesia_client.py
# PERSONA_TO_GENDER / tts_stream_app.py _OPENAI_VOICE_BY_PERSONA).
DEFAULT_TTS_VOICE = "auri"
DEFAULT_TTS_STREAM_ENDPOINT = f"{API_ENDPOINT}/tts/stream"
DEFAULT_TTS_SUPPORTED_VOICES = [
    "glen",
    "auri",
]
DEFAULT_REALTIME_WS_ENDPOINT = "wss://realtime.hey-auri.com/ws/realtime"

TRANSCRIBE_PATH = "/voice/transcribe"
TTS_STREAM_PATH = "/tts/stream"
# Raised from 300ms (9600 bytes): captures in the 300-1000ms range still
# passed this gate, got sent to the realtime service, then burned the full
# post-audio.end max_wait_seconds (1.5s, see stream_realtime_transcription)
# waiting for a transcript that never arrives for audio this short --
# dead time on top of the fallback-skip below. Real captures so far are all
# 1.5s+; raising this to match MIN_FALLBACK_AUDIO_BYTES means audio this
# short skips the realtime attempt (and its 1.5s dead wait) entirely.
MIN_REALTIME_AUDIO_BYTES = 32000  # 1000 ms of 16 kHz mono 16-bit PCM
MIN_FALLBACK_AUDIO_BYTES = 32000  # 1000 ms of 16 kHz mono 16-bit PCM

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
ATTR_SATELLITE_TIMER_RING = "satellite_timer_ring"
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
