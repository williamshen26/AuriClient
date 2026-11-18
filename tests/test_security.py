"""Repairs notices for a Home Assistant reachable from the internet: accounts without two-factor
login, and failed logins that never lock anyone out.

Uses pytest-homeassistant-custom-component (Linux only).
"""
from __future__ import annotations

from datetime import timedelta
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


class _Backend:
    """Hey Auri's API, keeping each summary sent (or failing while `down`)."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.down = False

    async def post_json(self, path: str, payload: dict, **_kwargs) -> dict:
        assert path == security.SECURITY_REPORT_PATH
        if self.down:
            raise ConnectionError("no route to host")
        self.sent.append(payload)
        return {"ok": True}


async def test_the_summary_has_counts_and_yes_or_no_only(hass: HomeAssistant, two_factor, monkeypatch) -> None:
    from pytest_homeassistant_custom_component.common import MockConfigEntry  # pylint: disable=import-outside-toplevel

    from custom_components.hey_auri_client.const import CONF_SUPPORT_USER  # pylint: disable=import-outside-toplevel
    from custom_components.hey_auri_client.support import SupportSessions  # pylint: disable=import-outside-toplevel

    monkeypatch.setattr(hass, "http", _server(threshold=None, proxies=TUNNEL))
    await _account(hass, "Will")
    staff = await _account(hass, "Support")
    await _account(hass, "Guest tablet", local_only=True)
    two_factor.add("Will")
    hass.states.async_set("update.home_assistant_core_update", "on")
    hass.states.async_set("update.zigbee2mqtt_update", "on")
    hass.states.async_set("update.home_assistant_operating_system_update", "off")
    entry = MockConfigEntry(domain=DOMAIN, options={CONF_SUPPORT_USER: staff.id})
    entry.add_to_hass(hass)
    support = SupportSessions(hass, entry)
    await support.async_start()
    signed_in = await hass.auth.async_create_refresh_token(staff, "https://h-0a1b2c3d4e5f.hey-auri.com/")
    await support.async_check()
    support.async_stop()

    summary = await security.async_security_summary(hass, support, "0.2.0")

    from homeassistant.const import __version__  # pylint: disable=import-outside-toplevel

    assert summary == {
        "v": 1,
        "ha_version": __version__,
        "client_version": "0.2.0",
        "remote_access": True,
        "accounts_without_two_factor": 1,  # Support (the tablet is home-only)
        "login_lockout": False,
        "updates_waiting": 2,
        "support_account": True,
        "support_two_factor": False,
        "support_signed_in": True,
        "support_last_sign_in": signed_in.created_at.isoformat(),
        "update_status": None,  # no automatic updater given
        "update_target": None,
    }
    assert "Will" not in str(summary) and "Support" not in str(summary)

    none_chosen = await security.async_security_summary(hass, None, "0.2.0")
    assert none_chosen["support_account"] is False
    assert none_chosen["support_two_factor"] is None and none_chosen["support_last_sign_in"] is None


async def test_the_summary_is_sent_when_it_changes_and_daily(hass: HomeAssistant, freezer) -> None:
    backend = _Backend()
    reporter = security.SecurityReporter(backend)
    summary = {"v": 1, "accounts_without_two_factor": 1}

    assert await reporter.async_report(summary) is True
    freezer.tick(timedelta(hours=1))
    assert await reporter.async_report(dict(summary)) is False  # the same, an hour later
    assert await reporter.async_report({**summary, "accounts_without_two_factor": 0}) is True
    freezer.tick(timedelta(hours=23, minutes=59))
    assert await reporter.async_report({**summary, "accounts_without_two_factor": 0}) is False
    freezer.tick(timedelta(minutes=1))
    assert await reporter.async_report({**summary, "accounts_without_two_factor": 0}) is True  # a day on

    assert [sent["accounts_without_two_factor"] for sent in backend.sent] == [1, 0, 0]


async def test_a_failed_send_is_tried_again_at_the_next_check(hass: HomeAssistant) -> None:
    backend = _Backend()
    reporter = security.SecurityReporter(backend)
    backend.down = True

    assert await reporter.async_report({"v": 1}) is False  # never raises

    backend.down = False
    assert await reporter.async_report({"v": 1}) is True
    assert backend.sent == [{"v": 1}]
