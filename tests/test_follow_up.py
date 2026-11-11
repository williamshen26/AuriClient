"""Turns heard from a mic left open after Auri's reply, and the limit on them in a row."""
from __future__ import annotations

import pytest

from custom_components.hey_auri_client.conversation.follow_up import (
    FOLLOW_UP_WINDOW_SECONDS,
    INTERRUPTION_WINDOW_SECONDS,
    WAKE_WINDOW_SECONDS,
    FollowUpTracker,
    is_echo,
)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def tracker(max_rounds: int = 10) -> tuple[FollowUpTracker, Clock]:
    clock = Clock()
    return FollowUpTracker(max_rounds=max_rounds, clock=clock), clock


def test_a_reply_that_keeps_listening_makes_the_next_turn_an_open_mic_one():
    follow_ups, clock = tracker()
    assert follow_ups.begin_turn("c1", "sat", "walk me through a steak recipe").open_mic is False  # the wake word
    assert follow_ups.finish_turn("c1", listen=True, reply="First, salt it.") is True
    clock.now += 20
    assert follow_ups.begin_turn("c1", "sat", "done, what's next").open_mic is True


def test_ten_follow_ups_in_a_row_then_the_wake_word_again():
    follow_ups, clock = tracker()
    follow_ups.begin_turn("c1", "sat", "walk me through it")
    for _ in range(10):
        assert follow_ups.finish_turn("c1", listen=True, reply="Next step.") is True
        clock.now += 10
        assert follow_ups.begin_turn("c1", "sat", "next").open_mic is True
    assert follow_ups.finish_turn("c1", listen=True, reply="Next step.") is False  # the 10th follow-up's reply
    clock.now += 10
    assert follow_ups.begin_turn("c1", "sat", "and then?").open_mic is False  # needs the wake word
    assert follow_ups.finish_turn("c1", listen=True) is True  # a new chain


def test_a_reply_without_listen_starts_the_count_over():
    follow_ups, _clock = tracker(max_rounds=2)
    follow_ups.begin_turn("c1", "sat", "a")
    assert follow_ups.finish_turn("c1", listen=True) is True
    follow_ups.begin_turn("c1", "sat", "b")
    assert follow_ups.finish_turn("c1", listen=False) is False  # done: the chain ends
    follow_ups.begin_turn("c1", "sat", "c")  # the wake word again
    assert follow_ups.finish_turn("c1", listen=True) is True
    follow_ups.begin_turn("c1", "sat", "d")
    assert follow_ups.finish_turn("c1", listen=True) is True
    follow_ups.begin_turn("c1", "sat", "e")
    assert follow_ups.finish_turn("c1", listen=True) is False  # two follow-ups in this chain



def test_after_a_reply_without_listen_an_interruption_starts_a_new_count():
    follow_ups, _clock = tracker(max_rounds=1)
    follow_ups.begin_turn("c1", "sat", "walk me through it")
    follow_ups.finish_turn("c1", listen=True)
    follow_ups.begin_turn("c1", "sat", "and then?")  # the one follow-up allowed
    follow_ups.finish_turn("c1", listen=False, reply="Then you're done.")  # the chain ends here
    follow_ups.note_interruption("sat")
    assert follow_ups.begin_turn("c1", "sat", "wait, write it down").over_limit is False

def test_a_listen_more_than_two_minutes_old_no_longer_counts():
    follow_ups, clock = tracker()
    follow_ups.begin_turn("c1", "sat", "a")
    follow_ups.finish_turn("c1", listen=True)
    clock.now += FOLLOW_UP_WINDOW_SECONDS + 1
    assert follow_ups.begin_turn("c1", "sat", "something new").open_mic is False


def test_each_conversation_counts_on_its_own():
    follow_ups, _clock = tracker()
    follow_ups.begin_turn("kitchen", "sat1", "a")
    follow_ups.finish_turn("kitchen", listen=True)
    assert follow_ups.begin_turn("phone", None, "b").open_mic is False  # the phone app's own conversation
    assert follow_ups.begin_turn("kitchen", "sat1", "c").open_mic is True


def test_talking_over_auri_counts_and_past_the_limit_gets_no_answer():
    follow_ups, clock = tracker(max_rounds=2)
    follow_ups.begin_turn("c1", "sat", "tell me a long story")
    follow_ups.finish_turn("c1", listen=False, reply="Once upon a time")
    seen = []
    for _ in range(3):
        follow_ups.note_interruption("sat")
        clock.now += 3
        heard = follow_ups.begin_turn("c1", "sat", "the TV talking")
        seen.append((heard.open_mic, heard.interrupted, heard.over_limit))
        follow_ups.finish_turn("c1", listen=False, reply="Sure.")  # a reply to an interruption keeps the count
    assert seen == [(True, True, False), (True, True, False), (True, True, True)]
    assert follow_ups.begin_turn("c1", "sat", "the wake word").over_limit is False  # starts over


