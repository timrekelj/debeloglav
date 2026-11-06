"""Debeloglav's brain: a Mistral agent that researches on the web before it answers."""

import json
import logging
import os
import re
from datetime import UTC, datetime, timedelta

import aiohttp
from mistralai import agents
from mistralai.client import Mistral
from mistralai.client import models as api_models

from . import context, store
from .context import TIMEZONE
from .scheduler import MIN_CRON_INTERVAL, cron_interval, next_cron_run

MODEL = os.environ.get("MISTRAL_MODEL", "mistral-large-4")
# The hosted web search and image generation run on a faster model: about 3x quicker than Large 4.
TOOL_MODEL = os.environ.get("TOOL_MODEL", "mistral-medium-latest")
REASONING_EFFORT = os.environ.get("REASONING_EFFORT", "low") or None
MAX_MEMORIES = 100
MAX_TASKS = 25  # per server
MAX_TASKS_PER_PERSON = 10

client = Mistral(api_key=os.environ["MISTRAL_API_KEY"])

NO_REPLY = "NO_REPLY"

QUIET = agents.Builtins(skills=False, subagents=False, programmatic_tool_calling=False)

log = logging.getLogger("debeloglav")


# --- Web ---------------------------------------------------------------------------------------


@agents.tool(model_access="direct")
async def web_search(ctx: agents.ToolCallbackContext, /, query: str) -> dict[str, object]:
    """Search the web for current or factual information. Returns a summary of what was found and its sources."""
    response = await client.beta.conversations.start_async(
        model=TOOL_MODEL,
        inputs=query,
        tools=[{"type": "web_search"}],
        store=False,
    )
    text: list[str] = []
    sources: dict[str, str] = {}  # url -> title
    for entry in response.outputs:
        if not isinstance(entry, api_models.MessageOutputEntry):
            continue
        if isinstance(entry.content, str):
            text.append(entry.content)
            continue
        for chunk in entry.content:
            if isinstance(chunk, api_models.TextChunk):
                text.append(chunk.text)
            elif isinstance(chunk, api_models.ToolReferenceChunk) and chunk.url:
                sources.setdefault(chunk.url, chunk.title)
    # The bot lists these under its reply, so the model never has to copy URLs.
    context.record(ctx, sources=sources)
    return {
        "answer": "".join(text).strip(),
        "sources": [{"title": title, "url": url} for url, title in sources.items()],
    }


# --- Images ------------------------------------------------------------------------------------


GENERATE_INSTRUCTIONS = "Use the image generation tool to create exactly the image described. Do not ask questions."
IMG_URL_RE = re.compile(r"https?://[^\s)\]\"'`}]+", re.IGNORECASE)


@agents.tool(model_access="direct")
async def generate_image(ctx: agents.ToolCallbackContext, /, prompt: str) -> str:
    """Generate an image from a detailed description. The image is attached to your reply automatically."""
    images = await _generate_images(prompt)
    if not images:
        raise agents.ToolError("The image could not be generated.")
    for data in images:
        context.record(ctx, image=data)
    return "The image is generated and will be attached to your reply."


def _tool_execution_entries(response: object, name: str) -> list[api_models.ToolExecutionEntry]:
    return [
        entry
        for entry in response.outputs  # type: ignore[attr-defined]
        if isinstance(entry, api_models.ToolExecutionEntry) and entry.name == name
    ]


def _result_image_urls(response: object) -> list[str]:
    """The image URLs the hosted image_generation tool returned in its result, in order."""
    urls: list[str] = []
    for entry in _tool_execution_entries(response, "image_generation"):
        info = getattr(entry, "info", None) or {}
        result = info.get("result")
        if not isinstance(result, str):
            continue
        try:
            data = json.loads(result)
        except ValueError:
            data = None
        url = data.get("url") if isinstance(data, dict) else None
        if isinstance(url, str) and url.startswith("http"):
            urls.append(url)
        else:
            urls.extend(IMG_URL_RE.findall(result))
    return list(dict.fromkeys(urls))


