"""The frontend files the client serves (frontend.py), cards included since 0.2.14."""
from __future__ import annotations

import colorsys
from pathlib import Path
import re
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urlsplit

import pytest

pytest.importorskip("homeassistant")

from custom_components.hey_auri_client import frontend  # noqa: E402

WWW = Path(frontend.__file__).parent / "www"
# The community cards served unchanged, with the version their own banner prints.
THIRD_PARTY = {"auto-entities.js": "1.16.1", "timer-bar-card.js": "1.31.0"}


def test_every_file_in_www_is_served_and_every_served_file_is_there():
    served = {*frontend._PAGE_FILES, *frontend._DASHBOARD_CARDS}
    assert served == {path.name for path in WWW.glob("*.js")}
    assert set(frontend._FONT_FILES) == {path.name for path in WWW.glob("*.woff2")}
    assert set(frontend._DASHBOARD_CARDS) == set(THIRD_PARTY)
    assert not list(WWW.glob("*/")), "flat, like the client's own files"


def test_the_community_cards_carry_their_notices_and_the_versions_listed():
    notices = (WWW / "THIRD_PARTY_LICENSES.md").read_text(encoding="utf-8")
    assert "Copyright (c) 2019 Thomas Lovén" in notices and "Apache License" in notices
    for name, version in THIRD_PARTY.items():
        assert re.search(rf"\| {re.escape(name)} \|[^\n]*\| {re.escape(version)} \|", notices), name
        assert version in (WWW / name).read_text(encoding="utf-8"), f"{name} isn't version {version} any more"
        assert version in Path(frontend.__file__).read_text(encoding="utf-8"), name


def test_the_sticky_notes_handwriting_is_served_from_the_hub_with_its_licence():
    font = WWW / "patrick-hand.woff2"
    assert font.read_bytes()[:4] == b"wOF2"
    notices = (WWW / "THIRD_PARTY_LICENSES.md").read_text(encoding="utf-8")
    assert "| patrick-hand.woff2 |" in notices and "SIL OPEN FONT LICENSE Version 1.1" in notices
    assert "Copyright (c) 2010-2012 Patrick Wagesreiter" in notices
    card = (WWW / "auri-sticky-note-card.js").read_text(encoding="utf-8")
    assert "/hey_auri_client/patrick-hand.woff2" in card
    assert "fonts.googleapis.com" not in card  # blocked in mainland China, and nothing to fetch offline


def test_the_sticky_note_card_steps_aside_for_an_older_copy():
    source = (WWW / "auri-sticky-note-card.js").read_text(encoding="utf-8")
    assert 'if (!customElements.get("auri-sticky-note-card"))' in source


def test_sticky_note_text_is_never_inside_a_filter():
    # Chrome left a photo note's credit and age undrawn until hovered (2026-11-26): the shadow, a filter, was on the
    # element holding the text. The shadow is now its own layer behind the paper.
    source = (WWW / "auri-sticky-note-card.js").read_text(encoding="utf-8")
    assert '<div class="auri-shadow" aria-hidden="true"></div>' in source
    rules = re.findall(r"([^{}]+)\{([^{}]*)\}", source[source.index("_injectStyles() {") :])
    filtered = [selector.strip() for selector, body in rules if re.search(r"(?<![-\w])filter\s*:", body)]
    assert filtered and all(selector.endswith(".auri-shadow") for selector in filtered), filtered


def _luminance(colour: str) -> float:
    def channel(part: str) -> float:
        value = int(part, 16) / 255
        return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4

    red, green, blue = (channel(colour[i : i + 2]) for i in (1, 3, 5))
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def test_sticky_notes_fade_with_age_and_stay_readable():
    source = (WWW / "auri-sticky-note-card.js").read_text(encoding="utf-8")
    start = source.index("const AURI_PAPERS = [")
    papers = [re.findall(r"#[0-9A-F]{6}", line) for line in source[start : source.index("];", start)].splitlines()[1:]]
    assert len(papers) == 6 and all(len(colours) == 6 for colours in papers)  # top, bottom, fold, then a year old
    ink = _luminance("#2B2118")
    for index, (top, bottom, fold, old_top, old_bottom, old_fold) in enumerate(papers):
        # Ink on the paper, new or a year old; the card mixes the two as light, so every age in between is as readable.
        for paper in (top, bottom, old_top, old_bottom):
            assert (_luminance(paper) + 0.05) / (ink + 0.05) >= 6.8
        for new, old in ((top, old_top), (bottom, old_bottom), (fold, old_fold)):
            new_hls, old_hls = (colorsys.rgb_to_hls(*(int(c[i : i + 2], 16) / 255 for i in (1, 3, 5))) for c in (new, old))
            if index < 5:  # the colours fade: at least 30% less saturated
                assert old_hls[2] <= new_hls[2] * 0.7
            else:  # white paper yellows
                assert old_hls[1] < new_hls[1]
    assert "const AURI_OLDEST_DAYS = 365;" in source  # the oldest look, at a year, and no older
    assert "Math.min(days, AURI_OLDEST_DAYS) / AURI_OLDEST_DAYS" in source
    assert 'style.setProperty("--paper-mat"' in source and "var(--paper-mat, #FDFDFB)" in source  # photo mats too


