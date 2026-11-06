"""Debeloglav on Discord: answers every message that tags it, as a reply to that message."""

import asyncio
import functools
import io
import logging
import os
import time
from collections import OrderedDict
from dataclasses import dataclass
from contextlib import suppress
from datetime import datetime

import discord
from mistralai import agents

from . import context, discord_search, guard, media, mentions, scheduler, store, style
from .agent import (
    NO_REPLY,
    build_agent,
    build_research_agent,
    download_file,
    memory_prompt,
    style_prompt,
)
from .context import TIMEZONE, Request

log = logging.getLogger("debeloglav")

CONTEXT_MESSAGES = int(os.environ.get("CONTEXT_MESSAGES", "10"))
MAX_RESEARCH_JOBS = int(os.environ.get("MAX_RESEARCH_JOBS", "3"))
RESEARCH_TIMEOUT = 20 * 60
SESSION_IDLE_SECONDS = 6 * 60 * 60
REPLY_CHAIN_DEPTH = 8
MAX_IMAGES = 8
MAX_SOURCES = 5
MAX_REPLY_CHARS = 3600  # two Discord messages
MAX_REPORT_CHARS = 7600  # four, for research reports
MAX_MEDIA_BYTES = 50 * 1024 * 1024
DISCORD_LIMIT = 2000

THINKING = "🤔"  # used where the server has no THINKING_EMOTE
THINKING_EMOTE = os.environ.get("THINKING_EMOTE", "hmmmm")  # the server's custom emote, by name
RESEARCHING = "🔬"

# Tags of people (resolved by mentions.People, at most a few per message) may ping;
# @everyone, @here and roles never do.
ALLOWED_MENTIONS = discord.AllowedMentions(everyone=False, roles=False, users=True, replied_user=True)


class Conversations:
    """Which session each message belongs to, so a reply to Debeloglav continues that conversation."""

    def __init__(self, max_messages: int = 5000) -> None:
        self.max_messages = max_messages
        self.by_message: OrderedDict[int, str] = OrderedDict()
        self.last_used: dict[str, float] = {}
        self.locks: dict[str, asyncio.Lock] = {}

    def get(self, message_id: int | None) -> str | None:
        return self.by_message.get(message_id) if message_id else None

    def link(self, session_id: str, *message_ids: int) -> None:
        for message_id in message_ids:
            self.by_message[message_id] = session_id
            self.by_message.move_to_end(message_id)
        while len(self.by_message) > self.max_messages:
            self.by_message.popitem(last=False)
        self.last_used[session_id] = time.monotonic()

    def lock(self, session_id: str) -> asyncio.Lock:
        return self.locks.setdefault(session_id, asyncio.Lock())

    def idle(self) -> list[str]:
        """Forget the sessions nobody used for a while, and return their ids to cancel."""
        cutoff = time.monotonic() - SESSION_IDLE_SECONDS
        stale = [
            sid
            for sid, used in self.last_used.items()
            if used < cutoff and not self.lock(sid).locked()
        ]
        for sid in stale:
            del self.last_used[sid]
            self.locks.pop(sid, None)
            context.forget(sid)
        stale_set = set(stale)
        for message_id in [m for m, sid in self.by_message.items() if sid in stale_set]:
            del self.by_message[message_id]
        return stale


