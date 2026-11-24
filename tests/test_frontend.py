"""The frontend files the client serves on every page (frontend.py), cards included since 0.2.14."""
from __future__ import annotations

from pathlib import Path
import re
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("homeassistant")

from custom_components.hey_auri_client import frontend  # noqa: E402

WWW = Path(frontend.__file__).parent / "www"
# The community cards served unchanged, with the version their own banner prints.
THIRD_PARTY = {"auto-entities.js": "1.16.1", "timer-bar-card.js": "1.31.0"}


def test_every_file_in_www_is_served_and_every_served_file_is_there():
    served = set(frontend._ASSET_FILES)
    assert served == {path.name for path in WWW.glob("*.js")}
    assert {"auri-sticky-note-card.js", "auto-entities.js", "timer-bar-card.js"} <= served
    assert not list(WWW.glob("*/")), "flat, like the client's own files"


def test_the_community_cards_carry_their_notices_and_the_versions_listed():
    notices = (WWW / "THIRD_PARTY_LICENSES.md").read_text(encoding="utf-8")
    assert "Copyright (c) 2019 Thomas Lovén" in notices and "Apache License" in notices
    for name, version in THIRD_PARTY.items():
        assert re.search(rf"\| {re.escape(name)} \|[^\n]*\| {re.escape(version)} \|", notices), name
        assert version in (WWW / name).read_text(encoding="utf-8"), f"{name} isn't version {version} any more"
        assert f"{version}" in Path(frontend.__file__).read_text(encoding="utf-8"), name


def test_the_sticky_note_card_steps_aside_for_an_older_copy():
    source = (WWW / "auri-sticky-note-card.js").read_text(encoding="utf-8")
    assert 'if (!customElements.get("auri-sticky-note-card"))' in source


async def test_each_file_is_served_and_loaded_on_every_page_with_a_content_hash(monkeypatch):
    added: list[str] = []
    monkeypatch.setattr(frontend, "add_extra_js_url", lambda hass, url: added.append(url))
    hass = MagicMock()
    hass.http.async_register_static_paths = AsyncMock()
    hass.async_add_executor_job = AsyncMock(side_effect=lambda func, *args: func(*args))
    await frontend.async_register_frontend_resources(hass)
    (paths,), _ = hass.http.async_register_static_paths.call_args
    assert [path.url_path for path in paths] == [f"/hey_auri_client/{name}" for name in frontend._ASSET_FILES]
    assert [url.split("?v=")[0] for url in added] == [f"/hey_auri_client/{name}" for name in frontend._ASSET_FILES]
    assert all(re.fullmatch(r"[0-9a-f]{12}", url.split("?v=")[1]) for url in added)
