"""Who uses each Home Assistant login: the question, asked once, and the logins that look like a service's.

Every login has a person in Home Assistant, so the login made for the MQTT
broker (the one Zigbee2MQTT uses) or a spare admin account is listed as a
household member until someone marks it Not a person under Who uses each
login, a page most people never open. So the question is asked once: right
after memory is turned on, and through a Repairs notice on a hub that had
memory before. Logins whose name looks like a service's or a device's start
as Not a person; nothing changes until the question is answered.
"""
from __future__ import annotations

import re
from typing import Any, Mapping, NamedTuple, Sequence

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir, selector

from ..const import DOMAIN
from .config import CONF_MEMORY_ENABLED, CONF_MEMORY_LOGIN_PEOPLE, CONF_MEMORY_LOGINS_ASKED
from .identity import LOGIN_NOT_A_PERSON, LOGIN_SHARED, household_persons

# The question's default: the person Home Assistant links the login to (not saved).
LOGIN_LINKED = "linked"
# The Repairs notice's translation key; each config entry has its own notice.
SERVICE_LOGINS_ISSUE = "service_logins"
# Names that services' logins get (the MQTT broker's, Node-RED's...) and
# devices' (a wall tablet), and people's don't.
_SERVICE_NAME = re.compile(
    r"mqtt|zigbee|z2m|node.?red|frigate|esphome|homebridge|scrypted|appdaemon"
    r"|\badmin|\b(?:kiosk|tablet|dashboard|wall.?panel|guest|test|service)\b",
    re.IGNORECASE,
)


class Login(NamedTuple):
    """A login people use. label is the question's field: its name, with the start of its id when two share one."""

    label: str
    user_id: str
    owner: bool


def looks_like_a_service(name: str | None) -> bool:
    """Whether a login's or a person's name looks like a service's or a device's, not a person's."""
    return bool(_SERVICE_NAME.search(name or ""))


def names_text(labels: Sequence[str]) -> str:
    """'Admin', 'Admin and MQTT-user', 'Admin, Frigate and MQTT-user'."""
    if len(labels) < 2:
        return "".join(labels)
    return f"{', '.join(labels[:-1])} and {labels[-1]}"


async def async_logins(hass: HomeAssistant) -> list[Login]:
    """Each login people use, by name."""
    users = sorted(
        (user for user in await hass.auth.async_get_users() if user.is_active and not user.system_generated),
        key=lambda user: (user.name or "").lower(),
    )
    names = [user.name or "Unnamed login" for user in users]
    return [
        Login(name if names.count(name) == 1 else f"{name} ({user.id[:6]})", user.id, user.is_owner)
        for name, user in zip(names, users)
    ]


def _answered(options: Mapping[str, Any]) -> set[str]:
    """The logins the question was answered for: saved with it, or set to anyone but their own person."""
    return {*(options.get(CONF_MEMORY_LOGIN_PEOPLE) or {}), *(options.get(CONF_MEMORY_LOGINS_ASKED) or ())}


def unanswered(logins: Sequence[Login], options: Mapping[str, Any]) -> list[Login]:
    """The logins nobody has said anything about yet."""
    answered = _answered(options)
    return [login for login in logins if login.user_id not in answered]


def service_logins(hass: HomeAssistant, logins: Sequence[Login], options: Mapping[str, Any]) -> list[Login]:
    """The unanswered logins whose name, or their person's, looks like a service's: the question starts them as Not a person.

    Only logins whose person is listed as a household member count. Someone
    has to stay: when every one of those looks like a service's, the owner's
    (whoever set up Home Assistant) is taken to be theirs.
    """
    persons = {
        person["user_id"]: person
        for person in household_persons(hass, options.get(CONF_MEMORY_LOGIN_PEOPLE))
        if person.get("user_id")
    }
    listed = [login for login in logins if login.user_id in persons]
    found = [
        login
        for login in unanswered(listed, options)
        if looks_like_a_service(login.label) or looks_like_a_service(persons[login.user_id]["name"])
    ]
    if len(found) == len(listed):
        found = [login for login in found if not login.owner]
    return found


def logins_form(hass: HomeAssistant, logins: Sequence[Login], options: Mapping[str, Any]) -> tuple[vol.Schema, list[str]]:
    """The question's fields, and the labels of the logins it starts as Not a person (service_logins)."""
    saved = options.get(CONF_MEMORY_LOGIN_PEOPLE) or {}
    persons = household_persons(hass)
    people = household_persons(hass, saved)  # without logins marked "not a person"
    preselected = {login.user_id for login in service_logins(hass, logins, options)}
    fields: dict[Any, Any] = {}
    for login in logins:
        linked = next((p["name"] for p in persons if p.get("user_id") == login.user_id), None)
        choices = [
            selector.SelectOptionDict(
                value=LOGIN_LINKED,
                label=f"{linked} (its person in Home Assistant)" if linked else "Nobody (no person in Home Assistant)",
            ),
            *(
                selector.SelectOptionDict(value=p["entity_id"], label=p["name"])
                for p in people
                if p.get("user_id") != login.user_id
            ),
            selector.SelectOptionDict(value=LOGIN_SHARED, label="Shared: nobody in particular, as on a voice satellite"),
            selector.SelectOptionDict(
                value=LOGIN_NOT_A_PERSON, label="Not a person: a service uses it (like MQTT), not a household member"
            ),
        ]
        current = LOGIN_NOT_A_PERSON if login.user_id in preselected else saved.get(login.user_id, LOGIN_LINKED)
        if current not in {choice["value"] for choice in choices}:
            current = LOGIN_LINKED  # the person was removed from Home Assistant
        fields[vol.Optional(login.label, default=current)] = selector.SelectSelector(
            selector.SelectSelectorConfig(options=choices, mode=selector.SelectSelectorMode.DROPDOWN)
        )
    return vol.Schema(fields), [login.label for login in logins if login.user_id in preselected]


def answers(logins: Sequence[Login], user_input: Mapping[str, Any], options: Mapping[str, Any]) -> dict[str, Any]:
    """The options with the question answered: who uses each login that isn't its own person's, and that every login was asked."""
    used_by = {
        login.user_id: str(user_input[login.label])
        for login in logins
        if user_input.get(login.label) not in (None, "", LOGIN_LINKED)
    }
    return {
        **options,
        CONF_MEMORY_LOGIN_PEOPLE: used_by,
        CONF_MEMORY_LOGINS_ASKED: sorted(login.user_id for login in logins),
    }


def _issue_id(entry_id: str) -> str:
    return f"{SERVICE_LOGINS_ISSUE}_{entry_id}"


async def async_update_logins_issue(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Raise the Repairs notice for the logins that look like a service's, or remove it when there are none.

    Only with memory on, since memory is what lists the household. Answering
    the question, here or under Who uses each login, removes the notice for
    good; a login added later that looks like a service's raises it again.
    """
    found = service_logins(hass, await async_logins(hass), entry.options) if entry.options.get(CONF_MEMORY_ENABLED) else []
    if not found:
        async_delete_logins_issue(hass, entry.entry_id)
        return
    ir.async_create_issue(
        hass,
        DOMAIN,
        _issue_id(entry.entry_id),
        data={"entry_id": entry.entry_id},
        is_fixable=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key=SERVICE_LOGINS_ISSUE,
        translation_placeholders={"logins": names_text([login.label for login in found])},
    )


def async_delete_logins_issue(hass: HomeAssistant, entry_id: str) -> None:
    """Remove the notice (deleting one that isn't there is fine)."""
    ir.async_delete_issue(hass, DOMAIN, _issue_id(entry_id))
