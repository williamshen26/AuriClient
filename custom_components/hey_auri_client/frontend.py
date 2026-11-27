"""Serves this integration's bundled frontend files: the Home dashboard's strategies (dashboard/ in the repository,
built into www/auri-dashboard.js), the session QR card, the sticky note card, and the two community cards the
dashboards build with (auto-entities and timer-bar-card; licences in www/THIRD_PARTY_LICENSES.md)."""
from __future__ import annotations

import hashlib
from pathlib import Path
from urllib.parse import urlencode

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant

from .const import DOMAIN

_WWW_DIR = Path(__file__).parent / "www"
# Served at /hey_auri_client/<file> and loaded on every page. These used to be copied to /config/www by hand and
# added as dashboard resources; they now install and update with the client, so one install sets up the dashboards.
# A hub that still has an old copy as a resource uses these (they load first); the old copy then steps aside.
_PAGE_FILES = (
    "session-qr-card.js",
    "auri-dashboard.js",
    "auri-sticky-note-card.js",  # the sticky note card (the Home dashboard and the Sticky Notes view), and Auri's
    # videos playing in the chat, which needs to be on every page
)
_DASHBOARD_BUNDLE = "auri-dashboard.js"
# Served too, but loaded by the dashboards' strategies when they generate (dashboard/helpers/communityCards.js),
# never on every page: timer-bar-card, loaded at page start, builds a hidden Lovelace panel before Home Assistant
# has set up its routes, which sent every refresh to the default Overview (2026-11-23). Both unchanged releases:
# - auto-entities 1.16.1 (thomasloven/lovelace-auto-entities, MIT);
# - timer-bar-card 1.31.0 (rianadon/timer-bar-card, Apache-2.0).
_DASHBOARD_CARDS = ("auto-entities.js", "timer-bar-card.js")
# Served for the sticky note card's handwriting, which declares it itself: Patrick Hand 1.003 (SIL Open Font
# License 1.1), converted to WOFF2. Served from the hub, not Google Fonts, so it works offline and in mainland China.
_FONT_FILES = ("patrick-hand.woff2",)


def _asset_version(path: Path) -> str:
    """Short content hash used to cache-bust the URL whenever the file changes.

    A fingerprinted URL sidesteps browser cache-header ambiguity entirely —
    old and new versions of the file are simply different URLs, so it
    doesn't matter whether/how aggressively any given browser caches them.
    """
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    except OSError:
        return "0"


async def async_register_frontend_resources(hass: HomeAssistant) -> None:
    """Serve every file; load the page files on every page, the dashboard bundle telling the cards' versions."""
    await hass.http.async_register_static_paths(
        [
            StaticPathConfig(f"/{DOMAIN}/{name}", str(_WWW_DIR / name), cache_headers=False)
            for name in (*_PAGE_FILES, *_DASHBOARD_CARDS, *_FONT_FILES)
        ]
    )
    card_versions = {
        name: await hass.async_add_executor_job(_asset_version, _WWW_DIR / name) for name in _DASHBOARD_CARDS
    }
    for name in _PAGE_FILES:
        query = {"v": await hass.async_add_executor_job(_asset_version, _WWW_DIR / name)}
        if name == _DASHBOARD_BUNDLE:
            # Read by communityCards.js: a card's new version gives the bundle a new URL, and the card one too.
            query.update(card_versions)
        add_extra_js_url(hass, f"/{DOMAIN}/{name}?{urlencode(query)}")
