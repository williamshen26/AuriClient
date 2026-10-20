"""Argument parsing in the memory tools."""
from __future__ import annotations

import pytest

pytest.importorskip("homeassistant")

from custom_components.hey_auri_client.memory.tools import _memory_ids, relative_date_words  # noqa: E402


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ([17, 18], [17, 18]),
        (["#17", " 18 ", "17"], [17, 18]),
        (17, [17]),
        ("#17", [17]),
        ([True, 0, -3, 2.5, "seventeen", None], []),
        (None, []),
        ("", []),
        (list(range(1, 30)), [1, 2, 3, 4, 5]),
    ],
)
def test_memory_ids(value, expected):
    assert _memory_ids(value, 5) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Will has a piano lesson this Thursday.", "this Thursday"),
        ("Mia's dentist appointment is tomorrow at 3 pm.", "tomorrow"),
        ("The plumber comes next week.", "next week"),
        ("Will is back in two weeks.", "in two weeks"),
        ("Grandma visits TONIGHT.", "TONIGHT"),
        ("Will has a piano lesson on Thursday, 2026-10-15.", None),
        ("Mia has piano every Thursday at 4 pm.", None),
        ("Will likes his coffee black.", None),
    ],
)
def test_relative_date_words(text, expected):
    assert relative_date_words(text) == expected
