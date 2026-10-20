"""Buffered, non-blocking writes of owner-mode turns to the conversation archive.

Turns are queued in memory and written in batches off the event loop, so a
slow or unreachable database never slows a conversation. While the database
is down up to MAX_BUFFERED_ROWS rows are kept (oldest dropped first); rows
still unwritten at shutdown are saved to a Store file and written on the
next start.
"""
from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime
import logging
from typing import Any, Awaitable, Callable

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from ..const import DOMAIN

_LOGGER = logging.getLogger(__name__)

MAX_BUFFERED_ROWS = 1000
_BATCH_SIZE = 100
_BATCH_WINDOW_SECONDS = 2.0
_MAX_BACKOFF_SECONDS = 300.0
_SPOOL_STORAGE_KEY = f"{DOMAIN}_memory_turn_spool"
_SPOOL_STORAGE_VERSION = 1


class TurnLogger:
    """Queue archive rows and write them in the background."""

    def __init__(
        self,
        hass: HomeAssistant,
        write: Callable[[list[dict[str, Any]]], Awaitable[None]],
    ) -> None:
        self._hass = hass
        self._write = write
        self._buffer: deque[dict[str, Any]] = deque(maxlen=MAX_BUFFERED_ROWS)
        self._task: asyncio.Task | None = None
        self._backoff = 0.0
        self._closed = False
        self._spool: Store[list[dict[str, Any]]] = Store(hass, _SPOOL_STORAGE_VERSION, _SPOOL_STORAGE_KEY)

    @property
    def pending(self) -> int:
        return len(self._buffer)

    async def async_load_spool(self) -> None:
        """Queue rows saved at the last shutdown."""
        rows = await self._spool.async_load()
        if not rows:
            return
        for row in rows:
            if isinstance(row, dict) and isinstance(row.get("created_at"), str):
                row["created_at"] = datetime.fromisoformat(row["created_at"])
                self._buffer.append(row)
        await self._spool.async_remove()
        _LOGGER.debug("Restored %d unwritten memory log rows", len(rows))
        self.async_schedule_flush()

    def log_nowait(self, rows: list[dict[str, Any]]) -> None:
        """Queue rows (already redacted, each with created_at) for writing."""
        if self._closed or not rows:
            return
        self._buffer.extend(rows)
        self.async_schedule_flush()

    def async_schedule_flush(self) -> None:
        if self._task is None or self._task.done():
            self._task = self._hass.async_create_background_task(
                self._async_flush_loop(),
                name="hey_auri_client memory turn log",
            )

    async def _async_flush_loop(self) -> None:
        await asyncio.sleep(0 if len(self._buffer) >= _BATCH_SIZE else _BATCH_WINDOW_SECONDS)
        while self._buffer and not self._closed:
            batch = [self._buffer.popleft() for _ in range(min(_BATCH_SIZE, len(self._buffer)))]
            try:
                await self._write(batch)
            except Exception as err:  # noqa: BLE001
                # Put the batch back (in order) and wait before retrying.
                self._buffer.extendleft(reversed(batch))
                self._backoff = min(max(self._backoff * 2, 5.0), _MAX_BACKOFF_SECONDS)
                _LOGGER.debug("Memory log write failed (%s); retrying in %.0fs", err, self._backoff)
                await asyncio.sleep(self._backoff)
            else:
                self._backoff = 0.0

    async def async_shutdown(self) -> None:
        """Try a last write, then save whatever is left for the next start."""
        self._closed = True
        if self._task is not None and not self._task.done():
            self._task.cancel()
        if self._buffer:
            batch = list(self._buffer)
            try:
                async with asyncio.timeout(5):
                    await self._write(batch)
                self._buffer.clear()
            except Exception:  # noqa: BLE001
                pass
        if self._buffer:
            rows = []
            for row in self._buffer:
                saved = dict(row)
                if isinstance(saved.get("created_at"), datetime):
                    saved["created_at"] = saved["created_at"].isoformat()
                rows.append(saved)
            await self._spool.async_save(rows)
            self._buffer.clear()
