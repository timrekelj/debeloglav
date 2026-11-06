from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from mistralai import agents

from debeloglav import blocked, catchup, context
from debeloglav.context import TIMEZONE, Request

ASKER = 42


def person(user_id, name, display_name=None, bot=False):
    return SimpleNamespace(
        id=user_id, name=name, display_name=display_name or name, bot=bot
    )


def attachment(filename, content_type=None, voice=False):
    return SimpleNamespace(
        filename=filename, content_type=content_type, is_voice_message=lambda: voice
    )


def message(
    id, author, content="", created_at=None, attachments=(), reply_to=None, poll=None
):
    return SimpleNamespace(
        id=id,
        author=author,
        clean_content=content,
        attachments=list(attachments),
        reference=SimpleNamespace(message_id=reply_to) if reply_to else None,
        created_at=created_at or datetime(2026, 11, 6, 12, 0, tzinfo=UTC),
        poll=poll,
    )


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


def poll_answer(text, count=0, emoji=None, id=1):
    return SimpleNamespace(id=id, text=text, vote_count=count, emoji=emoji)


class FakeChannel:
    def __init__(self, id, name, messages, view=True, history=True, category_id=None):
        self.id = id
        self.name = name
        self.category_id = category_id
        self.parent = None
        self.messages = messages  # newest first, like channel.history
        self.perms = SimpleNamespace(view_channel=view, read_message_history=history)

    def permissions_for(self, member):
        return self.perms

    def history(self, limit=None, before=None):
        async def stream():
            read = 0
            for m in self.messages:
                if before is not None and m.id >= before.id:
                    continue
                if limit is not None and read >= limit:
                    break
                read += 1
                yield m

        return stream()


class FakeGuild:
    def __init__(self, channels, members):
        self.id = 1
        self.channels = {c.id: c for c in channels}
        self.members = {m.id: m for m in members}
        self.text_channels = list(channels)
        self.threads = []
        self.forums = []
        self.voice_channels = []

    def get_member(self, user_id):
        return self.members.get(user_id)

    async def fetch_member(self, user_id):
        return self.members[user_id]

    def get_channel_or_thread(self, channel_id):
        return self.channels.get(channel_id)


class FakeBot:
    def __init__(self, guild):
        self.guild = guild

    def get_guild(self, guild_id):
        return self.guild if guild_id == self.guild.id else None

    def get_channel(self, channel_id):
        return self.guild.channels.get(channel_id)

    async def fetch_channel(self, channel_id):
        return self.guild.channels[channel_id]


def setup(messages, channel_id=10, name="general", **channel_kwargs):
    asker = person(ASKER, "asker", "Asker")
    channel = FakeChannel(channel_id, name, messages, **channel_kwargs)
    guild = FakeGuild([channel], [asker, person(7, "luka", "Luka")])
    return FakeBot(guild), channel


def request(channel_id=10, message_id=None, scope="guild:1"):
    return Request(
        scope=scope,
        channel_id=channel_id,
        user_id=ASKER,
        user_name="Asker (@asker)",
        message_id=message_id,
    )


async def test_stops_at_the_askers_last_message_and_excludes_it():
    asker = person(ASKER, "asker", "Asker")
    luka = person(7, "luka", "Luka")
    last = datetime(2026, 11, 5, 18, 30, tzinfo=UTC)
    bot, _ = setup(
        [
            message(100, luka, "newer than the asker"),
            message(99, asker, "the asker's last message", created_at=last),
            message(98, luka, "older"),
        ]
    )
    result = await catchup.catch_up(bot, request(message_id=101))
    assert result["count"] == 1
    assert "newer than the asker" in result["messages"]
    assert "the asker's last message" not in result["messages"]
    assert result["since"] == (
        f"the asker's last message at {last.astimezone(TIMEZONE):%Y-%m-%d %H:%M}"
    )


