"""Style learning records every run in store.style_runs, even the skipped and the failed ones."""

import asyncio
from contextlib import suppress

import pytest

from debeloglav import store, style


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
    async def collect(guild, channels, since):
        return "Tim: zdravo\nLuka: oj", 42

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
    async def collect(guild, channels, since):
        return "Malo sporočil.", style.MIN_MESSAGES - 1

    monkeypatch.setattr(style, "collect", collect)
    await style.learn(FakeGuild(2, "Testni"), "guild:learn2", store.Style())
    (run,) = store.style_runs("guild:learn2")
    assert run.messages == style.MIN_MESSAGES - 1
    assert run.guide is None  # skipped: too few messages
    assert run.error is None
    assert store.style("guild:learn2").learned_at is not None


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
