"""One pet (or person) under two spellings: find them, ask the household once, merge the answer.

Speech-to-text spells an uncommon name differently from one time to the next
(Cleo and Clio), so memory can end up with two pets, or two people Home
Assistant doesn't know, that are really one. Auri reads them as two even with
every memory about them in front of it, and merging two real pets would be
worse than leaving one split. So the household is asked, through a Repairs
notice: the same pet, and which spelling is right, or two? "The same" makes
them one subject that keeps the other spelling, so either name finds it.
"Two" is remembered, and that pair is never asked about again.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Any, NamedTuple
import unicodedata

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir, selector

from ..const import DOMAIN
from .recall import SubjectEntity

if TYPE_CHECKING:
    from .runtime import MemoryRuntime
    from .space import MemorySpace

# The Repairs notice's translation key; each config entry has its own notice.
SIMILAR_NAMES_ISSUE = "similar_names"
# Meta key: the pairs the household said are two ("<smaller id>-<larger id>").
DIFFERENT_NAMES = "different_names"
MAX_PAIRS = 10
SAME = "same:"
DIFFERENT = "different"
UNSURE = "unsure"
_KINDS = ("pet", "person")
# A name saved in conversation before Auri knew it was a pet is a topic ("Cleo is our pet dog"),
# so a topic can be a pet's or a person's other spelling, from 4 letters (not Zoo against Zoe).
_TOPIC = "topic"
_MIN_TOPIC_LETTERS = 4


class NamePair(NamedTuple):
    """Two subjects whose names are a letter or so apart. linked is the Home Assistant person, if one is."""

    first: SubjectEntity
    second: SubjectEntity
    linked: SubjectEntity | None

    @property
    def key(self) -> str:
        low, high = sorted((self.first.id, self.second.id))
        return f"{low}-{high}"

    @property
    def label(self) -> str:
        return f"{self.first.name} and {self.second.name}"

    @property
    def kind(self) -> str:
        """What the two are, if they're one: the pet or person, not the topic."""
        return self.second.kind if self.first.kind == _TOPIC else self.first.kind

    def kept(self, spelled: SubjectEntity) -> SubjectEntity:
        """The subject that stays when they're one, spelled like spelled: a Home Assistant person, else the pet or person."""
        if self.linked is not None:
            return self.linked
        if self.first.kind == _TOPIC:
            return self.second
        if self.second.kind == _TOPIC:
            return self.first
        return spelled


def _plain(name: str) -> str:
    """Lower case, with no accents and nothing but letters and digits, in any script."""
    decomposed = unicodedata.normalize("NFKD", name.casefold())
    return "".join(char for char in decomposed if char.isalnum())


def _distance(a: str, b: str) -> int:
    """How many letters to add, remove or change to turn a into b."""
    previous = list(range(len(b) + 1))
    for i, char_a in enumerate(a, start=1):
        current = [i]
        for j, char_b in enumerate(b, start=1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (char_a != char_b)))
        previous = current
    return previous[-1]


def close_names(a: str, b: str, *, min_letters: int = 3) -> bool:
    """Spelled a letter apart (two for names of 6 letters or more), or alike but for accents and case."""
    first, second = _plain(a), _plain(b)
    shorter = min(len(first), len(second))
    if shorter < min_letters:
        return False
    return _distance(first, second) <= (1 if shorter <= 5 else 2)


def _may_be_one(first: SubjectEntity, second: SubjectEntity, linked: set[int]) -> bool:
    """Two pets, two people (not both in Home Assistant), or a topic and a pet or person."""
    if first.id in linked and second.id in linked:
        return False
    kinds = {first.kind, second.kind}
    return (len(kinds) == 1 and first.kind in _KINDS) or (_TOPIC in kinds and bool(kinds & set(_KINDS)))


