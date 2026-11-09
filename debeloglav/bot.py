"""Debeloglav on Discord: answers every message that tags it, as a reply to that message."""

import asyncio
import base64
import functools
import io
import logging
import os
import shutil
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import discord
from mistralai import agents
from mistralai.agents.experimental import JsonFileSessionJournal

from . import (
    blocked,
    catchup,
    commands,
    context,
    dashboard,
    discord_search,
    guard,
    media,
    mentions,
    polls,
    scheduler,
    settings,
    store,
    style,
)
from .agent import (
    NO_REPLY,
    build_agent,
    build_research_agent,
    memory_prompt,
    settings_version,
    style_prompt,
)
from .context import Request, timezone
from .trace import Trace

log = logging.getLogger("debeloglav")

REPLY_CHAIN_DEPTH = 8
MAX_IMAGES = 8
MAX_SOURCES = 5
MAX_REPLY_CHARS = 3600  # two Discord messages
MAX_REPORT_CHARS = 7600  # four, for research reports
MAX_MEDIA_BYTES = 50 * 1024 * 1024
MAX_DOCUMENT_CHARS = 40_000  # about 10k tokens of a document's text in the prompt
IMAGE_SUFFIXES = (
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".gif",
    ".bmp",
    ".heic",
    ".heif",
    ".avif",
)
DISCORD_LIMIT = 2000
THREAD_NAME_CHARS = 90  # Discord allows 100

# The Agents SDK sessions are journaled here, next to the database, so conversations survive a restart
# (in Docker both land in the mounted data/ volume).
SESSIONS_DIR = store.DB_PATH.parent / "sessions"

THINKING = "🤔"  # used where the server has no thinking_emote
RESEARCHING = "🔬"

# The bot doesn't tag anyone. Only a reply pings the person it answers, and a reminder the person
# who set it.
ALLOWED_MENTIONS = discord.AllowedMentions(
    everyone=False, roles=False, users=False, replied_user=True
)


def session_idle() -> timedelta:
    """How long a conversation stays open after its last use (the session_idle_hours setting)."""
    return timedelta(hours=settings.get_int("session_idle_hours"))


class Conversations:
    """Which session each message belongs to, so a reply to Debeloglav continues that conversation.

    The map lives in SQLite (store.conversations), so it survives a restart; only the locks are in
    memory. There is no cap on the number of messages: idle pruning below bounds the table, and
    dropping a live mapping would silently split a conversation.
    """

    def __init__(self) -> None:
        self.locks: dict[str, asyncio.Lock] = {}

    def get(self, message_id: int | None) -> str | None:
        return store.session_of(message_id) if message_id else None

    def link(self, session_id: str, *message_ids: int) -> None:
        store.link_messages(session_id, *message_ids)

    def lock(self, session_id: str) -> asyncio.Lock:
        return self.locks.setdefault(session_id, asyncio.Lock())

    def idle(self) -> list[str]:
        """Forget the sessions nobody used for a while, and return their ids to cancel."""
        cutoff = (datetime.now(UTC) - session_idle()).isoformat()
        stale = [
            session_id
            for session_id in store.sessions_idle_since(cutoff)
            if not self.lock(session_id).locked()
        ]
        for session_id in stale:
            self.locks.pop(session_id, None)
            context.forget(session_id)
            store.delete_session_rows(session_id)
        return stale