def _message_image_urls(response: object) -> list[str]:
    """The image URLs in the model's message text (markdown links), in order."""
    urls: list[str] = []
    for entry in response.outputs:  # type: ignore[attr-defined]
        if not isinstance(entry, api_models.MessageOutputEntry):
            continue
        if isinstance(entry.content, str):
            urls.extend(IMG_URL_RE.findall(entry.content))
            continue
        for chunk in entry.content:
            if isinstance(chunk, api_models.TextChunk):
                urls.extend(IMG_URL_RE.findall(chunk.text))
    return list(dict.fromkeys(urls))


def _tool_file_ids(response: object) -> list[str]:
    return [
        chunk.file_id
        for entry in response.outputs  # type: ignore[attr-defined]
        if isinstance(entry, api_models.MessageOutputEntry)
        and not isinstance(entry.content, str)
        for chunk in entry.content
        if isinstance(chunk, api_models.ToolFileChunk) and chunk.file_id
    ]


async def _download_images(urls: list[str]) -> list[bytes]:
    """Download image URLs (they are short-lived signed links) and return the bytes that worked."""
    images: list[bytes] = []
    async with aiohttp.ClientSession() as session:
        for url in urls:
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=120)) as response:
                    if response.status == 200:
                        images.append(await response.read())
                    else:
                        log.warning("Image download from %s returned HTTP %s", url, response.status)
            except Exception:
                log.exception("Could not download the image from %s", url)
    return images


async def _generate_images(prompt: str, instructions: str = GENERATE_INSTRUCTIONS) -> list[bytes]:
    """Run the hosted image_generation tool and download what it made."""
    response = await client.beta.conversations.start_async(
        model=TOOL_MODEL,
        instructions=instructions,
        inputs=prompt,
        tools=[{"type": "image_generation"}],
        store=False,
    )
    urls = _result_image_urls(response)
    if urls:
        return await _download_images(urls)
    # Older behaviour: the image came back as a file id in a ToolFileChunk.
    images: list[bytes] = []
    for file_id in dict.fromkeys(_tool_file_ids(response)):
        try:
            download = await client.files.download_async(file_id=file_id)
            images.append(await download.aread())
        except Exception:
            log.exception("Could not download the generated image file %s", file_id)
    return images or await _download_images(_message_image_urls(response))


# --- Memory ------------------------------------------------------------------------------------


@agents.tool(model_access="direct")
def remember(ctx: agents.ToolCallbackContext, /, fact: str) -> str:
    """Save a lasting fact about this server or its people, to know in every future conversation."""
    request = context.current(ctx)
    if len(store.memories(request.scope)) >= MAX_MEMORIES:
        raise agents.ToolError(
            f"Memory is full ({MAX_MEMORIES} facts). Ask which facts to forget first."
        )
    memory_id = store.add_memory(request.scope, fact.strip(), request.user_name, request.user_id)
    return f"Saved as memory #{memory_id}."


@agents.tool(model_access="direct")
def forget_memory(ctx: agents.ToolCallbackContext, /, memory_id: int) -> str:
    """Delete a saved memory by its number. Only the person who saved it, or a server admin, may."""
    request = context.current(ctx)
    saved = store.memory(request.scope, memory_id)
    if saved is None:
        raise agents.ToolError(f"There is no memory #{memory_id}.")
    if not (request.is_admin or saved.author_id == request.user_id):
        raise agents.ToolError(f"Only {saved.author} or a server admin can delete memory #{memory_id}.")
    store.delete_memory(request.scope, memory_id)
    return f"Memory #{memory_id} deleted."


def memory_prompt(scope: str) -> str:
    saved = store.memories(scope)
    if not saved:
        return "What you remember about this server: nothing yet."
    lines = "\n".join(f"#{m.id}: {m.text} (from {m.author})" for m in saved)
    return f"What you remember about this server:\n{lines}"


# --- Discord history ---------------------------------------------------------------------------


