"""Learns how a server talks: a short style guide, refreshed daily from its recent messages."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import discord
from mistralai.client import models as api_models

from . import blocked, settings, store, usage
from .agent import client
from .mentions import who

log = logging.getLogger("debeloglav.style")

CHECK_SECONDS = 15 * 60
RETRY_AFTER = timedelta(
    hours=3
)  # after a failed run, instead of retrying at every check
MAX_PER_CHANNEL = 200
MAX_MESSAGES = 1500
MAX_CHARS = 300


def learn_every() -> timedelta:
    return timedelta(hours=settings.get_int("learn_every_hours"))


def first_window() -> timedelta:
    """How far back the first run reads; later runs read since the last one."""
    return timedelta(days=settings.get_int("style_window_days"))


def min_messages() -> int:
    return settings.get_int("style_min_messages")


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
    """Every CHECK_SECONDS, refresh the guide of each server whose guide is older than learn_every()."""
    await bot.wait_until_ready()
    missed_at: dict[str, datetime] = {}  # when the last run failed or was skipped
    while True:
        for guild in bot.guilds:
            scope = f"guild:{guild.id}"
            style = store.style(scope)
            now = datetime.now(UTC)
            if not style.enabled:
                continue
            if style.learned_at and now - style.learned_at < learn_every():
                continue
            if scope in missed_at and now - missed_at[scope] < RETRY_AFTER:
                continue
            try:
                if await learn(guild, scope, style):
                    missed_at.pop(scope, None)
                else:
                    missed_at[scope] = now
            except Exception as e:
                missed_at[scope] = now
                log.exception("Learning the style of %s failed", guild.name)
                record_style_run(scope, 0, None, f"{type(e).__name__}: {e}")
        await asyncio.sleep(CHECK_SECONDS)


async def learn(guild: discord.Guild, scope: str, style: store.Style) -> bool:
    """Refresh the guide from the messages since the last successful run. Returns whether it learned.

    A skipped run leaves learned_at alone, so the next run still reads everything since the last
    guide (at most first_window() back) instead of only what was written after the skip.
    """
    now = datetime.now(UTC)
    window = first_window()
    since = max(style.learned_at or now - window, now - window)
    transcript, count, skipped = await collect(guild, since)
    if count < min_messages():
        log.info(
            "Skipped learning the style of %s: only %d messages since the last run",
            guild.name,
            count,
        )
        record_style_run(scope, count, None, None, skipped)
        return False
    style.guide = await summarize(style.guide, transcript)
    style.learned_at = now
    store.save_style(scope, style)
    log.info("Learned the style of %s from %d messages", guild.name, count)
    record_style_run(scope, count, style.guide, None, skipped)
    return True


def record_style_run(
    scope: str,
    messages: int,
    guide: str | None,
    error: str | None,
    skipped: list[str] | None = None,
) -> None:
    """Persist a style-learning run; a database error must never stop learning, so it is only logged."""
    try:
        store.log_style_run(scope, messages, guide, error, skipped)
    except Exception:
        log.exception("Could not record the style run of %s", scope)


async def collect(guild: discord.Guild, since: datetime) -> tuple[str, int, list[str]]:
    """The server's recent human messages, grouped by channel, oldest first.

    Every text channel the bot can read is used, except the blocked ones. Also returns the
    channels that were left out, each as "#name: why", so the dashboard can show it.
    """
    sections: list[str] = []
    skipped: list[str] = []
    total = 0
    for channel in guild.text_channels:
        if blocked.channel(channel):
            skipped.append(f"#{channel.name}: blocked")
            continue
        if not readable(channel, guild.me):
            skipped.append(f"#{channel.name}: the bot can't read its history")
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
        except discord.HTTPException as e:
            skipped.append(f"#{channel.name}: {e.status} {e.text or type(e).__name__}")
            continue
        lines = lines[: MAX_MESSAGES - total]
        if lines:
            sections.append(f"#{channel.name}\n" + "\n".join(lines))
            total += len(lines)
        if total >= MAX_MESSAGES:
            break
    return "\n\n".join(sections), total, skipped


def readable(channel: discord.TextChannel, who: discord.Member | discord.Role) -> bool:
    permissions = channel.permissions_for(who)
    return permissions.view_channel and permissions.read_message_history


async def summarize(previous: str | None, transcript: str) -> str:
    model = settings.get("model")
    response = await client.chat.complete_async(
        model=model,
        temperature=0.3,
        messages=[
            {"role": "system", "content": SUMMARIZER},
            {
                "role": "user",
                "content": f"Current guide:\n{previous or '(empty)'}\n\nNew messages:\n{transcript}",
            },
        ],
    )
    usage.record_response(model, response)
    content = response.choices[0].message.content
    if isinstance(content, list):  # skip the model's thinking chunks
        content = "".join(
            c.text for c in content if isinstance(c, api_models.TextChunk)
        )
    guide = (content or "").strip()
    if not guide:
        raise RuntimeError("The model returned an empty style guide")
    return guide
