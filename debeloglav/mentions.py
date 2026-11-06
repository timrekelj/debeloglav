"""Turns the @Name the model writes into real Discord tags (<@id>)."""

import re
from collections import OrderedDict

import discord

MAX_TAGS = 5  # people tagged per message, so a reply can't mass-ping the server
MAX_NAMES = 3000  # names remembered per server
NAME_CHARS = 64
NOT_NAMES = {"everyone", "here"}
# Code and links keep their @ as written: "`@zoz`" or "https://x.com/@zoz" must not become a ping.
UNTOUCHED = re.compile(r"```.*?```|`[^`\n]*`|<?https?://\S+", re.DOTALL)


def who(user: discord.abc.User) -> str:
    """How the model sees a person: the server nickname, plus the @username that never changes with it."""
    if user.display_name.casefold() == user.name.casefold():
        return f"@{user.name}"
    return f"{user.display_name} (@{user.name})"


class People:
    """The names and ids of everyone the bot has seen talk or be mentioned, per server."""

    def __init__(self) -> None:
        self.names: dict[int, OrderedDict[str, int]] = {}

    def learn(self, guild_id: int, user_id: int, *names: str | None) -> None:
        known = self.names.setdefault(guild_id, OrderedDict())
        for name in set(names):
            if name and name.casefold() not in NOT_NAMES:
                key = name.casefold()
                known[key] = user_id
                known.move_to_end(key)
        while len(known) > MAX_NAMES:
            known.popitem(last=False)

    def see(self, guild_id: int, user: discord.abc.User) -> None:
        self.learn(
            guild_id,
            user.id,
            user.name,
            user.display_name,
            getattr(user, "global_name", None),
        )

    def see_message(self, message: discord.Message) -> None:
        if message.guild is None:
            return
        self.see(message.guild.id, message.author)
        for user in message.mentions:
            self.see(message.guild.id, user)

    def tag(
        self, guild: discord.Guild | None, text: str, *, notified: int | None = None
    ) -> str:
        """Replace each @Name with a real tag, matching the longest known name at each @.

        notified: the person the message replies to. They get a ping from the reply already,
        so their @Name stays plain text instead of tagging them again.
        """
        if guild is None or "@" not in text:
            return text
        if notified and (member := guild.get_member(notified)):
            # A raw <@id> the model wrote itself names them too.
            text = re.sub(rf"<@!?{notified}>", lambda _: member.display_name, text)
        names = dict(self.names.get(guild.id, {}))
        for member in guild.members:  # whatever discord.py has cached
            for name in (member.name, member.display_name):
                names.setdefault(name.casefold(), member.id)

        untouched = [m.span() for m in UNTOUCHED.finditer(text)]
        out: list[str] = []
        tagged: set[int] = set()
        i = 0
        while (at := text.find("@", i)) != -1:
            out.append(text[i:at])
            if span := next((sp for sp in untouched if sp[0] <= at < sp[1]), None):
                out.append(text[at : span[1]])
                i = span[1]
                continue
            match = _longest(names, text, at + 1)
            before = text[at - 1] if at else " "
            if not match or before.isalnum() or before == "<":
                out.append("@")
                i = at + 1
                continue
            length, user_id = match
            end = at + 1 + length
            # "Nickname (@username)" names one person once: drop the "(@username)".
            if (
                before == "("
                and text[end : end + 1] == ")"
                and _names_just_before("".join(out)[:-1], user_id, names, notified)
            ):
                out[-1] = out[-1].rstrip()[:-1].rstrip()
                i = end + 1
                continue
            if user_id == notified:
                out.append(text[at + 1 : end])
            elif user_id in tagged or len(tagged) < MAX_TAGS:
                out.append(f"<@{user_id}>")
                tagged.add(user_id)
            else:
                out.append(text[at:end])
            i = end
        out.append(text[i:])
        return "".join(out)


def _names_just_before(
    done: str, user_id: int, names: dict[str, int], notified: int | None
) -> bool:
    """Whether the text ends with this person: their tag, or one of their names."""
    done = done.rstrip()
    if done.endswith(f"<@{user_id}>"):
        return True
    lowered = done.casefold()
    return any(
        lowered.endswith(name)
        and (len(name) == len(done) or not done[-len(name) - 1].isalnum())
        for name, uid in names.items()
        if uid == user_id
    )


def _longest(names: dict[str, int], text: str, start: int) -> tuple[int, int] | None:
    window = text[start : start + NAME_CHARS].casefold()
    best: tuple[int, int] | None = None
    for name, user_id in names.items():
        if window.startswith(name) and (best is None or len(name) > best[0]):
            end = start + len(name)
            if end >= len(text) or not re.match(r"\w", text[end]):
                best = (len(name), user_id)
    return best
