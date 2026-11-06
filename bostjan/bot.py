"""Boštjan on Discord: answers every message that tags it, as a reply to that message."""

import io
import logging
import os
from datetime import date

import discord
from mistralai import agents

from . import media
from .agent import Reply, build_agent, download_file

log = logging.getLogger("bostjan")

CONTEXT_MESSAGES = int(os.environ.get("CONTEXT_MESSAGES", "10"))
REPLY_CHAIN_DEPTH = 8
MAX_IMAGES = 8
MAX_SOURCES = 5
MAX_MEDIA_BYTES = 50 * 1024 * 1024
DISCORD_LIMIT = 2000

# Never let the model's text ping anyone; only notify the person who asked.
ALLOWED_MENTIONS = discord.AllowedMentions.none()
ALLOWED_MENTIONS.replied_user = True


class Bostjan(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents, allowed_mentions=ALLOWED_MENTIONS)
        self.agent = build_agent()

    async def on_ready(self) -> None:
        log.info("Logged in as %s", self.user)

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not self.is_summoned(message):
            return
        files: list[discord.File] = []
        async with message.channel.typing():
            try:
                content = await self.build_content(message)
                state = await self.agent.run(content)
                text, files = await format_reply(state)
            except Exception:
                log.exception("Failed to answer %s", message.jump_url)
                text = "Ojoj, nekaj je šlo narobe. Poskusi znova čez trenutek."
        chunks = split(text) or [""]
        target = message
        for i, chunk in enumerate(chunks):
            last = i == len(chunks) - 1
            target = await target.reply(chunk or None, files=files if last else [])

    def is_summoned(self, message: discord.Message) -> bool:
        assert self.user is not None
        if message.guild is None:
            return True  # DMs always get an answer
        if self.user in message.mentions:
            return True
        # Tagging the bot's managed role from autocomplete also counts.
        return any(
            role.tags and role.tags.bot_id == self.user.id
            for role in message.role_mentions
        )

    async def build_content(self, message: discord.Message) -> list[dict[str, str]]:
        chain = await reply_chain(message)
        history = await recent_history(message, exclude={m.id for m in chain})

        sections = [f"Today's date: {date.today().isoformat()}"]
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

        # Media from the tagged message and the one it replies to, so "what is this?" works.
        images: list[dict[str, str]] = []
        notes: list[str] = []
        for source in [*chain[-1:], message]:
            for attachment in source.attachments:
                await read_attachment(source, attachment, images, notes)
        sections.extend(notes)

        return [{"type": "text", "text": "\n\n".join(sections)}, *images[:MAX_IMAGES]]


async def read_attachment(
    message: discord.Message,
    attachment: discord.Attachment,
    images: list[dict[str, str]],
    notes: list[str],
) -> None:
    """Add an attachment to the prompt: images as they are, voice and video as transcripts and frames."""
    kind = (attachment.content_type or "").split("/")[0]
    who = message.author.display_name
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
    return f"{message.author.display_name}: {' '.join(parts).strip()}"


async def reply_chain(message: discord.Message) -> list[discord.Message]:
    chain: list[discord.Message] = []
    current = message
    while (
        current.reference
        and current.reference.message_id
        and len(chain) < REPLY_CHAIN_DEPTH
    ):
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


async def format_reply(
    state: agents.TurnState | agents.Busy,
) -> tuple[str, list[discord.File]]:
    match state:
        case agents.Completed(output=Reply() as reply):
            text, sources, image_ids = reply.answer.strip(), reply.sources, reply.images
        case agents.Completed(text=raw) if raw:
            # The model did not follow the Reply format: post its text as it is.
            text, sources, image_ids = raw.strip(), [], []
        case _:
            log.warning("Turn did not complete: %r", state)
            return "Hmm, tole mi pa ni uspelo. Poskusi znova.", []

    files: list[discord.File] = []
    for i, file_id in enumerate(dict.fromkeys(image_ids)):
        try:
            data = await download_file(file_id)
        except Exception:
            log.exception("Could not download generated image %s", file_id)
            continue
        files.append(discord.File(io.BytesIO(data), filename=f"bostjan_{i + 1}.png"))

    if not text and not files:
        text = "Hmm, na to nimam odgovora."
    if sources:
        # <url> stops Discord from unfurling every link into an embed.
        links = [
            f"[{s.title.replace('[', '').replace(']', '')}](<{s.url}>)"
            for s in sources[:MAX_SOURCES]
        ]
        text += "\n-# Viri: " + " · ".join(links)
    return text, files


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
    Bostjan().run(os.environ["DISCORD_TOKEN"], log_handler=None)
