"""Repairs notices for a Home Assistant that can be reached from the internet.

With remote access (a tunnel such as the Cloudflared add-on, Home Assistant Cloud, or another
reverse proxy), Home Assistant's login page is on the internet, and two of its defaults matter:
- it allows unlimited failed logins: IP banning only acts once a number of attempts is set;
- two-factor login is each user's choice, so an account can sign in with a password alone.

Only shown when Home Assistant looks reachable from outside the home, so a hub without remote
access isn't told about it.
"""
from __future__ import annotations

from datetime import timedelta
import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.network import NoURLAvailableError, get_url

from .const import DOMAIN
from .memory.logins import names_text

_LOGGER = logging.getLogger(__name__)

TWO_FACTOR_ISSUE = "remote_logins_without_two_factor"
LOGIN_LOCKOUT_ISSUE = "remote_login_lockout_off"
SECURITY_CHECK_INTERVAL = timedelta(hours=1)

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
