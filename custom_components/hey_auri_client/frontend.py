"""Registers this integration's bundled frontend assets on every page: the Home dashboard's strategies
(dashboard/ in the repository, built into www/auri-dashboard.js), the session QR card, the sticky note card, and
the two community cards the Home dashboard builds with (auto-entities and timer-bar-card; licences in
www/THIRD_PARTY_LICENSES.md)."""
from __future__ import annotations

import hashlib
from pathlib import Path

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant

from .const import DOMAIN

_WWW_DIR = Path(__file__).parent / "www"
# Served at /hey_auri_client/<file>. These used to be copied to /config/www by hand and added as dashboard
# resources; they now install and update with the client, so one install sets up the dashboards.
# - auri-sticky-note-card.js: the sticky note card (the Home dashboard and the Sticky Notes view).
# - auto-entities.js: auto-entities 1.16.1 (thomasloven/lovelace-auto-entities, MIT), unchanged.
# - timer-bar-card.js: timer-bar-card 1.31.0 (rianadon/timer-bar-card, Apache-2.0), unchanged.
# They load before the dashboards' own resources, so a hub that still has the old copies as resources uses
# these; the old copies then fail to register again (a console message only).
_ASSET_FILES = (
    "session-qr-card.js",
    "auri-dashboard.js",
    "auri-sticky-note-card.js",
    "auto-entities.js",
    "timer-bar-card.js",
)


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
    """Serve the assets and register each to load on every dashboard."""
    await hass.http.async_register_static_paths(
        [
            StaticPathConfig(f"/{DOMAIN}/{name}", str(_WWW_DIR / name), cache_headers=False)
            for name in _ASSET_FILES
        ]
    )
    for name in _ASSET_FILES:
        version = await hass.async_add_executor_job(_asset_version, _WWW_DIR / name)
        add_extra_js_url(hass, f"/{DOMAIN}/{name}?v={version}")
