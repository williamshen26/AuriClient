"""The session page: adding the integration checks the credentials and gives a new account its
first session, and the Session Link sensor shows it without pressing Refresh Session.

Uses pytest-homeassistant-custom-component (Linux only); the backend call is mocked.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import aiohttp
import pytest

pytest.importorskip("pytest_homeassistant_custom_component.common")

from pytest_homeassistant_custom_component.common import (  # noqa: E402
    MockConfigEntry,
    MockEntityPlatform,
    mock_restore_cache,
)

from homeassistant.core import HomeAssistant, State  # noqa: E402
from homeassistant.data_entry_flow import FlowResultType  # noqa: E402

from custom_components.hey_auri_client.const import CONF_CLIENT_ID, CONF_SHARED_SECRET, DOMAIN  # noqa: E402
from custom_components.hey_auri_client.entities.session_link import AuriSessionLinkSensor  # noqa: E402
from custom_components.hey_auri_client.exceptions import SaaSRequestError  # noqa: E402
from custom_components.hey_auri_client.saas_client import SaaSClient  # noqa: E402

SESSION_URL = "https://hey-auri.com/s/first"
HOME_PAGE = "https://hey-auri.com"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


# ── adding the integration ──────────────────────────────────────────────────


async def _add_integration(hass: HomeAssistant, post: AsyncMock) -> dict:
    with (
        patch("homeassistant.config_entries.async_process_deps_reqs", AsyncMock()),
        patch("custom_components.hey_auri_client.async_setup_entry", AsyncMock(return_value=True)),
        patch.object(SaaSClient, "post_json", post),
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        assert result["type"] is FlowResultType.FORM
        return await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_CLIENT_ID: " cli_1 ", CONF_SHARED_SECRET: "secret"}
        )


async def test_adding_the_integration_checks_the_credentials_and_gets_the_first_session(hass: HomeAssistant) -> None:
    post = AsyncMock(return_value={"session_id": "first", "session_url": SESSION_URL})
    result = await _add_integration(hass, post)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_CLIENT_ID: " cli_1 ", CONF_SHARED_SECRET: "secret"}
    # One signed request, which keeps a session the account already has.
    post.assert_awaited_once_with("/session/refresh", {"if_missing": True})


@pytest.mark.parametrize(
    ("reply", "shown"),
    [
        ({"side_effect": SaaSRequestError("unauthorized", status_code=401)}, "invalid_auth"),
        ({"side_effect": SaaSRequestError("forbidden", status_code=403)}, "invalid_auth"),
        ({"side_effect": SaaSRequestError("bad gateway", status_code=502)}, "cannot_connect"),
        ({"return_value": {"session_id": "first"}}, "cannot_connect"),  # no session_url
        ({"side_effect": aiohttp.ClientConnectionError("unreachable")}, "cannot_connect"),
        ({"side_effect": TimeoutError()}, "cannot_connect"),
    ],
)
async def test_credentials_that_are_not_accepted_stop_the_setup(hass: HomeAssistant, reply, shown) -> None:
    result = await _add_integration(hass, AsyncMock(**reply))

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": shown}
    assert not hass.config_entries.async_entries(DOMAIN)


# ── the Session Link sensor ─────────────────────────────────────────────────


async def _add_sensor(hass: HomeAssistant, post: AsyncMock, sensor: AuriSessionLinkSensor | None = None):
    sensor = sensor or _sensor(hass)
    with patch.object(SaaSClient, "post_json", post):
        await MockEntityPlatform(hass, domain="sensor").async_add_entities([sensor])
        await hass.async_block_till_done(wait_background_tasks=True)
    return hass.states.get("sensor.session_link").state


def _sensor(hass: HomeAssistant) -> AuriSessionLinkSensor:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_CLIENT_ID: "cli_1", CONF_SHARED_SECRET: "secret"})
    entry.add_to_hass(hass)
    client = SaaSClient(hass, endpoint="https://api.example", timeout=5, client_id="cli_1", shared_secret="secret")
    sensor = AuriSessionLinkSensor(entry, client)
    sensor.entity_id = "sensor.session_link"
    return sensor


@pytest.mark.parametrize("restored", [None, HOME_PAGE])
async def test_a_new_install_shows_the_session_page_without_a_refresh(hass: HomeAssistant, restored) -> None:
    if restored:
        mock_restore_cache(hass, [State("sensor.session_link", restored)])
    post = AsyncMock(return_value={"session_id": "first", "session_url": SESSION_URL})

    assert await _add_sensor(hass, post) == SESSION_URL
    post.assert_awaited_once_with("/session/refresh", {"if_missing": True})


async def test_a_restored_session_page_is_kept(hass: HomeAssistant) -> None:
    mock_restore_cache(hass, [State("sensor.session_link", "https://hey-auri.com/s/before-restart")])
    post = AsyncMock()

    assert await _add_sensor(hass, post) == "https://hey-auri.com/s/before-restart"
    post.assert_not_awaited()


async def test_without_the_cloud_it_shows_the_home_page_until_refreshed(hass: HomeAssistant, caplog) -> None:
    post = AsyncMock(side_effect=SaaSRequestError("bad gateway", status_code=502))

    assert await _add_sensor(hass, post) == HOME_PAGE
    assert "press Refresh Session" in caplog.text


async def test_a_refresh_pressed_meanwhile_wins(hass: HomeAssistant) -> None:
    sensor = _sensor(hass)

    async def reply(*_args):
        sensor.async_update_session_url("https://hey-auri.com/s/pressed")
        return {"session_id": "first", "session_url": SESSION_URL}

    assert await _add_sensor(hass, AsyncMock(side_effect=reply), sensor) == "https://hey-auri.com/s/pressed"


async def test_the_refresh_button_starts_a_new_session(hass: HomeAssistant) -> None:
    from custom_components.hey_auri_client.button import AuriSessionRefreshButton

    sensor = _sensor(hass)
    mock_restore_cache(hass, [State("sensor.session_link", "https://hey-auri.com/s/old")])
    await _add_sensor(hass, AsyncMock(), sensor)
    hass.data.setdefault(DOMAIN, {})["runtime"] = {"session_link_sensor": sensor}

    post = AsyncMock(return_value={"session_id": "new", "session_url": "https://hey-auri.com/s/new"})
    with patch.object(SaaSClient, "post_json", post):
        await AuriSessionRefreshButton(hass, sensor.entry).async_press()

    post.assert_awaited_once_with("/session/refresh", {})
    assert hass.states.get("sensor.session_link").state == "https://hey-auri.com/s/new"
