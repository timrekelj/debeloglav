"""Channels the bot never reads: a global list from the environment, plus per-server ones set by admins."""

import os
import re

import discord

from . import store


def _ids(variable: str) -> frozenset[int]:
    return frozenset(
        int(i) for i in re.split(r"[\s,]+", os.environ.get(variable, "")) if i
    )


BLOCKED_CHANNELS = _ids("BLOCKED_CHANNELS")

# Channel and category ids blocked per server in the database, cached so channel() stays cheap
# and synchronous. Ids are unique across Discord, so one set of ids serves every server.
_db_blocked: set[int] = set()
_db_loaded = False


def _load_db() -> None:
    global _db_loaded
    if not _db_loaded:
        _db_blocked.update(store.all_blocked_channel_ids())
        _db_loaded = True


def block(guild_id: int, channel_id: int) -> None:
    """Block a channel or category in one server, in the database and in the cache."""
    store.block_channel(guild_id, channel_id)
    _load_db()
    _db_blocked.add(channel_id)


def unblock(guild_id: int, channel_id: int) -> bool:
    """Remove a per-server block. Returns whether it was blocked in the database."""
    removed = store.unblock_channel(guild_id, channel_id)
    if removed:
        _db_blocked.discard(channel_id)
    return removed


def channel(place: discord.abc.Messageable | None) -> bool:
    """Whether the bot must not read this channel: it, its parent channel or its category is blocked."""
    parent = getattr(place, "parent", None)  # a thread's channel
    ids = {
        getattr(place, "id", None),
        getattr(place, "category_id", None),
        getattr(parent, "id", None),
        getattr(parent, "category_id", None),
    }
    _load_db()
    return not (BLOCKED_CHANNELS | _db_blocked).isdisjoint(ids)
