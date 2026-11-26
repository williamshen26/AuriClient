"""Pictures Auri finds or draws, kept on the hub and shown in Home Assistant.

- Kept in <config>/hey_auri_client/pictures, which is in every backup, the off-site one too (Home Assistant
  backs up the config folder; /media is a folder of its own that hubs leave out). A picture is kept 30 days
  (RETENTION), and for as long as a sticky note shows it: async_expire_pictures, daily (__init__.py). The household's are there,
  the guests' in guest/: emptied when their stay ends (calendars.py), with the sticky notes that show them.
  A 256 px copy of each is in thumbs/, for the Media panel ("Hey Auri pictures", media_source.py).
- Served at /api/hey_auri_client/pictures/<path>?k=<key>: each picture has a key of its own, kept with it,
  so a link works in any card, in a browser tab opened before an update, and after a restart (a signed
  link dies when Home Assistant restarts). A signed-in user (or a signed link) needs no key. The view
  never answers 401: Home Assistant counts every 401 as a failed login, and on a hub that limits them
  (login_attempts_threshold) a dashboard of broken pictures banned the browser's address (2026-11-25).
  Anything it won't serve is a 404.
- What each picture is (title, photo or drawing, who to credit) is in .storage/hey_auri_client_pictures.
- Camera views are never kept: show_camera links to the camera itself (picture_services.py). Nor are
  videos: a video is YouTube's thumbnail linking to YouTube (video_block); a guest's video notes are
  listed here only to be cleared with the guests' pictures.

A picture is a guest's when the turn is in guest mode and nobody signed in to Home Assistant asked: a
household member using the app while the home is in Automatic guest mode keeps theirs.
"""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta
import html
import io
import logging
import os
import secrets
from pathlib import Path
import re
from typing import TYPE_CHECKING, Any

from aiohttp import web

from homeassistant.components.http import KEY_AUTHENTICATED, KEY_HASS, HomeAssistantView
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util, slugify

from .const import DOMAIN

if TYPE_CHECKING:
    from .memory.identity import TurnMeta

_LOGGER = logging.getLogger(__name__)

FOLDER = ("hey_auri_client", "pictures")
URL_PREFIX = "/api/hey_auri_client/pictures"
MAX_SIDE = 1280
THUMB_SIDE = 256
RETENTION = timedelta(days=30)
EXPIRY_INTERVAL = timedelta(days=1)
STORAGE_KEY = f"{DOMAIN}_pictures"
STORAGE_VERSION = 1
DATA_PICTURES = f"{DOMAIN}_pictures"
_PATH = re.compile(r"^(?:guest/)?(?:thumbs/)?[0-9]{8}-[0-9]{6}-[a-z0-9-]{1,48}\.jpg$")
# The picture lines of a reply (chat_block): an image, and the credit under it. Speech leaves them out.
_IMAGE_MARKDOWN = re.compile(r"\[!\[[^\]\n]*\]\([^)\s]*\)\]\([^)\s]*\)|!\[[^\]\n]*\]\([^)\s]*\)")
_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
# A picture's link in a note's text (keyed, or the first build's plain path, the picture or its thumbnail).
_LINKED = re.compile(r"/api/hey_auri_client/pictures/(guest/)?(?:thumbs/)?([0-9]{8}-[0-9]{6}-[a-z0-9-]{1,48}\.jpg)")
_CREDIT = re.compile(r"<small>.*?</small>", re.S)


@dataclass
class Picture:
    file: str
    title: str
    kind: str  # "photo" or "drawing"
    guest: bool
    created_at: str
    asked: str = ""
    author: str = ""
    licence: str = ""
    licence_url: str = ""
    file_page_url: str = ""  # a photo's page on Wikimedia Commons
    page_url: str = ""  # the article it came from
    note_id: str | None = None
    key: str = ""  # in its links (link_key); made when it's kept, or when an older record is loaded

    @property
    def path(self) -> str:
        """Its place in the pictures folder, and its id in the Media panel."""
        return f"guest/{self.file}" if self.guest else self.file

    @property
    def url_path(self) -> str:
        """Its link, with its key: works for anyone who has it, as a camera's picture link does."""
        return f"{URL_PREFIX}/{self.path}?k={self.key}"

    @property
    def thumb_url_path(self) -> str:
        return f"{URL_PREFIX}/{'guest/' if self.guest else ''}thumbs/{self.file}?k={self.key}"


_FIELDS = {item.name for item in fields(Picture)}


