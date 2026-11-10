"""Tests for debeloglav.discord_search.search: which matches it shows, and which it leaves out
(channels the asker can't read, and blocked channels)."""

from types import SimpleNamespace

from debeloglav import blocked, discord_search
from debeloglav.context import Request

ASKER = 42
GUILD_ID = 1


def person(user_id, name, display_name=None):
    return SimpleNamespace(id=user_id, name=name, display_name=display_name or name)


class FakeChannel:
    def __init__(self, id, name, view=True, history=True, category_id=None):
        self.id = id
        self.name = name
        self.category_id = category_id
        self.parent = None
        self.parent_id = None
        self.perms = SimpleNamespace(view_channel=view, read_message_history=history)

    def permissions_for(self, who):
        return self.perms


class FakeThread(FakeChannel):
    def __init__(self, id, name, parent_id, **kwargs):
        super().__init__(id, name, **kwargs)
        self.parent_id = parent_id


class FakeGuild:
    def __init__(self, channels, members):
        self.id = GUILD_ID
        self.channels = {c.id: c for c in channels}
        self.members = {m.id: m for m in members}
        self.text_channels = [c for c in channels if not isinstance(c, FakeThread)]
        self.threads = [c for c in channels if isinstance(c, FakeThread)]
        self.forums = []
        self.voice_channels = []

    def get_member(self, user_id):
        return self.members.get(user_id)

    async def fetch_member(self, user_id):
        return self.members[user_id]

    def get_channel_or_thread(self, channel_id):
        return self.channels.get(channel_id)

    def get_role(self, role_id):
        return None


def hit(channel_id, message_id, content, author_id=7, username="luka"):
    """One search hit, in the shape of Discord's messages/search response."""
    return {
        "channel_id": str(channel_id),
        "id": str(message_id),
        "timestamp": "2026-11-06T12:00:00+00:00",
        "author": {
            "id": str(author_id),
            "username": username,
            "global_name": username.capitalize(),
        },
        "content": content,
        "attachments": [],
    }


class FakeHTTP:
    """Discord's search: 25 hits per request, from the offset asked for."""

    def __init__(self, groups):
        self.groups = groups
        self.calls = []

    async def request(self, route, params=None):
        self.calls.append((route, params))
        offset = int(dict(params).get("offset", 0))
        return {
            "messages": self.groups[offset : offset + 25],
            "total_results": len(self.groups),
        }


class FakeBot:
    def __init__(self, guild, groups):
        self.guild = guild
        self.http = FakeHTTP(groups)

    def get_guild(self, guild_id):
        return self.guild if guild_id == self.guild.id else None


def setup(channels, groups):
    asker = person(ASKER, "asker", "Asker")
    guild = FakeGuild(channels, [asker, person(7, "luka", "Luka")])
    return FakeBot(guild, groups)


def request(channel_id=10):
    return Request(
        scope=f"guild:{GUILD_ID}",
        channel_id=channel_id,
        user_id=ASKER,
        user_name="Asker (@asker)",
    )


async def test_a_channel_not_everyone_can_read_is_shown_from_another_channel():
    # only some roles can read #mods; the asker is one of them, and asks in #general
    general = FakeChannel(10, "general")
    mods = FakeChannel(20, "mods")
    bot = setup([general, mods], [[hit(20, 100, "mod talk")]])
    result = await discord_search.search(bot, request(), content="mod")
    assert [m["channel"] for m in result["messages"]] == ["#mods"]
    assert [m["content"] for m in result["messages"]] == ["mod talk"]
    assert "hidden_private" not in result


async def test_a_thread_is_shown():
    general = FakeChannel(10, "general")
    thread = FakeThread(30, "nit", 10)
    bot = setup([general, thread], [[hit(30, 100, "in the thread")]])
    result = await discord_search.search(bot, request())
    assert [m["channel"] for m in result["messages"]] == ["#nit"]


async def test_a_channel_the_asker_cannot_read_is_skipped():
    general = FakeChannel(10, "general")
    mods = FakeChannel(20, "mods", view=False)
    bot = setup(
        [general, mods],
        [[hit(20, 101, "nope")], [hit(10, 100, "public talk")]],
    )
    result = await discord_search.search(bot, request())
    assert [m["content"] for m in result["messages"]] == ["public talk"]


async def test_a_blocked_channel_is_skipped(monkeypatch):
    monkeypatch.setattr(blocked, "_db_blocked", {20})
    monkeypatch.setattr(blocked, "_db_loaded", True)
    general = FakeChannel(10, "general")
    other = FakeChannel(20, "other")
    bot = setup(
        [general, other],
        [[hit(20, 101, "blocked talk")], [hit(10, 100, "public talk")]],
    )
    result = await discord_search.search(bot, request())
    assert [m["content"] for m in result["messages"]] == ["public talk"]


async def test_the_total_counts_every_match_and_one_page_is_returned_by_default():
    general = FakeChannel(10, "general")
    bot = setup([general], [[hit(10, i, f"m{i}")] for i in range(60)])
    result = await discord_search.search(bot, request())
    assert len(result["messages"]) == 25
    assert result["total"] == 60
    assert result["more_may_exist"] is True
    assert len(bot.http.calls) == 1


async def test_a_higher_limit_reads_more_pages():
    general = FakeChannel(10, "general")
    bot = setup([general], [[hit(10, i, f"m{i}")] for i in range(60)])
    result = await discord_search.search(bot, request(), limit=55)
    assert [m["content"] for m in result["messages"]] == [f"m{i}" for i in range(55)]
    assert result["total"] == 60
    assert result["more_may_exist"] is False  # every match was read from Discord
    assert [dict(params)["offset"] for _, params in bot.http.calls] == ["0", "25", "50"]


async def test_the_limit_is_capped():
    general = FakeChannel(10, "general")
    bot = setup([general], [[hit(10, i, f"m{i}")] for i in range(300)])
    result = await discord_search.search(bot, request(), limit=5000)
    assert len(result["messages"]) == discord_search.MAX_RESULTS
    assert result["total"] == 300
    assert result["more_may_exist"] is True
