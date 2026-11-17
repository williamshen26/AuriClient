"""Auri support's account on a hub: each sign-in is announced, and each session ends after 12 hours.

On hubs Auri sets up, Auri staff sign in remotely with one admin account, chosen under Auri
support in the options. Home Assistant keeps a sign-in for 90 days after its last use, so a
staff member who signed in once could stay signed in for months. With the account chosen:
- every new sign-in of that account, and every long-lived access token made for it, is
  announced to the household: a notification in Home Assistant and on their phones;
- each one is ended 12 hours after it was made, in use or not: the browser or app is signed
  out, and a long-lived access token stops working.

Home Assistant fires no event when it creates a sign-in (a refresh token), so the account's
tokens are checked every minute. Tokens already there when the account is first chosen are not
announced (that's usually staff setting up the hub), but they end 12 hours after they were made
too.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
import logging
from typing import Any

from homeassistant.auth import models as auth_models
from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import CONF_SUPPORT_USER, DOMAIN

_LOGGER = logging.getLogger(__name__)

SUPPORT_SESSION_LIMIT = timedelta(hours=12)
SUPPORT_CHECK_INTERVAL = timedelta(minutes=1)
SUPPORT_NOTICE_TITLE = "Auri support signed in"
# The username Auri gives its support account on the hubs it sets up: the options suggest it.
SUPPORT_USERNAME = "support"
NO_SUPPORT_USER = "none"

_STORAGE_VERSION = 1
_STORAGE_KEY = f"{DOMAIN}_support_sessions"
# What the Home Assistant apps put in a sign-in's client_id.
_APP_CLIENT_IDS = ("https://home-assistant.io/iOS", "https://home-assistant.io/android")


def support_user_id(options: dict[str, Any]) -> str | None:
    """The user id chosen under Auri support, or None."""
    user_id = str(options.get(CONF_SUPPORT_USER) or "").strip()
    return None if not user_id or user_id == NO_SUPPORT_USER else user_id


def username_of(user: auth_models.User) -> str | None:
    """A user's Home Assistant username (the login name), if it signs in with one."""
    for credential in user.credentials:
        if credential.auth_provider_type == "homeassistant":
            username = str((credential.data or {}).get("username") or "").strip()
            if username:
                return username
    return None


async def async_people_logins(hass: HomeAssistant) -> list[auth_models.User]:
    """Active accounts people sign in with (not Home Assistant's own), by name."""
    users = [user for user in await hass.auth.async_get_users() if user.is_active and not user.system_generated]
    return sorted(users, key=lambda user: (user.name or "").casefold())


def _sessions(user: auth_models.User) -> list[auth_models.RefreshToken]:
    """The account's sign-ins and long-lived access tokens (copied: ending one changes the dict)."""
    return [token for token in user.refresh_tokens.values() if token.token_type != auth_models.TOKEN_TYPE_SYSTEM]


