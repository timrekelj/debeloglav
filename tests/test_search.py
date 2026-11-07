"""Tests for debeloglav.discord_search.search: which matches it shows, and which it hides
as private (content from channels not everyone in the server can read)."""

from types import SimpleNamespace

from debeloglav import discord_search
from debeloglav.context import Request

ASKER = 42
GUILD_ID = 1


def person(user_id, name, display_name=None):
    return SimpleNamespace(id=user_id, name=name, display_name=display_name or name)


class FakeRole:
    """The @everyone role: what everyone in the server can read."""

    def __init__(self, id=0, name="@everyone"):
        self.id = id
        self.name = name


class FakeChannel:
    def __init__(
        self, id, name, view=True, history=True, public=True, category_id=None
    ):
        self.id = id
        self.name = name
        self.category_id = category_id
        self.parent = None
        self.parent_id = None
        self.perms = SimpleNamespace(view_channel=view, read_message_history=history)
        # what the @everyone role can read: a private channel is not public
        self.role_perms = SimpleNamespace(
            view_channel=public, read_message_history=public
        )

    def permissions_for(self, who):
        return self.role_perms if isinstance(who, FakeRole) else self.perms


class FakeThread(FakeChannel):
    def __init__(self, id, name, parent_id, private=False, **kwargs):
        super().__init__(id, name, **kwargs)
        self.parent_id = parent_id
        self._private = private

    def is_private(self):
        return self._private


class FakeGuild:
    def __init__(self, channels, members, default_role=None):
        self.id = GUILD_ID
        self.channels = {c.id: c for c in channels}
        self.members = {m.id: m for m in members}
        self.text_channels = [c for c in channels if not isinstance(c, FakeThread)]
        self.threads = [c for c in channels if isinstance(c, FakeThread)]
        self.forums = []
        self.voice_channels = []
        self.default_role = default_role or FakeRole()

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
    def __init__(self, groups):
        self.groups = groups
        self.calls = []

    async def request(self, route, params=None):
        self.calls.append((route, params))
        return {"messages": self.groups}


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


def request(channel_id=10, private=False):
    return Request(
        scope=f"guild:{GUILD_ID}",
        channel_id=channel_id,
        user_id=ASKER,
        user_name="Asker (@asker)",
        private=private,
    )


async def test_a_private_channel_is_hidden_when_asked_in_a_public_channel():
    # the asker (a mod) can read #mods, but not everyone; the question is in #general
    general = FakeChannel(10, "general")
    mods = FakeChannel(20, "mods", public=False)
    bot = setup([general, mods], [[hit(20, 100, "secret mod talk")]])
    result = await discord_search.search(bot, request(), content="secret")
    assert result["messages"] == []
    assert result["hidden_private"] == 1


async def test_the_channel_the_question_is_asked_in_is_shown_even_if_private():
    general = FakeChannel(10, "general", public=False)
    bot = setup([general], [[hit(10, 100, "here")]])
    result = await discord_search.search(bot, request())
    assert len(result["messages"]) == 1
    assert result["messages"][0]["channel"] == "#general"
    assert result["messages"][0]["content"] == "here"
    assert result["hidden_private"] == 0


async def test_a_public_channel_is_shown_from_another_channel():
    general = FakeChannel(10, "general")
    other = FakeChannel(20, "other", public=True)
    bot = setup([general, other], [[hit(20, 100, "public talk")]])
    result = await discord_search.search(bot, request())
    assert [m["content"] for m in result["messages"]] == ["public talk"]
    assert result["hidden_private"] == 0


async def test_a_private_request_shows_a_private_channel():
    # /zamujeno answers ephemerally, so only the asker sees the answer
    general = FakeChannel(10, "general")
    mods = FakeChannel(20, "mods", public=False)
    bot = setup([general, mods], [[hit(20, 100, "secret mod talk")]])
    result = await discord_search.search(bot, request(private=True))
    assert [m["content"] for m in result["messages"]] == ["secret mod talk"]
    assert result["hidden_private"] == 0


async def test_a_public_thread_of_the_asking_channel_is_shown():
    general = FakeChannel(10, "general", public=False)
    thread = FakeThread(30, "nit", 10, private=False, public=False)
    bot = setup([general, thread], [[hit(30, 100, "in the thread")]])
    result = await discord_search.search(bot, request())
    assert [m["channel"] for m in result["messages"]] == ["#nit"]
    assert result["hidden_private"] == 0


async def test_a_private_thread_of_the_asking_channel_is_hidden():
    general = FakeChannel(10, "general")
    thread = FakeThread(30, "skrivna-nit", 10, private=True, public=False)
    bot = setup([general, thread], [[hit(30, 100, "v nitki")]])
    result = await discord_search.search(bot, request())
    assert result["messages"] == []
    assert result["hidden_private"] == 1


async def test_only_the_private_matches_are_hidden_and_counted():
    general = FakeChannel(10, "general")
    mods = FakeChannel(20, "mods", public=False)
    bot = setup(
        [general, mods],
        [[hit(20, 101, "secret mod talk")], [hit(10, 100, "public talk")]],
    )
    result = await discord_search.search(bot, request())
    assert [m["content"] for m in result["messages"]] == ["public talk"]
    assert result["hidden_private"] == 1


async def test_a_channel_the_asker_cannot_read_is_skipped_without_counting():
    general = FakeChannel(10, "general")
    mods = FakeChannel(20, "mods", view=False, public=False)
    bot = setup([general, mods], [[hit(20, 100, "nope")]])
    result = await discord_search.search(bot, request())
    assert result["messages"] == []
    assert (
        result["hidden_private"] == 0
    )  # not shareable, but not a private-channel leak either
