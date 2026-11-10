"""The status of the bot's recent requests in a server: what was asked, how it ended, and why it failed.

It reads the activity log the dashboard shows, so people can ask the bot in Discord what happened
to their research or why it didn't answer. Only this server's rows are shown, and the question,
reply and failure details of a channel that isn't shareable where the answer is posted are left out.
"""

import discord
from mistralai import agents

from . import blocked, discord_search, store
from .context import Request, timezone

KINDS = ("reply", "research", "task")
MAX_ROWS = 20
SCANNED_ROWS = 1000  # the newest rows of all servers, searched for this server's
MAX_CHARS = 300


def clip(text: str | None, limit: int = MAX_CHARS) -> str | None:
    if not text:
        return None
    return text if len(text) <= limit else text[: limit - 1] + "…"


def problems(trace: dict) -> dict[str, object]:
    """What a failed request's trace says went wrong: the turn error, failed tools, unfinished researchers."""
    found: dict[str, object] = {}
    if turn_error := trace.get("turn_error"):
        found["turn_error"] = turn_error
    if trace.get("stop_reason"):
        found["stop_reason"] = trace["stop_reason"]
    steps = trace.get("steps", [])
    failed = [
        {
            "agent": s.get("agent") or "lead",
            "tool": (s.get("name") or "?").rsplit(".", 1)[-1],
            "error": clip(s.get("detail"), 200),
        }
        for s in steps
        if s.get("kind") == "tool_result" and s.get("error")
    ]
    if failed:
        found["failed_tool_calls"] = failed[:5]
    subagents = [s["name"] for s in steps if s.get("kind") == "subagent_started"]
    if subagents:
        found["researchers"] = len(subagents)
        unfinished = [
            s["name"] for s in steps if s.get("kind") == "subagent_unfinished"
        ]
        if unfinished:
            found["researchers_still_running_at_the_end"] = unfinished
    if stages := trace.get("stages"):
        found["seconds_per_stage"] = {k: round(v / 1000, 1) for k, v in stages.items()}
    return found


def describe(
    row: store.Activity, *, shown: bool, place_name: str | None
) -> dict[str, object]:
    item: dict[str, object] = {
        "id": row.id,
        "time": f"{row.at.astimezone(timezone()):%Y-%m-%d %H:%M}",
        "kind": row.kind,
        "asked_by": row.user_name,
        "channel": place_name if shown else "a private channel",
        "status": "ok" if row.ok else "failed",
        "seconds": round(row.duration_ms / 1000, 1)
        if row.duration_ms is not None
        else None,
    }
    if not shown:
        return item
    item["question"] = clip(row.question)
    if row.ok:
        item["reply"] = clip(row.reply)
    else:
        item["error"] = clip(row.error)
        item["details"] = problems(row.trace)
    return item


async def request_status(
    bot: discord.Client,
    request: Request,
    limit: int = 5,
    kind: str | None = None,
    failed_only: bool = False,
    asked_by: str | None = None,
) -> dict[str, object]:
    if kind is not None and kind not in KINDS:
        raise agents.ToolError(f"kind must be one of: {', '.join(KINDS)}.")
    limit = max(1, min(limit, MAX_ROWS))
    guild = (
        bot.get_guild(int(request.scope.removeprefix("guild:")))
        if request.scope.startswith("guild:")
        else None
    )
    asker = None
    if guild is not None:
        asker = guild.get_member(request.user_id)
        if asker is None:
            try:
                asker = await guild.fetch_member(request.user_id)
            except discord.HTTPException:
                asker = None

    rows = []
    for row in store.recent_activity(SCANNED_ROWS):
        if row.scope != request.scope or (kind and row.kind != kind):
            continue
        if failed_only and row.ok:
            continue
        if (
            asked_by
            and asked_by.casefold().lstrip("@") not in (row.user_name or "").casefold()
        ):
            continue
        rows.append(row)
        if len(rows) >= limit:
            break

    results = []
    for row in rows:
        shown, place_name = True, None
        if guild is not None:
            place = (
                guild.get_channel_or_thread(row.channel_id) if row.channel_id else None
            )
            shown = (
                place is not None
                and asker is not None
                and discord_search.readable(place, asker)
                and not blocked.channel(place)
                and discord_search.shareable(guild, place, request)
            )
            place_name = f"#{place.name}" if place is not None else None
        results.append(describe(row, shown=shown, place_name=place_name))
    return {"requests": results, "newest_first": True}
