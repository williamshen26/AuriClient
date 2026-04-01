"""Helpers for the thin frontend integration prototype."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import hmac
import json
import logging
from time import perf_counter
import secrets
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.components import conversation
from homeassistant.components.homeassistant.exposed_entities import async_should_expose
from homeassistant.const import ATTR_NAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er

from .const import (
    CONF_REQUEST_TIMEOUT,
    DEFAULT_REQUEST_TIMEOUT,
)
from .custom_services.automation_services import (
    add_automation_from_yaml,
    get_automation_metadata,
    remove_automation_by_id,
    update_automation_from_yaml,
)
from .custom_services.timer_services import (
    get_auri_timers_native,
    start_auri_timer_native,
)
from .custom_services.user_preferences_services import (
    apply_user_preference_update,
    get_user_preferences_helper,
)
from .exceptions import SaaSRequestError, ToolExecutionError
from .metric_service import RequestLatencyMetricService

try:
    from const import SKILL_REGISTRY  # type: ignore
except Exception:  # pragma: no cover
    SKILL_REGISTRY = {}

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class SaaSTurnResponse:
    """Normalized SaaS response for one step in the loop."""

    response_type: str
    conversation_id: str
    content: str | None = None
    turn_id: str | None = None
    tool_calls: list[dict[str, Any]] | None = None


class SaaSClient:
    """HTTP client for the SaaS conversation backend."""

    def __init__(
        self,
        hass: HomeAssistant,
        endpoint: str,
        timeout: int,
        client_id: str,
        shared_secret: str,
        metrics_service: RequestLatencyMetricService | None = None,
    ) -> None:
        self.hass = hass
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout
        self.client_id = client_id
        self.shared_secret = shared_secret
        self.metrics_service = metrics_service

    def _build_signed_headers(self, path: str, body: str) -> dict[str, str]:
        timestamp = str(int(datetime.now(UTC).timestamp()))
        nonce = secrets.token_hex(16)
        signing_payload = "\n".join(["POST", path, timestamp, nonce, body])
        signature = hmac.new(
            self.shared_secret.encode("utf-8"),
            signing_payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return {
            "Content-Type": "application/json",
            "X-Client-Id": self.client_id,
            "X-Timestamp": timestamp,
            "X-Nonce": nonce,
            "X-Signature": signature,
        }

    async def post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        started = perf_counter()
        success = False
        status_code: int | None = None
        error_type: str | None = None
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        headers = self._build_signed_headers(path, body)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    f"{self.endpoint}{path}",
                    data=body,
                    headers=headers,
                ) as response:
                    status_code = response.status
                    body = await response.text()
                    if response.status >= 400:
                        error_type = "http_error"
                        raise SaaSRequestError(
                            f"SaaS request failed: {response.status} {body}",
                            status_code=response.status,
                            response_body=body,
                        )
                    try:
                        parsed = json.loads(body)
                        success = True
                        return parsed
                    except json.JSONDecodeError as err:
                        error_type = "json_decode_error"
                        raise SaaSRequestError(
                            f"Invalid JSON response: {body}",
                            response_body=body,
                        ) from err
        except Exception as err:
            if error_type is None:
                error_type = type(err).__name__
            raise
        finally:
            latency_ms = max(int((perf_counter() - started) * 1000), 0)
            await self._record_request_latency(
                path=path,
                latency_ms=latency_ms,
                success=success,
                status_code=status_code,
                error_type=error_type,
            )

    async def _record_request_latency(
        self,
        *,
        path: str,
        latency_ms: int,
        success: bool,
        status_code: int | None,
        error_type: str | None,
    ) -> None:
        """Emit one request latency datapoint if metrics are enabled."""
        if self.metrics_service is None or not self.metrics_service.enabled:
            return

        await self.metrics_service.async_record_request_latency(
            client_id=self.client_id,
            path=path,
            latency_ms=latency_ms,
            success=success,
            status_code=status_code,
            error_type=error_type,
        )

    async def send_user_turn(self, payload: dict[str, Any]) -> SaaSTurnResponse:
        response = await self.post_json("/conversation", payload)
        return normalize_turn_response(response)

    async def send_tool_results(self, payload: dict[str, Any]) -> SaaSTurnResponse:
        response = await self.post_json("/conversation", payload)
        return normalize_turn_response(response)


class LocalToolExecutor:
    """Executes supported tool calls locally in Home Assistant."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def execute_tool_call(
        self,
        tool_call: dict[str, Any],
        user_input: conversation.ConversationInput,
        exposed_entities: list[dict[str, Any]],
    ) -> dict[str, Any]:
        function_payload = tool_call.get("function") or {}
        function_name = function_payload.get("name")
        if not function_name:
            raise ToolExecutionError(f"Missing function name in tool call: {tool_call}")

        arguments_raw = function_payload.get("arguments") or "{}"
        try:
            arguments = json.loads(arguments_raw)
        except json.JSONDecodeError as err:
            raise ToolExecutionError(
                f"Invalid tool arguments for {function_name}: {arguments_raw}"
            ) from err

        if function_name == "execute_service":
            result = await self._execute_service(arguments, exposed_entities)
        elif function_name == "get_skill_data":
            # Defensive compatibility fallback only.
            # Normal flow executes get_skill_data inside SaaS and should not return it.
            result = await self._get_skill_data(arguments)
        elif function_name == "get_entity_state":
            result = await self._get_entity_state(arguments)
        elif function_name == "adjust_light_brightness":
            result = await self._adjust_light_brightness(arguments)
        elif function_name == "adjust_media_volume":
            result = await self._adjust_media_volume(arguments)
        elif function_name == "select_media_source":
            result = await self._select_media_source(arguments)
        elif function_name == "set_media_mute":
            result = await self._set_media_mute(arguments)
        elif function_name == "get_forecasts":
            result = await self._get_forecasts(arguments)
        elif function_name == "update_user_preferences":
            result = await self._update_user_preferences(arguments)
        elif function_name == "get_user_preferences":
            result = await self._get_user_preferences(arguments)
        elif function_name == "get_shopping_list":
            result = await self._get_shopping_list(arguments)
        elif function_name == "add_shopping_list_item":
            result = await self._add_shopping_list_item(arguments)
        elif function_name == "remove_completed_shopping_list_item":
            result = await self._remove_completed_shopping_list_item(arguments)
        elif function_name == "get_automation_metadata_service":
            result = await self._get_automation_metadata()
        elif function_name == "add_automation":
            result = await self._add_automation(arguments)
        elif function_name == "update_automation":
            result = await self._update_automation(arguments)
        elif function_name == "remove_automation":
            result = await self._remove_automation(arguments)
        elif function_name == "start_timer":
            result = await self._start_timer(arguments)
        elif function_name == "list_timers":
            result = await self._list_timers()
        elif function_name == "get_calendar_events":
            result = await self._get_calendar_events(arguments)
        elif function_name == "add_calendar_event":
            result = await self._add_calendar_event(arguments)
        else:
            raise ToolExecutionError(f"Unsupported tool: {function_name}")

        return {
            "tool_call_id": tool_call.get("id"),
            "name": function_name,
            "result": result,
        }

    async def _get_skill_data(self, arguments: dict[str, Any]) -> dict[str, Any]:
        skill_name = str(arguments.get("skill", "")).strip().lower()
        if not skill_name:
            return {
                "success": False,
                "error": "skill is required",
                "available_skills": list(SKILL_REGISTRY.keys()),
            }

        skill = SKILL_REGISTRY.get(skill_name)
        if not skill:
            return {
                "success": False,
                "error": f"Unknown skill: {skill_name}",
                "available_skills": list(SKILL_REGISTRY.keys()),
            }

        return {
            "success": True,
            "skill": skill_name,
            "description": skill.get("description", ""),
            "prompt": skill.get("prompt", ""),
            "tools": skill.get("tools", []),
        }

    async def _execute_service(
        self,
        arguments: dict[str, Any],
        exposed_entities: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        allowed_entity_ids = {entity["entity_id"] for entity in exposed_entities}

        for service_argument in arguments.get("list", []):
            domain = str(service_argument.get("domain", "")).strip()
            service = str(service_argument.get("service", "")).strip()

            if "." in service:
                _, _, normalized_service = service.partition(".")
                if normalized_service:
                    service = normalized_service

            service_data = dict(
                service_argument.get("service_data", service_argument.get("data", {}))
            )
            entity_id = service_data.get("entity_id", service_argument.get("entity_id"))
            area_id = service_data.get("area_id")
            device_id = service_data.get("device_id")

            if isinstance(entity_id, str):
                entity_ids = [item.strip() for item in entity_id.split(",") if item.strip()]
                service_data["entity_id"] = entity_ids
            elif isinstance(entity_id, list):
                entity_ids = entity_id
            else:
                entity_ids = []

            if entity_ids and not set(entity_ids).issubset(allowed_entity_ids):
                raise ToolExecutionError(
                    f"One or more entities are not exposed: {entity_ids}"
                )

            if not entity_ids and area_id is None and device_id is None:
                raise ToolExecutionError(
                    f"Service call requires entity_id, area_id, or device_id: {service_argument}"
                )

            if not self.hass.services.has_service(domain, service):
                raise ToolExecutionError(f"Service not found: {domain}.{service}")

            try:
                await self.hass.services.async_call(
                    domain=domain,
                    service=service,
                    service_data=service_data,
                    blocking=True,
                )
                results.append({"success": True})
            except vol.error.MultipleInvalid as err:
                results.append({"retry": str(err)})
            except HomeAssistantError:
                try:
                    result = await self.hass.services.async_call(
                        domain=domain,
                        service=service,
                        service_data=service_data,
                        blocking=True,
                        return_response=True,
                    )
                    results.append({"success": True, "response": result})
                except vol.error.MultipleInvalid as err:
                    results.append({"retry": str(err)})
                except HomeAssistantError as err:
                    results.append({"success": False, "error": str(err)})

        return results


    async def _get_entity_state(self, arguments: dict[str, Any]) -> dict[str, Any]:
        entity_id = arguments.get("entity_id")
        if not entity_id:
            return {"retry": "entity_id is required for get_entity_state"}

        state = self.hass.states.get(entity_id)
        if state is None:
            raise ToolExecutionError(f"Entity not found: {entity_id}")

        return {
            "entity_id": entity_id,
            "state": state.state,
            "attributes": dict(state.attributes),
        }
    
    async def _set_media_mute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        entity_id = self._resolve_entity_id_no_fallback("media_player", arguments.get("entity_id"))
        is_volume_muted = arguments.get("is_volume_muted")
        if is_volume_muted is None:
            return {"retry": "is_volume_muted is required for set_media_mute"}
        try:
            await self.hass.services.async_call(
                domain="media_player",
                service="volume_mute",
                service_data={"entity_id": entity_id, "is_volume_muted": is_volume_muted},
                blocking=True,
            )
            return {"success": True, "entity_id": entity_id, "is_volume_muted": is_volume_muted}
        except vol.error.MultipleInvalid as err:
            return {"retry": str(err)}
        except HomeAssistantError as err:
            raise ToolExecutionError(str(err)) from err

    async def _select_media_source(self, arguments: dict[str, Any]) -> dict[str, Any]:
        entity_id = self._resolve_entity_id_no_fallback("media_player", arguments.get("entity_id"))
        source = arguments.get("source")
        if not source:
            return {"retry": "source is required for select_media_source"}
        try:
            await self.hass.services.async_call(
                domain="media_player",
                service="select_source",
                service_data={"entity_id": entity_id, "source": source},
                blocking=True,
            )
            return {"success": True, "entity_id": entity_id, "source": source}
        except vol.error.MultipleInvalid as err:
            return {"retry": str(err)}
        except HomeAssistantError as err:
            raise ToolExecutionError(str(err)) from err

    async def _adjust_media_volume(self, arguments: dict[str, Any]) -> dict[str, Any]:
        entity_id = self._resolve_entity_id_no_fallback("media_player", arguments.get("entity_id"))
        raw_volume_pct = arguments.get("volume_pct")
        raw_volume_step_pct = arguments.get("volume_step_pct")

        if raw_volume_pct is None and raw_volume_step_pct is None:
            return {
                "retry": "volume_pct or volume_step_pct is required for adjust_volume"
            }
        service_data: dict[str, Any] = {"entity_id": entity_id}
        response: dict[str, Any] = {
            "success": True,
            "entity_id": entity_id,
        }
        if raw_volume_pct is not None:
            target_volume_pct = _clamp_percentage(float(raw_volume_pct) * 100) / 100
            service_data["volume_level"] = target_volume_pct
            response["volume_pct"] = float(target_volume_pct)
        else:
            volume_step_pct = _clamp_step_percentage(float(raw_volume_step_pct) * 100) / 100
            # get entity_id volume
            state = self.hass.states.get(entity_id)
            if state is None:
                raise ToolExecutionError(f"Entity not found: {entity_id}")
            current_volume = state.attributes.get("volume_level")
            new_volume = _clamp_step_percentage((current_volume + volume_step_pct) * 100) / 100
            service_data["volume_level"] = new_volume
            response["volume_pct"] = float(new_volume)

        try:
            await self.hass.services.async_call(
                domain="media_player",
                service="volume_set",
                service_data=service_data,
                blocking=True,
            )
            return response
        except vol.error.MultipleInvalid as err:
            return {"retry": str(err)}
        except HomeAssistantError as err:
            raise ToolExecutionError(str(err)) from err

    async def _adjust_light_brightness(self, arguments: dict[str, Any]) -> dict[str, Any]:
        entity_id = self._resolve_entity_id_no_fallback("light", arguments.get("entity_id"))
        raw_brightness_pct = arguments.get("brightness_pct")
        raw_brightness_step_pct = arguments.get("brightness_step_pct")

        if raw_brightness_pct is None and raw_brightness_step_pct is None:
            return {
                "retry": "brightness_pct or brightness_step_pct is required for adjust_light_brightness"
            }

        service_data: dict[str, Any] = {"entity_id": entity_id}
        response: dict[str, Any] = {
            "success": True,
            "entity_id": entity_id,
        }

        if raw_brightness_pct is not None:
            target_brightness_pct = _clamp_percentage(raw_brightness_pct)
            service_data["brightness_pct"] = target_brightness_pct
            response["brightness_pct"] = target_brightness_pct
        else:
            brightness_step_pct = _clamp_step_percentage(raw_brightness_step_pct)
            service_data["brightness_step_pct"] = brightness_step_pct
            response["brightness_step_pct"] = brightness_step_pct

        try:
            await self.hass.services.async_call(
                domain="light",
                service="turn_on",
                service_data=service_data,
                blocking=True,
            )
            return response
        except vol.error.MultipleInvalid as err:
            return {"retry": str(err)}
        except HomeAssistantError as err:
            raise ToolExecutionError(str(err)) from err

    async def _get_forecasts(self, arguments: dict[str, Any]) -> dict[str, Any]:
        forecast_type = arguments.get("type")
        weather_entity_id = self._resolve_entity_id("weather", arguments.get("entity_id"))

        result = await self.hass.services.async_call(
            domain="weather",
            service="get_forecasts",
            service_data={"type": forecast_type, "entity_id": weather_entity_id},
            blocking=True,
            return_response=True,
        )
        return result

    async def _update_user_preferences(self, arguments: dict[str, Any]) -> dict[str, Any]:
        user_id = arguments.get("user_id")
        if not user_id:
            raise ToolExecutionError("user_id is required")

        updates = arguments.get("updates")
        user_preference = arguments.get("user_preference")
        return await apply_user_preference_update(user_id, user_preference, updates)

    async def _get_user_preferences(self, arguments: dict[str, Any]) -> dict[str, Any]:
        user_id = arguments.get("user_id")
        if not user_id:
            raise ToolExecutionError("user_id is required")
        return {
            "user_id": user_id,
            "user_preferences": await get_user_preferences_helper(user_id),
        }

    def _resolve_todo_entity_id(self, requested_entity_id: Any) -> str:
        """Resolve a valid todo entity id with fallback to first todo entity."""
        candidate = str(requested_entity_id or "").strip()
        if candidate and candidate.startswith("todo.") and self.hass.states.get(candidate):
            return candidate

        fallback = next(
            (
                state.entity_id
                for state in self.hass.states.async_all()
                if state.entity_id.startswith("todo.")
            ),
            None,
        )
        if fallback is None:
            raise ToolExecutionError("No todo/shopping list entity is available")
        return fallback

    async def _get_shopping_list(self, arguments: dict[str, Any]) -> dict[str, Any]:
        entity_id = self._resolve_todo_entity_id(arguments.get("entity_id"))
        raw_status = arguments.get("status")

        if not isinstance(raw_status, list) or not raw_status:
            raise ToolExecutionError("status is required and must be a non-empty list")

        status = [str(item).strip() for item in raw_status if str(item).strip()]
        if not status:
            raise ToolExecutionError("status is required and must include at least one value")

        try:
            result = await self.hass.services.async_call(
                domain="todo",
                service="get_items",
                service_data={"status": status},
                target={"entity_id": entity_id},
                blocking=True,
                return_response=True,
            )
            return result
        except HomeAssistantError as err:
            raise ToolExecutionError(str(err)) from err

    async def _add_shopping_list_item(self, arguments: dict[str, Any]) -> dict[str, Any]:
        entity_id = self._resolve_todo_entity_id(arguments.get("entity_id"))
        item = str(arguments.get("item", "")).strip()
        if not item:
            raise ToolExecutionError("item is required")

        try:
            await self.hass.services.async_call(
                domain="todo",
                service="add_item",
                service_data={"item": item},
                target={"entity_id": entity_id},
                blocking=True,
            )
            return {"success": True}
        except HomeAssistantError as err:
            raise ToolExecutionError(str(err)) from err

    async def _remove_completed_shopping_list_item(
        self, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        entity_id = self._resolve_todo_entity_id(arguments.get("entity_id"))

        try:
            await self.hass.services.async_call(
                domain="todo",
                service="remove_completed_items",
                target={"entity_id": entity_id},
                blocking=True,
            )
            return {"success": True}
        except HomeAssistantError as err:
            raise ToolExecutionError(str(err)) from err

    async def _get_automation_metadata(self) -> dict[str, Any]:
        return {"automation_metadata": await get_automation_metadata(self.hass)}

    async def _add_automation(self, arguments: dict[str, Any]) -> str:
        return await add_automation_from_yaml(
            self.hass,
            str(arguments.get("automation_config", "")),
        )

    async def _update_automation(self, arguments: dict[str, Any]) -> str:
        return await update_automation_from_yaml(
            self.hass,
            str(arguments.get("id", "")),
            str(arguments.get("automation_config", "")),
        )

    async def _remove_automation(self, arguments: dict[str, Any]) -> str:
        return await remove_automation_by_id(
            self.hass,
            str(arguments.get("id", "")),
        )

    def _resolve_entity_id_no_fallback(self, domain: str, requested_entity_id: Any) -> str:
        """Resolve a valid entity id for the given domain without fallback."""
        candidate = str(requested_entity_id or "").strip()
        if candidate and candidate.startswith(f"{domain}.") and self.hass.states.get(candidate):
            return candidate
        raise ToolExecutionError(f"Entity not found: {candidate}")

    def _resolve_entity_id(self, domain: str, requested_entity_id: Any) -> str:
        """Resolve a valid entity id for the given domain with fallback to first entity of that domain."""
        candidate = str(requested_entity_id or "").strip()
        if candidate and candidate.startswith(f"{domain}.") and self.hass.states.get(candidate):
            return candidate

        fallback = next(
            (
                state.entity_id
                for state in self.hass.states.async_all()
                if state.entity_id.startswith(f"{domain}.")
            ),
            None,
        )
        if fallback is None:
            raise ToolExecutionError(f"No {domain} entity is available, please add a {domain} entity to Home Assistant.")
        return fallback

    async def _get_calendar_events(self, arguments: dict[str, Any]) -> dict[str, Any]:
        entity_id = self._resolve_entity_id("calendar", arguments.get("entity_id"))
        start_time = arguments.get("start_time")
        end_time = arguments.get("end_time")
        if not start_time or not end_time:
            return {"retry": "start_time and end_time are required for get_calendar_events"}
        if end_time <= start_time:
            return {"retry": "end_time must be after start_time"}
        try:
            result = await self.hass.services.async_call(
                domain="calendar",
                service="get_events",
                service_data={
                    "start_date_time": start_time,
                    "end_date_time": end_time,
                },
                target={"entity_id": entity_id},
                blocking=True,
                return_response=True,
            )
            return result
        except HomeAssistantError as err:
            raise ToolExecutionError(str(err)) from err

    async def _add_calendar_event(self, arguments: dict[str, Any]) -> dict[str, Any]:
        entity_id = self._resolve_entity_id("calendar", arguments.get("entity_id"))
        event_title = arguments.get("event_title")
        event_description = arguments.get("event_description", "")
        event_start_time = arguments.get("event_start_time")
        event_end_time = arguments.get("event_end_time")
        event_location = arguments.get("event_location", "")
        if not event_title or not event_start_time or not event_end_time:
            return {"retry": "event_title, event_start_time, and event_end_time are required for add_calendar_event"}
        try:
            await self.hass.services.async_call(
                domain="calendar",
                service="create_event",
                service_data={
                    "summary": event_title,
                    "description": event_description,
                    "start_date_time": event_start_time,
                    "end_date_time": event_end_time,
                    "location": event_location,
                },
                target={"entity_id": entity_id},
                blocking=True,
            )
            return {"success": True}
        except HomeAssistantError as err:
            raise ToolExecutionError(str(err)) from err

    async def _start_timer(self, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            return await start_auri_timer_native(
                self.hass,
                duration=arguments.get("duration"),
                satellite_speaker=arguments.get("satellite_speaker"),
            )
        except (vol.error.MultipleInvalid, ValueError) as err:
            return {"retry": str(err)}
        except HomeAssistantError as err:
            raise ToolExecutionError(str(err)) from err

    async def _list_timers(self) -> dict[str, Any]:
        try:
            return await get_auri_timers_native(self.hass)
        except HomeAssistantError as err:
            raise ToolExecutionError(str(err)) from err


def normalize_turn_response(response: dict[str, Any]) -> SaaSTurnResponse:
    """Normalize the SaaS contract into a typed object."""
    response_type = response.get("type")
    if response_type not in {"final", "tool_calls"}:
        raise SaaSRequestError(f"Unknown response type: {response}")

    return SaaSTurnResponse(
        response_type=response_type,
        conversation_id=response["conversation_id"],
        content=response.get("content"),
        turn_id=response.get("turn_id"),
        tool_calls=response.get("tool_calls"),
    )


async def build_context_snapshot(
    hass: HomeAssistant,
    user_input: conversation.ConversationInput,
) -> dict[str, Any]:
    """Build the structured context payload sent to the SaaS backend."""
    user_id = user_input.context.user_id
    user_preferences = "" if user_id is None else await get_user_preferences_helper(user_id)
    now = datetime.now().astimezone()
    return {
        "user_id": user_id,
        "agent_id": user_input.agent_id,
        "device_id": user_input.device_id,
        "language": user_input.language,
        "text": user_input.text,
        "user_name": await get_user_name(hass, user_input),
        "satellite_speaker": get_device_media_player(hass, user_input.device_id),
        "request_area": get_request_area(hass, user_input.device_id),
        "exposed_entities": get_exposed_entities(hass),
        "user_preferences": user_preferences,
        "now": now.isoformat(),
        "day_of_week": now.strftime("%A"),
    }


async def get_user_name(
    hass: HomeAssistant, user_input: conversation.ConversationInput
) -> str | None:
    """Resolve the current Home Assistant user's display name."""
    if user_input.context.user_id is None:
        return None
    user = await hass.auth.async_get_user(user_input.context.user_id)
    return None if user is None else user.name


def get_device_media_player(hass: HomeAssistant, device_id: str | None) -> str | None:
    """Get the first exposed media_player entity for a device."""
    if device_id is None:
        return None

    entity_registry = er.async_get(hass)
    entities = er.async_entries_for_device(entity_registry, device_id)
    for entity in entities:
        if entity.domain != "media_player":
            continue
        if async_should_expose(hass, conversation.DOMAIN, entity.entity_id):
            return entity.entity_id
    return None


def get_exposed_entities(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Return the exposed entity snapshot sent to SaaS."""
    states = [
        state
        for state in hass.states.async_all()
        if async_should_expose(hass, conversation.DOMAIN, state.entity_id)
    ]
    entity_registry = er.async_get(hass)
    exposed_entities: list[dict[str, Any]] = []

    for state in states:
        entity = entity_registry.async_get(state.entity_id)
        aliases: list[str] = []
        if entity and entity.aliases:
            aliases = list(entity.aliases)

        exposed_entities.append(
            {
                "entity_id": state.entity_id,
                "name": state.name,
                ATTR_NAME: state.name,
                "state": state.state.replace("\n", " ").replace(",", " "),
                "aliases": aliases,
                "area_id": _resolve_entity_area_id(hass, state.entity_id),
            }
        )

    return exposed_entities


def get_request_area(hass: HomeAssistant, device_id: str | None) -> str | None:
    """Resolve area for the current request device."""
    if not device_id:
        return None

    device_registry = dr.async_get(hass)
    area_registry = ar.async_get(hass)
    device = device_registry.async_get(device_id)
    if not device:
        return None

    if device.area_id:
        area = area_registry.async_get_area(device.area_id)
        return area.name if area else device.area_id

    if device.suggested_area:
        return device.suggested_area

    return None


def _resolve_entity_area_id(hass: HomeAssistant, entity_id: str) -> str | None:
    """Resolve area id/name for an entity."""
    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)
    area_registry = ar.async_get(hass)

    entry = entity_registry.async_get(entity_id)
    if not entry:
        return None

    if entry.area_id:
        area = area_registry.async_get_area(entry.area_id)
        return area.name if area else entry.area_id

    if entry.device_id:
        device = device_registry.async_get(entry.device_id)
        if device and device.area_id:
            area = area_registry.async_get_area(device.area_id)
            return area.name if area else device.area_id

    return None


def get_timeout_seconds(entry_options: dict[str, Any]) -> int:
    """Resolve request timeout from options."""
    return int(entry_options.get(CONF_REQUEST_TIMEOUT, DEFAULT_REQUEST_TIMEOUT))


def _clamp_percentage(value: Any) -> int:
    """Normalize a percentage-like value into an integer from 1 to 100."""
    numeric = int(round(float(value)))
    return max(1, min(100, numeric))


def _clamp_step_percentage(value: Any) -> int:
    """Normalize a step percentage-like value into an integer from -100 to 100."""
    numeric = int(round(float(value)))
    return max(-100, min(100, numeric))
