"""Compatibility wrapper for STT platform module."""
from __future__ import annotations

from .speech_to_text.stt import async_setup_entry

__all__ = ["async_setup_entry"]
