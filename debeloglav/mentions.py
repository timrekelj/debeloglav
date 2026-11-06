"""Turns the @Name the model writes into real Discord tags (<@id>)."""

import re
from collections import OrderedDict

import discord

MAX_TAGS = 5  # people tagged per message, so a reply can't mass-ping the server
MAX_NAMES = 3000  # names remembered per server
NAME_CHARS = 64
NOT_NAMES = {"everyone", "here"}


def who(user: discord.abc.User) -> str:
    """How the model sees a person: the server nickname, plus the @username that never changes with it."""
    if user.display_name.casefold() == user.name.casefold():
        return f"@{user.name}"
    return f"{user.display_name} (@{user.name})"


class People:
    """The names and ids of everyone the bot has seen talk or be mentioned, per server."""

    def __init__(self) -> None:
        self.names: dict[int, OrderedDict[str, int]] = {}

    def see(self, guild_id: int, user: discord.abc.User) -> None:
        names = self.names.setdefault(guild_id, OrderedDict())
        for name in {user.name, user.display_name, getattr(user, "global_name", None)}:
            if name and name.casefold() not in NOT_NAMES:
                key = name.casefold()
                names[key] = user.id
                names.move_to_end(key)
        while len(names) > MAX_NAMES:
            names.popitem(last=False)

    def see_message(self, message: discord.Message) -> None:
        if message.guild is None:
            return
        self.see(message.guild.id, message.author)
        for user in message.mentions:
            self.see(message.guild.id, user)

    def tag(self, guild: discord.Guild | None, text: str) -> str:
        """Replace each @Name with a real tag, matching the longest known name at each @."""
        if guild is None or "@" not in text:
            return text
        names = dict(self.names.get(guild.id, {}))
        for member in guild.members:  # whatever discord.py has cached
            for name in (member.name, member.display_name):
                names.setdefault(name.casefold(), member.id)

        out: list[str] = []
        tagged: set[int] = set()
        i = 0
        while (at := text.find("@", i)) != -1:
            out.append(text[i:at])
            match = _longest(names, text, at + 1)
            before = text[at - 1] if at else " "
            if (
                match
                and not before.isalnum()
                and before != "<"
                and (match[1] in tagged or len(tagged) < MAX_TAGS)
            ):
                length, user_id = match
                out.append(f"<@{user_id}>")
                tagged.add(user_id)
                i = at + 1 + length
            else:
                out.append("@")
                i = at + 1
        out.append(text[i:])
        return "".join(out)


def _longest(names: dict[str, int], text: str, start: int) -> tuple[int, int] | None:
    window = text[start : start + NAME_CHARS].casefold()
    best: tuple[int, int] | None = None
    for name, user_id in names.items():
        if window.startswith(name) and (best is None or len(name) > best[0]):
            end = start + len(name)
            if end >= len(text) or not re.match(r"\w", text[end]):
                best = (len(name), user_id)
    return best
