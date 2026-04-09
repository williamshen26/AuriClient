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
    get_preference_by_key,
    get_preference_keys,
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
        elif function_name == "turn_on_light":
            result = await self._turn_on_light(arguments)
        elif function_name == "turn_off_light":
            result = await self._turn_off_light(arguments)
        elif function_name == "adjust_light_brightness":
            result = await self._adjust_light_brightness(arguments)
        elif function_name == "open_cover":
            result = await self._open_cover(arguments)
        elif function_name == "close_cover":
            result = await self._close_cover(arguments)
        elif function_name == "set_cover_position":
            result = await self._set_cover_position(arguments)
        elif function_name == "stop_cover":
            result = await self._stop_cover(arguments)
        elif function_name == "open_cover_tilt":
            result = await self._open_cover_tilt(arguments)
        elif function_name == "close_cover_tilt":
            result = await self._close_cover_tilt(arguments)
        elif function_name == "set_cover_tilt_position":
            result = await self._set_cover_tilt_position(arguments)
        elif function_name == "turn_on_climate":
            result = await self._turn_on_climate(arguments)
        elif function_name == "turn_off_climate":
            result = await self._turn_off_climate(arguments)
        elif function_name == "set_temperature":
            result = await self._set_temperature(arguments)
        elif function_name == "set_humidity":
            result = await self._set_humidity(arguments)
        elif function_name == "set_fan_mode":
            result = await self._set_fan_mode(arguments)
        elif function_name == "set_hvac_mode":
            result = await self._set_hvac_mode(arguments)
        elif function_name == "adjust_media_volume":
            result = await self._adjust_media_volume(arguments)
        elif function_name == "select_media_source":
            result = await self._select_media_source(arguments)
        elif function_name == "set_media_mute":
            result = await self._set_media_mute(arguments)
        elif function_name == "get_forecasts":
            result = await self._get_forecasts(arguments)
        elif function_name == "get_all_persons":
            result = await self._get_all_persons()
        elif function_name == "update_user_preferences":
            result = await self._update_user_preferences(arguments)
        elif function_name == "get_user_preferences":
            result = await self._get_user_preferences(arguments)
        elif function_name == "get_preference_keys":
            result = await self._get_preference_keys(arguments)
        elif function_name == "get_preference_by_key":
            result = await self._get_preference_by_key(arguments)
        elif function_name == "get_shopping_list":
            result = await self._get_shopping_list(arguments)
        elif function_name == "add_shopping_list_item":
            result = await self._add_shopping_list_item(arguments)
        elif function_name == "mark_shopping_list_item_complete":
            result = await self._mark_shopping_list_item_complete(arguments)
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
            raise ToolExecutionError(f"Unsupported tool: {function_name}, consider update your Auri client to the latest version that supports this tool.")

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
        
    async def _turn_on_light(self, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            entity_id = self._resolve_entity_id_no_fallback("light", arguments.get("entity_id"))
        except ToolExecutionError as err:
            entity_id = self._resolve_entity_id_no_fallback("switch", arguments.get("entity_id"))

        domain = entity_id.split(".")[0]
        try:
            await self.hass.services.async_call(
                domain=domain,
                service="turn_on",
                service_data={"entity_id": entity_id},
                blocking=True,
            )
            return {"success": True, "entity_id": entity_id}
        except vol.error.MultipleInvalid as err:
            return {"retry": str(err)}
        except HomeAssistantError as err:
            raise ToolExecutionError(str(err)) from err
        
    async def _turn_off_light(self, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            entity_id = self._resolve_entity_id_no_fallback("light", arguments.get("entity_id"))
        except ToolExecutionError as err:
            entity_id = self._resolve_entity_id_no_fallback("switch", arguments.get("entity_id"))

        domain = entity_id.split(".")[0]
        try:
            await self.hass.services.async_call(
                domain=domain,
                service="turn_off",
                service_data={"entity_id": entity_id},
                blocking=True,
            )
            return {"success": True, "entity_id": entity_id}
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

    async def _open_cover(self, arguments: dict[str, Any]) -> dict[str, Any]:
        entity_id = self._resolve_entity_id_no_fallback("cover", arguments.get("entity_id"))
        try:
            await self.hass.services.async_call(
                domain="cover",
                service="open_cover",
                service_data={"entity_id": entity_id},
                blocking=True,
            )
            return {"success": True, "entity_id": entity_id}
        except vol.error.MultipleInvalid as err:
            return {"retry": str(err)}
        except HomeAssistantError as err:
            raise ToolExecutionError(str(err)) from err

    async def _close_cover(self, arguments: dict[str, Any]) -> dict[str, Any]:
        entity_id = self._resolve_entity_id_no_fallback("cover", arguments.get("entity_id"))
        try:
            await self.hass.services.async_call(
                domain="cover",
                service="close_cover",
                service_data={"entity_id": entity_id},
                blocking=True,
            )
            return {"success": True, "entity_id": entity_id}
        except vol.error.MultipleInvalid as err:
            return {"retry": str(err)}
        except HomeAssistantError as err:
            raise ToolExecutionError(str(err)) from err

    async def _set_cover_position(self, arguments: dict[str, Any]) -> dict[str, Any]:
        entity_id = self._resolve_entity_id_no_fallback("cover", arguments.get("entity_id"))
        raw_position = arguments.get("position")
        if raw_position is None:
            return {"retry": "position is required for set_cover_position"}

        position = _clamp_percentage_zero_to_hundred(raw_position)
        try:
            await self.hass.services.async_call(
                domain="cover",
                service="set_cover_position",
                service_data={"entity_id": entity_id, "position": position},
                blocking=True,
            )
            return {"success": True, "entity_id": entity_id, "position": position}
        except vol.error.MultipleInvalid as err:
            return {"retry": str(err)}
        except HomeAssistantError as err:
            raise ToolExecutionError(str(err)) from err

    async def _stop_cover(self, arguments: dict[str, Any]) -> dict[str, Any]:
        entity_id = self._resolve_entity_id_no_fallback("cover", arguments.get("entity_id"))
        try:
            await self.hass.services.async_call(
                domain="cover",
                service="stop_cover",
                service_data={"entity_id": entity_id},
                blocking=True,
            )
            return {"success": True, "entity_id": entity_id}
        except vol.error.MultipleInvalid as err:
            return {"retry": str(err)}
        except HomeAssistantError as err:
            raise ToolExecutionError(str(err)) from err

    async def _open_cover_tilt(self, arguments: dict[str, Any]) -> dict[str, Any]:
        entity_id = self._resolve_entity_id_no_fallback("cover", arguments.get("entity_id"))
        try:
            await self.hass.services.async_call(
                domain="cover",
                service="open_cover_tilt",
                service_data={"entity_id": entity_id},
                blocking=True,
            )
            return {"success": True, "entity_id": entity_id}
        except vol.error.MultipleInvalid as err:
            return {"retry": str(err)}
        except HomeAssistantError as err:
            raise ToolExecutionError(str(err)) from err

    async def _close_cover_tilt(self, arguments: dict[str, Any]) -> dict[str, Any]:
        entity_id = self._resolve_entity_id_no_fallback("cover", arguments.get("entity_id"))
        try:
            await self.hass.services.async_call(
                domain="cover",
                service="close_cover_tilt",
                service_data={"entity_id": entity_id},
                blocking=True,
            )
            return {"success": True, "entity_id": entity_id}
        except vol.error.MultipleInvalid as err:
            return {"retry": str(err)}
        except HomeAssistantError as err:
            raise ToolExecutionError(str(err)) from err

    async def _set_cover_tilt_position(self, arguments: dict[str, Any]) -> dict[str, Any]:
        entity_id = self._resolve_entity_id_no_fallback("cover", arguments.get("entity_id"))
        raw_tilt_position = arguments.get("tilt_position")
        if raw_tilt_position is None:
            return {"retry": "tilt_position is required for set_cover_tilt_position"}

        tilt_position = _clamp_percentage_zero_to_hundred(raw_tilt_position)
        try:
            await self.hass.services.async_call(
                domain="cover",
                service="set_cover_tilt_position",
                service_data={"entity_id": entity_id, "tilt_position": tilt_position},
                blocking=True,
            )
            return {
                "success": True,
                "entity_id": entity_id,
                "tilt_position": tilt_position,
            }
        except vol.error.MultipleInvalid as err:
            return {"retry": str(err)}
        except HomeAssistantError as err:
            raise ToolExecutionError(str(err)) from err

    async def _turn_on_climate(self, arguments: dict[str, Any]) -> dict[str, Any]:
        entity_id = self._resolve_entity_id_no_fallback("climate", arguments.get("entity_id"))
        try:
            await self.hass.services.async_call(
                domain="climate",
                service="turn_on",
                service_data={"entity_id": entity_id},
                blocking=True,
            )
            return {"success": True, "entity_id": entity_id}
        except vol.error.MultipleInvalid as err:
            return {"retry": str(err)}
        except HomeAssistantError as err:
            raise ToolExecutionError(str(err)) from err

    async def _turn_off_climate(self, arguments: dict[str, Any]) -> dict[str, Any]:
        entity_id = self._resolve_entity_id_no_fallback("climate", arguments.get("entity_id"))
        try:
            await self.hass.services.async_call(
                domain="climate",
                service="turn_off",
                service_data={"entity_id": entity_id},
                blocking=True,
            )
            return {"success": True, "entity_id": entity_id}
        except vol.error.MultipleInvalid as err:
            return {"retry": str(err)}
        except HomeAssistantError as err:
            raise ToolExecutionError(str(err)) from err

    async def _set_temperature(self, arguments: dict[str, Any]) -> dict[str, Any]:
        entity_id = self._resolve_entity_id_no_fallback("climate", arguments.get("entity_id"))
        raw_temperature = arguments.get("temperature")
        raw_temperature_high = arguments.get("target_temp_high")
        raw_temperature_low = arguments.get("target_temp_low")
        state = self.hass.states.get(entity_id)
        hvac_mode = arguments.get("hvac_mode", state.state)
        requested_unit = _normalize_temperature_unit(arguments.get("temperature_unit"))
        target_unit = _normalize_temperature_unit(state.attributes.get("temperature_unit", arguments.get("temperature_unit")))
        service_data: dict[str, Any] = {}

        if hvac_mode == "heat_cool" or hvac_mode == "auto":
            if raw_temperature_high is None or raw_temperature_low is None:
                return {"retry": "target_temp_high and target_temp_low are required for heat_cool/auto hvac_mode"}
            
            try:
                requested_temperature_high = float(raw_temperature_high)
                requested_temperature_low = float(raw_temperature_low)
            except (TypeError, ValueError):
                return {"retry": "target_temp_high and target_temp_low must be numbers"}
            
            service_temperature_high = _convert_temperature(
                requested_temperature_high,
                from_unit=requested_unit,
                to_unit=target_unit,
            )
            service_temperature_low = _convert_temperature(
                requested_temperature_low,
                from_unit=requested_unit,
                to_unit=target_unit,
            )
            service_data = {
                "entity_id": entity_id,
                "target_temp_high": service_temperature_high,
                "target_temp_low": service_temperature_low,
            }
        if hvac_mode == "heat" or hvac_mode == "cool":
            if raw_temperature is None:
                return {"retry": "temperature is required for heat/cool hvac_mode"}

            try:
                requested_temperature = float(raw_temperature)
            except (TypeError, ValueError):
                return {"retry": "temperature must be a number"}

            service_temperature = _convert_temperature(
                requested_temperature,
                from_unit=requested_unit,
                to_unit=target_unit,
            )

            service_data = {
                "entity_id": entity_id,
                "temperature": service_temperature,
            }

        if hvac_mode is not None:
            service_data["hvac_mode"] = str(hvac_mode)

        try:
            await self.hass.services.async_call(
                domain="climate",
                service="set_temperature",
                service_data=service_data,
                blocking=True,
            )
            result: dict[str, Any] = {
                "success": True,
                "entity_id": entity_id,
                "state": self.hass.states.get(entity_id),
            }
            return result
        except vol.error.MultipleInvalid as err:
            return {"retry": str(err)}
        except HomeAssistantError as err:
            raise ToolExecutionError(str(err)) from err

    async def _set_humidity(self, arguments: dict[str, Any]) -> dict[str, Any]:
        entity_id = self._resolve_entity_id_no_fallback("climate", arguments.get("entity_id"))
        raw_humidity = arguments.get("humidity")
        if raw_humidity is None:
            return {"retry": "humidity is required for set_humidity"}

        try:
            humidity = float(raw_humidity)
        except (TypeError, ValueError):
            return {"retry": "humidity must be a number between 0 and 100"}

        if humidity < 0 or humidity > 100:
            return {"retry": "humidity must be a number between 0 and 100"}

        try:
            await self.hass.services.async_call(
                domain="climate",
                service="set_humidity",
                service_data={"entity_id": entity_id, "humidity": humidity},
                blocking=True,
            )
            return {"success": True, "entity_id": entity_id, "humidity": humidity}
        except vol.error.MultipleInvalid as err:
            return {"retry": str(err)}
        except HomeAssistantError as err:
            raise ToolExecutionError(str(err)) from err

    async def _set_fan_mode(self, arguments: dict[str, Any]) -> dict[str, Any]:
        entity_id = self._resolve_entity_id_no_fallback("climate", arguments.get("entity_id"))
        fan_mode = str(arguments.get("fan_mode", "")).strip()
        if not fan_mode:
            return {"retry": "fan_mode is required for set_fan_mode"}

        state = self.hass.states.get(entity_id)
        if state is None:
            raise ToolExecutionError(f"Entity not found: {entity_id}")

        supported_fan_modes = state.attributes.get("fan_modes") or []
        if supported_fan_modes and fan_mode not in supported_fan_modes:
            return {
                "retry": f"Unsupported fan_mode '{fan_mode}'. Supported fan_modes: {supported_fan_modes}"
            }

        try:
            await self.hass.services.async_call(
                domain="climate",
                service="set_fan_mode",
                service_data={"entity_id": entity_id, "fan_mode": fan_mode},
                blocking=True,
            )
            return {"success": True, "entity_id": entity_id, "fan_mode": fan_mode}
        except vol.error.MultipleInvalid as err:
            return {"retry": str(err)}
        except HomeAssistantError as err:
            raise ToolExecutionError(str(err)) from err

    async def _set_hvac_mode(self, arguments: dict[str, Any]) -> dict[str, Any]:
        entity_id = self._resolve_entity_id_no_fallback("climate", arguments.get("entity_id"))
        hvac_mode = str(arguments.get("hvac_mode", "")).strip()
        if not hvac_mode:
            return {"retry": "hvac_mode is required for set_hvac_mode"}

        state = self.hass.states.get(entity_id)
        if state is None:
            raise ToolExecutionError(f"Entity not found: {entity_id}")

        supported_hvac_modes = state.attributes.get("hvac_modes") or []
        if supported_hvac_modes and hvac_mode not in supported_hvac_modes:
            return {
                "retry": f"Unsupported hvac_mode '{hvac_mode}'. Supported hvac_modes: {supported_hvac_modes}"
            }

        try:
            await self.hass.services.async_call(
                domain="climate",
                service="set_hvac_mode",
                service_data={"entity_id": entity_id, "hvac_mode": hvac_mode},
                blocking=True,
            )
            return {"success": True, "entity_id": entity_id, "hvac_mode": hvac_mode}
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
        return {
            "temperature_unit": str(self.hass.config.units.temperature_unit),
            "result": result,
        }
    
    async def _get_all_persons(self) -> list[dict[str, str]]:
        """Return Home Assistant persons that are linked to a user/client id."""
        persons_with_user_id: list[dict[str, str]] = []

        for state in self.hass.states.async_all("person"):
            user_id = str(state.attributes.get("user_id") or "").strip()
            if not user_id:
                continue

            name = str(state.name or state.entity_id).strip()
            persons_with_user_id.append(
                {
                    "name": name,
                    "user_id": user_id[:8],
                }
            )

        return persons_with_user_id

    async def _update_user_preferences(self, arguments: dict[str, Any]) -> dict[str, Any]:
        user_id = arguments.get("user_id")
        if not user_id:
            return {"retry": "user_id is required for update_user_preferences"}
        if not await self._user_exists(user_id):
            return {"retry": f"user_id '{user_id}' was not found, ensure you are copying the exact user_id instead of hallucinating."}

        raw_updates = arguments.get("updates")
        if not isinstance(raw_updates, list) or not raw_updates:
            return {
                "retry": "updates is required and must be a non-empty array for update_user_preferences"
            }

        normalized_updates: dict[str, Any] = {}
        allowed_importance = {"low", "medium", "high"}
        timestamp = datetime.now(UTC).isoformat()

        for item in raw_updates:
            if not isinstance(item, dict):
                return {"retry": "each updates item must be an object"}

            key = str(item.get("key", "")).strip()
            if not key:
                return {"retry": "each updates item must include a non-empty key"}

            if "value" not in item:
                return {"retry": f"updates item '{key}' is missing value"}

            importance_raw = item.get("importance", "medium")
            importance = str(importance_raw or "medium").strip().lower()
            if importance not in allowed_importance:
                return {
                    "retry": f"updates item '{key}' has invalid importance '{importance}'. Allowed values: low, medium, high"
                }

            normalized_updates[key] = {
                "value": str(item.get("value", "")),
                "importance": importance,
                "timestamp": timestamp,
            }

        await apply_user_preference_update(user_id, normalized_updates)
        return {"success": True, "user_id": user_id}

    async def _get_user_preferences(self, arguments: dict[str, Any]) -> dict[str, Any]:
        user_id = arguments.get("user_id")
        if not user_id:
            return {"retry": "user_id is required for get_user_preferences"}
        if not await self._user_exists(user_id):
            return {"retry": f"user_id '{user_id}' was not found, ensure you are copying the exact user_id instead of hallucinating."}

        raw_preferences = await get_user_preferences_helper(user_id)
        user_preferences: dict[str, Any] | str = raw_preferences
        try:
            parsed_preferences = json.loads(raw_preferences)
            if isinstance(parsed_preferences, dict):
                user_preferences = parsed_preferences
        except (TypeError, ValueError, json.JSONDecodeError):
            user_preferences = raw_preferences

        return {
            "success": True,
            "user_id": user_id,
            "user_preferences": user_preferences,
        }

    async def _get_preference_keys(self, arguments: dict[str, Any]) -> dict[str, Any]:
        user_id = arguments.get("user_id")
        if user_id and not await self._user_exists(user_id):
            return {
                "retry": f"user_id '{user_id}' was not found, ensure you are copying the exact user_id instead of hallucinating."
            }

        keys = await get_preference_keys(user_id)
        return {
            "success": True,
            "user_id": user_id,
            "keys": sorted(keys),
        }

    async def _get_preference_by_key(self, arguments: dict[str, Any]) -> dict[str, Any]:
        key = str(arguments.get("key", "")).strip()
        if not key:
            return {"retry": "key is required for get_preference_by_key"}

        user_id = arguments.get("user_id")
        if user_id and not await self._user_exists(user_id):
            return {
                "retry": f"user_id '{user_id}' was not found, ensure you are copying the exact user_id instead of hallucinating."
            }

        values = await get_preference_by_key(key=key, user_id=user_id)
        return {
            "success": True,
            "user_id": user_id,
            "key": key,
            "values": values if len(values) > 1 else (values[0] if values else None),
        }

    async def _user_exists(self, user_id: Any) -> bool:
        """Return whether a Home Assistant auth user exists for the given id prefix."""
        candidate = str(user_id or "").strip()
        if not candidate:
            return False

        candidate_lower = candidate.lower()
        users = await self.hass.auth.async_get_users()
        return any(str(user.id).lower().startswith(candidate_lower) for user in users)

    async def _get_shopping_list(self, arguments: dict[str, Any]) -> dict[str, Any]:
        entity_id = self._resolve_entity_id_no_fallback("todo", arguments.get("entity_id"))
        raw_status = arguments.get("status")

        if not isinstance(raw_status, list) or not raw_status:
            return {"retry": "status is required and must be a non-empty list"}

        status = [str(item).strip() for item in raw_status if str(item).strip()]
        if not status:
            return {"retry": "status is required and must include at least one value"}

        try:
            result = await self.hass.services.async_call(
                domain="todo",
                service="get_items",
                service_data={"status": status},
                target={"entity_id": entity_id},
                blocking=True,
                return_response=True,
            )

            if isinstance(result, dict):
                for payload in result.values():
                    if not isinstance(payload, dict):
                        continue
                    items = payload.get("items")
                    if not isinstance(items, list):
                        continue
                    for item in items:
                        if not isinstance(item, dict):
                            continue
                        uid = item.get("uid")
                        if isinstance(uid, str):
                            item["uid"] = uid[:8]

            _LOGGER.info(f"get_items result: {result}")
            return result
        except HomeAssistantError as err:
            raise ToolExecutionError(str(err)) from err

    async def _add_shopping_list_item(self, arguments: dict[str, Any]) -> dict[str, Any]:
        entity_id = self._resolve_entity_id_no_fallback("todo", arguments.get("entity_id"))
        item = str(arguments.get("item", "")).strip()
        if not item:
            return {"retry": "item is required"}

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
        
    async def _get_full_shopping_list_item_id(self, uid: str, entity_id: str) -> str:
        try:
            get_items_result = await self.hass.services.async_call(
                domain="todo",
                service="get_items",
                service_data={"status": "needs_action"},
                target={"entity_id": entity_id},
                blocking=True,
                return_response=True,
            )
            if isinstance(get_items_result, dict):
                for payload in get_items_result.values():
                    if not isinstance(payload, dict):
                        continue
                    items = payload.get("items")
                    if not isinstance(items, list):
                        continue
                    for item in items:
                        if not isinstance(item, dict):
                            continue
                        item_uid = item.get("uid")
                        if isinstance(item_uid, str) and item_uid.startswith(str(uid)):
                            return item_uid
            raise ToolExecutionError(f"Item with uid starting with '{uid}' not found")
        except HomeAssistantError as err:
            raise ToolExecutionError(str(err)) from err
        
    async def _mark_shopping_list_item_complete(self, arguments: dict[str, Any]) -> dict[str, Any]:
        entity_id = self._resolve_entity_id_no_fallback("todo", arguments.get("entity_id"))

        uid = arguments.get("uid")
        if not uid:
            return {"retry": "uid is required for mark_shopping_list_item_complete"}
        
        # the uid is only the first 8 characters of the actual uid, so we need to find the full uid
        try:
            full_uid = await self._get_full_shopping_list_item_id(uid, entity_id)
        except ToolExecutionError as err:
            return {"retry": str(err)}
        
        try:
            await self.hass.services.async_call(
                domain="todo",
                service="update_item",
                service_data={"item": full_uid, "status": "completed"},
                target={"entity_id": entity_id},
                blocking=True,
            )
            return {"success": True}
        except HomeAssistantError as err:
            raise ToolExecutionError(str(err)) from err

    async def _remove_completed_shopping_list_item(
        self, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        entity_id = self._resolve_entity_id_no_fallback("todo", arguments.get("entity_id"))

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
    context_user_id = None if user_id is None else str(user_id)[:8]
    user_preferences = sorted(await get_preference_keys(context_user_id))
    user_name = await get_user_name(hass, user_input)
    now = datetime.now().astimezone()
    return {
        "user_id": context_user_id,
        "agent_id": user_input.agent_id,
        "device_id": user_input.device_id,
        "language": user_input.language,
        "text": user_input.text,
        "user_name": user_name,
        "satellite_speaker": get_device_media_player(hass, user_input.device_id),
        "request_area": get_request_area(hass, user_input.device_id),
        "exposed_entities": get_exposed_entities(hass),
        "user_preference_keys": user_preferences,
        "home_location": {
            "latitude": round(float(hass.config.latitude), 1),
            "longitude": round(float(hass.config.longitude), 1),
            "time_zone": hass.config.time_zone,
            "country": hass.config.country,
        },
        "temperature_unit_preference": str(hass.config.units.temperature_unit),
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


def _clamp_percentage_zero_to_hundred(value: Any) -> int:
    """Normalize a percentage-like value into an integer from 0 to 100."""
    numeric = int(round(float(value)))
    return max(0, min(100, numeric))


def _clamp_step_percentage(value: Any) -> int:
    """Normalize a step percentage-like value into an integer from -100 to 100."""
    numeric = int(round(float(value)))
    return max(-100, min(100, numeric))


def _normalize_temperature_unit(raw_unit: Any) -> str:
    """Normalize unit text to C or F, defaulting to C."""
    unit = str(raw_unit or "").strip().upper()
    if unit in {"F", "°F"}:
        return "F"
    return "C"


def _convert_temperature(value: float, *, from_unit: str, to_unit: str) -> float:
    """Convert temperature between C and F when units differ."""
    if from_unit == to_unit:
        return value
    if from_unit == "F" and to_unit == "C":
        return round((value - 32.0) * 5.0 / 9.0, 2)
    if from_unit == "C" and to_unit == "F":
        return round((value * 9.0 / 5.0) + 32.0, 2)
    return value
