"""Channels the bot never reads, set in the environment as a list of ids."""

import os
import re

import discord


def _ids(variable: str) -> frozenset[int]:
    return frozenset(
        int(i) for i in re.split(r"[\s,]+", os.environ.get(variable, "")) if i
    )


BLOCKED_CHANNELS = _ids("BLOCKED_CHANNELS")


def channel(place: discord.abc.Messageable | None) -> bool:
    """Whether the bot must not read this channel: it, its parent channel or its category is blocked."""
    parent = getattr(place, "parent", None)  # a thread's channel
    ids = {
        getattr(place, "id", None),
        getattr(place, "category_id", None),
        getattr(parent, "id", None),
        getattr(parent, "category_id", None),
    }
    return not BLOCKED_CHANNELS.isdisjoint(ids)

