from __future__ import annotations

from datetime import UTC, datetime
import logging
import uuid

from typing import TYPE_CHECKING, Literal

from homeassistant.components import conversation
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import MATCH_ALL
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import intent

from ..const import (
    API_ENDPOINT,
    CONF_CLIENT_ID,
    CONF_MEMORY_LOGIN_PEOPLE,
    CONF_SHARED_SECRET,
    DATA_FRONTEND_CLIENT,
    DOMAIN,
    EVENT_CONVERSATION_FINISHED,
    FAST_PATH_ACK_PHRASES,
    MEMORY_EXAM_CONVERSATION_PREFIX,
    VOICE_AGENT_ERROR_ACCOUNT_NOT_ACTIVE,
)
from ..cache import reset_processed_entities
from ..exceptions import SaaSRequestError, ToolExecutionError
from .helpers import build_context_snapshot, get_response_language
from ..helpers import get_timeout_seconds
from .local_tool_executor import MEMORY_TOOL_NAMES, LocalToolExecutor
from ..memory.identity import Speaker, TurnMeta, async_resolve_speaker
from ..metric_service import RequestLatencyMetricService
from ..saas_client import SaaSClient, SaaSTurnResponse

if TYPE_CHECKING:
    from ..memory.runtime import MemoryRuntime

_LOGGER = logging.getLogger(__name__)

# Simple device-action tools whose result is fully self-contained (no follow-up
# info the model might still want to add). When every tool call in a batch is
# one of these AND every result came back cleanly successful, we skip waiting
# for AuriService to turn that into a spoken confirmation and just say "Done"
# immediately -- the tool_results are still sent, just not awaited before
# replying. Read-only queries and the turn_on/turn_off/call_service
# hallucination fallbacks are deliberately excluded: the former have no fixed
# "success" shape to check, and the latter always carry a retry nudge the
# model needs to see.
_FAST_PATH_ACTION_TOOL_NAMES = frozenset(
    {
        "turn_on_light",
        "turn_off_light",
        "adjust_light_brightness",
        "adjust_light_color",
        "open_cover",
        "close_cover",
        "set_cover_position",
        "stop_cover",
        "open_cover_tilt",
        "close_cover_tilt",
        "set_cover_tilt_position",
        "turn_on_climate",
        "turn_off_climate",
        "set_temperature",
        "set_humidity",
        "set_fan_mode",
        "set_hvac_mode",
        "turn_on_media_player",
        "turn_off_media_player",
        "media_player_play",
        "media_player_pause",
        "play_next_track",
        "play_previous_track",
        "set_media_shuffle",
        "adjust_media_volume",
        "select_media_source",
        "search_and_play_music",
        "set_media_mute",
        "transfer_music",
    }
)
_DEVICE_TOOL_NAMES = _FAST_PATH_ACTION_TOOL_NAMES | {"turn_on", "turn_off", "call_service"}


def _turn_kind(user_input: conversation.ConversationInput, tool_names: list[str]) -> str:
    """How the memory archive classifies a turn (the nightly job skips some kinds)."""
    if user_input.context.user_id is None and user_input.device_id is None:
        # No person and no satellite: an automation or script talking to Auri.
        return "automatic"
    if any(name in MEMORY_TOOL_NAMES for name in tool_names):
        return "memory_command"
    if tool_names and all(name in _DEVICE_TOOL_NAMES for name in tool_names):
        return "device_control"
    return "chat"


def _is_fast_path_eligible(
    tool_calls: list[dict] | None,
    tool_results: list[dict],
) -> bool:
    """Return True when every tool call this round is a whitelisted action that succeeded cleanly."""
    if not tool_calls:
        return False

    for tool_call in tool_calls:
        function_name = (tool_call.get("function") or {}).get("name")
        if function_name not in _FAST_PATH_ACTION_TOOL_NAMES:
            return False

    for tool_result in tool_results:
        result = tool_result.get("result")
        if not isinstance(result, dict):
            return False
        if result.get("success") is not True:
            return False
        if "retry" in result or "error" in result:
            return False

    return True


