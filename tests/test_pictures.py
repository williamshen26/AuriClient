"""Pictures: kept on the hub, shown in the chat or on a sticky note, served with a login, cleared after a stay.

Hey Auri's /hub/pictures is played by FakeHeyAuri. Uses pytest-homeassistant-custom-component (Linux only:
Home Assistant's test harness doesn't run on Windows).
"""
from __future__ import annotations

import asyncio
import base64
from datetime import timedelta
import io
import json
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("pytest_homeassistant_custom_component.common")

from PIL import Image  # noqa: E402
from pytest_homeassistant_custom_component.common import (  # noqa: E402
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
)

from homeassistant.components import conversation  # noqa: E402
from homeassistant.components.homeassistant.exposed_entities import async_expose_entity  # noqa: E402
from homeassistant.components.http.auth import async_sign_path  # noqa: E402
from homeassistant.components.media_source import MediaSourceItem  # noqa: E402
from homeassistant.core import Context, HomeAssistant  # noqa: E402
from homeassistant.setup import async_setup_component  # noqa: E402
from homeassistant.util import dt as dt_util  # noqa: E402

from custom_components.hey_auri_client.const import (  # noqa: E402
    CONF_CLIENT_ID,
    CONF_SHARED_SECRET,
    DOMAIN,
    EVENT_CONVERSATION_FINISHED,
)
from custom_components.hey_auri_client.conversation import helpers  # noqa: E402
from custom_components.hey_auri_client.conversation.agent_class import ThinOpenAIAgent  # noqa: E402
from custom_components.hey_auri_client.conversation.custom_services import (  # noqa: E402
    picture_services,
    sticky_note_services,
)
from custom_components.hey_auri_client.conversation.custom_services.picture_services import (  # noqa: E402
    PictureToolService,
)
from custom_components.hey_auri_client.conversation.local_tool_executor import LocalToolExecutor  # noqa: E402
from custom_components.hey_auri_client.exceptions import SaaSRequestError  # noqa: E402
from custom_components.hey_auri_client.media_source import async_get_media_source  # noqa: E402
from custom_components.hey_auri_client.memory.identity import Speaker, TurnMeta  # noqa: E402
from custom_components.hey_auri_client.pictures import (  # noqa: E402
    async_clear_guest_pictures,
    async_get_pictures,
    async_register_view,
    without_pictures,
)
from custom_components.hey_auri_client.saas_client import SaaSTurnResponse  # noqa: E402


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


def _image(fmt: str, size: tuple[int, int] = (1600, 900), *, transparent: bool = False) -> bytes:
    image = Image.new("RGBA" if transparent else "RGB", size, (20, 110, 190, 90) if transparent else (20, 110, 190))
    out = io.BytesIO()
    image.save(out, fmt)
    return out.getvalue()


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


WHALE = {
    "found": True, "title": "Sperm whale", "description": "Largest toothed whale",
    "page_url": "https://en.wikipedia.org/wiki/Sperm_whale",
    "file_page_url": "https://commons.wikimedia.org/wiki/File:Mother_and_baby_sperm_whale.jpg",
    "author": "Gabriel Barathieu", "licence": "CC BY-SA 2.0", "licence_url": "https://creativecommons.org/licenses/by-sa/2.0",
    "source": "wikipedia", "mime": "image/jpeg", "width": 1280, "height": 720,
}


VIDEO = {
    "found": True, "video_id": "abc123DEF45", "title": "Sperm whales [up close]", "channel": "Nat & Geo",
    "published_at": "2024-05-01T10:00:00Z", "url": "https://www.youtube.com/watch?v=abc123DEF45",
    "thumbnail": "https://i.ytimg.com/vi/abc123DEF45/hqdefault.jpg", "left": 9,
}
VIDEO_LINES = (
    "[![▶ Sperm whales up close](https://i.ytimg.com/vi/abc123DEF45/hqdefault.jpg)](https://www.youtube.com/watch?v=abc123DEF45)\n"
    '<small>▶️ Sperm whales [up close] · Nat &amp; Geo · <a href="https://www.youtube.com/watch?v=abc123DEF45">YouTube</a></small>'
)


