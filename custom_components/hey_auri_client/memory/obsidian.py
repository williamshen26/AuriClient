"""Readable copy of the memory in an Obsidian vault folder.

Writes <vault>/Auri/Daily/YYYY-MM-DD.md for each processed day and
regenerates <vault>/Auri/Memory.md with the current facts. The database is
the source of truth: edits made in the vault are not read back. Everything
here blocks and must run in an executor.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, tzinfo
import os
from typing import Callable, Iterable

from .recall import RecallRow

AURI_FOLDER = "Auri"
_KIND_TITLES = {
    "preference": "Preferences",
    "fact": "Facts",
    "decision": "Decisions",
    "commitment": "Plans and commitments",
    "about_assistant": "About the assistant",
}


def validate_vault(path: str, *, config_dir: str, is_allowed_path: Callable[[str], bool]) -> str | None:
    """Return an error code for an unusable vault folder, or None when it's fine (empty = no vault)."""
    if not path:
        return None
    real = os.path.realpath(path)
    public = os.path.realpath(os.path.join(config_dir, "www"))
    if real == public or real.startswith(public + os.sep):
        return "vault_public"
    if not os.path.isdir(real):
        return "vault_not_found"
    if not is_allowed_path(real):
        return "vault_not_allowed"
    if not os.access(real, os.W_OK):
        return "vault_not_writable"
    return None


def _atomic_write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = f"{path}.auri-tmp"
    with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    os.replace(temporary, path)


def write_daily_note(vault: str, day: date, digest: str) -> str:
    """Write (or replace) the day's digest note; returns its path."""
    path = os.path.join(vault, AURI_FOLDER, "Daily", f"{day.isoformat()}.md")
    body = (
        "---\n"
        f"date: {day.isoformat()}\n"
        "source: Auri\n"
        "tags: [auri, daily-digest]\n"
        "---\n\n"
        f"# {day.strftime('%A, %B')} {day.day}, {day.year}\n\n"
        f"{digest.strip()}\n"
    )
    _atomic_write(path, body)
    return path


def remove_daily_note(vault: str, day: date) -> bool:
    """Delete a day's note (its digest was forgotten entirely); True when a file was removed."""
    path = os.path.join(vault, AURI_FOLDER, "Daily", f"{day.isoformat()}.md")
    try:
        os.remove(path)
    except FileNotFoundError:
        return False
    return True


def write_memory_note(vault: str, rows: Iterable[RecallRow], *, tz: tzinfo | None, generated_on: date) -> str:
    """Regenerate Memory.md: current memories grouped by who/what they're about."""
    groups: dict[str, list[RecallRow]] = defaultdict(list)
    for row in rows:
        groups[", ".join(row.about) if row.about else "Household"].append(row)

    lines = [
        "---",
        "source: Auri",
        "tags: [auri, memory]",
        "---",
        "",
        "# What Auri remembers",
        "",
        f"_Generated {generated_on.isoformat()}. This page is rewritten every night; edits here aren't read back._",
        "",
    ]
    for subject in sorted(groups, key=lambda name: (name != "Household", name.lower())):
        lines.append(f"## {subject}")
        by_kind: dict[str, list[RecallRow]] = defaultdict(list)
        for row in groups[subject]:
            by_kind[row.kind].append(row)
        for kind, title in _KIND_TITLES.items():
            if kind not in by_kind:
                continue
            lines.append(f"### {title}")
            for row in by_kind[kind]:
                if row.due is not None:
                    when = "due " + (row.due.astimezone(tz) if tz is not None else row.due).date().isoformat()
                else:
                    when = row.at.astimezone(tz).date().isoformat() if row.at is not None and tz is not None else ""
                private = " 🔒" if row.sensitivity == "private" else ""
                lines.append(f"- {row.content}{private}" + (f" ({when})" if when else ""))
            lines.append("")
    path = os.path.join(vault, AURI_FOLDER, "Memory.md")
    _atomic_write(path, "\n".join(lines).rstrip() + "\n")
    return path
