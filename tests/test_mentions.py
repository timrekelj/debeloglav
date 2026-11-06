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
    assert text.replace("​", "") == "@everyone in @here!"


async def test_email_addresses_are_left_alone():
    assert (
        await mentions.untag(FakeGuild(), "pisi na tim@here.si")
        == "pisi na tim@here.si"
    )


async def test_dms_have_no_guild():
    assert await mentions.untag(None, "<@1> <@&2>") == "nekdo vloga"