class FakeHeyAuri:
    """AuriService's POST /hub/pictures/{find,draw}, as SaaSClient.post_json returns it."""

    def __init__(self) -> None:
        self.find_answer: dict[str, Any] = {**WHALE, "data": _b64(_image("JPEG"))}
        self.draw_answer: dict[str, Any] = {"drawn": True, "mime": "image/png", "data": _b64(_image("PNG", (1024, 1024))), "left": 4}
        self.error: BaseException | None = None
        self.hold: asyncio.Event | None = None  # a drawing that takes its time
        self.video_answer: dict[str, Any] = dict(VIDEO)
        self.calls: list[tuple[str, dict[str, Any], int | None, str | None]] = []

    async def post_json(self, path: str, payload: dict[str, Any], *, timeout_seconds: int | None = None,
                        measurement_key: str | None = None) -> dict[str, Any]:
        self.calls.append((path, payload, timeout_seconds, measurement_key))
        if self.error is not None:
            raise self.error
        if path.endswith("/find"):
            return self.find_answer
        if path.endswith("/video"):
            return self.video_answer
        if self.hold is not None:
            await self.hold.wait()
        return self.draw_answer


class Notes:
    """Sticky notes, as the picture code creates and deletes them."""

    def __init__(self) -> None:
        self.notes: dict[str, dict[str, str]] = {}

    async def create(self, hass, markdown: str, title: str | None = None, note_id: str | None = None) -> dict:
        note_id = f"n{len(self.notes) + 1}"
        self.notes[note_id] = {"title": title or "", "markdown": markdown}
        return {"note_id": note_id}

    async def delete(self, hass, note_id: str) -> dict:
        if note_id not in self.notes:
            raise ValueError(f"note_id not found: {note_id}")
        del self.notes[note_id]
        return {"note_id": note_id, "deleted": True}


@pytest.fixture
async def setup(hass: HomeAssistant, tmp_path: Path, monkeypatch) -> Notes:
    """A hub with the web server (logins, signed links) and pictures kept in a folder of the test's own."""
    hass.config.config_dir = str(tmp_path)
    await async_setup_component(hass, "homeassistant", {})
    await async_setup_component(hass, "http", {})
    async_register_view(hass)
    notes = Notes()
    monkeypatch.setattr(picture_services, "create_auri_sticky_note_native", notes.create)
    monkeypatch.setattr(sticky_note_services, "delete_auri_sticky_note_native", notes.delete)
    return notes


def web_chat(mode: str = "owner") -> TurnMeta:
    """Home Assistant's web Assist dialog: a signed-in user, no device."""
    return TurnMeta(mode=mode, memory_allowed=False, conversation_id="c1", user_id="user-1",
                    speaker=Speaker(source="ha_user", name="Will", ha_user_id="user-1"))


def satellite(mode: str = "owner") -> TurnMeta:
    return TurnMeta(mode=mode, memory_allowed=False, conversation_id="c1", device_id="satellite-1")


def phone_app() -> TurnMeta:
    """The phone app's own Assist screen: a signed-in user and the phone's device, plain text."""
    return TurnMeta(mode="owner", memory_allowed=False, conversation_id="c1", user_id="user-1", device_id="phone-1",
                    speaker=Speaker(source="ha_user", name="Will", ha_user_id="user-1"))


# ── Keeping pictures ─────────────────────────────────────────────────────────


