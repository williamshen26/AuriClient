"""File utility helpers for conversation-related services."""
from __future__ import annotations

import os

import aiofiles


async def read_from_file(file_path: str) -> str:
    """Read UTF-8 text from a file."""
    async with aiofiles.open(file_path, "r", encoding="utf-8") as handle:
        return await handle.read()


async def write_to_file(file_path: str, mode: str, data: str) -> None:
    """Write UTF-8 text to a file, creating directories as needed."""
    directory = os.path.dirname(file_path)
    if directory:
        os.makedirs(directory, exist_ok=True)

    async with aiofiles.open(file_path, mode, encoding="utf-8") as handle:
        await handle.write(data)


async def write_bytes_to_file(file_path: str, mode: str, data: bytes) -> None:
    """Write binary data to a file, creating directories as needed."""
    directory = os.path.dirname(file_path)
    if directory:
        os.makedirs(directory, exist_ok=True)

    async with aiofiles.open(file_path, mode) as handle:
        await handle.write(data)
