"""Names that may be one pet or person spelled two ways: which pairs are asked about, and the answers (memory/names.py)."""
from __future__ import annotations

import pytest

pytest.importorskip("homeassistant")

from custom_components.hey_auri_client.memory.names import (  # noqa: E402
    Merge,
    NamePair,
    close_names,
    exact_duplicates,
    name_answers,
    names_form,
    names_text,
    similar_names,
)
from custom_components.hey_auri_client.memory.recall import SubjectEntity  # noqa: E402


def _entity(entity_id: int, kind: str, name: str, *aliases: str) -> SubjectEntity:
    return SubjectEntity(id=entity_id, kind=kind, name=name, aliases=tuple(aliases))


def test_names_a_letter_or_so_apart():
    assert close_names("Zoe", "Zoey") and close_names("Cleo", "Clio")
    assert close_names("Sofía", "sofia")  # accents and case don't count
    assert close_names("Lillian", "Lilian") and close_names("Stephanie", "Stefanie")  # longer names: two letters
    assert not close_names("Max", "Rex")  # two letters in a short name
    assert not close_names("Mia", "Leo") and not close_names("Biscuit", "Pepper")
    assert not close_names("Al", "Ali")  # too short to tell


def test_pets_or_people_nobody_has_answered_about():
    entities = [
        _entity(1, "pet", "Zoey"), _entity(2, "pet", "Zoe"),
        _entity(3, "person", "Mia"), _entity(4, "person", "Mila"),  # two Home Assistant persons: two people
        _entity(5, "person", "Lillian"), _entity(6, "person", "Lilian"),  # one of them is
        _entity(7, "topic", "Zoo"),  # a topic of 3 letters is too short to be another spelling
        _entity(8, "pet", "Max"), _entity(9, "person", "Mac"),  # a pet against a person: never
        _entity(10, "pet", "Cleo"), _entity(11, "pet", "Bear", "Clio"),  # another spelling counts too
        # Saved in conversation before Auri knew it was a pet, a name is a topic.
        _entity(12, "topic", "Biscit"), _entity(13, "pet", "Biscuit"),
        _entity(14, "topic", "Lincoln"), _entity(15, "topic", "Lincon"),  # two topics: never
    ]
    pairs = similar_names(entities, linked={3, 4, 5}, answered=set())
    assert [(pair.first.id, pair.second.id, pair.linked.id if pair.linked else None) for pair in pairs] == [
        (1, 2, None), (5, 6, 5), (10, 11, None), (12, 13, None),
    ]
    assert names_text(pairs) == "Zoey and Zoe; Lillian and Lilian; Cleo and Bear; Biscit and Biscuit"
    assert [pair.kind for pair in pairs] == ["pet", "person", "pet", "pet"]
    # Said to be two once, never asked again.
    assert [pair.key for pair in similar_names(entities, linked={3, 4, 5}, answered={"1-2"})] == ["5-6", "10-11", "12-13"]


def test_a_name_stored_twice_the_same_way_needs_no_question():
    entities = [
        _entity(4, "person", "William"), _entity(40, "topic", "william"),
        _entity(11, "person", "Lillian"), _entity(41, "topic", "Lillian"),
        _entity(12, "person", "Cleo", "Clio"), _entity(42, "topic", "Clio"),  # another spelling, stored again
        # A person and a pet with one name: which one a topic of that name is, only the household knows.
        _entity(5, "person", "Bella"), _entity(6, "pet", "Bella"), _entity(43, "topic", "Bella"),
        _entity(44, "topic", "Lincoln Elementary"),
    ]
    assert exact_duplicates(entities) == [Merge(4, 40), Merge(11, 41), Merge(12, 42)]


def test_the_question_changes_nothing_until_answered():
    zoey, zoe = _entity(1, "pet", "Zoey"), _entity(2, "pet", "Zoe")
    lillian, lilian = _entity(5, "person", "Lillian"), _entity(6, "person", "Lilian")
    cleo, clio = _entity(10, "pet", "Cleo"), _entity(11, "pet", "Clio")
    biscit, biscuit = _entity(12, "topic", "Biscit"), _entity(13, "pet", "Biscuit")
    pairs = [
        NamePair(zoey, zoe, None), NamePair(lillian, lilian, lillian), NamePair(cleo, clio, None),
        NamePair(biscit, biscuit, None),
    ]

    schema = names_form(pairs)
    assert schema({}) == {
        "Zoey and Zoe": "unsure", "Lillian and Lilian": "unsure", "Cleo and Clio": "unsure", "Biscit and Biscuit": "unsure",
    }
    options = {str(key): field.config["options"] for key, field in schema.schema.items()}
    assert [option["value"] for option in options["Zoey and Zoe"]] == ["same:1", "same:2", "different", "unsure"]
    # A Home Assistant person keeps the name Home Assistant gives them.
    assert [option["value"] for option in options["Lillian and Lilian"]] == ["same:5", "different", "unsure"]
    assert [option["label"] for option in options["Biscit and Biscuit"]][:3] == [
        "The same pet, spelled Biscit", "The same pet, spelled Biscuit", "Two different ones",
    ]

    assert name_answers(pairs, {}) == ([], [])
    merges, different = name_answers(
        pairs,
        {"Zoey and Zoe": "same:2", "Lillian and Lilian": "same:6", "Cleo and Clio": "different", "Biscit and Biscuit": "same:12"},
    )
    # The chosen spelling stays, and a Home Assistant person is never the one merged away. A topic's
    # spelling can be the right one, but the pet is what stays: it takes the topic's spelling.
    assert merges == [Merge(2, 1), Merge(13, 12, "Biscit")] and different == ["10-11"]
    assert name_answers(pairs, {"Biscit and Biscuit": "same:13"}) == ([Merge(13, 12)], [])
