"""Who is asking, for each running session, so tools can act for the right Discord server and user.

The bot registers a Request before each turn. A tool reads it through its ToolCallbackContext's
session id; the model never sees it.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from mistralai import agents

from . import settings


def timezone() -> ZoneInfo:
    """The bot's local time zone (the timezone setting), read each time so a change applies at once."""
    return ZoneInfo(settings.get("timezone"))


@dataclass
class EditableImage:
    """An image from this turn's message that edit_image can edit: JPEG bytes plus a label for the prompt."""

    data: bytes
    label: str  # e.g. "photo.jpg from Luka (@luka)"


@dataclass
class Request:
    scope: str  # "guild:<id>" or "dm:<user id>": whose memories and tasks
    channel_id: int
    user_id: int
    user_name: str
    message_id: int | None = None  # the message that asked; None for scheduled runs
    is_admin: bool = False  # may change server settings (Manage Server permission)
    session_id: str | None = None  # the conversation's session, set when its turn starts
    editable_images: list[EditableImage] = field(default_factory=list)  # this turn's images, set by the bot
    private: bool = False  # the answer is shown only to the asker (ephemeral), e.g. /zamujeno
    reply_channel_id: int | None = None  # the thread the bot opened for the answer, if any


_requests: dict[str, Request] = {}

# Set by the bot: starts a background research job for a request, or raises agents.ToolError.
launch_research: Callable[[Request, str], None] | None = None

# Set by the bot: searches the server's message history for a request (see discord_search.search).
search_messages: Callable[..., Awaitable[dict[str, object]]] | None = None

# Set by the bot: reads what was written in a channel since the asker last wrote there (see catchup.catch_up).
catch_up: Callable[..., Awaitable[dict[str, object]]] | None = None


@dataclass
class FoundImage:
    """An image a tool produced, for the bot to attach to the reply."""

    data: bytes
    regenerated: bool = False  # True when it was regenerated instead of edited


@dataclass
class Found:
    """What the tools of one turn found, for the bot to attach to the reply."""

    sources: dict[str, str] = field(default_factory=dict)  # url -> title
    images: list[FoundImage] = field(default_factory=list)  # generated or edited images, as bytes


_found: dict[str, Found] = {}


def record(ctx: agents.ToolCallbackContext, *, sources: dict[str, str] | None = None, image: bytes | None = None, regenerated: bool = False) -> None:
    """Note sources or a generated image for the conversation's current turn."""
    if ctx.session.id not in _requests:
        return  # a session the bot does not post from, such as a research subagent
    found = _found.setdefault(ctx.session.id, Found())
    for url, title in (sources or {}).items():
        found.sources.setdefault(url, title)
    if image is not None:
        found.images.append(FoundImage(data=image, regenerated=regenerated))


def take_found(session_id: str) -> Found:
    return _found.pop(session_id, None) or Found()


def register(session_id: str, request: Request) -> None:
    request.session_id = session_id
    _requests[session_id] = request


def forget(session_id: str) -> None:
    _requests.pop(session_id, None)
    _found.pop(session_id, None)


def current(ctx: agents.ToolCallbackContext) -> Request:
    request = _requests.get(ctx.session.id)
    if request is None:
        raise agents.ToolError("This tool only works in a Discord conversation.")
    return request