async def test_a_picture_is_kept_as_a_jpeg_with_a_thumbnail(hass: HomeAssistant, setup, hass_storage) -> None:
    pictures = await async_get_pictures(hass)

    picture = await pictures.async_add(_image("PNG", (2000, 1000), transparent=True), title="抹香鲸", kind="photo",
                                       guest=False, author="Gabriel Barathieu")

    folder = Path(hass.config.path("hey_auri_client", "pictures"))
    assert picture.file.endswith("-mo-xiang-jing.jpg") and picture.path == picture.file
    with Image.open(folder / picture.file) as kept:
        assert kept.format == "JPEG" and kept.size == (1280, 640) and kept.mode == "RGB"
    with Image.open(folder / "thumbs" / picture.file) as thumb:
        assert thumb.size == (256, 128)
    # Recorded, and back after a restart.
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=2))
    await hass.async_block_till_done()
    assert hass_storage["hey_auri_client_pictures"]["data"]["pictures"][0]["file"] == picture.file
    hass.data.pop("hey_auri_client_pictures")
    again = await async_get_pictures(hass)
    assert [(p.title, p.author, p.kind) for p in again.pictures(guest=False)] == [("抹香鲸", "Gabriel Barathieu", "photo")]


async def test_what_isnt_a_picture_isnt_kept(hass: HomeAssistant, setup) -> None:
    pictures = await async_get_pictures(hass)
    with pytest.raises(ValueError):
        await pictures.async_add(b"<html>nope</html>", title="x", kind="photo", guest=False)
    assert pictures.pictures(guest=False) == []


async def test_the_view_needs_the_pictures_key_a_login_or_a_signed_link(
    hass: HomeAssistant, setup, hass_client, hass_client_no_auth
) -> None:
    pictures = await async_get_pictures(hass)
    picture = await pictures.async_add(_image("JPEG"), title="Sperm whale", kind="photo", guest=False)
    other = await pictures.async_add(_image("JPEG"), title="Koala", kind="photo", guest=True)
    plain = f"/api/hey_auri_client/pictures/{picture.path}"
    anonymous = await hass_client_no_auth()

    assert len(picture.key) >= 20 and picture.key != other.key
    response = await anonymous.get(picture.url_path)  # its key, as notes and the chat have it
    assert response.status == 200 and response.headers["Content-Type"] == "image/jpeg"
    assert (await anonymous.get(picture.thumb_url_path)).status == 200
    assert (await anonymous.get(async_sign_path(hass, plain, timedelta(minutes=5)))).status == 200
    assert (await (await hass_client()).get(plain)).status == 200

    # Refused is always 404, never 401: Home Assistant counts each 401 as a failed login and bans the address.
    refused = [plain, f"{plain}?k=", f"{plain}?k=wrong", f"{plain}?k={other.key}",
               f"/api/hey_auri_client/pictures/{other.path}?k={picture.key}",
               "/api/hey_auri_client/pictures/secrets.yaml", "/api/hey_auri_client/pictures/x.jpg",
               f"/api/hey_auri_client/pictures/20261125-120000-a.png?k={picture.key}",
               plain + "?authSig=expired"]
    for url in refused:
        assert (await anonymous.get(url)).status == 404, url
    for name in ("secrets.yaml", "x.jpg", "20261125-120000-a.png", "guest/20261125-120000-missing.jpg"):
        assert (await (await hass_client()).get(f"/api/hey_auri_client/pictures/{name}")).status == 404


async def test_pictures_kept_before_they_had_keys_get_one(hass: HomeAssistant, setup, hass_storage, hass_client_no_auth) -> None:
    picture = await (await async_get_pictures(hass)).async_add(_image("JPEG"), title="Beluga whale", kind="photo", guest=False)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=2))  # its own save, out of the way
    await hass.async_block_till_done()
    hass.data.pop("hey_auri_client_pictures")
    hass_storage["hey_auri_client_pictures"] = {"version": 1, "key": "hey_auri_client_pictures", "data": {"pictures": [
        {"file": picture.file, "title": "Beluga whale", "kind": "photo", "guest": False, "created_at": picture.created_at}]}}

    [loaded] = (await async_get_pictures(hass)).pictures(guest=False)

    assert loaded.key and loaded.key != picture.key
    assert (await (await hass_client_no_auth()).get(loaded.url_path)).status == 200
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=2))
    await hass.async_block_till_done()
    assert hass_storage["hey_auri_client_pictures"]["data"]["pictures"][0]["key"] == loaded.key


