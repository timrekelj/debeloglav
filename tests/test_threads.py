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


async def test_a_scheduled_run_that_fails_posts_nothing(client, monkeypatch):
    channel = MagicMock()
    channel.send = AsyncMock()
    monkeypatch.setattr(client, "channel", AsyncMock(return_value=channel))
    monkeypatch.setattr(
        client, "run_turn", AsyncMock(return_value=("ses_x", SimpleNamespace()))
    )
    monkeypatch.setattr(client, "end_session", AsyncMock())
    task = store.Task(
        id=1,
        scope="guild:1",
        channel_id=2,
        user_id=3,
        user_name="@tim",
        prompt="Preveri novice",
        cron="0 * * * *",
        next_run=None,
        watch=True,
        mention=True,
        last_report=None,
    )
    with pytest.raises(RuntimeError, match="did not complete"):
        await client.run_scheduled_task(task)
    channel.send.assert_not_awaited()
    client.end_session.assert_awaited_once_with("ses_x")


# --- untagged messages in the bot's own threads --------------------------------------------------


def own_thread(monkeypatch):
    me = SimpleNamespace(id=999)
    monkeypatch.setattr(bot.Debeloglav, "user", me)
    thread = MagicMock(spec=discord.Thread)
    thread.owner_id = 999
    thread.id = 50
    return me, thread


def thread_message(thread, mentions=(), reference=None, guild=True):
    msg = MagicMock()
    msg.guild = SimpleNamespace(id=1) if guild else None
    msg.channel = thread
    msg.mentions = list(mentions)
    msg.reference = reference
    msg.role_mentions = []
    return msg


def test_an_untagged_message_in_the_bots_thread_is_not_addressed(client, monkeypatch):
    me, thread = own_thread(monkeypatch)
    plain = thread_message(thread)
    assert client.is_summoned(plain)
    assert not client.is_addressed(plain)
    assert client.is_addressed(thread_message(thread, mentions=[me]))
    assert client.is_addressed(thread_message(thread, guild=False))
    reply = MagicMock(spec=discord.MessageReference)
    reply.resolved = MagicMock(spec=discord.Message)
    reply.resolved.author = me
    assert client.is_addressed(thread_message(thread, reference=reply))


async def build(addressed):
    author = SimpleNamespace(id=2, name="luka", display_name="Luka")
    msg = SimpleNamespace(
        id=10,
        author=author,
        clean_content="a ti?",
        attachments=[],
        reference=None,
        mentions=[],
        embeds=[],
        poll=None,
        channel=SimpleNamespace(id=1),
    )
    request = bot.Request(scope="dm:2", channel_id=1, user_id=2, user_name="Luka")
    content = await bot.Debeloglav.build_content(
        SimpleNamespace(user=None),
        msg,
        request,
        continuing=True,
        addressed=addressed,
    )
    return content[0]["text"]


async def test_build_content_marks_an_untagged_message():
    tagged = await build(True)
    assert "Message that tagged you:" in tagged
    assert bot.NO_REPLY not in tagged
    untagged = await build(False)
    assert "Message that tagged you:" not in untagged
    assert "Nobody tagged you" in untagged
    assert f"answer exactly {bot.NO_REPLY}" in untagged
    assert "a ti?" in untagged


def answer_turn(client, monkeypatch, text, mentions=()):
    """on_message for a message in the bot's own thread, with the turn answering `text`."""
    me, thread = own_thread(monkeypatch)
    msg = thread_message(thread, mentions=[me] if mentions else [])
    msg.id = 77
    msg.author = SimpleNamespace(id=2, bot=False)
    msg.clean_content = "hej"
    msg.jump_url = "https://discord.com/channels/1/2/77"
    msg.attachments = []
    client.conversations = MagicMock()
    client.conversations.get.return_value = "ses_1"
    client.build_content = AsyncMock(return_value=[{"type": "text", "text": "x"}])
    client.run_turn = AsyncMock(return_value=("ses_1", SimpleNamespace()))
    sent = [SimpleNamespace(id=78)]
    calls = SimpleNamespace(
        reply=AsyncMock(return_value=sent),
        unreact=AsyncMock(),
        activity=MagicMock(),
    )
    monkeypatch.setattr(bot.blocked, "channel", lambda channel: False)
    monkeypatch.setattr(bot, "scope_of", lambda m: "guild:1")
    monkeypatch.setattr(bot, "thinking_emoji", lambda guild: "⏳")
    monkeypatch.setattr(bot, "react", AsyncMock())
    monkeypatch.setattr(bot, "unreact", calls.unreact)
    monkeypatch.setattr(bot, "reply_chunks", calls.reply)
    monkeypatch.setattr(bot, "record_activity", calls.activity)
    monkeypatch.setattr(bot.mentions, "who", lambda user: "Luka (@luka)")
    monkeypatch.setattr(bot.mentions, "untag", AsyncMock(side_effect=lambda g, t: t))
    monkeypatch.setattr(
        bot, "parse", lambda state, session_id: bot.Answer(text, bot.context.Found())
    )
    monkeypatch.setattr(client, "open_thread", AsyncMock(return_value=None))
    return msg, calls


async def test_an_untagged_message_not_for_the_bot_gets_no_reply(client, monkeypatch):
    msg, calls = answer_turn(client, monkeypatch, bot.NO_REPLY)
    await client.on_message(msg)
    calls.reply.assert_not_awaited()
    calls.unreact.assert_awaited_once()
    client.conversations.link.assert_called_once_with("ses_1", 77)
    assert calls.activity.call_args.kwargs["reply"] == bot.NO_REPLY
    assert calls.activity.call_args.kwargs["ok"] is True
    assert client.build_content.await_args.kwargs["addressed"] is False


async def test_an_untagged_message_for_the_bot_is_answered(client, monkeypatch):
    msg, calls = answer_turn(client, monkeypatch, "Seveda!")
    await client.on_message(msg)
    calls.reply.assert_awaited_once()
    client.conversations.link.assert_called_once_with("ses_1", 77, 78)


async def test_a_tagged_message_is_answered_even_if_the_model_says_no_reply(
    client, monkeypatch
):
    msg, calls = answer_turn(client, monkeypatch, bot.NO_REPLY, mentions=True)
    await client.on_message(msg)
    calls.reply.assert_awaited_once()
    assert client.build_content.await_args.kwargs["addressed"] is True
