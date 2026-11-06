"""How people are named: to the model, and in what the bot posts (as names, never as tags)."""

import re

import discord

RAW = re.compile(r"<@!?(\d+)>")
ROLE = re.compile(r"<@&(\d+)>")
EVERYONE = re.compile(r"(?<!\w)@(everyone|here)\b")


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
    return EVERYONE.sub("@\u200b\\1", text)  # a zero-width space keeps @everyone and @here as text
