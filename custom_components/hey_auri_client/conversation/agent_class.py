from __future__ import annotations

from datetime import UTC, datetime
import logging
import uuid

from typing import Literal

from homeassistant.components import conversation
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import MATCH_ALL
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import intent

from ..const import (
    API_ENDPOINT,
    CONF_CLIENT_ID,
    CONF_SHARED_SECRET,
    DATA_FRONTEND_CLIENT,
    DOMAIN,
    EVENT_CONVERSATION_FINISHED,
    VOICE_AGENT_ERROR_ACCOUNT_NOT_ACTIVE,
)
from ..cache import reset_processed_entities
from ..exceptions import SaaSRequestError, ToolExecutionError
from .helpers import build_context_snapshot
from ..helpers import get_timeout_seconds
from .local_tool_executor import LocalToolExecutor
from ..metric_service import RequestLatencyMetricService
from ..saas_client import SaaSClient

_LOGGER = logging.getLogger(__name__)


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
        self.tool_executor = LocalToolExecutor(hass)

    @property
    def supported_languages(self) -> list[str] | Literal["*"]:
        """Return supported languages."""
        return MATCH_ALL

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
            while response.response_type == "tool_calls":
                tool_results = []
                for tool_call in response.tool_calls or []:
                    tool_results.append(
                        await self.tool_executor.execute_tool_call(
                            tool_call,
                            context["exposed_entities"],
                            conversation_id=conversation_id,
                        )
                    )

                response = await self.client.send_tool_results(
                    {
                        "type": "tool_results",
                        "conversation_id": conversation_id,
                        "turn_id": response.turn_id,
                        "context": context,
                        "tool_results": tool_results,
                    }
                )
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

        intent_response = intent.IntentResponse(language=user_input.language)
        intent_response.async_set_speech(response.content or "")

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
