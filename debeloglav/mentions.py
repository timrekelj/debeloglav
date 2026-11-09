"""How people are named: to the model, and in what the bot posts (as names, never as tags).

Also the server's custom emojis: which the model may use, and writing them so Discord shows them.
"""

import re

import discord

RAW = re.compile(r"<@!?(\d+)>")
ROLE = re.compile(r"<@&(\d+)>")
EVERYONE = re.compile(r"(?<!\w)@(everyone|here)\b")
# A server emoji as the model writes it: <:name:id>, or <:name:> without the id it can't know.
FULL_EMOJI = re.compile(r"<(a?):(\w{2,32}):(\d*)>")
# Or :name:, but not inside <...> or a time such as 12:30:45.
SHORT_EMOJI = re.compile(r"(?<![<\w]):(\w{2,32}):(?![\w>])")
MAX_EMOJIS = 80  # how many of the server's emojis the model is told about


def who(user: discord.abc.User) -> str:
    """How the model sees a person: the server nickname, plus the @username that never changes with it."""
    if user.display_name.casefold() == user.name.casefold():
        return f"@{user.name}"
    return f"{user.display_name} (@{user.name})"


async def untag(guild: discord.Guild | None, text: str) -> str:
    """Write the tags in the model's reply as plain text: the bot doesn't tag people, roles, @everyone or @here.

    Each <@id> becomes the person's nickname and each <@&id> the role's name.
    """
    for user_id in {int(i) for i in RAW.findall(text)}:
        member = guild and guild.get_member(user_id)
        if guild and member is None:
            try:
                member = await guild.fetch_member(user_id)
            except discord.HTTPException:
                pass
        name = member.display_name if member else "nekdo"
        text = re.sub(rf"<@!?{user_id}>", lambda _, name=name: name, text)
    role = guild.get_role if guild else lambda _: None
    text = ROLE.sub(lambda m: getattr(role(int(m[1])), "name", "vloga"), text)
    text = EVERYONE.sub(
        "@\u200b\\1", text
    )  # a zero-width space keeps @everyone and @here as text
    return emojis(guild, text)


def usable_emojis(guild: discord.Guild | None) -> dict[str, discord.Emoji]:
    """The server's custom emojis the bot can post, by name."""
    if guild is None:
        return {}
    return {e.name: e for e in getattr(guild, "emojis", ()) if e.available}


def emojis(guild: discord.Guild | None, text: str) -> str:
    """Write the server's emojis in the reply as Discord shows them: <:name:id>.

    The model writes them as :name: or <:name:> (it doesn't know the ids); both become the real
    emoji when the server has one by that name. An unknown <:name:> becomes plain :name:, as
    Discord would show it as raw text; one with an id is left alone.
    """
    known = usable_emojis(guild)

    def full(match: re.Match[str]) -> str:
        emoji = known.get(match[2])
        if emoji is not None:
            return str(emoji)
        return match[0] if match[3] else f":{match[2]}:"

    text = FULL_EMOJI.sub(full, text)
    return SHORT_EMOJI.sub(lambda m: str(known[m[1]]) if m[1] in known else m[0], text)


def emoji_prompt(guild: discord.Guild | None) -> str:
    """The server's emojis, for the model: it may use them by writing :name:."""
    names = sorted(usable_emojis(guild))
    if not names:
        return ""
    return (
        "The server's own emojis, which you can use by writing :name: (only these): "
        + ", ".join(f":{name}:" for name in names[:MAX_EMOJIS])
    )
