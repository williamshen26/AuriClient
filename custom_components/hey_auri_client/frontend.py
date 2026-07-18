"""Registers this integration's bundled frontend assets (custom Lovelace cards)."""
from __future__ import annotations

import hashlib
from pathlib import Path

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant

from .const import DOMAIN

_WWW_DIR = Path(__file__).parent / "www"
_SESSION_QR_CARD_FILE = _WWW_DIR / "session-qr-card.js"
_SESSION_QR_CARD_URL = f"/{DOMAIN}/session-qr-card.js"


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
    """Serve session-qr-card.js and register it to auto-load on every dashboard."""
    await hass.http.async_register_static_paths(
        [
            StaticPathConfig(
                _SESSION_QR_CARD_URL,
                str(_SESSION_QR_CARD_FILE),
                cache_headers=False,
            )
        ]
    )
    versioned_url = f"{_SESSION_QR_CARD_URL}?v={_asset_version(_SESSION_QR_CARD_FILE)}"
    add_extra_js_url(hass, versioned_url)