class ThinOpenAIAgent(conversation.AbstractConversationAgent):
    """Thin Home Assistant frontend for the SaaS conversation backend."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        timeout = get_timeout_seconds(entry.options)
        self.request_latency_metrics = RequestLatencyMetricService.from_options(
            hass,
            entry,
        )
        self.client = SaaSClient(
            hass,
            endpoint=API_ENDPOINT,
            timeout=timeout,
            client_id=str(entry.data[CONF_CLIENT_ID]).strip(),
            shared_secret=str(entry.data[CONF_SHARED_SECRET]),
            metrics_service=self.request_latency_metrics,
        )
        self.memory: MemoryRuntime | None = None
        self.tool_executor = LocalToolExecutor(hass)

    def attach_memory(self, memory: MemoryRuntime) -> None:
        """Use the entry's memory runtime (created after this agent's client, which it calls)."""
        self.memory = memory
        self.tool_executor = LocalToolExecutor(self.hass, memory=memory)

    @property
    def supported_languages(self) -> list[str] | Literal["*"]:
        """Return supported languages."""
        return MATCH_ALL

    def _note_response(self, response: SaaSTurnResponse, turn: TurnMeta) -> None:
        """Track what the backend decided about mode and memory for this account."""
        turn.mode = response.mode
        turn.memory_allowed = bool(response.memory_allowed)
        if self.memory is not None:
            self.memory.note_mode(response.mode, bool(response.memory_allowed))

    def _log_turn(
        self,
        user_input: conversation.ConversationInput,
        turn: TurnMeta,
        *,
        reply: str | None,
        tool_names: list[str],
        kind: str | None = None,
    ) -> None:
        """Archive the exchange for the nightly memory job, in the memory of the turn's mode.

        Owner turns go to the household's memory; guest turns to the guests'
        (only when guest memory is on). Needs memory allowed for the account.
        """
        memory = self.memory
        if (
            memory is None
            or turn.mode not in ("owner", "guest")
            or not turn.memory_allowed
            or not turn.conversation_id
            or turn.conversation_id.startswith(MEMORY_EXAM_CONVERSATION_PREFIX)
        ):
            return
        memory.log_turn(
            mode=turn.mode,
            conversation_id=turn.conversation_id,
            speaker=turn.speaker or Speaker(),
            user_text=user_input.text,
            reply=reply,
            kind=kind or _turn_kind(user_input, tool_names),
            tool_names=tool_names,
            language=user_input.language,
        )

    async def _send_tool_results_fire_and_forget(self, payload: dict) -> None:
        """Best-effort background dispatch of tool results after a fast-path reply.

        The user already heard "Done"; there is no user-facing response left to
        attach an error to, so failures are logged only. This does mean
        AuriService's conversation history can end up missing this round if the
        request fails -- acceptable since it only ever fires after every tool
        call in the batch already reported success locally.
        """
        try:
            await self.client.send_tool_results(payload)
        except (SaaSRequestError, HomeAssistantError) as err:
            _LOGGER.error(
                "Fast-path tool_results dispatch failed (conversation history may be out of sync): %s",
                err,
                exc_info=err,
            )

    async def async_process(
        self, user_input: conversation.ConversationInput
    ) -> conversation.ConversationResult:
        """Process one conversation turn via the SaaS backend."""
        if user_input.text == VOICE_AGENT_ERROR_ACCOUNT_NOT_ACTIVE:
            intent_response = intent.IntentResponse(language=user_input.language)
            intent_response.async_set_speech(
                "There is an issue with your account, please log in to resolve it"
            )
            return conversation.ConversationResult(
                response=intent_response,
                conversation_id=user_input.conversation_id,
            )

        conversation_id = user_input.conversation_id or str(uuid.uuid4())
        reset_processed_entities(conversation_id)
        context = await build_context_snapshot(self.hass, user_input)
        speaker = await async_resolve_speaker(
            self.hass,
            user_id=user_input.context.user_id,
            device_id=user_input.device_id,
            area=context.get("request_area"),
            login_people=self.entry.options.get(CONF_MEMORY_LOGIN_PEOPLE),
        )
        # Filled in from each backend response: it alone decides mode and memory.
        turn = TurnMeta(mode=None, memory_allowed=False, conversation_id=conversation_id, speaker=speaker)
        if self.memory is not None:
            context["memory"] = await self.memory.async_turn_context(user_input.text, speaker, conversation_id)
        tool_names: list[str] = []
        payload = {
            "type": "user_turn",
            "conversation_id": conversation_id,
            "message": {
                "role": "user",
                "content": user_input.text,
                "ts": datetime.now(UTC).isoformat(),
            },
            "context": context,
        }

        try:
            response = await self.client.send_user_turn(payload)
            self._note_response(response, turn)
            while response.response_type == "tool_calls":
                tool_results = []
                for tool_call in response.tool_calls or []:
                    tool_names.append(str((tool_call.get("function") or {}).get("name") or ""))
                    tool_results.append(
                        await self.tool_executor.execute_tool_call(
                            tool_call,
                            conversation_id=conversation_id,
                            turn=turn,
                        )
                    )

                tool_results_payload = {
                    "type": "tool_results",
                    "conversation_id": conversation_id,
                    "turn_id": response.turn_id,
                    "context": context,
                    "tool_results": tool_results,
                }

                if _is_fast_path_eligible(response.tool_calls, tool_results):
                    self.hass.async_create_task(
                        self._send_tool_results_fire_and_forget(
                            {**tool_results_payload, "skip_ai_response": True}
                        ),
                        name="hey_auri_client_fast_path_tool_results",
                    )
                    response_language = get_response_language(self.hass)
                    ack_phrase = FAST_PATH_ACK_PHRASES.get(
                        response_language, FAST_PATH_ACK_PHRASES["en"]
                    )
                    intent_response = intent.IntentResponse(language=response_language)
                    intent_response.async_set_speech(ack_phrase)
                    self._log_turn(
                        user_input, turn, reply=ack_phrase, tool_names=tool_names, kind="device_control"
                    )
                    self.hass.bus.async_fire(
                        EVENT_CONVERSATION_FINISHED,
                        {
                            "conversation_id": conversation_id,
                            "client_id": self.entry.entry_id,
                            "response": ack_phrase,
                            "user_input": user_input.text,
                        },
                    )
                    return conversation.ConversationResult(
                        response=intent_response,
                        conversation_id=conversation_id,
                    )

                response = await self.client.send_tool_results(tool_results_payload)
                self._note_response(response, turn)
        except (SaaSRequestError, ToolExecutionError, HomeAssistantError) as err:
            _LOGGER.error("Thin frontend request failed: %s", err, exc_info=err)
            intent_response = intent.IntentResponse(language=user_input.language)
            if isinstance(err, SaaSRequestError) and (
                err.status_code == 429 or "SaaS request failed: 429" in str(err)
            ):
                intent_response.async_set_speech(
                    "There is a limit reached, please try again in a bit."
                )
                return conversation.ConversationResult(
                    response=intent_response,
                    conversation_id=conversation_id,
                )
            intent_response.async_set_error(
                intent.IntentResponseErrorCode.UNKNOWN,
                f"Something went wrong: {err}",
            )
            return conversation.ConversationResult(
                response=intent_response,
                conversation_id=conversation_id,
            )

        intent_response = intent.IntentResponse(language=get_response_language(self.hass))
        intent_response.async_set_speech(response.content or "")
        self._log_turn(user_input, turn, reply=response.content, tool_names=tool_names)

        self.hass.bus.async_fire(
            EVENT_CONVERSATION_FINISHED,
            {
                "conversation_id": conversation_id,
                "client_id": self.entry.entry_id,
                "response": response.content,
                "user_input": user_input.text,
            },
        )

        return conversation.ConversationResult(
            response=intent_response,
            conversation_id=conversation_id,
        )
