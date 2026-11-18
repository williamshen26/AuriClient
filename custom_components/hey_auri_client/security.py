"""Repairs notices for a Home Assistant that can be reached from the internet.

With remote access (a tunnel such as the Cloudflared add-on, Home Assistant Cloud, or another
reverse proxy), Home Assistant's login page is on the internet, and two of its defaults matter:
- it allows unlimited failed logins: IP banning only acts once a number of attempts is set;
- two-factor login is each user's choice, so an account can sign in with a password alone.

Only shown when Home Assistant looks reachable from outside the home, so a hub without remote
access isn't told about it.

The same checks, as counts and yes/no only, are sent to Hey Auri hourly when they change (and once
a day anyway): the account's dashboard shows them under Hub security, and Auri staff see which hubs
need attention. No names, addresses or settings beyond those are sent.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.const import STATE_ON, __version__ as HA_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.network import NoURLAvailableError, get_url
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .memory.logins import names_text

if TYPE_CHECKING:
    from .saas_client import SaaSClient
    from .support import SupportSessions
    from .updates import AutoUpdater

_LOGGER = logging.getLogger(__name__)

TWO_FACTOR_ISSUE = "remote_logins_without_two_factor"
LOGIN_LOCKOUT_ISSUE = "remote_login_lockout_off"
SECURITY_CHECK_INTERVAL = timedelta(hours=1)
SECURITY_REPORT_PATH = "/hub/security"
# A summary that hasn't changed is still sent this often, so the dashboard knows the hub is there.
SECURITY_REPORT_EVERY = timedelta(hours=24)
_SECURITY_REPORT_TIMEOUT_SECONDS = 15

_TWO_FACTOR_HELP = "https://www.home-assistant.io/docs/authentication/multi-factor-auth/"
_HTTP_HELP = "https://www.home-assistant.io/integrations/http/"
# Logins that need no password, and only from the home network.
_LOCAL_ONLY_PROVIDERS = frozenset({"trusted_networks"})


def reachable_from_internet(hass: HomeAssistant) -> bool:
    """Whether Home Assistant looks reachable from outside the home.

    A reverse proxy it trusts (a tunnel like the Cloudflared add-on needs one), an external URL,
    or Home Assistant Cloud's remote access.
    """
    if getattr(getattr(hass, "http", None), "trusted_proxies", None):
        return True
    try:
        get_url(hass, allow_internal=False)
    except NoURLAvailableError:
        return False
    return True


def login_lockout_on(hass: HomeAssistant) -> bool | None:
    """Whether repeated failed logins ban the address; None when the running server can't tell.

    Read from the server Home Assistant is running, so it's the setting in effect: Settings →
    System → Network, HTTP server, "Enable IP banning" and "Login attempts before ban".
    """
    app = getattr(getattr(hass, "http", None), "app", None)
    if app is None:
        return None
    try:
        from homeassistant.components.http.ban import (  # pylint: disable=import-outside-toplevel
            KEY_BAN_MANAGER,
            KEY_LOGIN_THRESHOLD,
        )
    except ImportError:  # Home Assistant moved them: no notice rather than a wrong one
        return None
    return KEY_BAN_MANAGER in app and app.get(KEY_LOGIN_THRESHOLD, 0) >= 1


async def async_remote_accounts_without_two_factor(hass: HomeAssistant) -> list[str]:
    """People's accounts that could sign in from outside with a password alone, by name.

    Not accounts limited to the home network ("Can only log in from the local network"), nor
    Home Assistant's own system users.
    """
    names = []
    for user in await hass.auth.async_get_users():
        if user.system_generated or not user.is_active or user.local_only:
            continue
        if not any(credential.auth_provider_type not in _LOCAL_ONLY_PROVIDERS for credential in user.credentials):
            continue
        if not await hass.auth.async_get_enabled_mfa(user):
            names.append(user.name or "an unnamed account")
    return sorted(names, key=str.casefold)


async def async_update_security_issues(hass: HomeAssistant) -> None:
    """Raise or remove both notices for how Home Assistant is set up right now. Never raises."""
    try:
        if not reachable_from_internet(hass):
            async_delete_security_issues(hass)
            return

        accounts = await async_remote_accounts_without_two_factor(hass)
        if accounts:
            ir.async_create_issue(
                hass,
                DOMAIN,
                TWO_FACTOR_ISSUE,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=TWO_FACTOR_ISSUE,
                translation_placeholders={"accounts": names_text(accounts)},
                learn_more_url=_TWO_FACTOR_HELP,
            )
        else:
            ir.async_delete_issue(hass, DOMAIN, TWO_FACTOR_ISSUE)

        if login_lockout_on(hass) is False:
            ir.async_create_issue(
                hass,
                DOMAIN,
                LOGIN_LOCKOUT_ISSUE,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=LOGIN_LOCKOUT_ISSUE,
                learn_more_url=_HTTP_HELP,
            )
        else:
            ir.async_delete_issue(hass, DOMAIN, LOGIN_LOCKOUT_ISSUE)
    except Exception:  # noqa: BLE001 (a notice must never break the integration)
        _LOGGER.exception("Couldn't check how Home Assistant's remote logins are protected")


def async_delete_security_issues(hass: HomeAssistant) -> None:
    """Remove both notices (removing one that isn't there is fine)."""
    for issue_id in (TWO_FACTOR_ISSUE, LOGIN_LOCKOUT_ISSUE):
        ir.async_delete_issue(hass, DOMAIN, issue_id)


def updates_waiting(hass: HomeAssistant) -> int:
    """How many updates Home Assistant offers (Settings → Updates): Home Assistant itself, apps, devices."""
    return sum(1 for state in hass.states.async_all("update") if state.state == STATE_ON)


async def async_security_summary(
    hass: HomeAssistant,
    support: SupportSessions | None,
    client_version: str,
    updater: AutoUpdater | None = None,
) -> dict[str, Any]:
    """This hub's security, as counts and yes/no only: what's sent to Hey Auri."""
    support_user = await support.async_user() if support is not None else None
    last_sign_in = support.last_sign_in if support_user is not None and support is not None else None
    return {
        "v": 1,
        "ha_version": HA_VERSION,
        "client_version": client_version,
        "remote_access": reachable_from_internet(hass),
        "accounts_without_two_factor": len(await async_remote_accounts_without_two_factor(hass)),
        "login_lockout": login_lockout_on(hass),
        "updates_waiting": updates_waiting(hass),
        "support_account": support_user is not None,
        "support_two_factor": (
            bool(await hass.auth.async_get_enabled_mfa(support_user)) if support_user is not None else None
        ),
        "support_signed_in": bool(support_user is not None and support is not None and await support.async_signed_in()),
        "support_last_sign_in": last_sign_in.isoformat() if last_sign_in else None,
        # Automatic updates (updates.py): off, current, pending, failed or no_entity.
        **(updater.summary() if updater is not None else {"update_status": None, "update_target": None}),
    }


class SecurityReporter:
    """Sends the summary to Hey Auri when it changes, and once a day anyway. Never raises."""

    def __init__(self, client: SaaSClient) -> None:
        self._client = client
        self._sent: dict[str, Any] | None = None
        self._sent_at: datetime | None = None

    async def async_report(self, summary: dict[str, Any]) -> bool:
        """Send it unless the same was sent within a day; whether it was sent."""
        now = dt_util.utcnow()
        if summary == self._sent and self._sent_at is not None and now - self._sent_at < SECURITY_REPORT_EVERY:
            return False
        try:
            await self._client.post_json(
                SECURITY_REPORT_PATH,
                summary,
                timeout_seconds=_SECURITY_REPORT_TIMEOUT_SECONDS,
                measurement_key="security",
            )
        except Exception as err:  # noqa: BLE001 (tried again at the next hourly check)
            _LOGGER.debug("Couldn't send the hub security summary to Hey Auri: %s", err)
            return False
        self._sent = dict(summary)
        self._sent_at = now
        return True