def similar_names(entities: Iterable[SubjectEntity], linked: set[int], answered: set[str]) -> list[NamePair]:
    """Pets, or people, whose names are a letter or so apart, that nobody has said are two.

    Two Home Assistant persons are two people, so they're never asked about,
    and nor is a pet against a person. A topic can be a pet's or a person's
    other spelling; two topics aren't asked about.
    """
    candidates = [entity for entity in entities if entity.kind in (*_KINDS, _TOPIC)]
    pairs: list[NamePair] = []
    for index, first in enumerate(candidates):
        for second in candidates[index + 1:]:
            if not _may_be_one(first, second, linked):
                continue
            least = _MIN_TOPIC_LETTERS if _TOPIC in (first.kind, second.kind) else 3
            names_first, names_second = (first.name, *first.aliases), (second.name, *second.aliases)
            if not any(close_names(a, b, min_letters=least) for a in names_first for b in names_second):
                continue
            pair = NamePair(first, second, first if first.id in linked else second if second.id in linked else None)
            if pair.key not in answered:
                pairs.append(pair)
    return pairs[:MAX_PAIRS]


def exact_duplicates(entities: Iterable[SubjectEntity]) -> list["Merge"]:
    """A topic with exactly a pet's or person's name or other spelling, case aside: the same one, stored twice.

    There's nothing to ask: a name already finds the pet or person first, so
    only the topic's older memories were apart (a person renamed in Home
    Assistant can leave one behind). A name that both a pet and a person
    have is left alone.
    """
    owners: dict[str, dict[int, SubjectEntity]] = {}
    for entity in entities:
        if entity.kind in _KINDS:
            for name in (entity.name, *entity.aliases):
                owners.setdefault(name.casefold(), {})[entity.id] = entity
    merges: list[Merge] = []
    for entity in entities:
        found = owners.get(entity.name.casefold(), {}) if entity.kind == _TOPIC else {}
        if len(found) == 1:
            merges.append(Merge(next(iter(found)), entity.id))
    return merges


def names_text(pairs: Sequence[NamePair]) -> str:
    """'Zoe and Zoey', or 'Zoe and Zoey; Cleo and Clio'."""
    return "; ".join(pair.label for pair in pairs)


def names_form(pairs: Sequence[NamePair]) -> vol.Schema:
    """One question per pair: the same (called which), two, or not sure yet (the default, which changes nothing)."""
    fields: dict[Any, Any] = {}
    for pair in pairs:
        what = pair.kind
        if _TOPIC in (pair.first.kind, pair.second.kind):
            two = "Two different ones"
        else:
            two = "Two different pets" if what == "pet" else "Two different people"
        if pair.linked is not None:
            # A Home Assistant person's name comes from Home Assistant.
            same = [
                selector.SelectOptionDict(
                    value=f"{SAME}{pair.linked.id}", label=f"The same {what}, {pair.linked.name} (the name in Home Assistant)"
                )
            ]
        else:
            same = [
                selector.SelectOptionDict(value=f"{SAME}{entity.id}", label=f"The same {what}, spelled {entity.name}")
                for entity in (pair.first, pair.second)
            ]
        choices = [
            *same,
            selector.SelectOptionDict(value=DIFFERENT, label=two),
            selector.SelectOptionDict(value=UNSURE, label="Not sure yet: ask again later"),
        ]
        fields[vol.Optional(pair.label, default=UNSURE)] = selector.SelectSelector(
            selector.SelectSelectorConfig(options=choices, mode=selector.SelectSelectorMode.DROPDOWN)
        )
    return vol.Schema(fields)


class Merge(NamedTuple):
    """One answer of "the same": keep keep_id, spelled name (None: as it is), and fold drop_id into it."""

    keep_id: int
    drop_id: int
    name: str | None = None


def name_answers(pairs: Sequence[NamePair], user_input: Mapping[str, Any]) -> tuple[list[Merge], list[str]]:
    """([Merge] for the pairs that are the same, [keys of the pairs that are two])."""
    merges: list[Merge] = []
    different: list[str] = []
    for pair in pairs:
        choice = str(user_input.get(pair.label) or UNSURE)
        if choice == DIFFERENT:
            different.append(pair.key)
        elif choice.startswith(SAME) and choice[len(SAME):].isdigit():
            chosen = int(choice[len(SAME):])
            spelled = pair.first if chosen == pair.first.id else pair.second if chosen == pair.second.id else None
            # A Home Assistant person keeps its own name, so only that one can be chosen.
            if spelled is None or (pair.linked is not None and spelled is not pair.linked):
                continue
            keep = pair.kept(spelled)
            drop = pair.second if keep is pair.first else pair.first
            merges.append(Merge(keep.id, drop.id, None if keep is spelled else spelled.name))
    return merges, different