async def test_oldest_first_with_ids_and_links():
    luka = person(7, "luka", "Luka")
    bot, _ = setup(
        [
            message(103, luka, "third"),
            message(102, luka, "second"),
            message(101, luka, "first"),
            message(100, person(ASKER, "asker"), "mine"),
        ]
    )
    result = await catchup.catch_up(bot, request())
    lines = result["messages"].splitlines()
    assert result["count"] == 3
    assert [line.split(" ")[0] for line in lines] == ["m1", "m2", "m3"]
    assert "first" in lines[0] and "second" in lines[1] and "third" in lines[2]
    assert result["links"] == {
        "m1": "https://discord.com/channels/1/10/101",
        "m2": "https://discord.com/channels/1/10/102",
        "m3": "https://discord.com/channels/1/10/103",
    }
    assert result["channel"] == "#general"


async def test_caps_at_300_messages():
    luka = person(7, "luka", "Luka")
    messages = [message(1000 - i, luka, f"msg {1000 - i}") for i in range(400)]
    bot, _ = setup(messages)
    result = await catchup.catch_up(bot, request())
    assert result["count"] == 300
    assert result["since"] == (
        "the last 300 messages (you haven't written here recently)"
    )
    # The 300 newest were read: the oldest of them is id 701, the older 700 are not in.
    lines = result["messages"].splitlines()
    assert "msg 701" in lines[0] and "msg 1000" in lines[-1]
    assert "msg 700" not in result["messages"]


async def test_bot_and_empty_messages_are_skipped_but_count_as_read():
    luka = person(7, "luka", "Luka")
    botmsg = person(9, "otherbot", "OtherBot", bot=True)
    messages = [message(1000 - i, luka, f"msg {1000 - i}") for i in range(299)]
    messages.append(message(1, botmsg, "a bot message"))
    messages.append(message(0, luka, ""))  # empty: no content, no attachments
    bot, _ = setup(messages)
    result = await catchup.catch_up(bot, request())
    # 301 messages exist, but only 300 were read: the bot and empty ones are in them.
    assert result["count"] == 299
    assert "a bot message" not in result["messages"]
    assert result["since"] == (
        "the last 300 messages (you haven't written here recently)"
    )


async def test_reply_marker_points_to_a_message_in_the_window():
    luka = person(7, "luka", "Luka")
    bot, _ = setup(
        [
            message(105, luka, "a reply", reply_to=103),
            message(104, luka, "middle"),
            message(103, luka, "the one replied to"),
            message(102, person(ASKER, "asker"), "mine"),
        ]
    )
    result = await catchup.catch_up(bot, request())
    lines = result["messages"].splitlines()
    assert "(reply to m1)" in lines[2]  # m105 replies to m103, which is m1
    assert "(reply" not in lines[0] and "(reply" not in lines[1]


async def test_reply_outside_the_window_has_no_marker():
    luka = person(7, "luka", "Luka")
    bot, _ = setup(
        [
            message(105, luka, "a reply", reply_to=102),
            message(104, luka, "middle"),
            message(102, person(ASKER, "asker"), "mine, not in the window"),
        ]
    )
    result = await catchup.catch_up(bot, request())
    assert "(reply" not in result["messages"]


async def test_attachment_markers_and_content_cut():
    luka = person(7, "luka", "Luka")
    bot, _ = setup(
        [
            message(
                105,
                luka,
                "x" * 400,
                attachments=[
                    attachment("photo.png", "image/png"),
                    attachment("doc.pdf", "application/pdf"),
                    attachment("voice.ogg", "audio/ogg", voice=True),
                ],
            ),
            message(104, person(ASKER, "asker"), "mine"),
        ]
    )
    result = await catchup.catch_up(bot, request())
    line = result["messages"]
    assert "x" * 300 in line and "x" * 301 not in line
    assert "[image]" in line
    assert "[attachment: doc.pdf]" in line
    assert "[voice message]" in line


async def test_a_poll_message_is_kept_with_its_marker():
    luka = person(7, "luka", "Luka")
    p = poll(
        answers=[
            poll_answer("Pizzeria A", 5, id=1),
            poll_answer("Pizzeria B", 2, id=2),
        ],
        ended=True,
    )
    bot, _ = setup(
        [
            message(105, luka, poll=p),  # a poll has empty content
            message(104, person(ASKER, "asker"), "mine"),
        ]
    )
    result = await catchup.catch_up(bot, request())
    assert result["count"] == 1
    assert (
        '[poll: "Kam gremo na pico?" — Pizzeria A: 5 votes, Pizzeria B: 2 votes · ended]'
        in result["messages"]
    )