@agents.tool(model_access="direct")
async def search_discord_messages(
    ctx: agents.ToolCallbackContext,
    /,
    content: str | None = None,
    author: str | None = None,
    channel: str | None = None,
    mentions: str | None = None,
    has: list[str] | None = None,
    after: str | None = None,
    before: str | None = None,
    oldest_first: bool = False,
    by_relevance: bool = False,
) -> dict[str, object]:
    """Search this server's message history. Returns up to 25 matching messages with author, channel, time and a link.
    If more_may_exist is true, narrow the search (dates, channel, author) to see the rest.

    content: keywords to find. Keyword search, not semantic: use the words people would have typed, and try variants.
    author: who wrote it: their name, nickname, @mention or user id.
    channel: channel name, without #.
    mentions: messages that mention this person.
    has: only messages with any of: image, sound, video, file, sticker, embed, link, poll.
    after / before: local ISO dates or date-times, e.g. 2026-03-01.
    oldest_first: sort oldest first, e.g. to find when something was first said.
    by_relevance: sort by relevance instead of time.
    """
    request = context.current(ctx)
    if context.search_messages is None:
        raise agents.ToolError("Message search is not available.")
    return await context.search_messages(
        request,
        content=content,
        author=author,
        channel=channel,
        mentioned=mentions,
        has=has,
        after=after,
        before=before,
        oldest_first=oldest_first,
        by_relevance=by_relevance,
    )


@agents.tool(model_access="direct")
async def catch_up(ctx: agents.ToolCallbackContext, /, channel: str | None = None) -> dict[str, object]:
    """Read what was written in a channel since the asker last wrote there, up to 300 messages, oldest first.
    Use it to summarize what someone missed. Returns the transcript with short message ids (m1, m2, ...)
    and a links map from each id to that message's URL.

    channel: channel name, without #. Defaults to the channel this was asked in.
    """
    request = context.current(ctx)
    if context.catch_up is None:
        raise agents.ToolError("Catch-up is not available.")
    return await context.catch_up(request, channel=channel)


# --- Style -------------------------------------------------------------------------------------


def style_prompt(scope: str) -> str:
    style = store.style(scope)
    if not (style.enabled and style.guide):
        return ""
    return f"How people in this server talk (your style guide, learned from their messages):\n{style.guide}"


@agents.tool(model_access="direct")
def show_style_guide(ctx: agents.ToolCallbackContext, /) -> dict[str, object]:
    """Show the style guide you learned from this server's messages, and the learning settings."""
    style = store.style(context.current(ctx).scope)
    return {
        "enabled": style.enabled,
        "channels": style.channels or "all channels",
        "learned_at": f"{style.learned_at.astimezone(TIMEZONE):%Y-%m-%d %H:%M}" if style.learned_at else None,
        "guide": style.guide or "(nothing learned yet)",
    }


@agents.tool(model_access="direct")
def configure_style_learning(
    ctx: agents.ToolCallbackContext,
    /,
    enabled: bool | None = None,
    channels: list[str] | None = None,
    all_channels: bool = False,
    relearn: bool = False,
) -> str:
    """Change how you learn this server's way of talking. Only server admins may do this.

    enabled: turn daily learning on or off. Turning it off also deletes the learned guide.
    channels: learn only from these channel names (without #). Private channels are used too when named here.
    all_channels: learn from every channel everyone can read again (the default).
    relearn: forget the guide and learn it again from the last week, within 15 minutes.
    """
    request = context.current(ctx)
    if not request.scope.startswith("guild:"):
        raise agents.ToolError("Style learning only works in a server.")
    if not request.is_admin:
        raise agents.ToolError("Only server admins (Manage Server permission) can change this.")
    style = store.style(request.scope)
    if enabled is not None:
        style.enabled = enabled
        if not enabled:
            style.guide = None
        style.learned_at = None
    if channels:
        style.channels = [name.strip().lstrip("#").lower() for name in channels if name.strip()]
    if all_channels:
        style.channels = None
    if relearn:
        style.guide, style.learned_at = None, None
    store.save_style(request.scope, style)
    where = ", ".join(f"#{name}" for name in style.channels) if style.channels else "all channels"
    return f"Style learning is {'on' if style.enabled else 'off'}, from {where}."


