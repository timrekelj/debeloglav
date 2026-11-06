"""Boštjan on Discord: answers every message that tags it, as a reply to that message."""

import logging
import os
from datetime import date

import discord
from mistralai import agents

from .agent import Reply, build_agent

log = logging.getLogger("bostjan")

CONTEXT_MESSAGES = int(os.environ.get("CONTEXT_MESSAGES", "10"))
REPLY_CHAIN_DEPTH = 8
MAX_IMAGES = 4
MAX_SOURCES = 5
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
        async with message.channel.typing():
            try:
                content = await self.build_content(message)
                state = await self.agent.run(content)
                text = format_reply(state)
            except Exception:
                log.exception("Failed to answer %s", message.jump_url)
                text = "Ojoj, nekaj je šlo narobe. Poskusi znova čez trenutek."
        target = message
        for chunk in split(text):
            target = await target.reply(chunk)

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

        content: list[dict[str, str]] = [
            {"type": "text", "text": "\n\n".join(sections)}
        ]
        # Images from the tagged message and the one it replies to, so "what is this?" works.
        for source in [message, *chain[-1:]]:
            for attachment in source.attachments:
                if attachment.content_type and attachment.content_type.startswith(
                    "image/"
                ):
                    content.append(
                        {
                            "type": "image",
                            "uri": attachment.url,
                            "mediaType": attachment.content_type,
                        }
                    )
        text, *images = content
        return [text, *images[:MAX_IMAGES]]


def describe(message: discord.Message) -> str:
    parts = [message.clean_content]
    for attachment in message.attachments:
        parts.append(f"[attachment: {attachment.filename}]")
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


def format_reply(state: agents.TurnState | agents.Busy) -> str:
    match state:
        case agents.Completed(output=Reply() as reply):
            text = reply.answer.strip()
            sources = reply.sources
        case agents.Completed(text=raw) if raw:
            # The model did not follow the Reply format: post its text as it is.
            text, sources = raw.strip(), []
        case _:
            log.warning("Turn did not complete: %r", state)
            return "Hmm, tole mi pa ni uspelo. Poskusi znova."

    text = text or "Hmm, na to nimam odgovora."
    if sources:
        # <url> stops Discord from unfurling every link into an embed.
        links = [
            f"[{s.title.replace('[', '').replace(']', '')}](<{s.url}>)"
            for s in sources[:MAX_SOURCES]
        ]
        text += "\n-# Viri: " + " · ".join(links)
    return text


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