async def test_the_media_panel_lists_them_newest_first(hass: HomeAssistant, setup, freezer) -> None:
    pictures = await async_get_pictures(hass)
    whale = await pictures.async_add(_image("JPEG"), title="Sperm whale", kind="photo", guest=False)
    freezer.tick(timedelta(seconds=5))
    dragon = await pictures.async_add(_image("JPEG"), title="A dragon eating pizza", kind="drawing", guest=False)
    freezer.tick(timedelta(seconds=5))
    koala = await pictures.async_add(_image("JPEG"), title="Koala", kind="photo", guest=True)
    source = await async_get_media_source(hass)

    root = await source.async_browse_media(MediaSourceItem(hass, DOMAIN, "", None))

    assert root.title == "Hey Auri pictures"
    assert [child.identifier for child in root.children] == ["guest", dragon.path, whale.path]
    assert root.children[1].title.startswith("A dragon eating pizza (20")
    assert root.children[1].thumbnail == f"/api/hey_auri_client/pictures/thumbs/{dragon.file}?k={dragon.key}"
    guests = await source.async_browse_media(MediaSourceItem(hass, DOMAIN, "guest", None))
    assert [child.identifier for child in guests.children] == [f"guest/{koala.file}"]
    played = await source.async_resolve_media(MediaSourceItem(hass, DOMAIN, whale.path, None))
    # The plain path: Home Assistant signs it, escaping any query.
    assert (played.url, played.mime_type) == (f"/api/hey_auri_client/pictures/{whale.path}", "image/jpeg")


# ── Finding a photo ──────────────────────────────────────────────────────────


async def test_a_photo_asked_for_in_the_web_chat_shows_under_the_reply(hass: HomeAssistant, setup) -> None:
    hey_auri = FakeHeyAuri()
    turn = web_chat()

    result = await PictureToolService(hass, hey_auri).find_picture(
        {"subject": "sperm whale", "english": "sperm whale", "_turn": turn})

    assert result == {"success": True, "shown_in": "chat", "where": "The picture is under your reply.",
                      "title": "Sperm whale", "about": "Largest toothed whale"}
    assert hey_auri.calls == [("/hub/pictures/find", {"subject": "sperm whale", "english": "sperm whale", "details": "",
                                                      "language": hass.config.language}, 15, "pictures")]
    [block] = turn.pictures
    picture = (await async_get_pictures(hass)).pictures(guest=False)[0]
    assert block.startswith(f"![Sperm whale](/api/hey_auri_client/pictures/{picture.file}?k={picture.key})\n")
    assert block.endswith('<small>📷 Gabriel Barathieu · CC BY-SA 2.0 · <a href="https://commons.wikimedia.org/wiki/'
                          'File:Mother_and_baby_sperm_whale.jpg">Wikimedia Commons</a></small>')
    assert (picture.author, picture.licence, picture.page_url) == ("Gabriel Barathieu", "CC BY-SA 2.0", WHALE["page_url"])
    assert picture.key not in json.dumps(result) and setup.notes == {}


@pytest.mark.parametrize("turn", [satellite(), phone_app()], ids=["satellite", "phone app"])
async def test_without_a_chat_that_shows_pictures_it_goes_on_a_sticky_note(hass: HomeAssistant, setup, turn) -> None:
    result = await PictureToolService(hass, FakeHeyAuri()).find_picture(
        {"subject": "sperm whale", "english": "sperm whale", "_turn": turn})

    assert result["shown_in"] == "sticky_note" and turn.pictures == []
    assert result["where"].startswith("Not on a screen here: the picture is on a new sticky note")  # for the model to say
    [(note_id, note)] = setup.notes.items()
    picture = (await async_get_pictures(hass)).pictures(guest=False)[0]
    assert note["title"] == "Sperm whale" and picture.note_id == note_id
    # The picture's link with its key: any card shows it, in a tab opened before an update, after a restart.
    assert "?k=" in picture.url_path
    assert note["markdown"].startswith(f"![Sperm whale]({picture.url_path})\n<small>📷 Gabriel Barathieu")