def test_videos_in_the_chat_play_with_the_notes_player_and_otherwise_open_youtube():
    # Checked in a browser against a stand-in for HA's chat (2026-11-26); these guard the parts that keep it safe.
    source = (WWW / "auri-sticky-note-card.js").read_text(encoding="utf-8")
    chat = source[source.index("// Videos in Home Assistant's chat") :]
    assert "frontend.py" in source and "auri-sticky-note-card.js" in frontend._PAGE_FILES  # on every page
    assert 'window.addEventListener("click", auriPlayChatVideo, true);' in chat  # sees the tap first
    assert 'Symbol.for("hey-auri-chat-videos")' in chat  # one listener, even if the file loads twice
    assert 'link.closest("ha-markdown-element")' in chat  # only inside Home Assistant's markdown
    assert "thumbnail[1] !== watch[1]" in chat  # the same video in the link and the thumbnail
    assert "auriPlayer(id," in chat  # the notes' player: its referrer fix and the no-cookie address
    # The tap is taken from the link only once the player is in; anything before that opens YouTube.
    assert chat.index("found.link.replaceWith(box);") < chat.index("event.preventDefault();")
    assert "event.ctrlKey || event.metaKey || event.shiftKey || event.altKey" in chat  # a new tab when asked


async def _register(monkeypatch) -> tuple[MagicMock, list[str]]:
    added: list[str] = []
    monkeypatch.setattr(frontend, "add_extra_js_url", lambda hass, url: added.append(url))
    hass = MagicMock()
    hass.http.async_register_static_paths = AsyncMock()
    hass.async_add_executor_job = AsyncMock(side_effect=lambda func, *args: func(*args))
    await frontend.async_register_frontend_resources(hass)
    return hass, added


async def test_every_file_is_served_and_only_the_page_files_load_on_every_page(monkeypatch):
    hass, added = await _register(monkeypatch)
    (paths,), _ = hass.http.async_register_static_paths.call_args
    assert {path.url_path for path in paths} == {
        f"/hey_auri_client/{name}" for name in (*frontend._PAGE_FILES, *frontend._DASHBOARD_CARDS, *frontend._FONT_FILES)
    }
    assert [urlsplit(url).path for url in added] == [f"/hey_auri_client/{name}" for name in frontend._PAGE_FILES]
    assert all(re.fullmatch(r"[0-9a-f]{12}", parse_qs(urlsplit(url).query)["v"][0]) for url in added)


async def test_the_community_cards_never_load_on_every_page(monkeypatch):
    # timer-bar-card loaded at page start sent every refresh to the Overview dashboard (2026-11-23).
    _, added = await _register(monkeypatch)
    for name in THIRD_PARTY:
        assert not any(urlsplit(url).path.endswith(name) for url in added), name


async def test_the_dashboard_bundle_carries_each_cards_version(monkeypatch):
    _, added = await _register(monkeypatch)
    (bundle,) = [url for url in added if urlsplit(url).path.endswith("/auri-dashboard.js")]
    query = parse_qs(urlsplit(bundle).query)
    for name in THIRD_PARTY:
        assert query[name] == [frontend._asset_version(WWW / name)], name


def test_the_dashboard_strategies_load_the_community_cards_themselves():
    bundle = (WWW / "auri-dashboard.js").read_text(encoding="utf-8")
    for name in THIRD_PARTY:
        assert f'"{name}"' in bundle, name
    assert "import.meta.url" in bundle
    # The Home dashboard, its area views and the Sticky Notes view: each before it builds any card.
    assert bundle.count("await loadCommunityCards()") == 3
