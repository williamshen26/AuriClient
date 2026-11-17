"""Auri support's account: each sign-in is announced, and each one ends 12 hours after it was made.

Uses pytest-homeassistant-custom-component (Linux only).
"""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("pytest_homeassistant_custom_component.common")

from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service  # noqa: E402

from homeassistant.auth import models as auth_models  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.data_entry_flow import FlowResultType  # noqa: E402

from custom_components.hey_auri_client import support as support_module  # noqa: E402
from custom_components.hey_auri_client.const import (  # noqa: E402
    CONF_CLIENT_ID,
    CONF_SHARED_SECRET,
    CONF_SUPPORT_USER,
    DOMAIN,
)
from custom_components.hey_auri_client.support import (  # noqa: E402
    NO_SUPPORT_USER,
    SUPPORT_NOTICE_TITLE,
    SupportSessions,
)

BROWSER = "https://h-0a1b2c3d4e5f.hey-auri.com/"
IOS_APP = "https://home-assistant.io/iOS"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


async def _account(hass: HomeAssistant, name: str, username: str, *, admin: bool = True) -> auth_models.User:
    user = await hass.auth.async_create_user(name, group_ids=["system-admin" if admin else "system-users"])
    credentials = auth_models.Credentials(
        auth_provider_type="homeassistant", auth_provider_id=None, data={"username": username}, is_new=False
    )
    await hass.auth.async_link_user(user, credentials)
    return user


async def _open_support_options(hass: HomeAssistant, entry: MockConfigEntry) -> dict:
    with patch("homeassistant.config_entries.async_process_deps_reqs", AsyncMock()):
        result = await hass.config_entries.options.async_init(entry.entry_id)
    return await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "support"})


def _entry(hass: HomeAssistant, options: dict) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_CLIENT_ID: "client", CONF_SHARED_SECRET: "secret"}, options=options
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
def notices():
    """The notifications made in Home Assistant, as (title, message, id)."""
    made: list[tuple[str, str, str]] = []

    def create(hass, message, title=None, notification_id=None):
        made.append((title, message, notification_id))

    with patch.object(support_module.persistent_notification, "async_create", side_effect=create):
        yield made


@pytest.fixture
async def watcher(hass: HomeAssistant):
    """Started watchers, stopped at the end (their minute timer)."""
    started: list[SupportSessions] = []

    async def start(entry: MockConfigEntry) -> SupportSessions:
        sessions = SupportSessions(hass, entry)
        await sessions.async_start()
        started.append(sessions)
        return sessions

    yield start
    for sessions in started:
        sessions.async_stop()


async def test_a_new_sign_in_is_announced_once_here_and_on_phones(hass: HomeAssistant, notices, watcher) -> None:
    phones = async_mock_service(hass, "notify", "notify")
    staff = await _account(hass, "Auri support", "support")
    owner = await _account(hass, "Will", "root")
    earlier = await hass.auth.async_create_refresh_token(staff, BROWSER)  # staff setting up the hub
    sessions = await watcher(_entry(hass, {CONF_SUPPORT_USER: staff.id}))
    assert notices == []  # there before the account was chosen

    token = await hass.auth.async_create_refresh_token(staff, BROWSER)
    token.last_used_ip = "203.0.113.7"
    await hass.auth.async_create_refresh_token(owner, BROWSER)  # the owner's own sign-in: nothing
    await sessions.async_check()
    await hass.async_block_till_done()
    await sessions.async_check()  # checked again: not announced twice
    await hass.async_block_till_done()

    assert len(notices) == 1
    title, message, notification_id = notices[0]
    assert title == SUPPORT_NOTICE_TITLE
    assert "in a web browser from 203.0.113.7" in message and "12 hours" in message
    assert notification_id.endswith(token.id[:12])
    assert [call.data for call in phones] == [{"title": SUPPORT_NOTICE_TITLE, "message": message}]
    assert sessions.last_sign_in == token.created_at
    assert await sessions.async_signed_in()
    assert earlier.id in staff.refresh_tokens  # younger than 12 hours: kept


async def test_without_the_phone_app_only_home_assistant_is_told(hass: HomeAssistant, notices, watcher) -> None:
    staff = await _account(hass, "Auri support", "support")
    sessions = await watcher(_entry(hass, {CONF_SUPPORT_USER: staff.id}))

    await hass.auth.async_create_refresh_token(staff, IOS_APP)
    await sessions.async_check()

    assert len(notices) == 1 and "in the Home Assistant app" in notices[0][1]


async def test_sign_ins_end_12_hours_after_they_were_made(hass: HomeAssistant, notices, watcher, freezer) -> None:
    async_mock_service(hass, "notify", "notify")
    staff = await _account(hass, "Auri support", "support")
    owner = await _account(hass, "Will", "root")
    sessions = await watcher(_entry(hass, {CONF_SUPPORT_USER: staff.id}))
    old = await hass.auth.async_create_refresh_token(staff, BROWSER)
    script = await hass.auth.async_create_refresh_token(
        staff, token_type=auth_models.TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN, client_name="Backup script"
    )
    owners = await hass.auth.async_create_refresh_token(owner, BROWSER)
    await sessions.async_check()
    closed: list[str] = []
    hass.auth.async_register_revoke_token_callback(old.id, lambda: closed.append("old"))
    assert "long-lived access token, \"Backup script\"" in notices[1][1]

    freezer.tick(timedelta(hours=11, minutes=59))
    await sessions.async_check()
    assert old.id in staff.refresh_tokens and script.id in staff.refresh_tokens
    newer = await hass.auth.async_create_refresh_token(staff, BROWSER)

    freezer.tick(timedelta(minutes=1))
    await sessions.async_check()

    assert set(staff.refresh_tokens) == {newer.id}  # both 12 hours old: ended, the new one kept
    assert hass.auth.async_get_refresh_token(old.id) is None
    assert closed == ["old"]  # its open connections closed too
    assert owners.id in owner.refresh_tokens  # other accounts are never touched
    assert await sessions.async_signed_in()