async def test_asked_for_on_a_note_it_goes_on_a_note_from_the_chat_too(hass: HomeAssistant, setup) -> None:
    turn = web_chat()
    result = await PictureToolService(hass, FakeHeyAuri()).find_picture(
        {"subject": "koala", "english": "koala", "on_note": True, "_turn": turn})
    assert result["shown_in"] == "sticky_note" and turn.pictures == [] and len(setup.notes) == 1


async def test_nothing_found(hass: HomeAssistant, setup) -> None:
    hey_auri = FakeHeyAuri()
    hey_auri.find_answer = {"found": False}
    result = await PictureToolService(hass, hey_auri).find_picture({"subject": "瓦登达", "english": "wadengda", "_turn": web_chat()})
    assert result == {"success": False, "found": False}


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (SaaSRequestError("SaaS request failed: 429", status_code=429), "busy"),
        (SaaSRequestError("SaaS request failed: 500", status_code=500), "failed"),
        (asyncio.TimeoutError(), "failed"),
    ],
)
async def test_hey_auri_not_answering_is_a_result_not_a_failed_turn(hass: HomeAssistant, setup, error, expected) -> None:
    hey_auri = FakeHeyAuri()
    hey_auri.error = error
    turn = web_chat()
    tools = PictureToolService(hass, hey_auri)

    assert await tools.find_picture({"subject": "a", "english": "a", "_turn": turn}) == {"success": False, "error": expected}
    assert await tools.draw_picture({"description": "a cat", "_turn": turn}) == {"success": False, "error": expected}
    assert turn.pictures == []


async def test_who_asked_decides_whose_picture_it_is(hass: HomeAssistant, setup) -> None:
    tools = PictureToolService(hass, FakeHeyAuri())

    # A guest at a satellite; the household on the app while the home is in Automatic guest mode; the household.
    for turn in (satellite("guest"), web_chat("guest"), satellite("owner")):
        await tools.find_picture({"subject": "koala", "english": "koala", "_turn": turn})

    pictures = await async_get_pictures(hass)
    assert len(pictures.pictures(guest=True)) == 1 and len(pictures.pictures(guest=False)) == 2


async def test_guest_pictures_and_their_notes_go_when_the_stay_ends(hass: HomeAssistant, setup) -> None:
    tools = PictureToolService(hass, FakeHeyAuri())
    for turn in (satellite("guest"), satellite("guest"), satellite("owner")):
        await tools.find_picture({"subject": "koala", "english": "koala", "_turn": turn})
    pictures = await async_get_pictures(hass)
    guest_files = [Path(hass.config.path("hey_auri_client", "pictures", p.path)) for p in pictures.pictures(guest=True)]
    household = pictures.pictures(guest=False)
    gone_note = pictures.pictures(guest=True)[0].note_id
    await setup.delete(hass, gone_note)  # a guest already deleted one: the rest still goes

    await async_clear_guest_pictures(hass, "the stay ended")

    assert pictures.pictures(guest=True) == [] and pictures.pictures(guest=False) == household
    assert not any(path.exists() for path in guest_files)
    assert list(setup.notes) == [household[0].note_id]


# ── Drawing ──────────────────────────────────────────────────────────────────


async def test_a_drawing_in_the_web_chat_is_waited_for(hass: HomeAssistant, setup) -> None:
    hey_auri = FakeHeyAuri()
    turn = web_chat()
    tools = PictureToolService(hass, hey_auri)

    result = await tools.draw_picture({"description": "a dragon eating pizza", "shape": "landscape", "_turn": turn})

    assert result == {"success": True, "shown_in": "chat", "where": "The picture is under your reply.",
                      "title": "a dragon eating pizza", "drawings_left_today": 4}
    path, payload, timeout, _ = hey_auri.calls[0]
    assert (path, timeout) == ("/hub/pictures/draw", 30)
    assert payload["description"] == "a dragon eating pizza" and payload["shape"] == "landscape"
    assert isinstance(payload["utc_offset_minutes"], int)
    assert turn.pictures[0].endswith("<small>🎨 Auri</small>")
    drawing = (await async_get_pictures(hass)).pictures(guest=False)[0]
    assert (drawing.kind, drawing.asked) == ("drawing", "a dragon eating pizza")
    # One a turn.
    assert (await tools.draw_picture({"description": "another", "_turn": turn}))["error"] == "one_at_a_time"
    assert len(hey_auri.calls) == 1