# --- Scheduling --------------------------------------------------------------------------------


@agents.tool(model_access="direct")
def schedule_task(
    ctx: agents.ToolCallbackContext,
    /,
    prompt: str,
    run_at: str | None = None,
    cron: str | None = None,
    watch: bool = False,
    mention_creator: bool = True,
) -> str:
    """Schedule a task that you will run later and post in this channel: a reminder, a recurring job or a watch.

    prompt: what to do when it runs, written as an instruction to yourself, with every detail needed
        (e.g. "Remind Tim to call the dentist" or "Post a short summary of today's AI news").
    run_at: for a one-off task, the local date and time, ISO 8601 (e.g. 2026-11-06T18:30).
    cron: for a recurring task, a 5-field cron expression in local time (e.g. "0 8 * * 1-5").
    watch: True for a recurring check that posts only when something new or relevant happens.
    mention_creator: ping the person who asked when it runs (True for reminders).
    Give exactly one of run_at and cron.
    """
    request = context.current(ctx)
    if (run_at is None) == (cron is None):
        raise agents.ToolError(
            "Give exactly one of run_at (one-off) or cron (recurring)."
        )
    active = store.tasks(request.scope)
    if len(active) >= MAX_TASKS:
        raise agents.ToolError(
            f"This server already has {MAX_TASKS} tasks. Cancel one first."
        )
    if sum(t.user_id == request.user_id for t in active) >= MAX_TASKS_PER_PERSON:
        raise agents.ToolError(
            f"You already have {MAX_TASKS_PER_PERSON} tasks. Cancel one of yours first."
        )

    now = datetime.now(UTC)
    if run_at is not None:
        try:
            when = datetime.fromisoformat(run_at)
        except ValueError:
            raise agents.ToolError(
                f"{run_at!r} is not an ISO date and time, e.g. 2026-11-06T18:30."
            ) from None
        if when.tzinfo is None:
            when = when.replace(tzinfo=TIMEZONE)
        if when < now - timedelta(minutes=1):
            raise agents.ToolError("That time is in the past.")
        if when > now + timedelta(days=366):
            raise agents.ToolError("Tasks can be scheduled at most a year ahead.")
        next_run = when.astimezone(UTC)
    else:
        try:
            interval = cron_interval(cron)
        except ValueError as error:
            raise agents.ToolError(str(error)) from None
        if interval < MIN_CRON_INTERVAL:
            raise agents.ToolError(
                f"Recurring tasks can run at most every {MIN_CRON_INTERVAL.seconds // 60} minutes."
            )
        next_run = next_cron_run(cron, now)

    task_id = store.add_task(
        scope=request.scope,
        channel_id=request.channel_id,
        user_id=request.user_id,
        user_name=request.user_name,
        prompt=prompt.strip(),
        cron=cron,
        next_run=next_run,
        watch=watch,
        mention=mention_creator,
    )
    local = next_run.astimezone(TIMEZONE)
    return f"Scheduled as task #{task_id}. Next run: {local:%A %Y-%m-%d %H:%M} ({TIMEZONE.key})."


@agents.tool(model_access="direct")
def list_tasks(ctx: agents.ToolCallbackContext, /) -> list[dict[str, object]]:
    """List this server's scheduled tasks."""
    return [
        {
            "id": task.id,
            "prompt": task.prompt,
            "schedule": task.cron or "once",
            "next_run": f"{task.next_run.astimezone(TIMEZONE):%Y-%m-%d %H:%M}",
            "watch": task.watch,
            "created_by": task.user_name,
        }
        for task in store.tasks(context.current(ctx).scope)
    ]


@agents.tool(model_access="direct")
def cancel_task(ctx: agents.ToolCallbackContext, /, task_id: int) -> str:
    """Cancel a scheduled task by its number. Only the person who created it, or a server admin, may.
    A scheduled run may cancel its own task."""
    request = context.current(ctx)
    task = store.task(request.scope, task_id)
    if task is None:
        raise agents.ToolError(f"There is no active task #{task_id}.")
    if not (request.is_admin or task.user_id == request.user_id):
        raise agents.ToolError(f"Only {task.user_name} or a server admin can cancel task #{task_id}.")
    store.cancel_task(request.scope, task_id)
    return f"Task #{task_id} cancelled."