def test_an_interruption_counts_only_for_its_satellite_and_only_soon_after():
    follow_ups, clock = tracker()
    follow_ups.note_interruption("sat1")
    assert follow_ups.begin_turn("c2", "sat2", "a").interrupted is False
    clock.now += INTERRUPTION_WINDOW_SECONDS + 1
    assert follow_ups.begin_turn("c1", "sat1", "b").interrupted is False


def test_auri_hearing_its_own_reply_is_an_echo_only_from_the_open_mic():
    reply = "First, take the steak out of the fridge thirty minutes before cooking, and pat it dry."
    follow_ups, _clock = tracker()
    follow_ups.begin_turn("c1", "sat", "a")
    follow_ups.finish_turn("c1", listen=True, reply=reply)
    assert follow_ups.begin_turn("c1", "sat", "the steak out of the fridge thirty minutes before").echo is True
    follow_ups.finish_turn("c1", listen=False, reply=reply)
    assert follow_ups.begin_turn("c1", "sat", "the steak out of the fridge thirty minutes before").echo is False


STEAK = "First, take the steak out of the fridge thirty minutes before cooking, and pat it dry."
STEAK_ZH = "首先，把牛排从冰箱里拿出来，放置三十分钟，然后擦干。"


@pytest.mark.parametrize(
    ("heard", "reply", "echo"),
    [
        ("First take the steak out of the fridge thirty minutes before cooking", STEAK, True),
        ("the steak out of the fridge thirty minutes", STEAK, True),
        ("把牛排从冰箱里拿出来放置三十分钟然后擦干", STEAK_ZH, True),
        # Repeating a few of Auri's words is an answer, not an echo.
        ("yes, the kitchen please", "Which light, the kitchen or the hall?", False),
        ("The kitchen one, and turn the hall off too", "Which light, the kitchen or the hall?", False),
        ("Can you just write it on a sticky note instead?", STEAK, False),
        ("能不能写在便签上", STEAK_ZH, False),
        ("", STEAK, False),
        ("the steak out of the fridge thirty minutes", "", False),
    ],
)
def test_echo(heard, reply, echo):
    assert is_echo(heard, reply) is echo


def test_the_wake_word_starts_over_even_right_after_a_reply_that_kept_listening():
    follow_ups, clock = tracker(max_rounds=2)
    follow_ups.begin_turn("c1", "sat", "walk me through it")
    follow_ups.finish_turn("c1", listen=True)  # the mic is open
    clock.now += 20
    follow_ups.note_wake("sat")  # but the user says the wake word
    clock.now += 3
    heard = follow_ups.begin_turn("c1", "sat", "what's the weather tomorrow")
    assert (heard.woken, heard.open_mic) == (True, False)  # a request they started: never ignored
    assert follow_ups.finish_turn("c1", listen=True) is True
    follow_ups.begin_turn("c1", "sat", "a")
    assert follow_ups.finish_turn("c1", listen=True) is True  # the count started over: two follow-ups
    follow_ups.begin_turn("c1", "sat", "b")
    assert follow_ups.finish_turn("c1", listen=True) is False


def test_the_wake_word_counts_only_for_its_satellite_and_only_soon_after():
    follow_ups, clock = tracker()
    follow_ups.begin_turn("c1", "sat1", "a")
    follow_ups.finish_turn("c1", listen=True)
    follow_ups.note_wake("sat2")
    assert follow_ups.begin_turn("c1", "sat1", "b").open_mic is True  # another satellite was woken
    follow_ups.finish_turn("c1", listen=True)
    follow_ups.note_wake("sat1")
    clock.now += WAKE_WINDOW_SECONDS + 1
    heard = follow_ups.begin_turn("c1", "sat1", "c")
    assert (heard.woken, heard.open_mic) == (False, True)  # too long ago: some other run's


def test_waking_auri_mid_reply_is_a_request_not_an_interruption():
    follow_ups, _clock = tracker()
    follow_ups.begin_turn("c1", "sat", "tell me a long story")
    follow_ups.finish_turn("c1", listen=False)
    follow_ups.note_interruption("sat")  # the talk-over watcher heard the wake word as speech too
    follow_ups.note_wake("sat")
    heard = follow_ups.begin_turn("c1", "sat", "write it on a sticky note")
    assert (heard.woken, heard.interrupted, heard.open_mic) == (True, False, False)