async def test_a_slow_drawing_at_a_satellite_is_said_to_be_coming_and_lands_on_a_note(hass: HomeAssistant, setup, monkeypatch) -> None:
    monkeypatch.setattr(picture_services, "SATELLITE_WAIT_SECONDS", 0.01)
    hey_auri = FakeHeyAuri()
    hey_auri.hold = asyncio.Event()

    result = await PictureToolService(hass, hey_auri).draw_picture({"description": "a unicorn", "_turn": satellite()})

    assert result == {"success": True, "status": "drawing", "shown_in": "sticky_note",
                      "where": "It's being drawn and will be on a new sticky note in a moment: say so."}
    assert setup.notes == {}
    hey_auri.hold.set()
    await hass.async_block_till_done(wait_background_tasks=True)  # the drawing runs in a task of its own
    [note] = setup.notes.values()
    assert note["title"] == "a unicorn" and "<small>🎨 Auri</small>" in note["markdown"]


async def test_a_chat_drawing_that_takes_too_long_lands_on_a_note(hass: HomeAssistant, setup, monkeypatch) -> None:
    monkeypatch.setattr(picture_services, "DRAW_WAIT_SECONDS", 0.01)
    hey_auri = FakeHeyAuri()
    hey_auri.hold = asyncio.Event()
    turn = web_chat()

    result = await PictureToolService(hass, hey_auri).draw_picture({"description": "a unicorn", "_turn": turn})
    hey_auri.hold.set()
    await hass.async_block_till_done(wait_background_tasks=True)  # the drawing runs in a task of its own

    assert result["status"] == "drawing" and turn.pictures == [] and len(setup.notes) == 1


async def test_the_days_drawings_made(hass: HomeAssistant, setup) -> None:
    hey_auri = FakeHeyAuri()
    hey_auri.draw_answer = {"drawn": False, "error": "daily_limit", "limit": 5}

    result = await PictureToolService(hass, hey_auri).draw_picture({"description": "a cat", "_turn": satellite()})

    assert result == {"success": False, "error": "daily_limit", "limit": 5,
                      "message": "This home has made its 5 drawings for today; more tomorrow."}
    assert (await async_get_pictures(hass)).pictures(guest=False) == [] and setup.notes == {}


# ── Cameras ──────────────────────────────────────────────────────────────────


async def test_a_camera_shows_in_the_web_chat_and_nothing_is_kept(hass: HomeAssistant, setup) -> None:
    hass.states.async_set("camera.front_door", "idle", {"friendly_name": "Front door"})
    async_expose_entity(hass, conversation.DOMAIN, "camera.front_door", True)
    hass.states.async_set("camera.garage", "idle", {"friendly_name": "Garage"})  # not exposed to Auri
    tools = PictureToolService(hass, FakeHeyAuri())
    turn = web_chat()

    result = await tools.show_camera({"entity_id": "camera.front_door", "_turn": turn})

    assert result == {"success": True, "shown_in": "chat", "where": "The picture is under your reply.", "camera": "Front door"}
    assert turn.pictures[0].startswith("![Front door](/api/camera_proxy/camera.front_door?authSig=")
    assert (await async_get_pictures(hass)).pictures(guest=False) == []
    assert "retry" in await tools.show_camera({"entity_id": "camera.garage", "_turn": web_chat()})
    assert "retry" in await tools.show_camera({"entity_id": "light.kitchen", "_turn": web_chat()})
    assert await tools.show_camera({"entity_id": "camera.front_door", "_turn": satellite()}) == {
        "success": False, "error": "no_screen"}
    assert await tools.show_camera({"entity_id": "camera.front_door", "_turn": web_chat("guest")}) == {
        "success": False, "error": "not_in_guest_mode"}


