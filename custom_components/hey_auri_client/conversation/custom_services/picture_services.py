"""Auri's picture tools: find_picture, draw_picture and show_camera.

- find_picture and draw_picture ask Hey Auri for the picture (AuriService POST /hub/pictures/{find,draw}:
  a photo found on Wikipedia or Wikimedia Commons, or a drawing), keep it (pictures.py) and show it: under
  the reply in Home Assistant's web Assist dialog, otherwise on a sticky note (pictures.shows_in_chat).
- A drawing takes 3 to 10 seconds. A turn that shows it in the chat waits for it (DRAW_WAIT_SECONDS); a
  satellite's waits SATELLITE_WAIT_SECONDS, then answers that it's coming ("it'll be on your sticky notes in
  a moment") and the drawing lands on a note. The drawing runs in a task of its own, so a turn cancelled on
  the way (the wake word again) doesn't lose a drawing already paid for: it goes on a note.
- find_video asks Hey Auri for a YouTube video (POST /hub/pictures/video) and shows its thumbnail linking to
  YouTube, where it plays: in the chat, or on a sticky note. Nothing is kept (YouTube's videos can't be).
  Not in mainland China, where YouTube is blocked (Home Assistant's country, as for maps).
- show_camera puts the camera's live picture in the chat, through a link valid 10 minutes. Nothing is kept,
  and nothing leaves the hub. Never in guest mode.

Every error is caught here and given to the model as a result, so a turn never fails because of a picture;
a result never carries the picture or a link (the model would only repeat them).
"""
from __future__ import annotations

import asyncio
import base64
import binascii
from datetime import timedelta
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components import conversation
from homeassistant.components.homeassistant.exposed_entities import async_should_expose
from homeassistant.components.http.auth import async_sign_path
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from ...exceptions import SaaSRequestError
from ...pictures import AuriPictures, Picture, async_get_pictures, is_guest_picture, shows_in_chat, video_block
from .sticky_note_services import create_auri_sticky_note_native

if TYPE_CHECKING:
    from ...memory.identity import TurnMeta
    from ...saas_client import SaaSClient

_LOGGER = logging.getLogger(__name__)

PICTURES_PATH = "/hub/pictures/"
FIND_TIMEOUT_SECONDS = 15
# The server gives Gemini 20 seconds; its Lambda stops at 29.
DRAW_TIMEOUT_SECONDS = 30
# How long a chat turn waits for its drawing, and a satellite's before it says the drawing is coming
# (most take about 3 seconds, and the day's drawings made is known at once).
DRAW_WAIT_SECONDS = 27.0
SATELLITE_WAIT_SECONDS = 6.0
CAMERA_LINK = timedelta(minutes=10)
SHAPES = ("square", "landscape", "portrait")
# YouTube is blocked in mainland China (homes there get Amap for maps the same way).
NO_YOUTUBE_COUNTRIES = frozenset({"CN"})
NO_YOUTUBE_MESSAGE = "No video: YouTube isn't available in this home's country. Tell the user that, then offer a photo; show one only on a yes."
# Where the picture is, for the model to say (the skill's rule alone was too weak: "Here is a sperm whale"
# from a satellite, 2026-11-25 prompt_bench).
WHERE = {
    "chat": "The picture is under your reply.",
    "sticky_note": "Not on a screen here: the picture is on a new sticky note. Say it's on the sticky notes, not \"here is\".",
    "drawing": "It's being drawn and will be on a new sticky note in a moment: say so.",
}
VIDEO_WHERE = {
    "chat": "The video is under your reply: tap it to watch on YouTube.",
    "sticky_note": "Not on a screen here: the video is on a new sticky note, to tap and watch on YouTube. Say it's on the sticky notes.",
}


