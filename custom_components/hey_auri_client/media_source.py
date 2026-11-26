"""The Media panel's "Hey Auri pictures": the photos and drawings Auri kept (pictures.py), newest first.

The household's pictures, and a Guests folder while a stay's guests have some (it empties when the stay
ends). A media source isn't told who is looking, so both are listed for everyone signed in.
"""
from __future__ import annotations

from homeassistant.components.media_player import BrowseError, MediaClass
from homeassistant.components.media_source import (
    BrowseMediaSource,
    MediaSource,
    MediaSourceItem,
    PlayMedia,
    Unresolvable,
)
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .pictures import URL_PREFIX, Picture, async_get_pictures

NAME = "Hey Auri pictures"
GUEST_FOLDER = "guest"


async def async_get_media_source(hass: HomeAssistant) -> MediaSource:
    """Home Assistant finds this the first time the Media panel is opened."""
    return AuriPicturesSource(hass)


class AuriPicturesSource(MediaSource):
    """The kept pictures, for browsing and showing."""

    name = NAME

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(DOMAIN)
        self.hass = hass

    async def async_resolve_media(self, item: MediaSourceItem) -> PlayMedia:
        picture = (await async_get_pictures(self.hass)).get(item.identifier or "")
        if picture is None:
            raise Unresolvable(f"No such picture: {item.identifier}")
        # The plain path: Home Assistant signs it for the one who asked (it escapes a query, so no key here).
        return PlayMedia(f"{URL_PREFIX}/{picture.path}", "image/jpeg")

    async def async_browse_media(self, item: MediaSourceItem) -> BrowseMediaSource:
        pictures = await async_get_pictures(self.hass)
        identifier = item.identifier or ""
        if identifier == GUEST_FOLDER:
            return _folder(GUEST_FOLDER, "Guests", [_picture(p) for p in pictures.pictures(guest=True)])
        if identifier:
            raise BrowseError(f"Not a folder: {identifier}")
        children = [_picture(p) for p in pictures.pictures(guest=False)]
        guests = pictures.pictures(guest=True)
        if guests:
            children.insert(0, _folder(GUEST_FOLDER, "Guests", None, thumbnail=guests[0].thumb_url_path))
        return _folder(None, NAME, children)


def _folder(identifier: str | None, title: str, children: list[BrowseMediaSource] | None, *,
            thumbnail: str | None = None) -> BrowseMediaSource:
    return BrowseMediaSource(
        domain=DOMAIN,
        identifier=identifier,
        media_class=MediaClass.DIRECTORY,
        media_content_type="",
        title=title,
        can_play=False,
        can_expand=True,
        children=children,
        children_media_class=MediaClass.IMAGE,
        thumbnail=thumbnail,
    )


def _picture(picture: Picture) -> BrowseMediaSource:
    return BrowseMediaSource(
        domain=DOMAIN,
        identifier=picture.path,
        media_class=MediaClass.IMAGE,
        media_content_type="image/jpeg",
        title=f"{picture.title} ({picture.file[:4]}-{picture.file[4:6]}-{picture.file[6:8]})",
        can_play=True,
        can_expand=False,
        # Fetched with the viewer's login by the Media panel, which shows it inline.
        thumbnail=picture.thumb_url_path,
    )