class Debeloglav(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents, allowed_mentions=ALLOWED_MENTIONS)
        self.tree = discord.app_commands.CommandTree(self)
        self.agent = build_agent()
        self._settings_version = settings_version()
        self.research_agent = build_research_agent()
        self.conversations = Conversations()
        self.background: set[asyncio.Task[None]] = set()
        self.research_jobs = 0

    async def setup_hook(self) -> None:
        context.launch_research = self.launch_research
        context.search_messages = functools.partial(discord_search.search, self)
        context.catch_up = functools.partial(catchup.catch_up, self)
        self.spawn(scheduler.run_forever(self.run_task))
        self.spawn(self.prune_sessions())
        self.spawn(style.run_forever(self))
        self.spawn(dashboard.serve(self))  # no-op unless DASHBOARD_PASSWORD is set
        commands.setup(self.tree)
        try:
            synced = await self.tree.sync()
            log.info("Synced %d slash commands", len(synced))
        except Exception:
            log.exception("Syncing the slash commands failed")

    async def on_ready(self) -> None:
        log.info("Logged in as %s", self.user)

    def spawn(self, coroutine) -> asyncio.Task[None]:
        task = asyncio.create_task(coroutine)
        self.background.add(task)
        task.add_done_callback(self.background.discard)
        return task

    # --- Answering -----------------------------------------------------------------------------

    async def on_message(self, message: discord.Message) -> None:
        if blocked.channel(message.channel):
            return  # the bot doesn't read these channels at all
        if message.author.bot or not self.is_summoned(message):
            return
        parent_id = message.reference.message_id if message.reference else None
        session_id = self.conversations.get(parent_id)
        if session_id is None and isinstance(message.channel, discord.Thread):
            # A thread opened on a message has that message's id, which is linked to the
            # conversation that answered it: a message in the thread continues it.
            session_id = self.conversations.get(message.channel.id)
        request = Request(
            scope=scope_of(message),
            channel_id=message.channel.id,
            user_id=message.author.id,
            user_name=mentions.who(message.author),
            message_id=message.id,
            is_admin=isinstance(message.author, discord.Member)
            and message.author.guild_permissions.manage_guild,
        )

        trace = Trace()
        trace.info.update(
            message_id=message.id,
            message_url=message.jump_url,
            continuing=session_id is not None,
            attachments=len(message.attachments),
        )
        thinking = thinking_emoji(message.guild)
        with trace.stage("react"):
            await react(message, thinking)
        with trace.stage("thread"):
            thread = await self.open_thread(message)
        if thread is not None:
            request.reply_channel_id = thread.id
            trace.info.update(thread_id=thread.id, thread_url=thread.jump_url)
        started = time.monotonic()
        content: list[dict[str, str]] | None = None
        error: str | None = None
        try:
            with trace.stage("context"):
                content = await self.build_content(
                    message, request, continuing=session_id is not None
                )
            try:
                session_id, state = await self.run_turn(
                    self.agent, session_id, request, content, trace=trace
                )
            except (agents.SessionTerminatedError, agents.SessionNotFoundError):
                # The conversation's session ended: start over with the full context.
                trace.step("session_restarted")
                with trace.stage("context"):
                    content = await self.build_content(
                        message, request, continuing=False
                    )
                session_id, state = await self.run_turn(
                    self.agent, None, request, content, trace=trace
                )
            with trace.stage("render"):
                text, files = await render(parse(state, session_id))
        except Exception as e:
            log.exception("Failed to answer %s", message.jump_url)
            error = f"{type(e).__name__}: {e}"
            text, files = "Ojoj, nekaj je šlo narobe. Poskusi znova čez trenutek.", []
        finally:
            with trace.stage("unreact"):
                await unreact(message, thinking, self.user)

        with trace.stage("send"):
            text = await mentions.untag(message.guild, text)
            sent = await reply_chunks(message, text, files, place=thread)
        trace.info.update(
            session_id=session_id,
            reply_messages=len(sent),
            files=len(files),
            **content_stats(content),
        )
        record_activity(
            kind="reply",
            scope=request.scope,
            channel_id=request.channel_id,
            user_name=request.user_name,
            question=message.clean_content,
            prompt=prompt_text(content) if content is not None else None,
            reply=text,
            ok=error is None,
            error=error,
            duration_ms=int((time.monotonic() - started) * 1000),
            trace=trace.as_dict(),
        )
        if session_id:
            self.conversations.link(session_id, message.id, *(m.id for m in sent))

    def apply_settings(self) -> bool:
        """Rebuild the agent when the admin settings changed, so new turns use them without a restart.

        Returns True when the agent was rebuilt. A conversation whose session is already running
        keeps the settings that session started with: the Agents SDK fixes a session's agent config
        at its start (and saves it in the session journal), and a rebuilt agent still resumes those
        sessions by id — only other tools, hooks, MCP servers, connectors or sandbox raise
        SessionConfigMismatchError, and build_agent always passes the same ones.
        """
        version = settings_version()
        if version == self._settings_version:
            return False
        self.agent = build_agent()
        self.research_agent = build_research_agent()
        self._settings_version = version
        log.info("Admin settings changed, rebuilt the agent: %s", version)
        return True

    async def run_turn(
        self,
        agent: agents.Agent,
        session_id: str | None,
        request: Request,
        content: list[dict[str, str]],
        trace: Trace | None = None,
    ) -> tuple[str, agents.TurnState]:
        """Run one turn in the conversation's session, or in a new one, with the request registered for tools.

        With a trace, the turn's steps and the time spent waiting for its session are recorded.
        """
        trace = trace or Trace()
        if self.apply_settings():
            agent = self.agent  # the caller read self.agent before the settings changed
        trace.info.update(
            model=getattr(agent, "model", None),
            reasoning_effort=getattr(agent, "reasoning_effort", None),
        )
        with trace.stage("session_start"):
            session = (
                agent.session(session_id) if session_id else await agent.start_session()
            )
        with trace.stage("lock_wait"):
            lock = self.conversations.lock(session.id)
            await lock.acquire()
        try:
            context.register(session.id, request)
            context.take_found(session.id)  # start the turn with nothing recorded
            with trace.stage("turn"):
                state = await session.run(content, on_event=trace.on_event)
                while isinstance(
                    state, agents.Busy
                ):  # a turn still runs in this session: wait, then send ours
                    trace.step("busy_wait")
                    await state.turn.result()
                    state = await session.run(content, on_event=trace.on_event)
        finally:
            lock.release()
        trace.finish(state)
        return session.id, state

    def is_summoned(self, message: discord.Message) -> bool:
        assert self.user is not None
        if message.guild is None:
            return True  # DMs always get an answer
        channel = message.channel
        if isinstance(channel, discord.Thread) and channel.owner_id == self.user.id:
            return True  # every message in a thread the bot opened is for it
        if self.user in message.mentions:
            return True
        reference = message.reference
        if (
            reference
            and isinstance(reference.resolved, discord.Message)
            and reference.resolved.author == self.user
        ):
            # A reply to Debeloglav continues the conversation, even without a ping;
            # a reply to someone else's message in that conversation doesn't.
            return True
        # Tagging the bot's managed role from autocomplete also counts.
        return any(
            role.tags and role.tags.bot_id == self.user.id
            for role in message.role_mentions
        )

    async def open_thread(self, message: discord.Message) -> discord.Thread | None:
        """The thread to answer a channel message in: a new one opened on it, or the one it has.

        None means answer inline: threads are off in the settings, the message is in a DM, a
        thread or another kind of channel, or the bot may not open a thread there.
        """
        channel = message.channel
        if settings.get("reply_in_threads") != "on" or not isinstance(
            channel, discord.TextChannel
        ):
            return None
        if message.thread is not None:
            return message.thread
        permissions = channel.permissions_for(channel.guild.me)
        if not (
            permissions.create_public_threads and permissions.send_messages_in_threads
        ):
            log.warning(
                "Can't open a thread in #%s (missing Create Public Threads or Send Messages "
                "in Threads), answering inline",
                channel.name,
            )
            return None
        try:
            return await message.create_thread(
                name=thread_name(message, channel.guild.me),
                auto_archive_duration=settings.get_int("thread_archive_hours") * 60,
            )
        except discord.HTTPException:
            log.exception(
                "Could not open a thread on %s, answering inline", message.jump_url
            )
            return None

    async def build_content(
        self, message: discord.Message, request: Request, *, continuing: bool
    ) -> list[dict[str, str]]:
        sections = [
            now_line(),
            memory_prompt(request.scope),
            style_prompt(request.scope),
        ]
        if continuing:
            # The session already has the conversation; add only what this message replies to.
            chain = await reply_chain(message, depth=1)
            if chain:
                sections.append("It replies to:\n" + describe(chain[0]))
        else:
            chain = await reply_chain(message, depth=REPLY_CHAIN_DEPTH)
            history = await recent_history(message, exclude={m.id for m in chain})
            starter = await thread_starter(message)
            if starter is not None and starter.id not in {
                m.id for m in [*chain, *history]
            }:
                sections.append(
                    "This thread was opened on this message:\n" + describe(starter)
                )
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
                "People it mentions: " + ", ".join(mentions.who(u) for u in mentioned)
            )

        # Media from the tagged message and the one it replies to, so "what is this?" works.
        images: list[dict[str, str]] = []
        notes: list[str] = []
        editable: list[context.EditableImage] = []
        for source in [*chain[-1:], message]:
            for attachment in source.attachments:
                await read_attachment(source, attachment, images, notes, editable)
        sections.extend(notes)
        if editable:
            request.editable_images = editable[:MAX_IMAGES]
            sections.append(
                "Images you can edit with edit_image:\n"
                + "\n".join(
                    f"{i + 1}: {item.label}"
                    for i, item in enumerate(request.editable_images)
                )
            )

        return [
            {"type": "text", "text": "\n\n".join(filter(None, sections))},
            *images[:MAX_IMAGES],
        ]

    # --- Background research -------------------------------------------------------------------

    def launch_research(self, request: Request, question: str) -> None:
        if self.research_jobs >= settings.get_int("max_research_jobs"):
            raise agents.ToolError(
                "Too many research jobs are running. Ask the person to try again in a few minutes."
            )
        # Runs on the bot's event loop: start_research is an async tool, so it is called there.
        self.spawn(self.research(request, question))
        self.research_jobs += 1

    async def research(self, request: Request, question: str) -> None:
        message = session = None
        started = time.monotonic()
        trace = Trace()
        prompt: str | None = None
        error: str | None = None
        try:
            channel = await self.channel(request.channel_id)
            message = channel.get_partial_message(request.message_id)
            await react(message, RESEARCHING)
            research_agent = self.research_agent
            trace.info.update(
                model=getattr(research_agent, "model", None),
                message_id=request.message_id,
            )
            with trace.stage("session_start"):
                session = await research_agent.start_session()
            trace.info["session_id"] = session.id
            prompt = f"{now_line()}\n\nResearch question from {request.user_name}:\n{question}"
            timeout = settings.get_int("research_timeout_minutes") * 60
            with trace.stage("turn"):
                async with asyncio.timeout(timeout):
                    state = await session.run(prompt, on_event=trace.on_event)
            trace.finish(state)
            text, files = await render(
                parse(state), MAX_REPORT_CHARS
            )  # lists its own sources
        except Exception as e:
            log.exception("Research failed: %s", question)
            error = f"{type(e).__name__}: {e}"
            text, files = (
                "Raziskava mi ni uspela. Poskusi znova ali vprašanje malo zoži.",
                [],
            )
        finally:
            self.research_jobs -= 1
            if session is not None:
                with suppress(Exception):
                    await session.cancel()
                delete_journal(
                    session.id
                )  # a finished research session is not continued
            if message is not None:
                await unreact(message, RESEARCHING, self.user)

        duration_ms = int((time.monotonic() - started) * 1000)
        if message is None:
            record_activity(
                kind="research",
                scope=request.scope,
                channel_id=request.channel_id,
                user_name=request.user_name,
                question=question,
                prompt=prompt,
                reply=None,
                ok=error is None,
                error=error,
                duration_ms=duration_ms,
                trace=trace.as_dict(),
            )
            return  # the channel is gone or unreachable: nowhere to post the report

        with trace.stage("send"):
            text = await mentions.untag(getattr(message, "guild", None), text)
            sent = await reply_chunks(
                message, text, files, place=await self.reply_place(request)
            )
        record_activity(
            kind="research",
            scope=request.scope,
            channel_id=request.channel_id,
            user_name=request.user_name,
            question=question,
            prompt=prompt,
            reply=text,
            ok=error is None,
            error=error,
            duration_ms=duration_ms,
            trace=trace.as_dict(),
        )
        # Replies to the report continue the conversation that asked for it.
        if request.session_id:
            self.conversations.link(request.session_id, *(m.id for m in sent))

    # --- Scheduled tasks -----------------------------------------------------------------------

    async def run_task(self, task: store.Task) -> None:
        """Run a scheduled task and record the run; a failure is recorded, then raised as before."""
        started = time.monotonic()
        trace = Trace()
        trace.info["task_id"] = task.id
        try:
            prompt, reply = await self.run_scheduled_task(task, trace)
        except Exception as e:
            record_activity(
                kind="task",
                scope=task.scope,
                channel_id=task.channel_id,
                user_name=task.user_name,
                question=task.prompt,
                prompt=None,
                reply=None,
                ok=False,
                error=f"{type(e).__name__}: {e}",
                duration_ms=int((time.monotonic() - started) * 1000),
                trace=trace.as_dict(),
            )
            raise
        record_activity(
            kind="task",
            scope=task.scope,
            channel_id=task.channel_id,
            user_name=task.user_name,
            question=task.prompt,
            prompt=prompt,
            reply=reply,
            ok=True,
            error=None,
            duration_ms=int((time.monotonic() - started) * 1000),
            trace=trace.as_dict(),
        )

    async def run_scheduled_task(
        self, task: store.Task, trace: Trace | None = None
    ) -> tuple[str, str]:
        """Run the task and post its answer; returns the prompt the model got and the text posted."""
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

        content = [{"type": "text", "text": "\n\n".join(filter(None, prompt))}]
        session_id, state = await self.run_turn(
            self.agent, None, request, content, trace=trace
        )
        sent_prompt = prompt_text(content)
        reply = parse(state, session_id)
        if reply is not None and reply.text.startswith(NO_REPLY):
            await self.end_session(session_id)
            return sent_prompt, NO_REPLY
        text, files = await render(reply)
        if task.watch and reply is not None:
            store.set_last_report(task.id, reply.text)

        text = await mentions.untag(getattr(channel, "guild", None), text)
        pings = discord.AllowedMentions.none()
        if task.mention:
            text = f"<@{task.user_id}> {text}"
            pings = discord.AllowedMentions(users=[discord.Object(task.user_id)])
        chunks = split(text) or [""]
        sent = [await channel.send(c, allowed_mentions=pings) for c in chunks[:-1]]
        sent.append(
            await channel.send(chunks[-1] or None, files=files, allowed_mentions=pings)
        )
        # People can reply to a scheduled post to follow up on it.
        self.conversations.link(session_id, *(m.id for m in sent))
        return sent_prompt, text

    async def prune_sessions(self) -> None:
        while True:
            for session_id in self.conversations.idle():
                await self.end_session(session_id)
            self.prune_orphan_journals()  # also once at startup, before the first sleep
            await asyncio.sleep(10 * 60)

    def prune_orphan_journals(self) -> None:
        """Delete saved journals that no stored message points at any more.

        A turn that raises before it is linked, or a crash mid-turn, leaves a journal behind.
        A directory goes only when the conversations table does not know its session, nobody
        holds its lock, and it has not been touched for session_idle() — so running
        turns, research jobs and fresh sessions are never deleted.
        """
        if not SESSIONS_DIR.is_dir():
            return
        known = store.session_ids()
        cutoff = datetime.now(UTC) - session_idle()
        for entry in SESSIONS_DIR.iterdir():
            if not entry.is_dir() or entry.name in known:
                continue
            lock = self.conversations.locks.get(entry.name)
            if lock is not None and lock.locked():
                continue
            if datetime.fromtimestamp(entry.stat().st_mtime, UTC) > cutoff:
                continue
            log.info("Deleting orphaned session journal %s", entry.name)
            delete_journal(entry.name)

    async def end_session(self, session_id: str) -> None:
        self.conversations.locks.pop(session_id, None)
        context.forget(session_id)
        store.delete_session_rows(session_id)
        with suppress(Exception):
            await self.agent.session(session_id).cancel()
        delete_journal(session_id)

    async def reply_place(self, request: Request) -> discord.abc.Messageable | None:
        """The thread opened for the request, to post a late answer in; None replies inline."""
        if request.reply_channel_id is None:
            return None
        try:
            return await self.channel(request.reply_channel_id)
        except discord.HTTPException:
            return None  # the thread is gone: reply to the message instead

    async def channel(self, channel_id: int) -> discord.abc.Messageable:
        channel = self.get_channel(channel_id) or await self.fetch_channel(channel_id)
        assert isinstance(channel, discord.abc.Messageable)
        return channel