# --- Research ----------------------------------------------------------------------------------


@agents.tool(model_access="direct")
async def start_research(ctx: agents.ToolCallbackContext, /, question: str) -> str:
    """Start a deep research job in the background. The full report is posted as a reply when it is done.

    question: the research question, with every detail and constraint the person gave.
    """
    request = context.current(ctx)
    if context.launch_research is None or request.message_id is None:
        raise agents.ToolError("Research can only be started from a Discord message.")
    context.launch_research(request, question.strip())
    return "Research started. The report will be posted as a reply to the request when it is done."


INSTRUCTIONS = f"""You are Debeloglav, a witty, sharp and helpful assistant living in a Discord server, like Grok on X.
People summon you by tagging you in a message, or by replying to your messages.

How to behave:
- Answer the message that tagged you. Use the rest of the conversation only as context.
- If the tagged message is a reply with little or no question (for example "is this true?" or just a tag), fact-check or explain the message it replies to.
- Use web_search when the answer depends on recent or changing information (news, prices, releases, results, who holds a position now), niche facts, or anything you are unsure about. Answer well-established facts and common knowledge directly, without searching.
- Search efficiently, people are waiting: plan your queries first and call them all in the same step, so they run in parallel. One or two searches are usually enough; do at most two rounds. Never search just to find a URL to cite.
- Don't list sources or URLs yourself: the web pages you searched are listed under your reply automatically. Never invent sources.
- Reply with just the message to post, in Discord markdown. No JSON, no preamble.
- Be direct and concise: a few short paragraphs at most, unless someone asks for detail. Humour is welcome, but correctness comes first.
- If a claim is false or misleading, say so plainly and explain why.
- People can send you images, voice messages, videos and documents. You see images and sampled video frames directly; voice messages and video sound come to you as transcripts, and documents (PDF, Word, PowerPoint, text and code files) as their text.
- When someone asks you to draw, generate, create or imagine an image, call generate_image with a detailed English description; the image is attached to your reply automatically. Keep the text answer short then.
- People are shown as "Nickname (@username)". Nicknames change; the @username stays the same. Know people by their @username: the same @username is the same person, even under a new nickname. Call people by their current nickname.
- When you talk about someone, write their nickname as plain text, without @ and without "(@username)".
- You can't tag or ping anyone: no people, roles, @everyone or @here. If someone asks you to, say that you don't tag.

Language and tone:
- Always write in Slovenian, even when the question or the sources are in another language. Switch only when someone explicitly asks for another language.
- Talk like a friend in the chat, not like an official or a customer-support bot: casual spoken Slovenian (pogovorni jezik), always tikaj (never vikaj), short sentences, no stiff phrases like "Spoštovani", "Z veseljem vam pomagam" or "Upam, da sem vam pomagal".
- Mirror how people in this server talk: pick up their words, slang, abbreviations and humour from your style guide (when a message includes one), the recent messages and what you remember. Don't overdo it, and don't make up slang that nobody uses.
- You learn the style guide every day from the server's messages. When asked what you learned, call show_style_guide. Admins can turn learning off, limit it to channels or make you relearn with configure_style_learning.
- Keep facts, numbers and names exact; casual tone never means sloppy content.
- Use emoji sparingly: at most two or three per message, even if the server uses more. Say what you have to say once, with at most one short sign-off, then stop. Never pad, repeat yourself or keep saying goodbye.

Memory:
- Each message starts with what you remember about this server. Use it naturally.
- Call remember when someone asks you to remember something, or tells you a lasting fact meant for you (names, roles, preferences, inside jokes). Don't save trivia from casual chat.
- In memories, always name people by their @username (e.g. "@ambros is the server owner"), never only by nickname, so the memory still fits after they change it.
- When someone explains a word, nickname or expression the server uses (e.g. "pri nas 'X' pomeni Y"), remember it as a slang entry, so you understand and use it later.
- Call forget_memory when someone asks you to forget something.

Server history:
- When someone asks what was said in this server, when or who said something, or to find an old message, link or file, call search_discord_messages. Try a few keyword variants (spelling, short forms, dialect) when the first search finds nothing.
- Quote briefly, say who said it and when, and link the message. To search by person, pass their @username as author.

Catching up:
- When someone asks what they missed, for a summary of the channel, or "kaj sem zamudil", call catch_up (with the channel name if they named one).
- Summarize the main topics, decisions, plans and anything addressed to the asker, grouped by topic, concise. Say who said what by nickname.
- Link the 3 to 5 key messages as markdown links [short description](<url>), taking the URLs from the links map.
- If count is 0, say nothing happened in the channel since then.

Scheduling:
- Each message gives the current local time. Use schedule_task for reminders ("remind me in 2 hours"), recurring jobs ("every morning at 8, post the AI news") and watches ("tell me when X happens"). Convert relative times to an absolute local time. Write the task's prompt so that it makes sense on its own later.
- After scheduling, confirm briefly with the task number and when it runs. Use list_tasks and cancel_task when asked.

Background research:
- When someone asks for deep or thorough research, a detailed comparison, or a question that needs many searches, call start_research and answer only that you are on it and will reply when done. Do not research it yourself in this turn.
- Answer normal questions directly.

Scheduled runs:
- A message that starts with "[Scheduled task #N]" is a task you scheduled earlier. Do what it says and write the message to post.
- For a watch, if there is nothing new or relevant since your last report, answer exactly {NO_REPLY}. When a watch has done its job and is no longer needed, call cancel_task for it."""


