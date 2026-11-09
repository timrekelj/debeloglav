"""Answering in threads: when the bot opens one, where the answer goes, and who it answers there."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from debeloglav import bot, settings, store

ME = SimpleNamespace(id=999, display_name="Debeloglav", name="debeloglav")


@pytest.fixture(autouse=True)
def clean_settings():
    with store._connect() as db:
        db.execute("DELETE FROM settings")


def text_channel(create=True, send=True):
    channel = MagicMock(spec=discord.TextChannel)
    channel.name = "general"
    channel.guild = SimpleNamespace(me=ME)
    channel.permissions_for.return_value = SimpleNamespace(
        create_public_threads=create, send_messages_in_threads=send
    )
    return channel


def message(channel, content="@Debeloglav kaj je novega v AI?", thread=None):
    msg = MagicMock()
    msg.channel = channel
    msg.clean_content = content
    msg.thread = thread
    msg.jump_url = "https://discord.com/channels/1/2/3"
    msg.create_thread = AsyncMock(return_value=SimpleNamespace(id=3))
    return msg


@pytest.fixture
def client():
    return bot.Debeloglav()


async def test_a_tag_in_a_channel_opens_a_thread_on_the_message(client):
    msg = message(text_channel())
    thread = await client.open_thread(msg)
    assert thread.id == 3
    msg.create_thread.assert_awaited_once_with(
        name="kaj je novega v AI?", auto_archive_duration=24 * 60
    )


async def test_the_archive_setting_is_used(client):
    settings.update("thread_archive_hours", "168")
    msg = message(text_channel())
    await client.open_thread(msg)
    assert msg.create_thread.await_args.kwargs["auto_archive_duration"] == 168 * 60


async def test_no_thread_when_switched_off_or_not_allowed(client):
    settings.update("reply_in_threads", "off")
    msg = message(text_channel())
    assert await client.open_thread(msg) is None
    settings.update("reply_in_threads", None)

    msg = message(text_channel(create=False))
    assert await client.open_thread(msg) is None
    msg.create_thread.assert_not_awaited()


async def test_no_thread_inside_a_thread_or_a_dm(client):
    assert await client.open_thread(message(MagicMock(spec=discord.Thread))) is None
    assert await client.open_thread(message(MagicMock(spec=discord.DMChannel))) is None


async def test_a_message_that_has_a_thread_is_answered_in_it(client):
    existing = SimpleNamespace(id=77)
    msg = message(text_channel(), thread=existing)
    assert await client.open_thread(msg) is existing
    msg.create_thread.assert_not_awaited()


async def test_a_failed_thread_falls_back_to_inline(client):
    msg = message(text_channel())
    msg.create_thread.side_effect = discord.HTTPException(
        MagicMock(status=400, reason="Bad Request"), "nope"
    )
    assert await client.open_thread(msg) is None


def test_thread_name():
    msg = message(text_channel(), content="@Debeloglav,   kaj   je @debeloglav to?")
    assert bot.thread_name(msg, ME) == "kaj je to?"
    assert (
        bot.thread_name(message(text_channel(), content="@Debeloglav"), ME)
        == "Debeloglav"
    )
    long = bot.thread_name(message(text_channel(), content="a" * 300), ME)
    assert len(long) == bot.THREAD_NAME_CHARS and long.endswith("…")


def summoned(client, channel, mentions=()):
    msg = MagicMock()
    msg.guild = SimpleNamespace(id=1)
    msg.channel = channel
    msg.mentions = list(mentions)
    msg.reference = None
    msg.role_mentions = []
    return client.is_summoned(msg)


def test_every_message_in_the_bots_thread_is_for_it(client, monkeypatch):
    me = SimpleNamespace(id=999)
    monkeypatch.setattr(bot.Debeloglav, "user", me)
    own = MagicMock(spec=discord.Thread)
    own.owner_id = 999
    other = MagicMock(spec=discord.Thread)
    other.owner_id = 5
    assert summoned(client, own)
    assert not summoned(client, other)  # someone else's thread: only tags and replies
    assert summoned(client, other, mentions=[me])
    assert not summoned(client, text_channel())


class FakeSent:
    def __init__(self, n):
        self.n = n

    def to_reference(self, fail_if_not_exists=True):
        return f"ref-{self.n}"


class FakePlace:
    def __init__(self):
        self.sends = []

    async def send(self, content=None, *, files=None, reference=None):
        self.sends.append(reference)
        return FakeSent(len(self.sends))


async def test_the_answer_starts_the_thread_instead_of_replying():
    original = MagicMock()
    original.channel = FakePlace()
    thread = FakePlace()
    sent = await bot.reply_chunks(original, "x" * 2500, [], place=thread)
    assert len(sent) == 2
    assert thread.sends == [None, "ref-1"]  # the second part replies to the first
    assert original.channel.sends == []  # nothing in the channel itself


async def test_inline_replies_still_reply_to_the_message():
    original = MagicMock()
    original.to_reference.return_value = "ref-original"
    original.channel = FakePlace()
    await bot.reply_chunks(original, "Živjo", [])
    assert original.channel.sends == ["ref-original"]


async def test_the_threads_starter_message_is_fetched_from_its_channel():
    starter = SimpleNamespace(id=3)
    parent = MagicMock(spec=discord.TextChannel)
    parent.fetch_message = AsyncMock(return_value=starter)
    thread = MagicMock(spec=discord.Thread)
    thread.id = 3
    thread.starter_message = None
    thread.parent = parent
    msg = message(thread)
    assert await bot.thread_starter(msg) is starter
    parent.fetch_message.assert_awaited_once_with(3)
    assert await bot.thread_starter(message(text_channel())) is None
