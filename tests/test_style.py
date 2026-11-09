"""Style learning records every run in store.style_runs, even the skipped and the failed ones."""

import asyncio
from contextlib import suppress

import discord
import pytest

from debeloglav import blocked, store, style


class FakeGuild:
    def __init__(self, id: int, name: str) -> None:
        self.id = id
        self.name = name


class FakeBot:
    def __init__(self, guilds: list[FakeGuild]) -> None:
        self.guilds = guilds

    async def wait_until_ready(self) -> None:
        return None


@pytest.fixture(autouse=True)
def clean_style_tables():
    """Each test starts with empty style tables: conftest points all tests at one temp database."""
    with store._connect() as db:
        db.execute("DELETE FROM styles")
        db.execute("DELETE FROM style_runs")


async def test_learn_records_the_summarized_guide(monkeypatch):
    async def collect(guild, since):
        return "Tim: zdravo\nLuka: oj", 42, []

    async def summarize(previous, transcript):
        return "## Ton\nLjudje so sproščeni."

    monkeypatch.setattr(style, "collect", collect)
    monkeypatch.setattr(style, "summarize", summarize)
    await style.learn(FakeGuild(1, "Testni"), "guild:learn1", store.Style())
    (run,) = store.style_runs("guild:learn1")
    assert run.messages == 42
    assert run.guide == "## Ton\nLjudje so sproščeni."
    assert run.error is None
    assert store.style("guild:learn1").guide == "## Ton\nLjudje so sproščeni."


async def test_learn_records_a_skipped_run(monkeypatch):
    async def collect(guild, since):
        return "Malo sporočil.", style.min_messages() - 1, ["#tajno: blocked"]

    monkeypatch.setattr(style, "collect", collect)
    assert not await style.learn(FakeGuild(2, "Testni"), "guild:learn2", store.Style())
    (run,) = store.style_runs("guild:learn2")
    assert run.messages == style.min_messages() - 1
    assert run.guide is None  # skipped: too few messages
    assert run.error is None
    assert run.skipped == ["#tajno: blocked"]
    # a skip does not move learned_at, so the next run still reads the whole window
    assert store.style("guild:learn2").learned_at is None


class FakeChannel:
    def __init__(self, id, name, messages=(), view=True, error=None):
        self.id = id
        self.name = name
        self.category_id = None
        self.messages = list(messages)
        self.view = view
        self.error = error

    def permissions_for(self, who):
        return discord.Permissions(view_channel=self.view, read_message_history=self.view)

    async def history(self, **kwargs):
        if self.error:
            raise self.error
        for message in self.messages:
            yield message


def message(name, text, bot=False):
    author = type("Author", (), {"bot": bot, "name": name, "display_name": name})()
    return type("Message", (), {"author": author, "clean_content": text})()


async def test_collect_reads_every_channel_but_blocked_and_unreadable(monkeypatch):
    monkeypatch.setattr(blocked, "_db_blocked", {12})
    monkeypatch.setattr(blocked, "_db_loaded", True)
    response = type("Response", (), {"status": 403, "reason": "Forbidden"})()
    guild = FakeGuild(5, "Testni")
    guild.me = object()
    guild.text_channels = [
        FakeChannel(10, "general", [message("tim", "zdravo"), message("bot", "beep", bot=True)]),
        FakeChannel(11, "private", [message("luka", "oj")]),  # not public: still read
        FakeChannel(12, "tajno", [message("ana", "skrivnost")]),
        FakeChannel(13, "skrit", [message("ana", "nevidno")], view=False),
        FakeChannel(14, "pokvarjen", error=discord.Forbidden(response, "Missing Access")),
    ]
    transcript, count, skipped = await style.collect(guild, since=None)
    assert count == 2
    assert "#general" in transcript and "#private" in transcript
    assert "skrivnost" not in transcript and "nevidno" not in transcript
    assert skipped == [
        "#tajno: blocked",
        "#skrit: the bot can't read its history",
        "#pokvarjen: 403 Missing Access",
    ]


async def test_run_forever_records_a_failed_run(monkeypatch):
    async def failing_learn(guild, scope, current):
        raise RuntimeError("boom")

    monkeypatch.setattr(style, "learn", failing_learn)
    task = asyncio.create_task(style.run_forever(FakeBot([FakeGuild(4242, "Testni")])))
    try:
        for _ in range(200):
            if store.style_runs():
                break
            await asyncio.sleep(0.01)
        (run,) = store.style_runs("guild:4242")
        assert run.messages == 0
        assert run.guide is None
        assert run.error == "RuntimeError: boom"
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
