"""Tests for the /zamujeno command: the tree entry, the blocked-channel refusal and the handler
with a fake interaction and client."""

from types import SimpleNamespace

import discord
import pytest
from mistralai import agents

from debeloglav import blocked, bot, commands
from debeloglav.context import Request

GUILD = 4242


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


def test_zamujeno_is_registered_for_everyone():
    tree = make_tree()
    assert "zamujeno" in [c.qualified_name for c in tree.walk_commands()]
    command = tree.get_command("zamujeno")
    assert isinstance(command, discord.app_commands.Command)
    assert command.guild_only
    assert command.default_permissions is None  # no admin restriction, unlike the group
    # the optional channel parameter accepts text channels and threads
    kanal = command.parameters[0]
    assert kanal.name == "kanal" and not kanal.required
    assert discord.ChannelType.text in kanal.channel_types
    assert discord.ChannelType.public_thread in kanal.channel_types


class FakeResponse:
    def __init__(self):
        self.deferred = None
        self.messages: list[tuple[str, bool]] = []

    async def defer(self, *, ephemeral=False, thinking=False):
        self.deferred = {"ephemeral": ephemeral, "thinking": thinking}

    async def send_message(self, content, *, ephemeral=False):
        self.messages.append((content, ephemeral))


class FakeFollowup:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, content=None, *, ephemeral=False, files=None):
        self.sent.append({"content": content, "ephemeral": ephemeral, "files": files})


class FakeClient:
    def __init__(self, state=None, error=None):
        self.agent = SimpleNamespace(name="fake-agent")
        self.state = state
        self.error = error
        self.turns: list[dict] = []
        self.ended: list[str] = []

    async def run_turn(self, agent, session_id, request, content):
        self.turns.append(
            {
                "agent": agent,
                "session_id": session_id,
                "request": request,
                "content": content,
            }
        )
        if self.error is not None:
            raise self.error
        return "ses-1", self.state

    async def end_session(self, session_id):
        self.ended.append(session_id)


def user(user_id=42, name="tim", display_name="Timmy", manage_guild=False):
    permissions = (
        discord.Permissions(manage_guild=True)
        if manage_guild
        else discord.Permissions.none()
    )
    return SimpleNamespace(
        id=user_id, name=name, display_name=display_name, guild_permissions=permissions
    )


def channel(channel_id=10, name="general"):
    return SimpleNamespace(
        id=channel_id,
        name=name,
        mention=f"<#{channel_id}>",
        parent=None,
        category_id=None,
    )


def interaction(*, client, place=None, asker=None, guild_id=GUILD):
    place = place or channel()
    return SimpleNamespace(
        client=client,
        user=asker or user(),
        guild_id=guild_id,
        guild=SimpleNamespace(
            id=guild_id,
            get_role=lambda role_id: None,
            get_member=lambda member_id: SimpleNamespace(display_name="Luka"),
        ),
        channel=place,
        channel_id=place.id,
        response=FakeResponse(),
        followup=FakeFollowup(),
    )


def completed(text):
    return agents.Completed(
        session_id="ses-1", turn_id="t1", cursor="c1", text=text, output=None
    )


async def test_a_blocked_channel_is_refused(monkeypatch):
    monkeypatch.setattr(blocked, "BLOCKED_CHANNELS", frozenset({10}))
    client = FakeClient(state=completed("ne bi smelo"))
    it = interaction(client=client, place=channel(10, "general"))
    await commands.zamujeno.callback(it)
    ((text, ephemeral),) = it.response.messages
    assert ephemeral and "blokiran" in text
    assert it.response.deferred is None  # no turn was started
    assert client.turns == [] and it.followup.sent == [] and client.ended == []


async def test_a_blocked_named_channel_is_refused():
    blocked.block(GUILD, 20)  # blocked in the database, current channel is not
    client = FakeClient(state=completed("ne bi smelo"))
    it = interaction(client=client, place=channel(10, "general"))
    await commands.zamujeno.callback(it, kanal=channel(20, "tajno"))
    ((text, ephemeral),) = it.response.messages
    assert ephemeral and "blokiran" in text
    assert client.turns == [] and it.followup.sent == []