class AuriPictures:
    """The kept pictures: files, records and the links to them."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.root = Path(hass.config.path(*FOLDER))
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._pictures: list[Picture] = []
        self._guest_notes: list[str] = []  # guests' video notes, to clear with their pictures
        self._loaded = False
        self._lock = asyncio.Lock()

    async def async_ready(self) -> None:
        if self._loaded:
            return
        async with self._lock:
            if self._loaded:
                return
            data = await self._store.async_load() or {}
            for item in data.get("pictures") or []:
                if isinstance(item, dict) and _PATH.match(str(item.get("file") or "")):
                    self._pictures.append(Picture(**{key: value for key, value in item.items() if key in _FIELDS}))
            self._guest_notes = [str(note) for note in data.get("guest_video_notes") or [] if note]
            keyless = [picture for picture in self._pictures if not picture.key]
            for picture in keyless:  # kept by 0.2.19's first build, before pictures had keys
                picture.key = link_key()
            if keyless:
                self._save()
            self._loaded = True

    def pictures(self, *, guest: bool) -> list[Picture]:
        """Newest first."""
        return sorted((p for p in self._pictures if p.guest == guest), key=lambda p: p.file, reverse=True)

    def get(self, path: str) -> Picture | None:
        return next((p for p in self._pictures if p.path == path), None)

    def for_path(self, path: str) -> Picture | None:
        """The picture a URL's path shows: the picture or its thumbnail."""
        return self.get(path.replace("thumbs/", "", 1))

    def file_path(self, path: str) -> Path | None:
        """The file behind a URL's path, when it's one of ours (nothing else is ever served)."""
        return self.root / path if _PATH.match(path) else None

    async def async_add(self, data: bytes, *, title: str, kind: str, guest: bool, **details: str) -> Picture:
        """Keep a picture: a JPEG at most MAX_SIDE across, and its thumbnail. ValueError if it isn't a picture."""
        title = " ".join(str(title or "").split())[:120] or kind
        slug = (slugify(title, separator="-") or "picture")[:40].strip("-") or "picture"
        base = f"{dt_util.now():%Y%m%d-%H%M%S}-{slug}"
        folder = self.root / "guest" if guest else self.root
        async with self._lock:
            file = await self.hass.async_add_executor_job(_save_files, data, folder, base)
            picture = Picture(
                file=file, title=title, kind=kind, guest=guest, created_at=dt_util.utcnow().isoformat(), key=link_key(),
                **{key: str(value) for key, value in details.items() if key in _FIELDS and value},
            )
            self._pictures.append(picture)
            self._save()
        return picture

    @callback
    def add_guest_note(self, note_id: str) -> None:
        self._guest_notes.append(note_id)
        self._save()

    @callback
    def set_note(self, picture: Picture, note_id: str) -> None:
        picture.note_id = note_id
        self._save()

    def chat_block(self, picture: Picture) -> str:
        """The lines under Auri's reply in the chat: the picture and who to credit."""
        return _block(picture, picture.url_path)

    def note_markdown(self, picture: Picture) -> str:
        """A picture note's text: the same lines, which any markdown card shows."""
        return _block(picture, picture.url_path)

    async def async_clear_guest(self, why: str) -> int:
        """Delete the guests' pictures and their notes. A note already deleted doesn't stop the rest."""
        from .conversation.custom_services.sticky_note_services import (  # noqa: PLC0415 (it imports the entities)
            delete_auri_sticky_note_native,
        )

        await self.async_ready()
        guests = [picture for picture in self._pictures if picture.guest]
        notes = [picture.note_id for picture in guests if picture.note_id] + self._guest_notes
        for note_id in notes:
            try:
                await delete_auri_sticky_note_native(self.hass, note_id)
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Guest picture note %s already gone: %s", note_id, err)
        if self._guest_notes:
            self._guest_notes = []
            self._save()
        if guests:
            await self.hass.async_add_executor_job(_delete_files, self.root, [p.path for p in guests])
            async with self._lock:
                self._pictures = [picture for picture in self._pictures if not picture.guest]
                self._save()
        _LOGGER.info("Deleted the guests' pictures (%s): %s", why, len(guests))
        return len(guests)

    async def async_expire(self, notes: Mapping[str, str], now: datetime | None = None) -> int:
        """Delete the pictures over RETENTION old that no note shows; `notes` is every note's text by id.

        Whether a note shows a picture is read from its text (notes are never edited, only made and deleted),
        so a note deleted from the dashboard, by Auri or with its entity frees its picture all the same.
        """
        await self.async_ready()
        shown = {(match.group(1) or "") + match.group(2) for text in notes.values() for match in _LINKED.finditer(text or "")}
        cutoff = (now or dt_util.utcnow()) - RETENTION
        expired = [picture for picture in self._pictures if picture.path not in shown and _made(picture) < cutoff]
        if expired:
            await self.hass.async_add_executor_job(_delete_files, self.root, [picture.path for picture in expired])
        gone = {id(picture) for picture in expired}
        async with self._lock:
            guest_notes = [note_id for note_id in self._guest_notes if note_id in notes]
            if gone or guest_notes != self._guest_notes:
                self._pictures = [picture for picture in self._pictures if id(picture) not in gone]
                self._guest_notes = guest_notes
                self._save()
        if expired:
            _LOGGER.info("Deleted %s pictures over %s days old that no sticky note shows", len(expired), RETENTION.days)
        return len(expired)

    @callback
    def _save(self) -> None:
        self._store.async_delay_save(
            lambda: {"pictures": [asdict(p) for p in self._pictures], "guest_video_notes": list(self._guest_notes)}, 1
        )