def _notice_text(token: auth_models.RefreshToken) -> str:
    """What the household is told about one new sign-in or token."""
    limit = int(SUPPORT_SESSION_LIMIT.total_seconds() // 3600)
    if token.token_type == auth_models.TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN:
        return (
            f"Auri support made a long-lived access token, \"{token.client_name}\", for their account in your "
            f"Home Assistant. It stops working {limit} hours after it was made."
        )
    where = "the Home Assistant app" if (token.client_id or "").startswith(_APP_CLIENT_IDS) else "a web browser"
    source = f" from {token.last_used_ip}" if token.last_used_ip else ""
    return (
        f"Auri support signed in to your Home Assistant in {where}{source}. They're signed out "
        f"automatically {limit} hours after signing in."
    )


class SupportSessions:
    """Watches the account chosen under Auri support: announces its sign-ins and ends them after 12 hours."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._store: Store[dict[str, Any]] = Store(hass, _STORAGE_VERSION, _STORAGE_KEY)
        # The account the stored tokens belong to, and the tokens already announced (or there
        # before the account was chosen).
        self._user_id: str | None = None
        self._seen: set[str] = set()
        self._last_sign_in: datetime | None = None
        self._loaded = False
        self._unsub: Callable[[], None] | None = None

    async def async_start(self) -> None:
        """Load what was announced before, check now, then every minute."""
        stored = await self._store.async_load()
        if isinstance(stored, dict):
            self._user_id = str(stored.get("user_id") or "") or None
            self._seen = {str(token_id) for token_id in stored.get("seen") or []}
            self._last_sign_in = dt_util.parse_datetime(str(stored.get("last_sign_in") or ""))
        self._loaded = True
        self._unsub = async_track_time_interval(self.hass, self._async_tick, SUPPORT_CHECK_INTERVAL)
        await self.async_check()

    @callback
    def async_stop(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    async def _async_tick(self, _now: datetime) -> None:
        await self.async_check()

    @property
    def user_id(self) -> str | None:
        return support_user_id(dict(self.entry.options))

    async def async_user(self) -> auth_models.User | None:
        """The account chosen under Auri support, when it still exists."""
        user_id = self.user_id
        return None if user_id is None else await self.hass.auth.async_get_user(user_id)

    @property
    def last_sign_in(self) -> datetime | None:
        """When the support account last signed in (or a token was made for it), as far as this hub saw."""
        return self._last_sign_in

    async def async_signed_in(self) -> bool:
        """Whether the support account has a sign-in or token that hasn't ended yet."""
        user = await self.async_user()
        return bool(user and _sessions(user))

    async def async_check(self) -> None:
        """Announce new sign-ins of the support account, and end those older than 12 hours. Never raises."""
        if not self._loaded:
            return
        try:
            await self._async_check()
        except Exception:  # noqa: BLE001 (a check must never break the integration)
            _LOGGER.exception("Couldn't check Auri support's sign-ins")

    async def _async_check(self) -> None:
        user = await self.async_user()
        if user is None:
            return
        tokens = _sessions(user)
        changed = False
        if user.id != self._user_id:
            # Newly chosen (or another account): what's there now isn't announced.
            self._user_id = user.id
            self._seen = {token.id for token in tokens}
            changed = True
        now = dt_util.utcnow()
        for token in tokens:
            if token.id not in self._seen:
                self._seen.add(token.id)
                changed = True
                if self._last_sign_in is None or token.created_at > self._last_sign_in:
                    self._last_sign_in = token.created_at
                self._announce(token)
            if now - token.created_at >= SUPPORT_SESSION_LIMIT:
                _LOGGER.info(
                    "Ending a sign-in of Auri support's account made at %s (%s): sessions end after %s",
                    token.created_at.isoformat(),
                    token.token_type,
                    SUPPORT_SESSION_LIMIT,
                )
                # Home Assistant also closes the connections using it (its revoke callbacks).
                self.hass.auth.async_remove_refresh_token(token)
        # Forget tokens that are gone (ended here, signed out, or expired).
        current = {token.id for token in _sessions(user)}
        if not self._seen <= current:
            self._seen &= current
            changed = True
        if changed:
            await self._store.async_save(
                {
                    "user_id": self._user_id,
                    "seen": sorted(self._seen),
                    "last_sign_in": self._last_sign_in.isoformat() if self._last_sign_in else None,
                }
            )

    @callback
    def _announce(self, token: auth_models.RefreshToken) -> None:
        """A notification in Home Assistant, and on the household's phones (the Home Assistant app)."""
        message = _notice_text(token)
        persistent_notification.async_create(
            self.hass,
            message,
            title=SUPPORT_NOTICE_TITLE,
            notification_id=f"{DOMAIN}_support_sign_in_{token.id[:12]}",
        )
        if self.hass.services.has_service("notify", "notify"):
            self.entry.async_create_background_task(
                self.hass, self._async_push(message), "hey_auri_client support sign-in notification"
            )

    async def _async_push(self, message: str) -> None:
        try:
            await self.hass.services.async_call(
                "notify", "notify", {"title": SUPPORT_NOTICE_TITLE, "message": message}, blocking=True
            )
        except Exception:  # noqa: BLE001 (the notification in Home Assistant is already there)
            _LOGGER.warning("Couldn't send the Auri support sign-in notification to phones", exc_info=True)
