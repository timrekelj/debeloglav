from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import discord

from debeloglav import polls
from debeloglav.context import TIMEZONE


def answer(text, count=0, emoji=None, id=1):
    return SimpleNamespace(id=id, text=text, vote_count=count, emoji=emoji)


def poll(
    question="Kam gremo na pico?",
    answers=(),
    ended=False,
    expires_at=None,
    multiple=False,
):
    return SimpleNamespace(
        question=question,
        answers=list(answers),
        is_finalized=lambda: ended,
        expires_at=expires_at,
        multiple=multiple,
    )


def raw_answer(id, text, emoji=None):
    media = {"text": text}
    if emoji:
        media["emoji"] = emoji
    return {"answer_id": id, "poll_media": media}


def test_describe_an_ended_poll():
    p = poll(
        answers=[
            answer("Pizzeria A", 5, id=1),
            answer("Pizzeria B", 2, id=2),
            answer("Pizzeria C", 0, id=3),
        ],
        ended=True,
    )
    assert (
        polls.describe(p)
        == '[poll: "Kam gremo na pico?" — Pizzeria A: 5 votes, Pizzeria B: 2 votes, Pizzeria C: 0 votes · ended]'
    )


def test_describe_an_open_poll_with_expiry_in_local_time():
    expiry = datetime(2026, 11, 7, 18, 0, tzinfo=UTC)
    p = poll(answers=[answer("A", 1, id=1)], expires_at=expiry)
    assert (
        polls.describe(p)
        == f'[poll: "Kam gremo na pico?" — A: 1 votes · open until {expiry.astimezone(TIMEZONE):%Y-%m-%d %H:%M}]'
    )


def test_describe_an_open_poll_without_expiry():
    p = poll(answers=[answer("A", id=1)])
    assert polls.describe(p) == '[poll: "Kam gremo na pico?" — A: 0 votes · open]'


def test_describe_a_multiple_choice_poll():
    p = poll(answers=[answer("A", 3, id=1)], ended=True, multiple=True)
    assert polls.describe(p).endswith("· ended · multiple choice]")


def test_describe_includes_answer_emoji():
    p = poll(answers=[answer("Pica", 4, emoji="🍕", id=1)], ended=True)
    assert (
        polls.describe(p) == '[poll: "Kam gremo na pico?" — 🍕 Pica: 4 votes · ended]'
    )


def test_describe_works_with_a_real_discord_poll():
    p = discord.Poll(question="Kam gremo na pico?", duration=timedelta(hours=24))
    p.add_answer(text="Pizzeria A", emoji="🍕")
    p.add_answer(text="Pizzeria B")
    assert (
        polls.describe(p)
        == '[poll: "Kam gremo na pico?" — 🍕 Pizzeria A: 0 votes, Pizzeria B: 0 votes · open]'
    )


def test_describe_raw_an_ended_poll():
    data = {
        "question": {"text": "Kam gremo na pico?"},
        "answers": [raw_answer(1, "Pizzeria A"), raw_answer(2, "Pizzeria B")],
        "expiry": "2026-11-07T18:00:00+00:00",
        "allow_multiselect": False,
        "results": {
            "is_finalized": True,
            "answer_counts": [
                {"id": 1, "count": 5, "me_voted": True},
                {"id": 2, "count": 2, "me_voted": False},
            ],
        },
    }
    assert (
        polls.describe_raw(data)
        == '[poll: "Kam gremo na pico?" — Pizzeria A: 5 votes, Pizzeria B: 2 votes · ended]'
    )


def test_describe_raw_without_results_counts_are_zero_and_open_until_expiry():
    data = {
        "question": {"text": "Kam gremo na pico?"},
        "answers": [raw_answer(1, "A"), raw_answer(2, "B")],
        "expiry": "2026-11-07T18:00:00+01:00",
        "allow_multiselect": True,
    }
    assert polls.describe_raw(data) == (
        '[poll: "Kam gremo na pico?" — A: 0 votes, B: 0 votes · open until 2026-11-07 18:00 · multiple choice]'
    )


def test_describe_raw_emoji_unicode_and_server_emoji():
    data = {
        "question": {"text": "Kaj?"},
        "answers": [
            raw_answer(1, "Pica", {"name": "🍕"}),
            raw_answer(2, "Burek", {"name": "🥟", "id": 123, "animated": True}),
        ],
        "expiry": None,
        "allow_multiselect": False,
        "results": {
            "is_finalized": False,
            "answer_counts": [{"id": 1, "count": 1, "me_voted": False}],
        },
    }
    assert (
        polls.describe_raw(data)
        == '[poll: "Kaj?" — 🍕 Pica: 1 votes, <a:🥟:123> Burek: 0 votes · open]'
    )


def test_describe_and_describe_raw_produce_the_same_text():
    p = poll(
        question="Kam gremo na pico?",
        answers=[
            answer("Pizzeria A", 5, emoji="🍕", id=1),
            answer("Pizzeria B", 2, id=2),
        ],
        ended=True,
        expires_at=datetime(2026, 11, 7, 18, 0, tzinfo=UTC),
        multiple=True,
    )
    data = {
        "question": {"text": "Kam gremo na pico?"},
        "answers": [
            raw_answer(1, "Pizzeria A", {"name": "🍕"}),
            raw_answer(2, "Pizzeria B"),
        ],
        "expiry": "2026-11-07T18:00:00+00:00",
        "allow_multiselect": True,
        "results": {
            "is_finalized": True,
            "answer_counts": [
                {"id": 1, "count": 5, "me_voted": False},
                {"id": 2, "count": 2, "me_voted": False},
            ],
        },
    }
    assert polls.describe(p) == polls.describe_raw(data)