async def test_until_one_is_chosen_the_account_named_support_counts(hass: HomeAssistant, notices, watcher, freezer) -> None:
    """Nobody opened Auri support in the options: the account Auri names support on its hubs is used."""
    staff = await _account(hass, "Auri support", "support")
    earlier = await hass.auth.async_create_refresh_token(staff, BROWSER)  # staff setting up the hub
    sessions = await watcher(_entry(hass, {}))
    assert notices == []

    token = await hass.auth.async_create_refresh_token(staff, BROWSER)
    await sessions.async_check()
    assert len(notices) == 1 and notices[0][2].endswith(token.id[:12])

    freezer.tick(timedelta(hours=12))
    await sessions.async_check()
    assert earlier.id not in staff.refresh_tokens and token.id not in staff.refresh_tokens


@pytest.mark.parametrize("options", [{}, {CONF_SUPPORT_USER: NO_SUPPORT_USER}])
async def test_nothing_happens_with_none_chosen_or_no_account_named_support(
    hass: HomeAssistant, notices, watcher, freezer, options: dict
) -> None:
    username = "support" if options else "helper"  # None chosen; or never chosen, and nobody named support
    user = await _account(hass, "Someone", username)
    sessions = await watcher(_entry(hass, options))
    token = await hass.auth.async_create_refresh_token(user, BROWSER)

    freezer.tick(timedelta(hours=13))
    await sessions.async_check()

    assert notices == []
    assert token.id in user.refresh_tokens
    assert not await sessions.async_signed_in()


async def test_after_a_restart_only_sign_ins_not_announced_yet_are(hass: HomeAssistant, notices, watcher) -> None:
    staff = await _account(hass, "Auri support", "support")
    entry = _entry(hass, {CONF_SUPPORT_USER: staff.id})
    first = await watcher(entry)
    signed_in = await hass.auth.async_create_refresh_token(staff, BROWSER)
    await first.async_check()
    # Signed in again in the last minute before Home Assistant stopped: not checked yet.
    just_before = await hass.auth.async_create_refresh_token(staff, BROWSER)
    first.async_stop()
    await hass.async_block_till_done()  # the store is saved

    again = await watcher(entry)  # Home Assistant restarted: checks at once

    assert [notice[2][-12:] for notice in notices] == [signed_in.id[:12], just_before.id[:12]]
    assert again.last_sign_in == just_before.created_at
    again.async_stop()
    await hass.async_block_till_done()

    third = await watcher(entry)  # restarted again, nobody signed in meanwhile

    assert third.last_sign_in == just_before.created_at  # still known, for the hub's security summary
    assert len(notices) == 2


async def test_choosing_another_account_announces_only_its_new_sign_ins(hass: HomeAssistant, notices, watcher) -> None:
    staff = await _account(hass, "Auri support", "support")
    other = await _account(hass, "Helper", "helper")
    entry = _entry(hass, {CONF_SUPPORT_USER: staff.id})
    sessions = await watcher(entry)
    await hass.auth.async_create_refresh_token(other, BROWSER)  # before it was chosen

    hass.config_entries.async_update_entry(entry, options={CONF_SUPPORT_USER: other.id})
    await sessions.async_check()
    assert notices == []
    await hass.auth.async_create_refresh_token(other, BROWSER)
    await sessions.async_check()

    assert len(notices) == 1


async def test_the_options_suggest_the_account_named_support(hass: HomeAssistant) -> None:
    owner = await _account(hass, "Will", "root")
    staff = await _account(hass, "Auri support", "support")
    await hass.auth.async_create_system_user("Supervisor")
    entry = _entry(hass, {})

    result = await _open_support_options(hass, entry)
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "support"
    field = next(key for key in result["data_schema"].schema if key == CONF_SUPPORT_USER)
    assert field.default() == staff.id
    choices = result["data_schema"].schema[field].config["options"]
    assert [choice["value"] for choice in choices] == [NO_SUPPORT_USER, staff.id, owner.id]  # no system users
    assert choices[1]["label"] == "Auri support (support)"

    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_SUPPORT_USER: NO_SUPPORT_USER})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_SUPPORT_USER] == NO_SUPPORT_USER

    # Chosen None once: not suggested again.
    result = await _open_support_options(hass, entry)
    field = next(key for key in result["data_schema"].schema if key == CONF_SUPPORT_USER)
    assert field.default() == NO_SUPPORT_USER


async def test_an_unknown_account_is_saved_as_none(hass: HomeAssistant) -> None:
    await _account(hass, "Will", "root")  # the owner, who can't be removed
    staff = await _account(hass, "Auri support", "support")
    entry = _entry(hass, {CONF_SUPPORT_USER: staff.id})

    result = await _open_support_options(hass, entry)
    await hass.auth.async_remove_user(staff)  # removed while the form was open
    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_SUPPORT_USER: staff.id})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_SUPPORT_USER] == NO_SUPPORT_USER
