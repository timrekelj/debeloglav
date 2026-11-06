"""Who is asking, for each running session, so tools can act for the right Discord server and user.

The bot registers a Request before each turn. A tool reads it through its ToolCallbackContext's
session id; the model never sees it.
"""

import os
from collections.abc import Callable
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from mistralai import agents

TIMEZONE = ZoneInfo(os.environ.get("TIMEZONE", "Europe/Ljubljana"))


@dataclass
class Request:
    scope: str  # "guild:<id>" or "dm:<user id>": whose memories and tasks
    channel_id: int
    user_id: int
    user_name: str
    message_id: int | None = None  # the message that asked; None for scheduled runs
    session_id: str | None = None  # the conversation's session, set when its turn starts


_requests: dict[str, Request] = {}

# Set by the bot: starts a background research job for a request, or raises agents.ToolError.
launch_research: Callable[[Request, str], None] | None = None


def register(session_id: str, request: Request) -> None:
    request.session_id = session_id
    _requests[session_id] = request


def forget(session_id: str) -> None:
    _requests.pop(session_id, None)


def current(ctx: agents.ToolCallbackContext) -> Request:
    request = _requests.get(ctx.session.id)
    if request is None:
        raise agents.ToolError("This tool only works in a Discord conversation.")
    return request