class PictureToolService:
    """Handle the picture tool calls."""

    def __init__(self, hass: HomeAssistant, client: SaaSClient | None = None) -> None:
        self.hass = hass
        self.client = client

    # ── find_picture ─────────────────────────────────────────────────────────

    async def find_picture(self, arguments: dict[str, Any]) -> dict[str, Any]:
        turn: TurnMeta | None = arguments.get("_turn")
        subject = _text(arguments.get("subject"), 200)
        english = _text(arguments.get("english"), 200) or subject
        if not english:
            return {"retry": "subject and english are required"}
        answer = await self._call(
            "find",
            {
                "subject": subject or english,
                "english": english,
                "details": _text(arguments.get("details"), 200),
                "language": self.hass.config.language,
            },
            FIND_TIMEOUT_SECONDS,
        )
        if "error" in answer:
            return answer
        if not answer.get("found"):
            return {"success": False, "found": False}
        try:
            pictures = await async_get_pictures(self.hass)
            picture = await pictures.async_add(
                _decode(answer.get("data")),
                title=str(answer.get("title") or subject or english),
                kind="photo",
                guest=is_guest_picture(turn),
                asked=subject or english,
                author=str(answer.get("author") or ""),
                licence=str(answer.get("licence") or ""),
                licence_url=str(answer.get("licence_url") or ""),
                file_page_url=str(answer.get("file_page_url") or ""),
                page_url=str(answer.get("page_url") or ""),
            )
            shown = await self._show(pictures, picture, turn, on_note=arguments.get("on_note") is True)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Couldn't keep the photo found for %r: %s", english, err)
            return {"success": False, "error": "failed"}
        # where first: the model reads it before the rest.
        result: dict[str, Any] = {"where": WHERE[shown], "success": True, "shown_in": shown, "title": picture.title}
        if answer.get("description"):
            result["about"] = str(answer["description"])[:200]
        return result

    # ── draw_picture ─────────────────────────────────────────────────────────

    async def draw_picture(self, arguments: dict[str, Any]) -> dict[str, Any]:
        turn: TurnMeta | None = arguments.get("_turn")
        description = _text(arguments.get("description"), 1000)
        if not description:
            return {"retry": "description is required"}
        if turn is not None:
            if turn.drawings:
                return {"success": False, "error": "one_at_a_time",
                        "message": "One drawing a turn: offer to draw the next one after this."}
            turn.drawings += 1
        shape = arguments.get("shape") if arguments.get("shape") in SHAPES else "square"
        on_note = arguments.get("on_note") is True
        in_chat = not on_note and shows_in_chat(self.hass, turn)
        offset = dt_util.now().utcoffset()
        payload = {
            "description": description,
            "shape": shape,
            "utc_offset_minutes": int(offset.total_seconds() // 60) if offset is not None else 0,
        }
        # "late": the turn has stopped waiting, so the drawing goes on a note when it comes.
        state = {"late": False}
        task = self.hass.async_create_background_task(
            self._draw(payload, turn, on_note=on_note, state=state), "hey_auri_client drawing"
        )
        try:
            done, _ = await asyncio.wait({task}, timeout=DRAW_WAIT_SECONDS if in_chat else SATELLITE_WAIT_SECONDS)
        except asyncio.CancelledError:
            state["late"] = True
            raise
        if task in done:
            return task.result()
        state["late"] = True
        return {"where": WHERE["drawing"], "success": True, "status": "drawing", "shown_in": "sticky_note"}

    async def _draw(self, payload: dict[str, Any], turn: TurnMeta | None, *, on_note: bool,
                    state: dict[str, bool]) -> dict[str, Any]:
        answer = await self._call("draw", payload, DRAW_TIMEOUT_SECONDS)
        if "error" in answer and "drawn" not in answer:
            return answer
        if not answer.get("drawn"):
            result: dict[str, Any] = {"success": False, "error": str(answer.get("error") or "failed")}
            if answer.get("limit") is not None:
                result["limit"] = answer["limit"]
            if result["error"] == "daily_limit":
                result["message"] = f"This home has made its {answer.get('limit', 5)} drawings for today; more tomorrow."
            return result
        description = payload["description"]
        try:
            pictures = await async_get_pictures(self.hass)
            picture = await pictures.async_add(
                _decode(answer.get("data")),
                title=_short_title(description),
                kind="drawing",
                guest=is_guest_picture(turn),
                asked=description,
            )
            shown = await self._show(pictures, picture, turn, on_note=on_note or state["late"])
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Couldn't keep a drawing: %s", err)
            return {"success": False, "error": "failed"}
        result = {"where": WHERE[shown], "success": True, "shown_in": shown, "title": picture.title}
        if answer.get("left") is not None:
            result["drawings_left_today"] = answer["left"]
        return result

    # ── find_video ───────────────────────────────────────────────────────────

    async def find_video(self, arguments: dict[str, Any]) -> dict[str, Any]:
        turn: TurnMeta | None = arguments.get("_turn")
        subject = _text(arguments.get("subject"), 200)
        english = _text(arguments.get("english"), 200)
        if not subject and not english:
            return {"retry": "subject and english are required"}
        if (self.hass.config.country or "").upper() in NO_YOUTUBE_COUNTRIES:
            return {"success": False, "error": "not_available_here", "message": NO_YOUTUBE_MESSAGE}
        offset = dt_util.now().utcoffset()
        answer = await self._call(
            "video",
            {
                "subject": subject or english,
                "english": english or subject,
                "language": self.hass.config.language,
                "country": self.hass.config.country or "",
                "utc_offset_minutes": int(offset.total_seconds() // 60) if offset is not None else 0,
            },
            FIND_TIMEOUT_SECONDS,
        )
        if "error" in answer and "found" not in answer:
            return answer
        if not answer.get("found"):
            if answer.get("error") == "daily_limit":
                limit = answer.get("limit", 10)
                return {"success": False, "error": "daily_limit", "limit": limit,
                        "message": f"This home has searched its {limit} videos for today; more tomorrow."}
            if answer.get("error"):
                return {"success": False, "error": str(answer["error"])}
            return {"success": False, "found": False}
        block = video_block(str(answer.get("video_id") or ""), str(answer.get("title") or ""),
                            str(answer.get("channel") or ""))
        if block is None:
            return {"success": False, "error": "failed"}
        title = " ".join(str(answer.get("title") or subject or english).split())[:120]
        try:
            shown = await self._show_block(block, title, turn, on_note=arguments.get("on_note") is True)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Couldn't show the video found for %r: %s", english or subject, err)
            return {"success": False, "error": "failed"}
        return {"where": VIDEO_WHERE[shown], "success": True, "shown_in": shown, "title": title}

    # ── show_camera ──────────────────────────────────────────────────────────

    async def show_camera(self, arguments: dict[str, Any]) -> dict[str, Any]:
        turn: TurnMeta | None = arguments.get("_turn")
        if turn is not None and turn.mode == "guest":
            return {"success": False, "error": "not_in_guest_mode"}
        entity_id = _text(arguments.get("entity_id"), 255)
        state = self.hass.states.get(entity_id) if entity_id.startswith("camera.") else None
        if state is None or not async_should_expose(self.hass, conversation.DOMAIN, entity_id):
            return {"retry": "entity_id must be a camera's from Available Entities."}
        if not shows_in_chat(self.hass, turn):
            return {"success": False, "error": "no_screen"}
        url = async_sign_path(self.hass, f"/api/camera_proxy/{entity_id}", CAMERA_LINK)
        alt = " ".join(str(state.name).replace("[", " ").replace("]", " ").split())
        turn.pictures.append(f"![{alt}]({url})")
        return {"where": WHERE["chat"], "success": True, "shown_in": "chat", "camera": state.name}

    # ── Shared ───────────────────────────────────────────────────────────────

    async def _show(self, pictures: AuriPictures, picture: Picture, turn: TurnMeta | None, *, on_note: bool) -> str:
        """In the reply, or on a sticky note: where it went."""
        if not on_note and turn is not None and shows_in_chat(self.hass, turn):
            turn.pictures.append(pictures.chat_block(picture))
            return "chat"
        note = await create_auri_sticky_note_native(
            self.hass, markdown=pictures.note_markdown(picture), title=picture.title
        )
        pictures.set_note(picture, str(note["note_id"]))
        return "sticky_note"

    async def _show_block(self, block: str, title: str, turn: TurnMeta | None, *, on_note: bool) -> str:
        """A video's lines: in the reply, or on a sticky note (a guest's is cleared when the stay ends)."""
        if not on_note and turn is not None and shows_in_chat(self.hass, turn):
            turn.pictures.append(block)
            return "chat"
        note = await create_auri_sticky_note_native(self.hass, markdown=block, title=title)
        if is_guest_picture(turn):
            (await async_get_pictures(self.hass)).add_guest_note(str(note["note_id"]))
        return "sticky_note"

    async def _call(self, action: str, payload: dict[str, Any], timeout: int) -> dict[str, Any]:
        """Hey Auri's answer, or {"success": False, "error": "busy" | "failed"}: never raises (but cancels)."""
        if self.client is None:
            return {"success": False, "error": "failed"}
        try:
            return await self.client.post_json(
                f"{PICTURES_PATH}{action}", payload, timeout_seconds=timeout, measurement_key="pictures"
            )
        except SaaSRequestError as err:
            if getattr(err, "status_code", None) == 429:
                return {"success": False, "error": "busy"}
            _LOGGER.warning("Hey Auri couldn't %s a picture: %s", action, err)
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 (a timeout, the connection, anything else)
            _LOGGER.warning("Couldn't reach Hey Auri to %s a picture: %s", action, type(err).__name__)
        return {"success": False, "error": "failed"}


def _decode(data: Any) -> bytes:
    try:
        return base64.b64decode(str(data or ""), validate=True)
    except (binascii.Error, ValueError) as err:
        raise ValueError("The picture didn't come through") from err


def _text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _short_title(description: str) -> str:
    """A drawing's name in the Media panel: the start of what was asked."""
    if len(description) <= 60:
        return description
    return description[:60].rsplit(" ", 1)[0].rstrip(",.;:") + "…"
