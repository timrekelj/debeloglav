"""Search a server's message history with Discord's Search Guild Messages endpoint."""

import asyncio
import re
from datetime import datetime

import discord
from discord.http import Route
from mistralai import agents

from . import blocked, polls
from .context import Request, timezone
from .mentions import who

PAGE = 25  # the most Discord returns per request
DEFAULT_RESULTS = 25
MAX_RESULTS = 200
MAX_CHARS = 400
INDEX_RETRIES = 3

HAS_TYPES = {"image", "sound", "video", "file", "sticker", "embed", "link", "poll"}


async def search(
    bot: discord.Client,
    request: Request,
    *,
    content: str | None = None,
    author: str | None = None,
    channel: str | None = None,
    mentioned: str | None = None,
    has: list[str] | None = None,
    after: str | None = None,
    before: str | None = None,
    oldest_first: bool = False,
    by_relevance: bool = False,
    limit: int = DEFAULT_RESULTS,
) -> dict[str, object]:
    if not request.scope.startswith("guild:"):
        raise agents.ToolError("Message search only works in a server, not in DMs.")
    guild = bot.get_guild(int(request.scope.removeprefix("guild:")))
    if guild is None:
        raise agents.ToolError("I can't find this server.")
    asker = await asker_of(guild, request)

    params: list[tuple[str, str]] = [
        ("limit", str(PAGE)),
        ("author_type", "user"),
    ]
    if content:
        params.append(("content", content[:1024]))
    if author:
        params.append(("author_id", str((await find_member(guild, author)).id)))
    if mentioned:
        params.append(("mentions", str((await find_member(guild, mentioned)).id)))
    if channel:
        params.append(("channel_id", str(find_channel(guild, channel).id)))
    for kind in has or []:
        if kind not in HAS_TYPES:
            raise agents.ToolError(
                f"has must be one of: {', '.join(sorted(HAS_TYPES))}."
            )
        params.append(("has", kind))
    if after:
        params.append(("min_id", str(discord.utils.time_snowflake(parse_date(after)))))
    if before:
        params.append(("max_id", str(discord.utils.time_snowflake(parse_date(before)))))
    if by_relevance:
        params.append(("sort_by", "relevance"))
    else:
        params.append(("sort_order", "asc" if oldest_first else "desc"))

    limit = max(1, min(limit, MAX_RESULTS))
    route = Route("GET", "/guilds/{guild_id}/messages/search", guild_id=guild.id)
    results: list[dict[str, object]] = []
    total = 0
    offset = 0
    while offset < limit:
        data = await fetch_page(bot, route, [*params, ("offset", str(offset))])
        total = int(data.get("total_results") or 0)
        groups = data.get("messages", [])
        offset += len(groups)
        for group in groups:
            for message in group[:1]:
                place = guild.get_channel_or_thread(int(message["channel_id"]))
                # Only show what the person asking could read themselves, minus the blocked channels.
                if (
                    place is None
                    or not readable(place, asker)
                    or blocked.channel(place)
                ):
                    continue
                results.append(describe(guild, place, message))
        if len(groups) < PAGE or offset >= total:
            break
    return {
        "messages": results[:limit],
        # Discord's count of every match, including those in channels left out above.
        "total": total,
        "more_may_exist": offset < total,
    }


async def fetch_page(
    bot: discord.Client, route: Route, params: list[tuple[str, str]]
) -> dict:
    for _ in range(INDEX_RETRIES):
        data = await bot.http.request(route, params=params)
        if (
            data.get("code") != 110000
        ):  # 110000: the server's search index is still being built
            return data
        await asyncio.sleep(min(float(data.get("retry_after") or 2), 5))
    raise agents.ToolError(
        "Discord is still indexing this server for search. Try again in a minute."
    )


