"""Repairs notices for a Home Assistant reachable from the internet: accounts without two-factor
login, and failed logins that never lock anyone out.

Uses pytest-homeassistant-custom-component (Linux only).
"""
from __future__ import annotations

import ipaddress
from types import SimpleNamespace
from unittest.mock import patch

import pytest

pytest.importorskip("pytest_homeassistant_custom_component.common")

from homeassistant.auth import models as auth_models  # noqa: E402
from homeassistant.components.http.ban import KEY_BAN_MANAGER, KEY_LOGIN_THRESHOLD  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.helpers import issue_registry as ir  # noqa: E402

from custom_components.hey_auri_client import security  # noqa: E402
from custom_components.hey_auri_client.const import DOMAIN  # noqa: E402

TUNNEL = [ipaddress.ip_network("172.30.33.0/24")]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


async def _account(hass: HomeAssistant, name: str, *, provider: str = "homeassistant", local_only: bool = False):
    user = await hass.auth.async_create_user(name, local_only=local_only)
    credentials = auth_models.Credentials(
        auth_provider_type=provider, auth_provider_id=None, data={"username": name.lower()}, is_new=False
    )
    await hass.auth.async_link_user(user, credentials)
    return user


def _server(*, threshold: int | None = 5, proxies=()) -> SimpleNamespace:
    """Home Assistant's HTTP server as the integration reads it: IP banning on (a threshold) or off (None)."""
    app = {} if threshold is None else {KEY_BAN_MANAGER: object(), KEY_LOGIN_THRESHOLD: threshold}
    return SimpleNamespace(app=app, trusted_proxies=list(proxies))


def _issue(hass: HomeAssistant, issue_id: str) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(DOMAIN, issue_id)


@pytest.fixture
def two_factor(hass: HomeAssistant):
    """Which accounts have two-factor login (the test harness has no two-factor module)."""
    enabled: set[str] = set()

    async def get_enabled_mfa(user):
        return {"totp": "Authenticator app"} if user.name in enabled else {}

    with patch.object(hass.auth, "async_get_enabled_mfa", side_effect=get_enabled_mfa):
        yield enabled


async def test_behind_a_tunnel_accounts_without_two_factor_get_a_notice(hass: HomeAssistant, two_factor, monkeypatch) -> None:
    monkeypatch.setattr(hass, "http", _server(proxies=TUNNEL))  # the Cloudflared add-on, no external URL set
    await _account(hass, "Will")
    await _account(hass, "mia")
    await _account(hass, "Guest tablet", local_only=True)  # can only log in at home
    await _account(hass, "Hallway", provider="trusted_networks")  # no password, home network only
    await hass.auth.async_create_system_user("Supervisor")
    two_factor.add("Will")

    await security.async_update_security_issues(hass)

    issue = _issue(hass, security.TWO_FACTOR_ISSUE)
    assert issue is not None and issue.translation_placeholders == {"accounts": "mia"}
    assert _issue(hass, security.LOGIN_LOCKOUT_ISSUE) is None  # banning after 5 failures

    two_factor.add("mia")
    await security.async_update_security_issues(hass)
    assert _issue(hass, security.TWO_FACTOR_ISSUE) is None


@pytest.mark.parametrize("threshold", [None, -1, 0])  # banning off, or unlimited attempts (the default)
async def test_failed_logins_that_never_ban_get_a_notice(hass: HomeAssistant, two_factor, monkeypatch, threshold) -> None:
    monkeypatch.setattr(hass, "http", _server(threshold=threshold, proxies=TUNNEL))

    await security.async_update_security_issues(hass)

    assert _issue(hass, security.LOGIN_LOCKOUT_ISSUE) is not None


async def test_an_external_url_counts_as_reachable(hass: HomeAssistant, two_factor, monkeypatch) -> None:
    monkeypatch.setattr(hass, "http", _server(threshold=None))
    await hass.config.async_update(external_url="https://h-0141616a8c51.example.com")
    await _account(hass, "Will")

    await security.async_update_security_issues(hass)

    assert _issue(hass, security.TWO_FACTOR_ISSUE) is not None
    assert _issue(hass, security.LOGIN_LOCKOUT_ISSUE) is not None


async def test_without_remote_access_nothing_is_raised_and_old_notices_go(hass: HomeAssistant, two_factor, monkeypatch) -> None:
    monkeypatch.setattr(hass, "http", _server(threshold=None, proxies=TUNNEL))
    await _account(hass, "Will")
    await security.async_update_security_issues(hass)
    assert _issue(hass, security.TWO_FACTOR_ISSUE) is not None

    monkeypatch.setattr(hass, "http", _server(threshold=None))  # the tunnel was removed

    await security.async_update_security_issues(hass)

    assert _issue(hass, security.TWO_FACTOR_ISSUE) is None
    assert _issue(hass, security.LOGIN_LOCKOUT_ISSUE) is None


async def test_an_unknown_server_raises_no_lockout_notice(hass: HomeAssistant, two_factor, monkeypatch) -> None:
    """When the running server can't say (no app), no notice rather than a wrong one."""
    monkeypatch.setattr(hass, "http", SimpleNamespace(app=None, trusted_proxies=TUNNEL))

    await security.async_update_security_issues(hass)

    assert _issue(hass, security.LOGIN_LOCKOUT_ISSUE) is None


@pytest.mark.usefixtures("socket_enabled")  # Home Assistant's own HTTP server binds a port
@pytest.mark.parametrize(("settings", "lockout"), [
    ({"login_attempts_threshold": 5}, True),
    ({}, False),  # Home Assistant's default: unlimited attempts
    ({"ip_ban_enabled": False, "login_attempts_threshold": 5}, False),
])
async def test_the_real_http_server_is_read(hass: HomeAssistant, settings: dict, lockout: bool) -> None:
    """The settings as the Cloudflared add-on's docs set them, read from the server Home Assistant runs."""
    from homeassistant.setup import async_setup_component  # pylint: disable=import-outside-toplevel

    config = {"server_port": 18123, "use_x_forwarded_for": True, "trusted_proxies": ["172.30.33.0/24"], **settings}
    assert await async_setup_component(hass, "http", {"http": config})
    await hass.async_block_till_done()

    assert security.reachable_from_internet(hass) is True
    assert security.login_lockout_on(hass) is lockout
    await hass.http.stop()


async def test_a_failing_check_never_raises(hass: HomeAssistant, monkeypatch, caplog) -> None:
    monkeypatch.setattr(hass, "http", _server(proxies=TUNNEL))

    async def broken():
        raise RuntimeError("auth store unavailable")

    monkeypatch.setattr(hass.auth, "async_get_users", broken)

    await security.async_update_security_issues(hass)

    assert "Couldn't check how Home Assistant's remote logins are protected" in caplog.text