async def test_a_cameras_access_token_never_leaves_the_hub() -> None:
    attributes = helpers._filter_entity_attributes(  # noqa: SLF001
        "camera.front_door", "idle", {"access_token": "abc", "entity_picture": "/api/camera_proxy/x?token=abc", "brand": "Reolink"})
    assert attributes == {"brand": "Reolink"}


# ── The reply ────────────────────────────────────────────────────────────────


# ── Videos ───────────────────────────────────────────────────────────────────


async def test_a_video_in_the_web_chat_is_youtubes_thumbnail_linking_to_it(hass: HomeAssistant, setup) -> None:
    hey_auri = FakeHeyAuri()
    turn = web_chat()
    hass.config.country = "CA"

    result = await PictureToolService(hass, hey_auri).find_video({"subject": "抹香鲸", "english": "sperm whale", "_turn": turn})

    assert result == {"where": "The video is under your reply: tap it to watch on YouTube.", "success": True,
                      "shown_in": "chat", "title": "Sperm whales [up close]"}
    [(path, payload, timeout, key)] = hey_auri.calls
    assert (path, timeout, key) == ("/hub/pictures/video", 15, "pictures")
    assert {name: payload[name] for name in ("subject", "english", "country")} == {
        "subject": "抹香鲸", "english": "sperm whale", "country": "CA"}
    assert turn.pictures == [VIDEO_LINES] and setup.notes == {}
    assert (await async_get_pictures(hass)).pictures(guest=False) == []  # nothing kept


async def test_a_video_at_a_satellite_goes_on_a_note_and_a_guests_goes_with_the_stay(hass: HomeAssistant, setup) -> None:
    result = await PictureToolService(hass, FakeHeyAuri()).find_video(
        {"subject": "whales", "english": "whales", "_turn": satellite()})
    assert result["shown_in"] == "sticky_note" and result["where"].startswith("Not on a screen here: the video is on a new sticky note")
    await PictureToolService(hass, FakeHeyAuri()).find_video({"subject": "koalas", "english": "koalas", "_turn": satellite("guest")})
    assert [note["markdown"] for note in setup.notes.values()] == [VIDEO_LINES, VIDEO_LINES]

    await async_clear_guest_pictures(hass, "the stay ended")

    assert list(setup.notes) == ["n1"]  # the household's stays


async def test_no_video_in_mainland_china_and_youtubes_answers(hass: HomeAssistant, setup) -> None:
    hey_auri = FakeHeyAuri()
    service = PictureToolService(hass, hey_auri)
    ask = {"subject": "whales", "english": "whales", "_turn": web_chat()}
    hass.config.country = "CN"
    assert await service.find_video(ask) == {"success": False, "error": "not_available_here", "message": "No video: YouTube isn't available in this home's country. Tell the user that, then offer a photo; show one only on a yes."}
    assert hey_auri.calls == []

    hass.config.country = "US"
    hey_auri.video_answer = {"found": False, "left": 3}
    assert await service.find_video(ask) == {"success": False, "found": False}
    hey_auri.video_answer = {"found": False, "error": "daily_limit", "limit": 10}
    assert (await service.find_video(ask))["message"] == "This home has searched its 10 videos for today; more tomorrow."
    hey_auri.video_answer = {"found": False, "error": "busy"}
    assert await service.find_video(ask) == {"success": False, "error": "busy"}
    hey_auri.video_answer = {**VIDEO, "video_id": "x)](https://evil.example"}  # only a real id makes a link
    assert await service.find_video(ask) == {"success": False, "error": "failed"}
    assert await service.find_video({"_turn": web_chat()}) == {"retry": "subject and english are required"}


def test_speech_leaves_the_pictures_out() -> None:
    reply = ("Here's a sperm whale, the largest toothed whale.\n\n"
             "![Sperm whale](/api/hey_auri_client/pictures/20261125-120000-sperm-whale.jpg?authSig=abc.def)\n"
             "<small>📷 Gabriel Barathieu · CC BY-SA 2.0 · <a href=\"https://commons.wikimedia.org/x\">Wikimedia Commons</a></small>")
    assert without_pictures(reply) == "Here's a sperm whale, the largest toothed whale."
    assert without_pictures("No pictures [here](https://example.com).") == "No pictures [here](https://example.com)."
    assert without_pictures(f"Here's a video of sperm whales.\n\n{VIDEO_LINES}") == "Here's a video of sperm whales."


