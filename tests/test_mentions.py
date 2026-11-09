from types import SimpleNamespace

import discord

from debeloglav import mentions


class FakeGuild:
    def __init__(self, members=None, roles=None, fetchable=None):
        self.members = members or {}
        self.roles = roles or {}
        self.fetchable = fetchable or {}

    def get_member(self, user_id):
        return self.members.get(user_id)

    async def fetch_member(self, user_id):
        if user_id in self.fetchable:
            return self.fetchable[user_id]
        raise discord.NotFound(
            SimpleNamespace(status=404, reason="Not Found"), "unknown member"
        )

    def get_role(self, role_id):
        return self.roles.get(role_id)


def person(name, display_name):
    return SimpleNamespace(name=name, display_name=display_name)


def test_who_shows_nickname_and_username():
    assert mentions.who(person("ambros", "Ambrož")) == "Ambrož (@ambros)"


def test_who_without_a_nickname():
    assert mentions.who(person("tim", "Tim")) == "@tim"


async def test_user_tags_become_nicknames():
    guild = FakeGuild(members={1: person("ambros", "Ambrož")})
    assert await mentions.untag(guild, "Hej <@1> in <@!1>") == "Hej Ambrož in Ambrož"


async def test_members_not_in_the_cache_are_fetched():
    guild = FakeGuild(fetchable={2: person("luka", "Luka")})
    assert await mentions.untag(guild, "<@2>") == "Luka"


async def test_unknown_people_are_named_nekdo():
    assert await mentions.untag(FakeGuild(), "<@3> je rekel") == "nekdo je rekel"


async def test_role_tags_become_names():
    guild = FakeGuild(roles={5: SimpleNamespace(name="admini")})
    assert await mentions.untag(guild, "<@&5> in <@&6>") == "admini in vloga"


async def test_everyone_and_here_never_ping():
    text = await mentions.untag(FakeGuild(), "@everyone in @here!")
    assert "@everyone" not in text and "@here" not in text
    assert text.replace("\u200b", "") == "@everyone in @here!"


async def test_email_addresses_are_left_alone():
    assert (
        await mentions.untag(FakeGuild(), "pisi na tim@here.si")
        == "pisi na tim@here.si"
    )


async def test_dms_have_no_guild():
    assert await mentions.untag(None, "<@1> <@&2>") == "nekdo vloga"


class FakeEmoji:
    def __init__(self, name, id, animated=False, available=True):
        self.name, self.id, self.animated, self.available = (
            name,
            id,
            animated,
            available,
        )

    def __str__(self):
        return f"<{'a' if self.animated else ''}:{self.name}:{self.id}>"


def emoji_guild():
    guild = FakeGuild()
    guild.emojis = [
        FakeEmoji("kekw", 11),
        FakeEmoji("thistbh", 12),
        FakeEmoji("dance", 13, animated=True),
        FakeEmoji("gone", 14, available=False),
    ]
    return guild


async def test_server_emojis_get_their_ids():
    guild = emoji_guild()
    text = await mentions.untag(
        guild, "fak ja <:kekw:> <:thistbh:> :dance: <:kekw:999>"
    )
    assert text == "fak ja <:kekw:11> <:thistbh:12> <a:dance:13> <:kekw:11>"


async def test_unknown_emojis_and_lookalikes_stay_text():
    guild = emoji_guild()
    text = await mentions.untag(guild, "<:nope:> :nope: :gone: <:other:42> ob 12:30:45")
    assert text == ":nope: :nope: :gone: <:other:42> ob 12:30:45"
    assert await mentions.untag(None, "<:kekw:> :kekw:") == ":kekw: :kekw:"


def test_the_model_is_told_the_usable_emojis():
    prompt = mentions.emoji_prompt(emoji_guild())
    assert ":dance:, :kekw:, :thistbh:" in prompt and "gone" not in prompt
    assert mentions.emoji_prompt(None) == ""
    assert mentions.emoji_prompt(FakeGuild()) == ""