# --- Helpers -----------------------------------------------------------------------------------


def delete_journal(session_id: str) -> None:
    """Delete a session's journal directory under SESSIONS_DIR, so pruned sessions leave nothing behind."""
    if session_id in {"", ".", ".."} or Path(session_id).name != session_id:
        log.warning("Refusing to delete a journal for session id %r", session_id)
        return
    with suppress(OSError):  # already gone
        shutil.rmtree(SESSIONS_DIR / session_id)


def scope_of(message: discord.Message) -> str:
    return f"guild:{message.guild.id}" if message.guild else f"dm:{message.author.id}"


def now_line() -> str:
    return f"Current local time: {datetime.now(timezone()):%A %Y-%m-%d %H:%M} ({timezone().key})"


def thinking_emoji(guild: discord.Guild | None) -> discord.Emoji | str:
    """The server's thinking_emote (a setting) if it has one the bot can use, else 🤔."""
    name = settings.get("thinking_emote")
    emoji = discord.utils.get(guild.emojis, name=name) if guild else None
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
    editable: list[context.EditableImage] | None = None,
) -> None:
    """Add an attachment to the prompt: images as they are, voice and video as transcripts and frames,
    documents as their text. Real images also go to `editable`, so edit_image can edit them."""
    kind = (attachment.content_type or "").split("/")[0]
    if not kind and attachment.filename.lower().endswith(IMAGE_SUFFIXES):
        kind = "image"  # Discord didn't say what it is
    who = mentions.who(message.author)
    if kind == "image":
        # Sent as data: the model can't always download a Discord link, or a big phone screenshot.
        try:
            if attachment.size > MAX_MEDIA_BYTES:
                raise ValueError("too large")
            jpeg = await media.image(await attachment.read(), attachment.filename)
        except Exception:
            log.exception("Could not read image %s", attachment.filename)
            notes.append(
                f"[{who} sent the image {attachment.filename}, but it could not be opened]"
            )
            return
        images.append(
            {
                "type": "image",
                "uri": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode(),
                "mediaType": "image/jpeg",
            }
        )
        if editable is not None:
            editable.append(
                context.EditableImage(
                    data=jpeg, label=f"{attachment.filename} from {who}"
                )
            )
        return
    document = media.is_document(attachment.filename, attachment.content_type)
    if (
        kind not in ("audio", "video")
        and not attachment.is_voice_message()
        and not document
    ):
        return
    if attachment.size > MAX_MEDIA_BYTES:
        notes.append(f"[{who} sent {attachment.filename}, too large to process]")
        return

    try:
        data = await attachment.read()
        if document:
            text = await media.document_text(data, attachment.filename)
            if len(text) > MAX_DOCUMENT_CHARS:
                text = (
                    text[:MAX_DOCUMENT_CHARS]
                    + "\n[… the rest of the document is cut off]"
                )
            notes.append(
                f"[Document {attachment.filename} from {who}, its text:\n{text or '(no text)'}\n]"
            )
        elif kind == "video":
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
    if message.poll:
        parts.append(polls.describe(message.poll))
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
    limit = settings.get_int("context_messages")
    if limit <= 0:
        return []
    try:
        history = [
            m async for m in message.channel.history(limit=limit, before=message)
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
    for i, image in enumerate(reply.found.images):
        # Generated images are JPEG; anything else is kept as PNG.
        suffix = ".jpg" if image.data[:2] == b"\xff\xd8" else ".png"
        files.append(
            discord.File(io.BytesIO(image.data), filename=f"debeloglav_{i + 1}{suffix}")
        )

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
    if any(image.regenerated for image in reply.found.images):
        text += "\n-# Slike ni bilo mogoče urediti, zato je ustvarjena na novo."
    return text, files


async def reply_chunks(
    message: discord.Message | discord.PartialMessage,
    text: str,
    files: list[discord.File],
    place: discord.abc.Messageable | None = None,
) -> list[discord.Message]:
    """Reply with text split to Discord's limit, the files with the last part.

    Each part replies to the previous one. With a place (the thread opened on the message), the
    answer is posted there: its first part starts the thread instead of replying, as a reply
    can't point to a message in another channel.
    """
    chunks = split(text) or [""]
    sent: list[discord.Message] = []
    channel = place if place is not None else message.channel
    target = None if place is not None else message
    for i, chunk in enumerate(chunks):
        last = i == len(chunks) - 1
        # fail_if_not_exists=False: if the message was deleted meanwhile, post the answer anyway.
        reference = target.to_reference(fail_if_not_exists=False) if target else None
        target = await channel.send(
            chunk or None,
            files=files if last else [],
            reference=reference,
        )
        sent.append(target)
    return sent


def thread_name(message: discord.Message, me: discord.Member) -> str:
    """A thread's name: the question, without the bot's own tag, cut to THREAD_NAME_CHARS."""
    text = message.clean_content
    for name in {me.display_name, me.name}:
        text = text.replace(f"@{name}", " ")
    text = " ".join(text.split()).strip(" ,:")
    if len(text) > THREAD_NAME_CHARS:
        text = text[: THREAD_NAME_CHARS - 1].rstrip() + "…"
    return text or "Debeloglav"


async def thread_starter(message: discord.Message) -> discord.Message | None:
    """The message a thread was opened on, which the thread's own history leaves out."""
    thread = message.channel
    if not isinstance(thread, discord.Thread):
        return None
    if thread.starter_message is not None:
        return thread.starter_message
    parent = thread.parent
    if not isinstance(parent, discord.TextChannel):
        return None  # a forum post's first message is in the thread itself
    try:
        return await parent.fetch_message(thread.id)
    except discord.HTTPException:
        return None


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


def prompt_text(content: list[dict[str, str]] | str) -> str:
    """The text the model received for a turn, with a note of how many images came with it."""
    if isinstance(content, str):
        return content
    text = "\n\n".join(part["text"] for part in content if part.get("type") == "text")
    images = sum(1 for part in content if part.get("type") == "image")
    if images:
        text += f"\n\n[+{images} images]"
    return text


def content_stats(content: list[dict[str, str]] | None) -> dict[str, int]:
    """How big the turn's input was: characters of text and the number of images."""
    if not content:
        return {}
    return {
        "prompt_chars": sum(
            len(p.get("text", "")) for p in content if p.get("type") == "text"
        ),
        "images": sum(1 for p in content if p.get("type") == "image"),
    }


def record_activity(
    *,
    kind: str,
    scope: str,
    channel_id: int | None = None,
    user_name: str | None = None,
    question: str | None = None,
    prompt: str | None = None,
    reply: str | None = None,
    ok: bool = True,
    error: str | None = None,
    duration_ms: int | None = None,
    trace: dict | None = None,
) -> None:
    """Record what the bot just did. A database error must never break answering, so it is only logged."""
    try:
        store.log_activity(
            kind=kind,
            scope=scope,
            channel_id=channel_id,
            user_name=user_name,
            question=question,
            prompt=prompt,
            reply=reply,
            ok=ok,
            error=error,
            duration_ms=duration_ms,
            trace=trace,
        )
    except Exception:
        log.exception("Could not record the activity")


async def amain() -> None:
    """Run the bot with every session journaled to SESSIONS_DIR, so conversations survive a restart."""
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    client = Debeloglav()
    async with agents.local_runtime(
        journal_factory=JsonFileSessionJournal.in_directory(SESSIONS_DIR)
    ):
        await client.start(os.environ["DISCORD_TOKEN"])


def main() -> None:
    discord.utils.setup_logging()
    asyncio.run(amain())