def _find_call(subject: str) -> dict:
    return {"id": "call-1", "function": {"name": "find_picture", "arguments": json.dumps({"subject": subject, "english": subject})}}


class ScriptedClient:
    def __init__(self, *responses: SaaSTurnResponse) -> None:
        self.responses = list(responses)
        self.payloads: list[dict] = []

    async def send_user_turn(self, payload: dict) -> SaaSTurnResponse:
        self.payloads.append(payload)
        if not self.responses:
            return SaaSTurnResponse(response_type="final", conversation_id="c1", content="OK", mode="owner")
        return self.responses.pop(0)

    send_tool_results = send_user_turn


@pytest.fixture
async def agent(hass: HomeAssistant, setup) -> ThinOpenAIAgent:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_CLIENT_ID: "client", CONF_SHARED_SECRET: "secret"})
    entry.add_to_hass(hass)
    agent = ThinOpenAIAgent(hass, entry)
    agent.tool_executor = LocalToolExecutor(hass, client=FakeHeyAuri())
    return agent


async def _ask_in_the_web_chat(hass: HomeAssistant, agent: ThinOpenAIAgent, text: str):
    user = await hass.auth.async_create_user("Will", group_ids=["system-admin"])
    return await agent.async_process(conversation.ConversationInput(
        text=text, context=Context(user_id=user.id), conversation_id="c1", device_id=None,
        satellite_id=None, language="en", agent_id="hey_auri_client",
    ))


async def test_the_picture_shows_under_the_reply_and_nowhere_else(hass: HomeAssistant, agent: ThinOpenAIAgent) -> None:
    agent.client = ScriptedClient(
        SaaSTurnResponse(response_type="tool_calls", conversation_id="c1", turn_id="r1",
                         tool_calls=[_find_call("sperm whale")], mode="owner"),
        SaaSTurnResponse(response_type="final", conversation_id="c1", content="Here's a sperm whale.", mode="owner"),
    )
    finished = async_capture_events(hass, EVENT_CONVERSATION_FINISHED)

    result = await _ask_in_the_web_chat(hass, agent, "Show me a picture of a sperm whale")

    speech = result.response.speech["plain"]["speech"]
    assert speech.startswith("Here's a sperm whale.\n\n![Sperm whale](/api/hey_auri_client/pictures/")
    assert "Wikimedia Commons" in speech
    assert finished[0].data["response"] == "Here's a sperm whale."
    sent = json.dumps(agent.client.payloads)
    assert "authSig" not in sent and "/api/hey_auri_client" not in sent  # the backend never gets a link


async def test_a_picture_still_shows_when_the_last_round_takes_the_fast_path(hass: HomeAssistant, agent: ThinOpenAIAgent) -> None:
    note = {"id": "call-2", "function": {"name": "create_sticky_note", "arguments": json.dumps({"title": "Whales", "markdown": "x"})}}
    agent.client = ScriptedClient(
        SaaSTurnResponse(response_type="tool_calls", conversation_id="c1", turn_id="r1",
                         tool_calls=[_find_call("sperm whale")], mode="owner"),
        SaaSTurnResponse(response_type="tool_calls", conversation_id="c1", turn_id="r2", tool_calls=[note], mode="owner"),
    )

    async def create_sticky_note(arguments: dict) -> dict:
        return {"success": True}

    agent.tool_executor._handlers["create_sticky_note"] = create_sticky_note  # noqa: SLF001

    result = await _ask_in_the_web_chat(hass, agent, "Show me a sperm whale and note it down")
    await hass.async_block_till_done()

    speech = result.response.speech["plain"]["speech"]
    assert speech.startswith("OK\n\n![Sperm whale](")