def describe(
    guild: discord.Guild,
    place: discord.abc.GuildChannel | discord.Thread,
    message: dict,
) -> dict[str, object]:
    author_data = message["author"]
    member = guild.get_member(int(author_data["id"]))
    sent = datetime.fromisoformat(message["timestamp"]).astimezone(timezone())
    result: dict[str, object] = {
        "author": who(member)
        if member
        else f"{author_data.get('global_name') or author_data['username']} (@{author_data['username']})",
        "channel": f"#{place.name}",
        "time": f"{sent:%Y-%m-%d %H:%M}",
        "content": readable_content(guild, message)[:MAX_CHARS],
        "attachments": [a["filename"] for a in message.get("attachments", [])],
        "link": f"https://discord.com/channels/{guild.id}/{message['channel_id']}/{message['id']}",
    }
    if poll := message.get("poll"):
        result["poll"] = polls.describe_raw(poll)
    return result


async def asker_of(guild: discord.Guild, request: Request) -> discord.Member:
    """The member who asked; a ToolError when they are no longer in the server."""
    member = guild.get_member(request.user_id)
    if member is not None:
        return member
    try:
        return await guild.fetch_member(request.user_id)
    except discord.HTTPException:
        raise agents.ToolError(
            "I can't find the person asking in this server."
        ) from None


def readable_content(guild: discord.Guild, message: dict) -> str:
    """The message text with <@id>, <@&id> and <#id> written as names, like Discord shows them."""
    users = {
        int(u["id"]): u.get("member", {}).get("nick")
        or u.get("global_name")
        or u["username"]
        for u in message.get("mentions", [])
    }

    def name(match: re.Match[str]) -> str:
        kind, id_ = match.group(1), int(match.group(2))
        if kind == "#":
            place = guild.get_channel_or_thread(id_)
            return f"#{place.name}" if place else "#deleted-channel"
        if kind == "@&":
            role = guild.get_role(id_)
            return f"@{role.name}" if role else "@deleted-role"
        member = guild.get_member(id_)
        return member.display_name if member else users.get(id_, "unknown-user")

    return re.sub(r"<(@!?|@&|#)(\d+)>", name, message.get("content") or "")


def readable(
    place: discord.abc.GuildChannel | discord.Thread, who: discord.Member | discord.Role
) -> bool:
    permissions = place.permissions_for(who)
    return permissions.view_channel and permissions.read_message_history


async def find_member(guild: discord.Guild, who: str) -> discord.Member:
    """A member by id, mention, username or nickname."""
    if handle := re.search(r"\(@([^)\s]+)\)", who):  # "Nickname (@username)"
        who = handle.group(1)
    who = who.strip().lstrip("@")
    digits = who.removeprefix("<@").removeprefix("!").removesuffix(">")
    if digits.isdigit():
        member = guild.get_member(int(digits))
        if member is None:
            try:
                member = await guild.fetch_member(int(digits))
            except discord.HTTPException:
                member = None
        if member:
            return member
    lowered = who.casefold()
    for member in guild.members:
        if lowered in (member.name.casefold(), member.display_name.casefold()):
            return member
    try:
        found = await guild.query_members(query=who, limit=5)
    except (TimeoutError, discord.ClientException):
        found = []
    if len(found) == 1:
        return found[0]
    if found:
        names = ", ".join(f"{m.display_name} (@{m.name})" for m in found)
        raise agents.ToolError(
            f"{who!r} matches several people: {names}. Ask which one."
        )
    raise agents.ToolError(
        f"No member called {who!r}. Ask the person to @mention them."
    )


def find_channel(
    guild: discord.Guild, name: str
) -> discord.abc.GuildChannel | discord.Thread:
    name = name.strip().lstrip("#").casefold()
    if (
        name.isdigit()
        and (place := guild.get_channel_or_thread(int(name)))
        and not blocked.channel(place)
    ):
        return place
    for place in [
        *guild.text_channels,
        *guild.threads,
        *guild.forums,
        *guild.voice_channels,
    ]:
        if place.name.casefold() == name and not blocked.channel(place):
            return place
    raise agents.ToolError(f"There is no channel called #{name}.")


def parse_date(value: str) -> datetime:
    try:
        when = datetime.fromisoformat(value)
    except ValueError:
        raise agents.ToolError(
            f"{value!r} is not an ISO date, e.g. 2026-03-01 or 2026-03-01T18:00."
        ) from None
    return when if when.tzinfo else when.replace(tzinfo=timezone())