class Debeloglav(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents, allowed_mentions=ALLOWED_MENTIONS)
        self.agent = build_agent()
        self.research_agent = build_research_agent()
        self.conversations = Conversations()
        self.background: set[asyncio.Task[None]] = set()
        self.research_jobs = 0
        self.people = mentions.People()

    async def setup_hook(self) -> None:
        context.launch_research = self.launch_research
        context.search_messages = functools.partial(discord_search.search, self)
        self.spawn(scheduler.run_forever(self.run_task))
        self.spawn(self.prune_sessions())
        self.spawn(style.run_forever(self))

    async def on_ready(self) -> None:
        log.info("Logged in as %s", self.user)

    def spawn(self, coroutine) -> asyncio.Task[None]:
        task = asyncio.create_task(coroutine)
        self.background.add(task)
        task.add_done_callback(self.background.discard)
        return task

    # --- Answering -----------------------------------------------------------------------------

    async def on_message(self, message: discord.Message) -> None:
        self.people.see_message(message)  # learn names, so "@Name" in a reply becomes a real tag
        if message.author.bot or not self.is_summoned(message):
            return
        parent_id = message.reference.message_id if message.reference else None
        session_id = self.conversations.get(parent_id)
        request = Request(
            scope=scope_of(message),
            channel_id=message.channel.id,
            user_id=message.author.id,
            user_name=mentions.who(message.author),
            message_id=message.id,
            is_admin=isinstance(message.author, discord.Member)
            and message.author.guild_permissions.manage_guild,
        )

        thinking = thinking_emoji(message.guild)
        await react(message, thinking)
        try:
            content = await self.build_content(
                message, request, continuing=session_id is not None
            )
            try:
                session_id, state = await self.run_turn(
                    self.agent, session_id, request, content
                )
            except (agents.SessionTerminatedError, agents.SessionNotFoundError):
                # The conversation's session ended: start over with the full context.
                content = await self.build_content(message, request, continuing=False)
                session_id, state = await self.run_turn(
                    self.agent, None, request, content
                )
            text, files = await render(parse(state, session_id))
        except Exception:
            log.exception("Failed to answer %s", message.jump_url)
            text, files = "Ojoj, nekaj je šlo narobe. Poskusi znova čez trenutek.", []
        finally:
            await unreact(message, thinking, self.user)

        text = self.people.tag(getattr(message, "guild", None), text)
        sent = await reply_chunks(message, text, files)
        if session_id:
            self.conversations.link(session_id, message.id, *(m.id for m in sent))

    async def run_turn(
        self,
        agent: agents.Agent,
        session_id: str | None,
        request: Request,
        content: list[dict[str, str]],
    ) -> tuple[str, agents.TurnState]:
        """Run one turn in the conversation's session, or in a new one, with the request registered for tools."""
        session = (
            agent.session(session_id) if session_id else await agent.start_session()
        )
        async with self.conversations.lock(session.id):
            context.register(session.id, request)
            context.take_found(session.id)  # start the turn with nothing recorded
            state = await session.run(content)
            while isinstance(
                state, agents.Busy
            ):  # a turn still runs in this session: wait, then send ours
                await state.turn.result()
                state = await session.run(content)
        return session.id, state

    def is_summoned(self, message: discord.Message) -> bool:
        assert self.user is not None
        if message.guild is None:
            return True  # DMs always get an answer
        if self.user in message.mentions:
            return True
        reference = message.reference
        if reference and (
            self.conversations.get(reference.message_id)
            or (
                isinstance(reference.resolved, discord.Message)
                and reference.resolved.author == self.user
            )
        ):
            return True  # a reply to Debeloglav continues the conversation, even without a ping
        # Tagging the bot's managed role from autocomplete also counts.
        return any(
            role.tags and role.tags.bot_id == self.user.id
            for role in message.role_mentions
        )

    async def build_content(
        self, message: discord.Message, request: Request, *, continuing: bool
    ) -> list[dict[str, str]]:
        sections = [now_line(), memory_prompt(request.scope), style_prompt(request.scope)]
        if continuing:
            # The session already has the conversation; add only what this message replies to.
            chain = await reply_chain(message, depth=1)
            if chain:
                sections.append("It replies to:\n" + describe(chain[0]))
        else:
            chain = await reply_chain(message, depth=REPLY_CHAIN_DEPTH)
            history = await recent_history(message, exclude={m.id for m in chain})
            if history:
                sections.append(
                    "Recent messages in the channel (oldest first):\n"
                    + "\n".join(map(describe, history))
                )
            if chain:
                sections.append(
                    "The tagged message replies to this thread (oldest first):\n"
                    + "\n".join(map(describe, chain))
                )
        sections.append("Message that tagged you:\n" + describe(message))
        mentioned = [u for u in message.mentions if u != self.user]
        if mentioned:
            sections.append(
                "People it mentions: "
                + ", ".join(f"{mentions.who(u)}, id {u.id}" for u in mentioned)
            )

        # Media from the tagged message and the one it replies to, so "what is this?" works.
        images: list[dict[str, str]] = []
        notes: list[str] = []
        for source in [*chain[-1:], message]:
            for attachment in source.attachments:
                await read_attachment(source, attachment, images, notes)
        sections.extend(notes)

        return [{"type": "text", "text": "\n\n".join(filter(None, sections))}, *images[:MAX_IMAGES]]

    # --- Background research -------------------------------------------------------------------

    def launch_research(self, request: Request, question: str) -> None:
        if self.research_jobs >= MAX_RESEARCH_JOBS:
            raise agents.ToolError(
                "Too many research jobs are running. Ask the person to try again in a few minutes."
            )
        # Runs on the bot's event loop: start_research is an async tool, so it is called there.
        self.spawn(self.research(request, question))
        self.research_jobs += 1

    async def research(self, request: Request, question: str) -> None:
        message = session = None
        try:
            channel = await self.channel(request.channel_id)
            message = channel.get_partial_message(request.message_id)
            await react(message, RESEARCHING)
            session = await self.research_agent.start_session()
            async with asyncio.timeout(RESEARCH_TIMEOUT):
                state = await session.run(
                    f"{now_line()}\n\nResearch question from {request.user_name}:\n{question}"
                )
            text, files = await render(parse(state), MAX_REPORT_CHARS)  # lists its own sources
        except Exception:
            log.exception("Research failed: %s", question)
            text, files = (
                "Raziskava mi ni uspela. Poskusi znova ali vprašanje malo zoži.",
                [],
            )
        finally:
            self.research_jobs -= 1
            if session is not None:
                with suppress(Exception):
                    await session.cancel()
            if message is not None:
                await unreact(message, RESEARCHING, self.user)

        if message is None:
            return  # the channel is gone or unreachable: nowhere to post the report

        text = self.people.tag(getattr(message, "guild", None), text)
        sent = await reply_chunks(message, text, files)
        # Replies to the report continue the conversation that asked for it.
        if request.session_id:
            self.conversations.link(request.session_id, *(m.id for m in sent))

    # --- Scheduled tasks -----------------------------------------------------------------------

    async def run_task(self, task: store.Task) -> None:
        channel = await self.channel(task.channel_id)
        request = Request(
            scope=task.scope,
            channel_id=task.channel_id,
            user_id=task.user_id,
            user_name=task.user_name,
        )
        kind = f"Recurring task ({task.cron})" if task.cron else "One-off task"
        prompt = [
            f"[Scheduled task #{task.id}] {kind}, created by {task.user_name}.",
            now_line(),
            memory_prompt(task.scope),
            style_prompt(task.scope),
        ]
        if task.watch:
            prompt.append(
                f"This is a watch. Your last report was: {task.last_report or '(none yet)'}\n"
                f"If there is nothing new or relevant since then, answer exactly {NO_REPLY}."
            )
        prompt.append(f"Task: {task.prompt}")

        session_id, state = await self.run_turn(
            self.agent, None, request, [{"type": "text", "text": "\n\n".join(filter(None, prompt))}]
        )
        reply = parse(state, session_id)
        if reply is not None and reply.text.startswith(NO_REPLY):
            await self.end_session(session_id)
            return
        text, files = await render(reply)
        if task.watch and reply is not None:
            store.set_last_report(task.id, reply.text)

        text = self.people.tag(getattr(channel, "guild", None), text)
        if task.mention:
            text = f"<@{task.user_id}> {text}"
        chunks = split(text) or [""]
        sent = [await channel.send(c) for c in chunks[:-1]]
        sent.append(await channel.send(chunks[-1] or None, files=files))
        # People can reply to a scheduled post to follow up on it.
        self.conversations.link(session_id, *(m.id for m in sent))

    async def prune_sessions(self) -> None:
        while True:
            await asyncio.sleep(10 * 60)
            for session_id in self.conversations.idle():
                await self.end_session(session_id)

    async def end_session(self, session_id: str) -> None:
        self.conversations.locks.pop(session_id, None)
        context.forget(session_id)
        with suppress(Exception):
            await self.agent.session(session_id).cancel()

    async def channel(self, channel_id: int) -> discord.abc.Messageable:
        channel = self.get_channel(channel_id) or await self.fetch_channel(channel_id)
        assert isinstance(channel, discord.abc.Messageable)
        return channel


