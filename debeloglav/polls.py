"""How a poll is shown to the model in one line, from a discord.Poll or from raw API data."""

from datetime import datetime

import discord

from .context import TIMEZONE


def describe(poll: discord.Poll) -> str:
    """The question, each answer with its vote count, and whether the poll ended."""
    answers = [
        (a.text, str(a.emoji) if a.emoji else None, a.vote_count) for a in poll.answers
    ]
    return _line(
        poll.question,
        answers,
        ended=poll.is_finalized(),
        expires_at=poll.expires_at,
        multiple=poll.multiple,
    )


def describe_raw(data: dict) -> str:
    """The same line from a raw message's "poll" object, as Discord's API returns it."""
    results = data.get("results") or {}
    counts = {
        int(c["id"]): int(c.get("count") or 0)
        for c in results.get("answer_counts") or []
    }
    answers = []
    for answer in data.get("answers") or []:
        media = answer.get("poll_media") or {}
        answers.append(
            (
                media.get("text") or "",
                _emoji(media.get("emoji")),
                counts.get(int(answer.get("answer_id") or 0), 0),
            )
        )
    expiry = data.get("expiry")
    return _line(
        (data.get("question") or {}).get("text") or "",
        answers,
        ended=bool(results.get("is_finalized")),
        expires_at=datetime.fromisoformat(expiry) if expiry else None,
        multiple=bool(data.get("allow_multiselect")),
    )


def _emoji(data: dict | None) -> str | None:
    """The emoji's text: the character itself, or <:name:id> for a server emoji."""
    if not data:
        return None
    name = data.get("name") or ""
    if not data.get("id"):
        return name or None
    kind = "a" if data.get("animated") else ""
    return f"<{kind}:{name}:{data['id']}>"


def _line(
    question: str,
    answers: list[tuple[str, str | None, int]],
    *,
    ended: bool,
    expires_at: datetime | None,
    multiple: bool,
) -> str:
    parts = []
    for text, emoji, count in answers:
        label = f"{emoji} {text}" if emoji else text
        parts.append(f"{label}: {count} votes")
    if ended:
        state = "ended"
    elif expires_at:
        state = f"open until {expires_at.astimezone(TIMEZONE):%Y-%m-%d %H:%M}"
    else:
        state = "open"
    if multiple:
        state += " · multiple choice"
    return f'[poll: "{question}" — {", ".join(parts)} · {state}]'