async def async_get_pictures(hass: HomeAssistant) -> AuriPictures:
    pictures: AuriPictures | None = hass.data.get(DATA_PICTURES)
    if pictures is None:
        pictures = hass.data[DATA_PICTURES] = AuriPictures(hass)
    await pictures.async_ready()
    return pictures


async def async_clear_guest_pictures(hass: HomeAssistant, why: str) -> None:
    """When a stay ends (calendars.py). Never raises: the calendar and list are emptied whatever happens here."""
    try:
        await (await async_get_pictures(hass)).async_clear_guest(why)
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("Couldn't delete the guests' pictures (%s): %s", why, err)


async def async_expire_pictures(hass: HomeAssistant, *_: Any) -> None:
    """Daily, and once Home Assistant has started (__init__.py). Never raises; deletes nothing without the notes."""
    from .conversation.custom_services.sticky_note_services import (  # noqa: PLC0415 (it imports the entities)
        async_sticky_note_markdowns,
    )

    try:
        notes = await async_sticky_note_markdowns(hass)
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("Couldn't read the sticky notes, so no old pictures were deleted: %s", err)
        return
    try:
        await (await async_get_pictures(hass)).async_expire(notes)
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("Couldn't delete the old pictures: %s", err)


def _made(picture: Picture) -> datetime:
    """When it was kept; a record whose time can't be read counts as new (kept)."""
    made = dt_util.parse_datetime(picture.created_at or "")
    if made is None:
        return dt_util.utcnow()
    return made if made.tzinfo else made.replace(tzinfo=dt_util.UTC)


def link_key() -> str:
    return secrets.token_urlsafe(16)


def is_guest_picture(turn: TurnMeta | None) -> bool:
    """A guest-mode turn that nobody signed in to Home Assistant asked (a satellite in a rental, say)."""
    if turn is None or turn.mode != "guest":
        return False
    return not (turn.speaker is not None and turn.speaker.source == "ha_user")


def shows_in_chat(hass: HomeAssistant, turn: TurnMeta | None) -> bool:
    """Whether a picture can go in the reply itself.

    Only Home Assistant's web Assist dialog (a browser, or the app's built-in web view) shows a reply's
    pictures: it has a signed-in user and no device. Satellites, and the phone apps' own Assist screens
    (Android, iOS, CarPlay, watches), send a device and show plain text. Typed turns aren't spoken, and
    Hey Auri's voice leaves the picture lines out (without_pictures); another voice would read them, so a
    home with a pipeline that pairs Auri with another voice gets pictures on notes instead.
    """
    if turn is None or not turn.user_id or turn.device_id:
        return False
    return not _another_voice_reads_auri(hass)


def _another_voice_reads_auri(hass: HomeAssistant) -> bool:
    if "assist_pipeline" not in hass.config.components:
        return False
    try:
        from homeassistant.components.assist_pipeline import async_get_pipelines  # noqa: PLC0415
    except ImportError:
        return False
    agents = {entry.entry_id for entry in hass.config_entries.async_entries(DOMAIN)}
    voices = {DOMAIN} | {
        entry.entity_id for entry in er.async_get(hass).entities.values()
        if entry.platform == DOMAIN and entry.domain == "tts"
    }
    return any(
        pipeline.conversation_engine in agents and pipeline.tts_engine and pipeline.tts_engine not in voices
        for pipeline in async_get_pipelines(hass)
    )


def without_pictures(text: str) -> str:
    """A reply as it's spoken: without its picture lines."""
    spoken = _CREDIT.sub("", _IMAGE_MARKDOWN.sub("", str(text or "")))
    return re.sub(r"\n{3,}", "\n\n", spoken).strip()


