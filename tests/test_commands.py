"""Tests for the /debeloglav admin command group: the tree, the permission check and a few handlers."""

from types import SimpleNamespace

import discord
import pytest

from debeloglav import blocked, commands, store


@pytest.fixture(autouse=True)
def reset_blocked_cache(monkeypatch):
    # start each test with an empty database cache, as if freshly loaded
    monkeypatch.setattr(blocked, "_db_blocked", set())
    monkeypatch.setattr(blocked, "_db_loaded", True)


def make_tree() -> discord.app_commands.CommandTree:
    client = discord.Client(intents=discord.Intents.default())  # never connected
    tree = discord.app_commands.CommandTree(client)
    commands.setup(tree)
    return tree


def test_the_group_and_its_subcommands_load():
    tree = make_tree()
    assert sorted(c.qualified_name for c in tree.walk_commands()) == [
        "debeloglav",
        "debeloglav blokiraj",
        "debeloglav naloge",
        "debeloglav nastavitve",
        "debeloglav odblokiraj",
        "debeloglav pozabi",
        "debeloglav preklici",
        "debeloglav spomini",
        "debeloglav ucenje",
    ]
    group = tree.get_command("debeloglav")
    assert isinstance(group, discord.app_commands.Group)
    assert group.guild_only
    assert group.default_permissions == discord.Permissions(manage_guild=True)
    # the channel parameter accepts channels, categories, forums and threads
    kanal = commands.blokiraj.parameters[0]
    assert discord.ChannelType.category in kanal.channel_types
    assert discord.ChannelType.forum in kanal.channel_types
    assert discord.ChannelType.public_thread in kanal.channel_types


class FakeResponse:
    def __init__(self):
        self.messages: list[tuple[str, bool]] = []

    async def send_message(self, content, *, ephemeral=False):
        self.messages.append((content, ephemeral))


def fake_interaction(*, admin=True, guild_id):
    permissions = (
        discord.Permissions(manage_guild=True) if admin else discord.Permissions.none()
    )
    return SimpleNamespace(
        user=SimpleNamespace(guild_permissions=permissions),
        guild_id=guild_id,
        response=FakeResponse(),
    )


async def test_a_non_admin_is_refused():
    interaction = fake_interaction(admin=False, guild_id=4242)
    await commands.nastavitve.callback(interaction)
    assert interaction.response.messages == [(commands.ADMIN_ONLY, True)]


async def test_nastavitve_summarizes_the_server():
    guild = 4243
    scope = f"guild:{guild}"
    store.add_memory(scope, "Tim je admin", "Tim (@tim)", 1)
    blocked.block(guild, 900)
    interaction = fake_interaction(guild_id=guild)
    await commands.nastavitve.callback(interaction)
    ((text, ephemeral),) = interaction.response.messages
    assert ephemeral
    assert "<#900>" in text
    assert "**Spomini:** 1" in text
    assert "**Aktivne naloge:** 0" in text


async def test_blokiraj_and_odblokiraj():
    guild = 4244
    kanal = SimpleNamespace(id=901, name="spam", mention="<#901>")
    interaction = fake_interaction(guild_id=guild)
    await commands.blokiraj.callback(interaction, kanal=kanal)
    assert store.blocked_channels(guild) == [901]
    assert blocked.channel(
        SimpleNamespace(id=901)
    )  # blocked.channel honours the database

    interaction = fake_interaction(guild_id=guild)
    await commands.odblokiraj.callback(interaction, kanal=kanal)
    assert store.blocked_channels(guild) == []
    assert not blocked.channel(SimpleNamespace(id=901))
    assert "ni več blokiran" in interaction.response.messages[0][0]


async def test_odblokiraj_of_an_env_channel_points_to_env(monkeypatch):
    guild = 4245
    monkeypatch.setattr(blocked, "BLOCKED_CHANNELS", frozenset({902}))
    kanal = SimpleNamespace(id=902, name="env", mention="<#902>")
    interaction = fake_interaction(guild_id=guild)
    await commands.odblokiraj.callback(interaction, kanal=kanal)
    assert ".env" in interaction.response.messages[0][0]


async def test_ucenje_vklop_with_a_channel_list():
    guild = 4246
    interaction = fake_interaction(guild_id=guild)
    await commands.ucenje.callback(
        interaction, akcija="vklop", kanali="General, #Memes"
    )
    style = store.style(f"guild:{guild}")
    assert style.enabled and style.channels == ["general", "memes"]
    assert interaction.response.messages[0][1]  # ephemeral


async def test_ucenje_izklop_deletes_the_guide():
    guild = 4247
    scope = f"guild:{guild}"
    from datetime import UTC, datetime

    store.save_style(
        scope, store.Style(True, None, "## Ton", datetime(2026, 11, 1, tzinfo=UTC))
    )
    interaction = fake_interaction(guild_id=guild)
    await commands.ucenje.callback(interaction, akcija="izklop")
    style = store.style(scope)
    assert not style.enabled and style.guide is None and style.learned_at is None


async def test_spomini_and_pozabi():
    guild = 4248
    scope = f"guild:{guild}"
    first = store.add_memory(scope, "prvi spomin", "Tim (@tim)", 1)
    store.add_memory(scope, "drugi spomin", "Luka (@luka)", 2)
    interaction = fake_interaction(guild_id=guild)
    await commands.spomini.callback(interaction)
    ((text, ephemeral),) = interaction.response.messages
    assert ephemeral and f"#{first}: prvi spomin" in text and "drugi spomin" in text

    interaction = fake_interaction(guild_id=guild)
    await commands.pozabi.callback(interaction, id=first)
    assert f"Spomin #{first} je izbrisan." in interaction.response.messages[0][0]
    assert [m.text for m in store.memories(scope)] == ["drugi spomin"]

    interaction = fake_interaction(guild_id=guild)
    await commands.pozabi.callback(interaction, id=999)
    assert "Spomina #999 ni." in interaction.response.messages[0][0]


async def test_naloge_and_preklici():
    guild = 4249
    from datetime import UTC, datetime, timedelta

    scope = f"guild:{guild}"
    task_id = store.add_task(
        scope=scope,
        channel_id=10,
        user_id=1,
        user_name="Tim (@tim)",
        prompt="Spomni Tima, da pokliče zobozdravnika",
        cron="0 8 * * *",
        next_run=datetime.now(UTC) + timedelta(hours=1),
        watch=False,
        mention=True,
    )
    interaction = fake_interaction(guild_id=guild)
    await commands.naloge.callback(interaction)
    ((text, ephemeral),) = interaction.response.messages
    assert (
        ephemeral
        and f"#{task_id}" in text
        and "0 8 * * *" in text
        and "Tim (@tim)" in text
    )

    interaction = fake_interaction(guild_id=guild)
    await commands.preklici.callback(interaction, id=task_id)
    assert f"Naloga #{task_id} je preklicana." in interaction.response.messages[0][0]
    assert store.tasks(scope) == []