# --- Helpers -----------------------------------------------------------------------------------


def scope_of(message: discord.Message) -> str:
    return f"guild:{message.guild.id}" if message.guild else f"dm:{message.author.id}"


def now_line() -> str:
    return f"Current local time: {datetime.now(TIMEZONE):%A %Y-%m-%d %H:%M} ({TIMEZONE.key})"


def thinking_emoji(guild: discord.Guild | None) -> discord.Emoji | str:
    """The server's :THINKING_EMOTE: if it has one the bot can use, else 🤔."""
    emoji = discord.utils.get(guild.emojis, name=THINKING_EMOTE) if guild else None
    return emoji if emoji and emoji.available else THINKING


async def react(
    message: discord.Message | discord.PartialMessage, emoji: discord.Emoji | str
) -> None:
    with suppress(discord.HTTPException):
        await message.add_reaction(emoji)


async def unreact(
    message: discord.Message | discord.PartialMessage,
    emoji: discord.Emoji | str,
    me: discord.abc.Snowflake | None,
) -> None:
    """Remove the bot's own reaction."""
    if me is None:
        return
    with suppress(discord.HTTPException):
        await message.remove_reaction(emoji, me)


async def read_attachment(
    message: discord.Message,
    attachment: discord.Attachment,
    images: list[dict[str, str]],
    notes: list[str],
) -> None:
    """Add an attachment to the prompt: images as they are, voice and video as transcripts and frames."""
    kind = (attachment.content_type or "").split("/")[0]
    who = mentions.who(message.author)
    if kind == "image":
        images.append(
            {
                "type": "image",
                "uri": attachment.url,
                "mediaType": attachment.content_type or "",
            }
        )
        return
    if kind not in ("audio", "video") and not attachment.is_voice_message():
        return
    if attachment.size > MAX_MEDIA_BYTES:
        notes.append(f"[{who} sent {attachment.filename}, too large to process]")
        return

    try:
        data = await attachment.read()
        if kind == "video":
            frames, audio = await media.video_frames_and_audio(
                data, attachment.filename
            )
            images.extend(
                {"type": "image", "uri": frame, "mediaType": "image/jpeg"}
                for frame in frames
            )
            transcript = await media.transcribe(audio, "audio.mp3") if audio else ""
            notes.append(
                f"[Video {attachment.filename} from {who}: {len(frames)} frames attached in order as images. "
                f"Transcript of its sound: {transcript or '(no speech)'}]"
            )
        else:
            label = (
                "Voice message"
                if attachment.is_voice_message()
                else f"Audio file {attachment.filename}"
            )
            transcript = await media.transcribe(data, attachment.filename)
            notes.append(
                f"[{label} from {who}, transcript: {transcript or '(no speech)'}]"
            )
    except Exception:
        log.exception("Could not process %s", attachment.url)
        notes.append(
            f"[{who} sent {attachment.filename}, which could not be processed]"
        )


