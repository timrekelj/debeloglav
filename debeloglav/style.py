"""Learns how a server talks: a short style guide, refreshed daily from its recent messages."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import discord
from mistralai.client import models as api_models

from . import store
from .agent import MODEL, client
from .mentions import who

log = logging.getLogger("debeloglav.style")

LEARN_EVERY = timedelta(hours=24)
FIRST_WINDOW = timedelta(
    days=7
)  # how far back the first run reads; later runs read since the last one
CHECK_SECONDS = 15 * 60
RETRY_AFTER = timedelta(hours=3)  # after a failed run, instead of retrying at every check
MAX_PER_CHANNEL = 200
MAX_MESSAGES = 1500
MAX_CHARS = 300
MIN_MESSAGES = 20

SUMMARIZER = """You maintain a style guide that describes how the members of a Slovenian Discord server talk, so that a bot can talk like them.
You get the current guide (it may be empty) and the server's messages since the last update. Return the updated guide.

Write the guide in Slovenian, at most 350 words, with these sections:
## Ton
## Besede in sleng (word or expression: what it means or when it's used)
## Vzdevki (who is called what, by their @username, which never changes)
## Interne šale
## Emoji, kletvice in angleščina (how much and which)
## Tipični stavki (up to 10 short, typical phrases, each under 12 words)

Rules:
- Describe how people talk, not what they talked about. No news, plans or summaries of conversations.
- Keep what the current guide says unless the new messages clearly contradict it. Add what is new and used by more than one message.
- Never include personal data (addresses, phone numbers, emails, passwords, health, money matters) or anything hurtful or embarrassing about a person.
- Ignore commands and messages addressed to bots.
- Return only the guide."""


async def run_forever(bot: discord.Client) -> None:
    """Every CHECK_SECONDS, refresh the guide of each server whose guide is older than LEARN_EVERY."""
    await bot.wait_until_ready()
    failed_at: dict[str, datetime] = {}
    while True:
        for guild in bot.guilds:
            scope = f"guild:{guild.id}"
            style = store.style(scope)
            now = datetime.now(UTC)
            if not style.enabled:
                continue
            if style.learned_at and now - style.learned_at < LEARN_EVERY:
                continue
            if scope in failed_at and now - failed_at[scope] < RETRY_AFTER:
                continue
            try:
                await learn(guild, scope, style)
                failed_at.pop(scope, None)
            except Exception:
                failed_at[scope] = now
                log.exception("Learning the style of %s failed", guild.name)
        await asyncio.sleep(CHECK_SECONDS)


async def learn(guild: discord.Guild, scope: str, style: store.Style) -> None:
    now = datetime.now(UTC)
    since = max(style.learned_at or now - FIRST_WINDOW, now - FIRST_WINDOW)
    transcript, count = await collect(guild, style.channels, since)
    if count >= MIN_MESSAGES:
        style.guide = await summarize(style.guide, transcript)
        log.info("Learned the style of %s from %d messages", guild.name, count)
    style.learned_at = now
    store.save_style(scope, style)


async def collect(
    guild: discord.Guild, channels: list[str] | None, since: datetime
) -> tuple[str, int]:
    """The server's recent human messages, grouped by channel, oldest first.

    Without a channel list, only channels everyone in the server can read are used: the guide is
    shown in every channel, so it must not carry anything from private ones. An admin's explicit
    channel list is used as given.
    """
    sections: list[str] = []
    total = 0
    for channel in guild.text_channels:
        if channels and channel.name not in channels:
            continue
        if not channels and not readable(channel, guild.default_role):
            continue
        if not readable(channel, guild.me):
            continue
        lines: list[str] = []
        try:
            async for message in channel.history(
                after=since, limit=MAX_PER_CHANNEL, oldest_first=True
            ):
                text = message.clean_content.strip()
                if message.author.bot or not text:
                    continue
                lines.append(f"{who(message.author)}: {text[:MAX_CHARS]}")
        except discord.HTTPException:
            continue
        lines = lines[: MAX_MESSAGES - total]
        if lines:
            sections.append(f"#{channel.name}\n" + "\n".join(lines))
            total += len(lines)
        if total >= MAX_MESSAGES:
            break
    return "\n\n".join(sections), total


def readable(channel: discord.TextChannel, who: discord.Member | discord.Role) -> bool:
    permissions = channel.permissions_for(who)
    return permissions.view_channel and permissions.read_message_history


async def summarize(previous: str | None, transcript: str) -> str:
    response = await client.chat.complete_async(
        model=MODEL,
        temperature=0.3,
        messages=[
            {"role": "system", "content": SUMMARIZER},
            {
                "role": "user",
                "content": f"Current guide:\n{previous or '(empty)'}\n\nNew messages:\n{transcript}",
            },
        ],
    )
    content = response.choices[0].message.content
    if isinstance(content, list):  # skip the model's thinking chunks
        content = "".join(
            c.text for c in content if isinstance(c, api_models.TextChunk)
        )
    guide = (content or "").strip()
    if not guide:
        raise RuntimeError("The model returned an empty style guide")
    return guide