async def test_the_normal_path_defers_runs_one_turn_and_replies_ephemerally():
    client = FakeClient(state=completed("**Povzetek:** Luka je pisal o <@7>."))
    it = interaction(client=client, place=channel(10, "general"))
    await commands.zamujeno.callback(it)

    assert it.response.deferred == {"ephemeral": True, "thinking": True}
    assert it.response.messages == []  # nothing before the defer

    ((turn,)) = client.turns  # exactly one fresh turn on the bot's agent
    assert turn["agent"] is client.agent
    assert turn["session_id"] is None
    request = turn["request"]
    assert isinstance(request, Request)
    assert request.scope == f"guild:{GUILD}"
    assert request.channel_id == 10
    assert request.user_id == 42
    assert request.user_name == "Timmy (@tim)"
    assert request.message_id is None  # read up to now
    assert not request.is_admin

    ((part,)) = turn["content"]
    assert part["type"] == "text"
    text = part["text"]
    assert "Current local time" in text  # now_line()
    assert "What you remember about this server" in text  # memory_prompt(scope)
    assert "[/zamujeno] Timmy (@tim) asks what they missed in #general." in text
    assert 'Call catch_up with channel "10"' in text  # by id: names are not unique

    ((sent,)) = it.followup.sent
    assert sent["ephemeral"] is True
    assert sent["content"] == "**Povzetek:** Luka je pisal o Luka."  # untagged
    assert sent["files"] == []
    assert client.ended == ["ses-1"]  # the session is not kept


async def test_an_admin_asker_is_marked_as_admin():
    client = FakeClient(state=completed("ok"))
    it = interaction(client=client, asker=user(manage_guild=True))
    await commands.zamujeno.callback(it)
    assert client.turns[0]["request"].is_admin is True


async def test_the_named_channel_is_summarized_instead():
    client = FakeClient(state=completed("ok"))
    it = interaction(client=client, place=channel(10, "general"))
    await commands.zamujeno.callback(it, kanal=channel(20, "memes"))
    (turn,) = client.turns
    assert turn["request"].channel_id == 10  # still the interaction's channel
    text = turn["content"][0]["text"]
    assert "#memes" in text and 'Call catch_up with channel "20"' in text  # by id
    assert "general" not in text
    assert client.ended == ["ses-1"]


async def test_a_long_answer_is_split_into_ephemeral_chunks():
    import random

    long_text = " ".join(random.Random(7).choices("abcdefghijklmnopqrstuvwxyz", k=6000))
    client = FakeClient(state=completed(long_text))  # longer than Discord's limit
    it = interaction(client=client)
    await commands.zamujeno.callback(it)
    sent = it.followup.sent
    assert len(sent) > 1
    assert all(s["ephemeral"] for s in sent)
    assert all(len(s["content"]) <= 2000 for s in sent)
    assert sent[-1]["files"] == [] and all(s["files"] == [] for s in sent[:-1])
    assert client.ended == ["ses-1"]


async def test_an_incomplete_turn_still_replies_and_ends_the_session():
    client = FakeClient(
        state=None
    )  # parse() returns None, render() has its own fallback text
    it = interaction(client=client)
    await commands.zamujeno.callback(it)
    ((sent,)) = it.followup.sent
    assert sent["ephemeral"] and "ni uspelo" in sent["content"]
    assert client.ended == ["ses-1"]


async def test_a_failed_turn_sends_the_ephemeral_error():
    client = FakeClient(error=RuntimeError("boom"))
    it = interaction(client=client)
    await commands.zamujeno.callback(it)
    assert it.response.deferred == {"ephemeral": True, "thinking": True}
    ((sent,)) = it.followup.sent
    assert sent["ephemeral"] is True
    assert sent["content"] == commands.ERROR
    assert client.ended == []  # no session was started


async def test_a_failure_after_the_turn_still_ends_the_session(monkeypatch):
    async def boom(reply, max_chars=bot.MAX_REPLY_CHARS):
        raise RuntimeError("render boom")

    monkeypatch.setattr(bot, "render", boom)
    client = FakeClient(state=completed("ok"))
    it = interaction(client=client)
    await commands.zamujeno.callback(it)
    ((sent,)) = it.followup.sent
    assert sent["ephemeral"] and sent["content"] == commands.ERROR
    assert client.ended == ["ses-1"]