def build_agent() -> agents.Agent:
    return agents.Agent(
        name="debeloglav",
        model=MODEL,
        instructions=INSTRUCTIONS,
        tools=[
            web_search,
            generate_image,
            remember,
            forget_memory,
            schedule_task,
            list_tasks,
            cancel_task,
            start_research,
            search_discord_messages,
            catch_up,
            show_style_guide,
            configure_style_learning,
        ],
        builtins=QUIET,
        reasoning_effort=REASONING_EFFORT,
        max_iterations=16,
    )


RESEARCH_INSTRUCTIONS = """You are Debeloglav's research lead. You get one research question and produce a thorough, well-sourced report.

1. Split the question into 2 to 5 independent sub-questions that together cover it.
2. Start one researcher subagent per sub-question, all at once, each with a precise task.
3. Wait for every researcher's result before you write anything.
4. If something important is missing or contradictory, run web_search yourself or start another researcher.
5. Write the report: a short direct answer first, then the findings, organised with headings and bullet points, then caveats or open questions. Point out where sources disagree.

Write in Slovenian, in a relaxed, friendly tone (tikaj, no stiff formal phrases), while keeping the content precise. Use Discord markdown (headings with ##, bullets, bold), no tables. Aim for 300 to 900 words.
End with a "Viri" section: the 3 to 8 most important URLs from the researchers' results, as markdown links written [title](<url>). Never invent sources."""

RESEARCHER_INSTRUCTIONS = """You research one sub-question on the web. Run several web_search queries from different angles, compare what you find, and check claims across sources.
Report your findings as concise facts with numbers and dates, and list the URL of the source behind each fact. Say plainly what you could not verify."""


def build_research_agent() -> agents.Agent:
    return agents.Agent(
        name="debeloglav-research",
        model=MODEL,
        instructions=RESEARCH_INSTRUCTIONS,
        tools=[web_search],
        # Subagents need programmatic tool calling: the lead starts them from code it writes.
        builtins=agents.Builtins(skills=False),
        max_iterations=30,
        subagents=[
            agents.Subagent(
                name="researcher",
                description="Researches one sub-question on the web and reports the facts with their source URLs.",
                instructions=RESEARCHER_INSTRUCTIONS,
                tools=[web_search],
                builtins=QUIET,
                max_iterations=12,
            )
        ],
        subagent_policy=agents.SubagentPolicy(
            concurrent_child_turn_limit=5, max_depth=1
        ),
    )
