"""Registers this integration's bundled frontend assets: the session QR card, and the Home dashboard's
strategies (dashboard/ in the repository, built into www/auri-dashboard.js), on every page."""
from __future__ import annotations

import hashlib
from pathlib import Path

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant

from .const import DOMAIN

_WWW_DIR = Path(__file__).parent / "www"
# Served at /hey_auri_client/<file>. The dashboard used to be copied to /config/www/dashboards and
# added as a resource by hand; it now updates with the client.
_ASSET_FILES = ("session-qr-card.js", "auri-dashboard.js")


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
