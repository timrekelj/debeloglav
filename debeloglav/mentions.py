"""Turns the @username the model writes into real Discord tags (<@id>)."""

import asyncio
import re
from collections import OrderedDict

import discord

MAX_NAMES = 3000  # usernames remembered per server
# Code and links keep their @ as written: "`@zoz`" or "https://x.com/@zoz" must not become a ping.
# Anything else that looks like @username (lowercase letters, digits, _ and .) may be a tag.
TAG = re.compile(
    r"```.*?```|`[^`\n]*`|<?https?://\S+|(?<![\w@<])@(?P<name>[\w.]{2,32})",
    re.DOTALL | re.ASCII,
)
DOUBLE = re.compile(r"(<@(\d+)>)\s*\(<@\2>\)")  # "@ambros (@ambros)"


def who(user: discord.abc.User) -> str:
    """How the model sees a person: the server nickname, plus the @username that never changes with it."""
    if user.display_name.casefold() == user.name.casefold():
        return f"@{user.name}"
    return f"{user.display_name} (@{user.name})"


class People:
    """The usernames and ids of everyone the bot has seen, per server."""

    def __init__(self) -> None:
        self.usernames: dict[int, OrderedDict[str, int]] = {}

    def learn(self, guild_id: int, user_id: int, username: str) -> None:
        known = self.usernames.setdefault(guild_id, OrderedDict())
        known[username.lower()] = user_id
        known.move_to_end(username.lower())
        while len(known) > MAX_NAMES:
            known.popitem(last=False)

    def see_message(self, message: discord.Message) -> None:
        if message.guild is None:
            return
        for user in [message.author, *message.mentions]:
            self.learn(message.guild.id, user.id, user.name)

    async def tag(self, guild: discord.Guild | None, text: str) -> str:
        """Replace each @username of someone in the server with a real tag."""
        if guild is None or "@" not in text:
            return text
        known = self.usernames.setdefault(guild.id, OrderedDict())
        # Ask Discord about the usernames the bot hasn't seen yet.
        wanted = {_username(m) for m in TAG.finditer(text) if m["name"]}
        await asyncio.gather(*(self.look_up(guild, name) for name in wanted if name not in known))

        def replace(match: re.Match[str]) -> str:
            if not match["name"]:
                return match[0]  # code or a link
            username = _username(match)
            user_id = known.get(username)
            if user_id is None:
                return match[0]
            return f"<@{user_id}>" + match["name"][len(username) :]

        return DOUBLE.sub(r"\1", TAG.sub(replace, text))

    async def look_up(self, guild: discord.Guild, username: str) -> None:
        member = discord.utils.get(guild.members, name=username)  # whatever discord.py has cached
        if member is None:
            try:
                found = await guild.query_members(query=username, limit=25)
            except (asyncio.TimeoutError, discord.ClientException):
                return
            member = discord.utils.get(found, name=username)  # the query also matches nicknames
        if member:
            self.learn(guild.id, member.id, member.name)


def _username(match: re.Match[str]) -> str:
    return match["name"].rstrip(".").lower()  # "@ambros." ends a sentence
