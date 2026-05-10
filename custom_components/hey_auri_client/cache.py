"""In-memory cache for per-conversation processed entities."""
from __future__ import annotations

processed_entities: dict[str, list[str]] = {}
media_player_sources: dict[str, list[str]] = {}


def reset_processed_entities(conversation_id: str) -> None:
    """Reset tracked processed entities for one conversation."""
    processed_entities[conversation_id] = []


def add_processed_entity(conversation_id: str, entity_id: str) -> None:
    """Track an entity as processed for one conversation."""
    tracked = processed_entities.setdefault(conversation_id, [])
    if entity_id not in tracked:
        tracked.append(entity_id)


def get_processed_entities(conversation_id: str | None) -> set[str]:
    """Return processed entity ids for one conversation."""
    if not conversation_id:
        return set()
    return set(processed_entities.get(conversation_id, []))


def set_media_player_sources(sources: dict[str, list[str]]) -> None:
    """Replace cached media player sources with a preloaded snapshot."""
    media_player_sources.clear()
    for entity_id, source_list in sources.items():
        media_player_sources[entity_id] = list(source_list)


def upsert_media_player_sources(entity_id: str, source_list: list[str]) -> bool:
    """Store source_list and return True when the cached value changes."""
    normalized = list(source_list)
    if media_player_sources.get(entity_id) == normalized:
        return False
    media_player_sources[entity_id] = normalized
    return True


def get_media_player_sources(entity_id: str) -> list[str]:
    """Return the latest known source_list for a media_player entity."""
    return list(media_player_sources.get(entity_id, []))


def dump_media_player_sources() -> dict[str, list[str]]:
    """Return a copy of all cached media player source lists."""
    return {
        entity_id: list(source_list)
        for entity_id, source_list in media_player_sources.items()
    }