def describe(message: discord.Message) -> str:
    parts = [message.clean_content]
    for attachment in message.attachments:
        parts.append(
            "[voice message]"
            if attachment.is_voice_message()
            else f"[attachment: {attachment.filename}]"
        )
    for embed in message.embeds:
        text = " — ".join(filter(None, [embed.title, embed.description, embed.url]))
        if text:
            parts.append(f"[embed: {text}]")
    return f"{mentions.who(message.author)}: {' '.join(parts).strip()}"


async def reply_chain(message: discord.Message, depth: int) -> list[discord.Message]:
    chain: list[discord.Message] = []
    current = message
    while current.reference and current.reference.message_id and len(chain) < depth:
        parent = current.reference.resolved
        if not isinstance(parent, discord.Message):
            try:
                parent = await current.channel.fetch_message(
                    current.reference.message_id
                )
            except discord.HTTPException:
                break
        chain.insert(0, parent)
        current = parent
    return chain


async def recent_history(
    message: discord.Message, exclude: set[int]
) -> list[discord.Message]:
    if CONTEXT_MESSAGES <= 0:
        return []
    try:
        history = [
            m
            async for m in message.channel.history(
                limit=CONTEXT_MESSAGES, before=message
            )
        ]
    except discord.HTTPException:
        return []
    return [m for m in reversed(history) if m.id not in exclude]


