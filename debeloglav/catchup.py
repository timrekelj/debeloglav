"""Catch up on what was written in a channel since the asker last wrote there."""

from datetime import datetime

import discord
from mistralai import agents

from . import blocked, discord_search, polls
from .context import TIMEZONE, Request
from .mentions import who

MAX_MESSAGES = 300  # how far back to read
MAX_CHARS = 300  # per message in the transcript


async def catch_up(
    bot: discord.Client,
    request: Request,
    channel: str | None = None,
) -> dict[str, object]:
    """The messages in a channel since the asker's last one there, oldest first, with links."""
    if not request.scope.startswith("guild:"):
        raise agents.ToolError("Catching up only works in a server, not in DMs.")
    guild = bot.get_guild(int(request.scope.removeprefix("guild:")))
    if guild is None:
        raise agents.ToolError("I can't find this server.")
    asker = guild.get_member(request.user_id) or await guild.fetch_member(
        request.user_id
    )
    if channel:
        place = discord_search.find_channel(guild, channel)
    else:
        place = guild.get_channel_or_thread(request.channel_id) or bot.get_channel(
            request.channel_id
        )
        if place is None:
            try:
                place = await bot.fetch_channel(request.channel_id)
            except discord.HTTPException:
                raise agents.ToolError("I can't find this channel.") from None
    if blocked.channel(place) or not discord_search.readable(place, asker):
        raise agents.ToolError(f"#{place.name} is not a channel you can read.")
    if not discord_search.shareable(guild, place, request):
        raise agents.ToolError(
            f"#{place.name} is private: its summary can't be posted here. "
            f"Tell the person to use /zamujeno {place.name}, which only they will see."
        )

    # Newest first, back to the message that asked (excluded) or to the asker's last one there.
    before = (
        discord.Object(id=request.message_id)
        if request.message_id is not None and place.id == request.channel_id
        else None
    )
    window: list[discord.Message] = []
    since: datetime | None = None
    async for message in place.history(limit=MAX_MESSAGES, before=before):
        if message.author.id == request.user_id:
            since = message.created_at
            break  # the asker's own message is not included
        window.append(message)
    window.reverse()  # oldest first

    ids: dict[int, int] = {}  # message id -> short id, only for the messages kept
    lines: list[str] = []
    links: dict[str, str] = {}
    for message in window:
        if message.author.bot or (
            not message.clean_content and not message.attachments and not message.poll
        ):
            continue  # counted as read, left out of the transcript
        i = len(ids) + 1
        ids[message.id] = i
        links[f"m{i}"] = (
            f"https://discord.com/channels/{guild.id}/{place.id}/{message.id}"
        )
        lines.append(line(message, i, ids))
    return {
        "channel": f"#{place.name}",
        "since": (
            f"the asker's last message at {since.astimezone(TIMEZONE):%Y-%m-%d %H:%M}"
            if since
            else f"the last {MAX_MESSAGES} messages (you haven't written here recently)"
        ),
        "count": len(lines),
        "messages": "\n".join(lines),
        "links": links,
    }


def line(message: discord.Message, i: int, ids: dict[int, int]) -> str:
    """One transcript line: short id, local time, author, content and markers."""
    sent = message.created_at.astimezone(TIMEZONE)
    stamp = (
        f"{sent:%H:%M}"
        if sent.date() == datetime.now(TIMEZONE).date()
        else f"{sent:%Y-%m-%d %H:%M}"
    )
    parts = [message.clean_content[:MAX_CHARS]]
    for attachment in message.attachments:
        if attachment.is_voice_message():
            parts.append("[voice message]")
        elif (attachment.content_type or "").startswith("image"):
            parts.append("[image]")
        else:
            parts.append(f"[attachment: {attachment.filename}]")
    if message.poll:
        parts.append(polls.describe(message.poll))
    text = " ".join(part for part in parts if part)
    reply = ""
    if message.reference and (j := ids.get(message.reference.message_id)):
        reply = f" (reply to m{j})"
    return f"m{i} [{stamp}] {who(message.author)}: {text}{reply}"