def video_block(video_id: str, title: str, channel: str) -> str | None:
    """A video's lines: YouTube's thumbnail linking to the video, and what it is. Built from its id alone."""
    if not _VIDEO_ID.match(video_id):
        return None
    url = f"https://www.youtube.com/watch?v={video_id}"
    alt = " ".join(re.sub(r"[\[\]()]", " ", title).split()) or "YouTube video"
    parts = [html.escape(" ".join(title.split())), html.escape(" ".join(channel.split())), f'<a href="{url}">YouTube</a>']
    credit = " · ".join(part for part in parts if part)
    return f"[![▶ {alt}](https://i.ytimg.com/vi/{video_id}/hqdefault.jpg)]({url})\n<small>▶️ {credit}</small>"


def _block(picture: Picture, src: str) -> str:
    alt = re.sub(r"[\[\]\n]", " ", picture.title).strip()
    return f"![{alt}]({src})\n<small>{_credit(picture)}</small>"


def _credit(picture: Picture) -> str:
    if picture.kind == "drawing":
        return "🎨 Auri"
    source = "Wikimedia Commons"
    if picture.file_page_url.startswith("https://"):
        source = f'<a href="{html.escape(picture.file_page_url, quote=True)}">{source}</a>'
    parts = [html.escape(picture.author), html.escape(picture.licence), source]
    return "📷 " + " · ".join(part for part in parts if part)


# ── Files (in the executor) ──────────────────────────────────────────────────


def _save_files(data: bytes, folder: Path, base: str) -> str:
    from PIL import Image, ImageOps  # noqa: PLC0415 (Home Assistant has Pillow)

    try:
        with Image.open(io.BytesIO(data)) as probe:
            probe.verify()
        with Image.open(io.BytesIO(data)) as opened:
            image = ImageOps.exif_transpose(opened) or opened
            image = _rgb(image)
    except Exception as err:  # noqa: BLE001 (Pillow raises many kinds)
        raise ValueError(f"Not a picture: {err}") from err
    image.thumbnail((MAX_SIDE, MAX_SIDE))
    (folder / "thumbs").mkdir(parents=True, exist_ok=True)
    name = f"{base}.jpg"
    count = 2
    while (folder / name).exists():
        name = f"{base}-{count}.jpg"
        count += 1
    _write_jpeg(image, folder / name, quality=86)
    thumb = image.copy()
    thumb.thumbnail((THUMB_SIDE, THUMB_SIDE))
    _write_jpeg(thumb, folder / "thumbs" / name, quality=80)
    return name


def _rgb(image):
    """A JPEG has no transparency: a transparent picture goes onto white."""
    from PIL import Image  # noqa: PLC0415

    if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    return image.convert("RGB")


def _write_jpeg(image, path: Path, *, quality: int) -> None:
    temporary = path.with_suffix(".tmp")
    image.save(temporary, "JPEG", quality=quality, optimize=True)
    os.replace(temporary, path)


def _delete_files(root: Path, paths: list[str]) -> None:
    for path in paths:
        folder, _, file = path.rpartition("/")
        for target in (root / path, root / folder / "thumbs" / file):
            try:
                target.unlink()
            except FileNotFoundError:
                pass


# ── Serving them ─────────────────────────────────────────────────────────────


class AuriPictureView(HomeAssistantView):
    """GET /api/hey_auri_client/pictures/<path>?k=<key>: a kept picture, by its key or for a signed-in user.

    requires_auth is off so that a refused picture is a 404, never a 401 (counted as a failed login).
    Home Assistant still checks a login or a signed link and says so in KEY_AUTHENTICATED.
    """

    url = URL_PREFIX + "/{path:.+}"
    name = "api:hey_auri_client:pictures"
    requires_auth = False

    async def get(self, request: web.Request, path: str) -> web.StreamResponse:
        hass = request.app[KEY_HASS]
        pictures = await async_get_pictures(hass)
        target = pictures.file_path(path)
        picture = pictures.for_path(path) if target is not None else None
        key = request.query.get("k", "")
        allowed = request.get(KEY_AUTHENTICATED, False) or (
            picture is not None and bool(key) and secrets.compare_digest(key, picture.key)
        )
        if not allowed or target is None or not await hass.async_add_executor_job(target.is_file):
            return web.Response(status=404)
        return web.FileResponse(target, headers={"Cache-Control": "private, max-age=86400"})


@callback
def async_register_view(hass: HomeAssistant) -> None:
    """Once per Home Assistant start (async_setup): the view outlives config entry reloads."""
    if hass.http is not None and not hass.data.get(f"{DATA_PICTURES}_view"):
        hass.http.register_view(AuriPictureView())
        hass.data[f"{DATA_PICTURES}_view"] = True
