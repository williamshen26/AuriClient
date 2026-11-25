"""Hey Auri's off-site backup location: "Hey Auri" under Settings → System → Backups → Locations.

Home Assistant makes and encrypts each backup as usual, with the hub's own backup key. This agent
uploads it straight to Hey Auri's storage in Canada through short-lived links, which Hey Auri's API
hands out only after checking the hub's signature (POST /hub/backups/<action>). The bytes never pass
through Hey Auri's servers, and the hub holds no storage keys.

- Upload: parts of 100 MiB (Hey Auri says the size), one in memory at a time. Each part is checked
  against the MD5 S3 sends back and tried up to 5 times; a link that ran out is asked for again.
- Hey Auri keeps encrypted backups only, and at most 10 per hub. Home Assistant's "keep 7" deletes the
  oldest automatic ones through this agent; a deleted copy can be brought back for 15 days by Auri staff.
- Restoring on another hub: sign it in with the same Hey Auri account, and its Backups page lists these;
  restoring one needs the backup key, which Auri staff keep in their vault.
- Not offered in mainland China: the home's backups would leave China.
- The first time it's offered, it's added to the automatic backup locations, once. Removing it later
  sticks, and the hourly summary tells Hey Auri (backup_offsite).

The summary (security.py) also carries the automatic backup schedule, the copies kept, and a fingerprint
of the backup key (backup_key_id): 16 hex characters of a SHA-256, which can't be turned back into the
key. Auri staff see from it which hubs still have the master image's key, and whether their vault holds
each hub's current one.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Coroutine
import hashlib
import json
import logging
import random
from time import monotonic
from typing import TYPE_CHECKING, Any

import aiohttp

from homeassistant.components.backup import (
    AgentBackup,
    BackupAgent,
    BackupAgentError,
    BackupNotFound,
    OnProgressCallback,
    async_get_manager,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DATA_AGENT, DOMAIN
from .exceptions import SaaSRequestError

if TYPE_CHECKING:
    from .saas_client import SaaSClient

_LOGGER = logging.getLogger(__name__)

OFFSITE_UNIQUE_ID = "offsite"
OFFSITE_AGENT_ID = f"{DOMAIN}.{OFFSITE_UNIQUE_ID}"
OFFSITE_NAME = "Hey Auri"
BACKUPS_PATH = "/hub/backups/"
# Home Assistant's country codes where the location isn't offered (the backups would leave the country).
NOT_OFFERED_COUNTRIES = frozenset({"CN"})
PART_TRIES = 5
CALL_TRIES = 3
LIST_CACHE_SECONDS = 300
DOWNLOAD_CHUNK_BYTES = 1024 * 1024
# A 100 MiB part takes 14 minutes at 1 Mbps up; S3 waits as long as bytes keep coming.
_PART_TIMEOUT = aiohttp.ClientTimeout(total=30 * 60, sock_connect=30, sock_read=120)
_DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=300)
_CALL_TIMEOUT_SECONDS = 30
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_STORE_VERSION = 1
_STORE_KEY = f"{DOMAIN}_offsite_backup"
DATA_BACKUP_AGENT_LISTENERS = f"{DOMAIN}_backup_agent_listeners"
# Waits between tries (tests skip them).
_sleep = asyncio.sleep
_DATA_OFFERED_ONCE = f"{DOMAIN}_offsite_offered_once"


def backup_key_id(key: str) -> str:
    """A backup key's fingerprint: 16 hex characters of a SHA-256. It can't be turned back into the key.

    Hey Auri's staff vault fingerprints the key it keeps the same way (AuriService support_logins.py).
    """
    return hashlib.sha256(f"hey-auri-backup-key:{key.strip()}".encode("utf-8")).hexdigest()[:16]


def offered(hass: HomeAssistant) -> bool:
    """Whether this home gets Hey Auri's off-site location (not where the backups would leave the country)."""
    return str(hass.config.country or "").upper() not in NOT_OFFERED_COUNTRIES


# ── Home Assistant's backup platform ─────────────────────────────────────────


async def async_get_backup_agents(hass: HomeAssistant, **kwargs: Any) -> list[BackupAgent]:
    """The Hey Auri location, for the hub's (first) Hey Auri account."""
    if not offered(hass):
        return []
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        return []
    # One location per hub: a second account (a developer's test entry) gets none, so the ID stays fixed.
    return [HeyAuriBackupAgent(hass, entries[0])]