@dataclass
class Answer:
    text: str
    found: context.Found


def parse(state: agents.TurnState, session_id: str | None = None) -> Answer | None:
    """The turn's reply text, with the sources and images its tools recorded."""
    found = context.take_found(session_id) if session_id else context.Found()
    match state:
        case agents.Completed(text=text) if text or found.images:
            return Answer((text or "").strip(), found)
        case _:
            log.warning("Turn did not complete: %r", state)
            return None


async def render(
    reply: Answer | None, max_chars: int = MAX_REPLY_CHARS
) -> tuple[str, list[discord.File]]:
    if reply is None:
        return "Hmm, tole mi pa ni uspelo. Poskusi znova.", []

    files: list[discord.File] = []
    for i, file_id in enumerate(dict.fromkeys(reply.found.images)):
        try:
            data = await download_file(file_id)
        except Exception:
            log.exception("Could not download generated image %s", file_id)
            continue
        files.append(discord.File(io.BytesIO(data), filename=f"debeloglav_{i + 1}.png"))

    text = guard.cap(guard.trim_runaway(reply.text), max_chars)
    if not text and not files:
        text = "Hmm, na to nimam odgovora."
    if reply.found.sources:
        # <url> stops Discord from unfurling every link into an embed.
        links = [
            f"[{title.replace('[', '').replace(']', '')}](<{url}>)"
            for url, title in list(reply.found.sources.items())[:MAX_SOURCES]
        ]
        text += "\n-# Viri: " + " · ".join(links)
    return text, files


async def reply_chunks(
    message: discord.Message | discord.PartialMessage,
    text: str,
    files: list[discord.File],
) -> list[discord.Message]:
    """Reply with text split to Discord's limit, the files with the last part.

    Each part replies to the previous one.
    """
    chunks = split(text) or [""]
    sent: list[discord.Message] = []
    target = message
    for i, chunk in enumerate(chunks):
        last = i == len(chunks) - 1
        # fail_if_not_exists=False: if the message was deleted meanwhile, post the answer anyway.
        target = await message.channel.send(
            chunk or None,
            files=files if last else [],
            reference=target.to_reference(fail_if_not_exists=False),
        )
        sent.append(target)
    return sent


def split(text: str) -> list[str]:
    chunks: list[str] = []
    while len(text) > DISCORD_LIMIT:
        cut = text.rfind("\n", 0, DISCORD_LIMIT)
        if cut < DISCORD_LIMIT // 2:
            cut = text.rfind(" ", 0, DISCORD_LIMIT)
        if cut < DISCORD_LIMIT // 2:
            cut = DISCORD_LIMIT
        chunks.append(text[:cut])
        text = text[cut:].lstrip()
    if text:
        chunks.append(text)
    return chunks


def main() -> None:
    discord.utils.setup_logging()
    Debeloglav().run(os.environ["DISCORD_TOKEN"], log_handler=None)