def merge_blocking(store, merges: Sequence[Merge], different: Sequence[str]) -> None:
    """Merge the pairs that are the same and remember the ones that are two, in one transaction."""
    went_to: dict[int, int] = {}

    def follow(entity_id: int) -> int:
        while entity_id in went_to:
            entity_id = went_to[entity_id]
        return entity_id

    with store.engine.begin() as conn:
        for merge in merges:
            # Zoe into Zoey and then Zoie into Zoe: Zoie goes to Zoey.
            keep_id, drop_id = follow(merge.keep_id), follow(merge.drop_id)
            if keep_id != drop_id and store.merge_entities(conn, keep_id, drop_id, name=merge.name):
                went_to[drop_id] = keep_id
        if different:
            known = set(store.get_meta(DIFFERENT_NAMES) or [])
            store.set_meta(DIFFERENT_NAMES, sorted(known | set(different)), conn)


async def async_merge_exact_duplicates(runtime: "MemoryRuntime") -> int:
    """Merge the topics stored under exactly a pet's or person's name; how many there were."""
    space = runtime.owner
    store = space.store
    if store is None or not runtime.enabled:
        return 0
    merges = exact_duplicates(space.entities)
    if merges:
        await runtime.hass.async_add_executor_job(merge_blocking, store, merges, [])
        space.invalidate()
        await space.async_refresh_entities()
    return len(merges)


async def async_name_pairs(runtime: "MemoryRuntime") -> list[NamePair]:
    """The pairs to ask about in the household's memory, or none while memory isn't working."""
    space = runtime.owner
    store = space.store
    if store is None or not runtime.enabled:
        return []
    answered = set(await runtime.hass.async_add_executor_job(store.get_meta, DIFFERENT_NAMES) or [])
    return space_pairs(space, answered)


def space_pairs(space: "MemorySpace", answered: set[str]) -> list[NamePair]:
    return similar_names(space.entities, set(space.person_ids.values()), answered)


async def async_apply_name_answers(runtime: "MemoryRuntime", pairs: Sequence[NamePair], user_input: Mapping[str, Any]) -> None:
    merges, different = name_answers(pairs, user_input)
    space = runtime.owner
    if (merges or different) and space.store is not None:
        await runtime.hass.async_add_executor_job(merge_blocking, space.store, merges, different)
        space.invalidate()
        await space.async_refresh_entities()


def _issue_id(entry_id: str) -> str:
    return f"{SIMILAR_NAMES_ISSUE}_{entry_id}"


async def async_update_names_issue(hass: HomeAssistant, entry: ConfigEntry, runtime: "MemoryRuntime | None") -> None:
    """Raise the Repairs notice for names that may be one pet or person, or remove it when there are none.

    Checked when memory connects, after each nightly run (which may learn a
    new name) and after an options change, which can turn memory off. A name
    stored twice exactly the same way is merged first, without asking.
    """
    if runtime is not None:
        await async_merge_exact_duplicates(runtime)
    pairs = await async_name_pairs(runtime) if runtime is not None else []
    if not pairs:
        async_delete_names_issue(hass, entry.entry_id)
        return
    ir.async_create_issue(
        hass,
        DOMAIN,
        _issue_id(entry.entry_id),
        data={"entry_id": entry.entry_id},
        is_fixable=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key=SIMILAR_NAMES_ISSUE,
        translation_placeholders={"names": names_text(pairs)},
    )


def async_delete_names_issue(hass: HomeAssistant, entry_id: str) -> None:
    """Remove the notice (deleting one that isn't there is fine)."""
    ir.async_delete_issue(hass, DOMAIN, _issue_id(entry_id))