@callback
def async_register_backup_agents_listener(
    hass: HomeAssistant,
    *,
    listener: Callable[[], None],
    **kwargs: Any,
) -> Callable[[], None]:
    """Home Assistant asks for the agents again when an entry loads or unloads (async_notify_backup_listeners)."""
    hass.data.setdefault(DATA_BACKUP_AGENT_LISTENERS, []).append(listener)

    @callback
    def remove_listener() -> None:
        hass.data[DATA_BACKUP_AGENT_LISTENERS].remove(listener)
        if not hass.data[DATA_BACKUP_AGENT_LISTENERS]:
            del hass.data[DATA_BACKUP_AGENT_LISTENERS]

    return remove_listener


@callback
def async_notify_backup_listeners(hass: HomeAssistant) -> None:
    for listener in list(hass.data.get(DATA_BACKUP_AGENT_LISTENERS, [])):
        listener()


class _Refused(Exception):
    """Hey Auri refused a backup request: the HTTP status, its error code and message."""

    def __init__(self, status: int | None, error: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.error = error
        self.message = message


class HeyAuriBackupAgent(BackupAgent):
    """Hey Auri's off-site location for this hub's backups."""

    domain = DOMAIN
    name = OFFSITE_NAME
    unique_id = OFFSITE_UNIQUE_ID

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__()
        self._hass = hass
        self._entry = entry
        # The last list and when it came (Home Assistant asks often: its Backups page, each retention check).
        self._cache: dict[str, Any] = {}

    def _client(self) -> SaaSClient:
        agent = self._hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {}).get(DATA_AGENT)
        if agent is None:
            raise BackupAgentError("Hey Auri Client isn't running")
        return agent.client

    # ── Calls to Hey Auri ────────────────────────────────────────────────────

    async def _call(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        """A signed call, tried again on a busy or failing server; raises _Refused when Hey Auri says no."""
        client = self._client()
        delay = 2.0
        for attempt in range(1, CALL_TRIES + 1):
            try:
                return await client.post_json(
                    f"{BACKUPS_PATH}{action}", payload, timeout_seconds=_CALL_TIMEOUT_SECONDS, measurement_key="backup"
                )
            except SaaSRequestError as err:
                status = getattr(err, "status_code", None)
                if status in _RETRY_STATUSES and attempt < CALL_TRIES:
                    await _sleep(_retry_after(err) or delay * random.uniform(1, 1.5))
                    delay *= 4
                    continue
                error, message = _error_of(err)
                raise _Refused(status, error, message) from err
            except (aiohttp.ClientError, TimeoutError) as err:
                if attempt < CALL_TRIES:
                    await _sleep(delay * random.uniform(1, 1.5))
                    delay *= 4
                    continue
                raise _Refused(None, "unreachable", f"Couldn't reach Hey Auri: {type(err).__name__}") from err
        raise AssertionError("unreachable")

    # ── Upload ───────────────────────────────────────────────────────────────

    async def async_upload_backup(
        self,
        *,
        open_stream: Callable[[], Coroutine[Any, Any, AsyncIterator[bytes]]],
        backup: AgentBackup,
        on_progress: OnProgressCallback,
        **kwargs: Any,
    ) -> None:
        if not backup.protected:
            raise BackupAgentError(
                "Hey Auri keeps encrypted backups only: turn encryption on for the Hey Auri location"
            )
        details = backup.as_dict()
        try:
            started = await self._call("start", {"backup": details})
        except _Refused as err:
            raise BackupAgentError(f"Hey Auri didn't accept the backup: {err.message}") from err

        upload_id = str(started["upload_id"])
        part_bytes = int(started["part_bytes"])
        links = {int(part["number"]): str(part["url"]) for part in started.get("parts") or []}
        parts: list[dict[str, Any]] = []
        sent = 0
        try:
            stream = await open_stream()
            async for number, data in _parts(stream, part_bytes):
                if number not in links:  # the stream is longer than Home Assistant said
                    links.update(await self._links(backup.backup_id, upload_id, [number]))
                etag = await self._put_part(backup.backup_id, upload_id, number, data, links)
                parts.append({"number": number, "etag": etag})
                sent += len(data)
                on_progress(bytes_uploaded=sent)
            if not parts:
                raise BackupAgentError("The backup was empty")
            await self._call(
                "complete", {"backup": details, "upload_id": upload_id, "parts": parts, "bytes": sent}
            )
        except BaseException as err:
            # Its parts would go after a day anyway; dropping them now keeps the storage tidy.
            await asyncio.shield(self._abort(backup.backup_id, upload_id))
            if isinstance(err, _Refused):
                raise BackupAgentError(f"Couldn't finish the upload to Hey Auri: {err.message}") from err
            raise
        finally:
            self._cache.clear()
        _LOGGER.info("Backup %s is in Hey Auri's off-site location (%s bytes in %s parts)", backup.backup_id, sent, len(parts))

    async def _links(self, backup_id: str, upload_id: str, numbers: list[int]) -> dict[int, str]:
        answer = await self._call("links", {"backup_id": backup_id, "upload_id": upload_id, "parts": numbers})
        return {int(part["number"]): str(part["url"]) for part in answer.get("parts") or []}

    async def _put_part(self, backup_id: str, upload_id: str, number: int, data: bytes, links: dict[int, str]) -> str:
        """PUT one part to its link and check S3 kept it; the part's ETag."""
        expected = await self._hass.async_add_executor_job(_md5_hex, data)
        session = async_get_clientsession(self._hass)
        delay = 2.0
        why = ""
        for attempt in range(1, PART_TRIES + 1):
            try:
                async with session.put(links[number], data=data, timeout=_PART_TIMEOUT) as response:
                    await response.read()
                    if response.status == 200:
                        etag = response.headers.get("ETag", "").strip().strip('"').lower()
                        if etag == expected:
                            return f'"{etag}"'
                        why = "S3 kept different bytes"
                    elif response.status == 403:
                        # The link ran out (or its signature's credentials did): a new one, straight away.
                        why = "the link was refused"
                        if attempt < PART_TRIES:
                            links.update(await self._links(backup_id, upload_id, [number]))
                            continue
                    elif response.status in _RETRY_STATUSES:
                        why = f"S3 answered {response.status}"
                    else:
                        raise BackupAgentError(f"S3 refused part {number} of the backup (HTTP {response.status})")
            except (aiohttp.ClientError, TimeoutError) as err:
                why = type(err).__name__
            if attempt < PART_TRIES:
                await _sleep(delay * random.uniform(1, 1.5))
                delay *= 2
        raise BackupAgentError(f"Couldn't upload part {number} of the backup after {PART_TRIES} tries: {why}")

    async def _abort(self, backup_id: str, upload_id: str) -> None:
        try:
            await self._call("abort", {"backup_id": backup_id, "upload_id": upload_id})
        except Exception as err:  # noqa: BLE001 (the bucket's lifecycle drops it after a day anyway)
            _LOGGER.debug("Couldn't drop the unfinished upload of backup %s: %s", backup_id, err)

    # ── The location's backups ───────────────────────────────────────────────

    async def async_list_backups(self, **kwargs: Any) -> list[AgentBackup]:
        cache = self._cache
        if cache.get("backups") is not None and monotonic() - cache.get("at", 0.0) < LIST_CACHE_SECONDS:
            return list(cache["backups"])
        try:
            answer = await self._call("list", {})
        except _Refused as err:
            raise BackupAgentError(f"Couldn't list the backups in Hey Auri: {err.message}") from err
        backups: list[AgentBackup] = []
        for details in answer.get("backups") or []:
            try:
                backups.append(AgentBackup.from_dict(details))
            except (KeyError, TypeError, ValueError) as err:
                _LOGGER.warning("Skipped a backup in Hey Auri that Home Assistant can't read: %s", err)
        cache.update(backups=backups, at=monotonic())
        return list(backups)

    async def async_get_backup(self, backup_id: str, **kwargs: Any) -> AgentBackup:
        for backup in await self.async_list_backups():
            if backup.backup_id == backup_id:
                return backup
        raise BackupNotFound(f"Backup {backup_id} isn't in Hey Auri")

    async def async_download_backup(self, backup_id: str, **kwargs: Any) -> AsyncIterator[bytes]:
        try:
            link = await self._call("download", {"backup_id": backup_id})
        except _Refused as err:
            if err.status == 404:
                raise BackupNotFound(f"Backup {backup_id} isn't in Hey Auri") from err
            raise BackupAgentError(f"Couldn't get the backup from Hey Auri: {err.message}") from err
        session = async_get_clientsession(self._hass)
        try:
            response = await session.get(str(link["url"]), timeout=_DOWNLOAD_TIMEOUT)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise BackupAgentError(f"Couldn't download the backup from Hey Auri: {type(err).__name__}") from err
        if response.status != 200:
            response.release()
            raise BackupAgentError(f"Couldn't download the backup from Hey Auri (HTTP {response.status})")

        async def chunks() -> AsyncIterator[bytes]:
            try:
                async for chunk in response.content.iter_chunked(DOWNLOAD_CHUNK_BYTES):
                    yield chunk
            finally:
                response.release()

        return chunks()

    async def async_delete_backup(self, backup_id: str, **kwargs: Any) -> None:
        try:
            await self._call("delete", {"backup_id": backup_id})
        except _Refused as err:
            if err.status == 404:
                raise BackupNotFound(f"Backup {backup_id} isn't in Hey Auri") from err
            raise BackupAgentError(f"Couldn't delete the backup in Hey Auri: {err.message}") from err
        finally:
            self._cache.clear()


async def _parts(stream: AsyncIterator[bytes], part_bytes: int) -> AsyncIterator[tuple[int, bytes]]:
    """The stream in parts of exactly part_bytes (the last one shorter), numbered from 1.

    While a part uploads, it's the only one in memory: the stream's chunks are joined once, then dropped.
    """
    pending: list[bytes] = []
    size = 0
    number = 0
    async for chunk in stream:
        while chunk:
            room = part_bytes - size
            pending.append(chunk[:room])
            size += min(len(chunk), room)
            chunk = chunk[room:]
            if size == part_bytes:
                number += 1
                part = b"".join(pending)
                pending, size = [], 0
                yield number, part
    if size:
        part = b"".join(pending)
        pending = []
        yield number + 1, part


def _md5_hex(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()  # noqa: S324 (S3's ETag of a part is its MD5)


def _error_of(err: SaaSRequestError) -> tuple[str, str]:
    """Hey Auri's error code and message from a refused request's body."""
    try:
        body = json.loads(getattr(err, "response_body", "") or "{}")
    except (TypeError, ValueError):
        body = {}
    if not isinstance(body, dict):
        body = {}
    status = getattr(err, "status_code", None)
    return str(body.get("error") or "error"), str(body.get("message") or f"HTTP {status}")


def _retry_after(err: SaaSRequestError) -> float | None:
    """The wait Hey Auri asked for (429), up to five minutes."""
    try:
        body = json.loads(getattr(err, "response_body", "") or "{}")
        seconds = float(body.get("retry_after")) if isinstance(body, dict) and body.get("retry_after") else None
    except (TypeError, ValueError):
        return None
    return None if seconds is None else min(max(seconds, 1.0), 300.0)


# ── The hub's backup settings ────────────────────────────────────────────────


def backup_summary(hass: HomeAssistant) -> dict[str, Any]:
    """The backup fields of the hub's hourly summary (security.py); None each when unknown.

    backup_schedule: Home Assistant's automatic backups ("daily", "custom_days", "never");
    backup_copies: how many it keeps (None when it keeps them by age or forever);
    backup_offsite: whether Hey Auri is one of their locations (None where it isn't offered);
    backup_key_id: the backup key's fingerprint (backup_key_id()).
    """
    summary: dict[str, Any] = {"backup_schedule": None, "backup_copies": None, "backup_offsite": None, "backup_key_id": None}
    try:
        manager = async_get_manager(hass)
        data = manager.config.data
    except Exception:  # noqa: BLE001 (backups not set up: nothing to say)
        return summary
    try:
        recurrence = getattr(data.schedule.recurrence, "value", data.schedule.recurrence)
        summary["backup_schedule"] = recurrence if recurrence in ("daily", "custom_days", "never") else None
        copies = data.retention.copies
        summary["backup_copies"] = copies if isinstance(copies, int) and 0 <= copies <= 1000 else None
        if offered(hass):
            summary["backup_offsite"] = OFFSITE_AGENT_ID in (data.create_backup.agent_ids or [])
        password = data.create_backup.password
        summary["backup_key_id"] = backup_key_id(password) if password else None
    except Exception:  # noqa: BLE001 (a Home Assistant update moved these: the summary goes without them)
        _LOGGER.warning("Couldn't read Home Assistant's backup settings", exc_info=True)
    return summary


async def async_offer_offsite(hass: HomeAssistant) -> bool:
    """Add Hey Auri to the automatic backup locations, once per hub; whether it's been done.

    Removing the location later sticks: this never adds it again. Tried at start and hourly until the
    backup integration has the agent loaded.
    """
    if not offered(hass) or hass.data.get(_DATA_OFFERED_ONCE):
        return bool(hass.data.get(_DATA_OFFERED_ONCE))
    store: Store[dict[str, Any]] = Store(hass, _STORE_VERSION, _STORE_KEY)
    saved = await store.async_load() or {}
    if saved.get("added_at"):
        hass.data[_DATA_OFFERED_ONCE] = True
        return True
    try:
        manager = async_get_manager(hass)
    except Exception:  # noqa: BLE001 (backups not set up yet)
        return False
    if OFFSITE_AGENT_ID not in manager.backup_agents:
        return False
    agent_ids = list(manager.config.data.create_backup.agent_ids or [])
    if OFFSITE_AGENT_ID not in agent_ids:
        manager.config.update(create_backup={"agent_ids": [*agent_ids, OFFSITE_AGENT_ID]})
        _LOGGER.info("Added Hey Auri to Home Assistant's automatic backup locations")
    await store.async_save({"added_at": dt_util.utcnow().isoformat()})
    hass.data[_DATA_OFFERED_ONCE] = True
    return True
