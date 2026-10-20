"""Move and import preferences saved by the old file-based preference tool.

That tool wrote /config/www/user_data/<user id prefix>.json, a folder Home
Assistant serves without login, so the files are always moved somewhere
private on startup, whether or not memory is set up. Once memory works,
each file is imported once, as preference memories about the matching
person. A file is only imported when its prefix matches exactly one Home
Assistant user; nothing is ever guessed.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import os
import shutil
from typing import Any, Iterable

from .recall import normalize_statement
from .redaction import contains_secret
from .store import NewMemory

_IMPORTANCE = {"high": 4, "medium": 3, "low": 2}
_MAX_STATEMENT_CHARS = 500


@dataclass(slots=True)
class LegacyFile:
    name: str
    sha256: str
    data: dict[str, Any]


@dataclass(slots=True)
class LegacyImportPlan:
    """What to import from one legacy file."""

    file: LegacyFile
    person_name: str
    person_entity_id: str | None
    memories: list[NewMemory]


def relocate_legacy_user_data(config_dir: str, target_dir_name: str) -> list[str]:
    """Move <config>/www/user_data/*.json into <config>/.storage/<target_dir_name>. Blocking."""
    source = os.path.join(config_dir, "www", "user_data")
    if not os.path.isdir(source):
        return []
    target = os.path.join(config_dir, ".storage", target_dir_name)
    moved: list[str] = []
    for name in sorted(os.listdir(source)):
        path = os.path.join(source, name)
        if not name.endswith(".json") or not os.path.isfile(path):
            continue
        os.makedirs(target, mode=0o700, exist_ok=True)
        destination = os.path.join(target, name)
        if os.path.exists(destination):
            stem, ext = os.path.splitext(name)
            destination = os.path.join(target, f"{stem}.{datetime.now().strftime('%Y%m%d%H%M%S')}{ext}")
        shutil.move(path, destination)
        moved.append(name)
    try:
        os.rmdir(source)  # only succeeds when the folder is now empty
    except OSError:
        pass
    if moved:
        try:
            os.chmod(target, 0o700)
        except OSError:
            pass
    return moved


def load_legacy_files(config_dir: str, target_dir_name: str) -> list[LegacyFile]:
    """Read the relocated files. Blocking."""
    folder = os.path.join(config_dir, ".storage", target_dir_name)
    if not os.path.isdir(folder):
        return []
    files: list[LegacyFile] = []
    for name in sorted(os.listdir(folder)):
        if not name.endswith(".json"):
            continue
        with open(os.path.join(folder, name), "rb") as handle:
            raw = handle.read()
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, ValueError):
            data = {}
        files.append(LegacyFile(name=name, sha256=hashlib.sha256(raw).hexdigest(), data=data if isinstance(data, dict) else {}))
    return files


def plan_legacy_import(
    files: Iterable[LegacyFile],
    *,
    users: Iterable[dict[str, Any]],
    persons: Iterable[dict[str, Any]],
    already_imported: set[str],
) -> tuple[list[LegacyImportPlan], list[str]]:
    """Turn legacy files into memories; returns (plans, names of files skipped as unmatched).

    users: {id, name, system_generated}; persons: {entity_id, name, user_id}.
    """
    user_list = [user for user in users if not user.get("system_generated")]
    person_list = list(persons)
    plans: list[LegacyImportPlan] = []
    skipped: list[str] = []

    for legacy in files:
        if legacy.sha256 in already_imported:
            continue
        prefix = legacy.name[:-5].lower()
        matches = [user for user in user_list if prefix and str(user.get("id") or "").lower().startswith(prefix)]
        if len(matches) != 1:
            skipped.append(legacy.name)
            continue
        user = matches[0]
        person = next((p for p in person_list if p.get("user_id") == user.get("id")), None)
        name = str((person or {}).get("name") or user.get("name") or "").strip()
        if not name:
            skipped.append(legacy.name)
            continue

        memories: list[NewMemory] = []
        seen: set[str] = set()
        for key, record in legacy.data.items():
            value = record.get("value") if isinstance(record, dict) else record
            if value is None or not str(value).strip():
                continue
            statement = f"{name}'s {str(key).replace('_', ' ').strip()}: {str(value).strip()}."
            statement = statement[:_MAX_STATEMENT_CHARS]
            if contains_secret(statement) or normalize_statement(statement) in seen:
                continue
            seen.add(normalize_statement(statement))
            importance = _IMPORTANCE.get(str(record.get("importance") if isinstance(record, dict) else "").lower(), 3)
            occurred_at = None
            if isinstance(record, dict) and record.get("timestamp"):
                try:
                    occurred_at = datetime.fromisoformat(str(record["timestamp"]).replace("Z", "+00:00"))
                except ValueError:
                    occurred_at = None
            memories.append(
                NewMemory(
                    kind="preference",
                    content=statement,
                    source="import",
                    importance=importance,
                    occurred_at=occurred_at,
                    speaker_source="ha_user",
                    speaker_name=name,
                    speaker_person_entity_id=(person or {}).get("entity_id"),
                )
            )
        plans.append(
            LegacyImportPlan(
                file=legacy,
                person_name=name,
                person_entity_id=(person or {}).get("entity_id"),
                memories=memories,
            )
        )
    return plans, skipped