async def test_an_open_poll_shows_until_when_in_local_time():
    luka = person(7, "luka", "Luka")
    expiry = datetime(2026, 11, 7, 18, 0, tzinfo=UTC)
    p = poll(answers=[poll_answer("A", 1, id=1)], expires_at=expiry)
    bot, _ = setup([message(105, luka, poll=p)])
    result = await catchup.catch_up(bot, request(message_id=None))
    assert (
        f"open until {expiry.astimezone(TIMEZONE):%Y-%m-%d %H:%M}" in result["messages"]
    )


async def test_today_shows_only_the_time():
    luka = person(7, "luka", "Luka")
    now = datetime.now(TIMEZONE).replace(microsecond=0)
    bot, _ = setup(
        [
            message(105, luka, "today", created_at=now.astimezone(UTC)),
            message(
                104,
                luka,
                "some days ago",
                created_at=(now - timedelta(days=3)).astimezone(UTC),
            ),
            message(103, person(ASKER, "asker"), "mine"),
        ]
    )
    result = await catchup.catch_up(bot, request())
    lines = result["messages"].splitlines()
    assert f"[{now:%H:%M}]" in lines[1]
    assert f"[{(now - timedelta(days=3)):%Y-%m-%d %H:%M}]" in lines[0]


async def test_the_asking_message_is_the_before_boundary():
    luka = person(7, "luka", "Luka")
    bot, _ = setup(
        [
            message(160, luka, "newer than the question"),
            message(145, luka, "after the question"),
            message(130, person(ASKER, "asker"), "mine"),
        ]
    )
    result = await catchup.catch_up(bot, request(message_id=150))
    assert "after the question" in result["messages"]
    assert "newer than the question" not in result["messages"]


async def test_without_a_message_id_it_reads_to_now():
    luka = person(7, "luka", "Luka")
    bot, _ = setup([message(160, luka, "the newest"), message(150, luka, "older")])
    result = await catchup.catch_up(bot, request(message_id=None))
    assert "the newest" in result["messages"] and "older" in result["messages"]
    assert result["since"] == (
        "the last 300 messages (you haven't written here recently)"
    )


async def test_another_channel_is_read_from_now_even_with_a_message_id():
    luka = person(7, "luka", "Luka")
    other = FakeChannel(
        20,
        "other",
        [message(160, luka, "newest in other"), message(150, luka, "older")],
    )
    guild_channel = FakeChannel(10, "general", [message(100, luka, "in general")])
    guild = FakeGuild([guild_channel, other], [person(ASKER, "asker"), luka])
    bot = FakeBot(guild)
    result = await catchup.catch_up(bot, request(message_id=155), channel="other")
    assert result["channel"] == "#other"
    assert "newest in other" in result["messages"]  # before=None: id 160 > 155 is read
    assert "in general" not in result["messages"]


async def test_an_unknown_channel_name_raises():
    bot, _ = setup([message(100, person(7, "luka"), "hi")])
    with pytest.raises(agents.ToolError):
        await catchup.catch_up(bot, request(), channel="nope")


async def test_dms_raise():
    bot, _ = setup([])
    with pytest.raises(agents.ToolError):
        await catchup.catch_up(bot, request(scope="dm:42"))


async def test_a_blocked_channel_raises(monkeypatch):
    monkeypatch.setattr(blocked, "BLOCKED_CHANNELS", frozenset({10}))
    bot, _ = setup([message(100, person(7, "luka"), "hi")])
    with pytest.raises(agents.ToolError):
        await catchup.catch_up(bot, request())


async def test_a_channel_the_asker_cannot_read_raises():
    bot, _ = setup([message(100, person(7, "luka"), "hi")], view=False)
    with pytest.raises(agents.ToolError):
        await catchup.catch_up(bot, request())


async def test_a_channel_without_history_permission_raises():
    bot, _ = setup([message(100, person(7, "luka"), "hi")], history=False)
    with pytest.raises(agents.ToolError):
        await catchup.catch_up(bot, request())


def test_the_hook_is_unset_until_the_bot_sets_it():
    assert context.catch_up is None
