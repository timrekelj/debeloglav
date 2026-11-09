"""The admin dashboard: a small password-protected web UI served by the bot itself.

Enabled only when DASHBOARD_PASSWORD is set (then it listens on DASHBOARD_HOST:DASHBOARD_PORT,
127.0.0.1:8080 by default). HTTP Basic auth with any username, and a simple CSRF guard: a POST
whose Origin/Referer host is not the Host header is rejected. Server-rendered HTML with inline
CSS, plain forms that POST and answer 303, no JavaScript framework. Failed logins are
rate-limited per client IP and globally (429 with Retry-After), and every response carries
clickjacking/cache hardening headers.
"""

import asyncio
import base64
import hmac
import html
import logging
import math
import os
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import NoReturn
from urllib.parse import urlencode, urlsplit

import discord
from aiohttp import web

from . import agent, blocked, store, style
from .context import TIMEZONE

log = logging.getLogger("debeloglav.dashboard")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8080
REALM = "debeloglav"
KINDS = ("reply", "research", "task")

BOT = web.AppKey("bot", object)
PASSWORD = web.AppKey("password", str | None)
STARTED = web.AppKey("started", float)

LAYOUT_CSS = """
body { font-family: system-ui, sans-serif; margin: 0; background: #f6f7f9; color: #1c1e21; }
nav { display: flex; gap: 4px; background: #2b2f36; padding: 8px 12px; flex-wrap: wrap; }
nav a { color: #cfd4dc; text-decoration: none; padding: 4px 10px; border-radius: 6px; }
nav a:hover { background: #3a3f47; }
nav a.here { background: #4c6ef5; color: #fff; }
main { padding: 16px 20px; max-width: 1150px; margin: 0 auto; }
table { border-collapse: collapse; width: 100%; background: #fff; margin-bottom: 8px; }
th, td { border: 1px solid #dde1e6; padding: 6px 8px; text-align: left; vertical-align: top;
         font-size: 14px; }
th { background: #eef0f3; }
pre { background: #fff; border: 1px solid #dde1e6; padding: 10px; overflow-x: auto;
      white-space: pre-wrap; word-break: break-word; font-size: 12.5px; margin: 4px 0; }
form.inline { display: inline; }
button { font: inherit; padding: 3px 10px; border: 1px solid #b9c0c9; background: #fff;
         border-radius: 6px; cursor: pointer; }
button:hover { background: #eef0f3; }
input[type=text], select, textarea { font: inherit; padding: 4px 6px; border: 1px solid #b9c0c9;
                                      border-radius: 6px; }
textarea { width: 100%; min-height: 90px; }
label { display: block; margin: 8px 0 2px; font-weight: 600; font-size: 13px; }
.error { background: #fdecea; border: 1px solid #f5c6c0; padding: 8px 12px; border-radius: 6px; }
.note { color: #5f6672; font-size: 13px; }
section { margin-bottom: 28px; }
h2 { font-size: 18px; margin-bottom: 6px; }
h3 { font-size: 15px; margin-bottom: 4px; }
code { background: #eef0f3; padding: 1px 4px; border-radius: 4px; }
td.ok { color: #2b8a3e; }
td.err { color: #c92a2a; }
details { margin: 4px 0; }
"""

NAV = [
    ("/", "Overview"),
    ("/requests", "Requests"),
    ("/prompts", "Prompts"),
    ("/learning", "Learning"),
    ("/memories", "Memories"),
    ("/channels", "Channels"),
    ("/tasks", "Tasks"),
    ("/settings", "Settings"),
]


def e(text: object) -> str:
    """Escape anything that came from the database or the user, for HTML."""
    return html.escape(str(text) if text is not None else "")


def fmt_time(at: datetime | None) -> str:
    """A UTC timestamp in the bot's local timezone, as the admin sees it in Discord."""
    return at.astimezone(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S") if at else "—"


def snippet(text: str | None, limit: int = 80) -> str:
    text = (text or "").strip().replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def guild_id_of(scope: str) -> int | None:
    if scope.startswith("guild:"):
        try:
            return int(scope.split(":", 1)[1])
        except ValueError:
            return None
    return None


def scope_label(bot: object, scope: str) -> str:
    guild_id = guild_id_of(scope)
    if guild_id is not None:
        guild = bot.get_guild(guild_id)  # type: ignore[attr-defined]
        if guild is not None:
            return f"{guild.name} ({scope})"
    return scope


def guild_label(bot: object, guild_id: int) -> str:
    guild = bot.get_guild(guild_id)  # type: ignore[attr-defined]
    return f"{guild.name} ({guild_id})" if guild is not None else str(guild_id)


def channel_label(bot: object, channel_id: int | None) -> str:
    if channel_id is None:
        return "—"
    channel = bot.get_channel(channel_id)  # type: ignore[attr-defined]
    if channel is None:
        return str(channel_id)
    name = getattr(channel, "name", None) or str(channel_id)
    prefix = "" if isinstance(channel, discord.CategoryChannel) else "#"
    return f"{prefix}{name} ({channel_id})"


def redirect(location: str) -> NoReturn:
    """303 back to a page of this dashboard (never to another site)."""
    if not location.startswith("/") or location.startswith("//"):
        location = "/"
    raise web.HTTPSeeOther(location=location)


def page(title: str, body: str, current: str = "/") -> web.Response:
    links = []
    for href, label in NAV:
        here = ' class="here"' if href == current else ""
        links.append(f'<a href="{e(href)}"{here}>{e(label)}</a>')
    nav = "".join(links)
    return web.Response(
        text="<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>"
        f"<title>{e(title)} · Debeloglav dashboard</title>"
        f"<style>{LAYOUT_CSS}</style></head><body>"
        f"<nav>{nav}</nav><main><h1>{e(title)}</h1>{body}</main></body></html>",
        content_type="text/html",
    )


# --- Security ------------------------------------------------------------------------------------

# Brute-force protection for the Basic auth: after PER_IP_FAILURE_LIMIT failed logins from one
# client IP within FAILURE_WINDOW seconds, that IP is locked out for LOCKOUT_SECONDS (429 with
# Retry-After, checked before the password, so a correct guess during the lockout does not help).
# More than GLOBAL_FAILURE_LIMIT failures from all IPs together lock out every request for the
# same time, except ones with the correct password. The client IP is the TCP peer, unless the
# peer is listed in DASHBOARD_TRUSTED_PROXIES: then it is the right-most X-Forwarded-For entry
# that is not itself a trusted proxy (without a trusted proxy the header is ignored, it is
# spoofable).
PER_IP_FAILURE_LIMIT = 5
GLOBAL_FAILURE_LIMIT = 50
FAILURE_WINDOW = 900.0  # seconds a failed login still counts (15 minutes)
LOCKOUT_SECONDS = 900.0  # seconds a lockout lasts (15 minutes)

# Hardening headers sent with every response. The CSP keeps the inline <style> (and any inline
# script) working while blocking framing and external loads.
SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    ),
    "Cache-Control": "no-store",
    "Referrer-Policy": "same-origin",
    "X-Content-Type-Options": "nosniff",
}


class LoginLimiter:
    """In-memory failed-login rate limiting, per client IP and globally.

    `clock` is a callable returning the current time in seconds (time.monotonic by default),
    so tests can fake time instead of sleeping.
    """

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        trusted_proxies: frozenset[str] = frozenset(),
    ) -> None:
        self._clock = clock
        self.trusted_proxies = frozenset(trusted_proxies)
        self._failures: dict[
            str, list[float]
        ] = {}  # ip -> failure times inside the window
        self._locks: dict[str, float] = {}  # ip -> lockout expiry
        self._global: list[float] = []  # every failure time inside the window
        self._global_lock_until = 0.0

    def now(self) -> float:
        return float(self._clock())

    def client_ip(self, request: web.Request) -> str:
        """The IP failures count against: the peer, or behind a trusted proxy the right-most
        X-Forwarded-For entry that is not itself a trusted proxy."""
        peer = request.remote or "unknown"
        if peer not in self.trusted_proxies:
            return peer
        for entry in reversed(request.headers.get("X-Forwarded-For", "").split(",")):
            ip = entry.strip()
            if ip and ip not in self.trusted_proxies:
                return ip
        return peer

    def _prune(self, now: float) -> None:
        """Forget failures and lockouts older than the window, so memory stays bounded."""
        cutoff = now - FAILURE_WINDOW
        self._global = [at for at in self._global if at > cutoff]
        for ip in list(self._failures):
            recent = [at for at in self._failures[ip] if at > cutoff]
            if recent:
                self._failures[ip] = recent
            else:
                del self._failures[ip]
        for ip in list(self._locks):
            if self._locks[ip] <= now:
                del self._locks[ip]
                self._failures.pop(ip, None)  # the lockout is over, start fresh

    def ip_retry_after(self, ip: str) -> int | None:
        """How many more seconds this IP is locked out, or None if it may try again."""
        now = self.now()
        self._prune(now)
        until = self._locks.get(ip)
        return None if until is None else max(1, math.ceil(until - now))

    def global_retry_after(self) -> int | None:
        """How many more seconds the global lockout lasts, or None if it is over."""
        now = self.now()
        self._prune(now)
        if self._global_lock_until <= now:
            return None
        return max(1, math.ceil(self._global_lock_until - now))

    def record_failure(self, ip: str) -> None:
        """Count one failed login (logs the client IP, never the attempted password)."""
        now = self.now()
        self._prune(now)
        self._failures.setdefault(ip, []).append(now)
        self._global.append(now)
        log.warning(
            "dashboard login failed from %s (%d failures within %ds)",
            ip,
            len(self._failures[ip]),
            int(FAILURE_WINDOW),
        )
        if (
            len(self._failures[ip]) >= PER_IP_FAILURE_LIMIT
            and self._locks.get(ip, 0.0) <= now
        ):
            self._locks[ip] = now + LOCKOUT_SECONDS
            log.warning(
                "dashboard lockout for %s: %d failed logins, locked for %ds",
                ip,
                PER_IP_FAILURE_LIMIT,
                int(LOCKOUT_SECONDS),
            )
        if len(self._global) > GLOBAL_FAILURE_LIMIT and self._global_lock_until <= now:
            self._global_lock_until = now + LOCKOUT_SECONDS
            log.warning(
                "dashboard global lockout: more than %d failed logins from all IPs, locked "
                "for %ds (only the correct password gets through)",
                GLOBAL_FAILURE_LIMIT,
                int(LOCKOUT_SECONDS),
            )

    def record_success(self, ip: str) -> None:
        """A successful login clears that IP's failure count."""
        self._failures.pop(ip, None)


LIMITER = web.AppKey("login_limiter", LoginLimiter)


def _trusted_proxies() -> frozenset[str]:
    """DASHBOARD_TRUSTED_PROXIES: comma-separated proxy IPs whose X-Forwarded-For we trust."""
    raw = os.environ.get("DASHBOARD_TRUSTED_PROXIES", "")
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def _authorized(request: web.Request, password: str) -> bool:
    """HTTP Basic auth: any username, the password compared in constant time."""
    scheme, _, encoded = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() != "basic" or not encoded:
        return False
    try:
        decoded = base64.b64decode(encoded.strip(), validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return False
    _, _, given = decoded.partition(":")
    return hmac.compare_digest(given.encode(), password.encode())


def _same_origin(request: web.Request) -> bool:
    """A POST must come from a page we served: its Origin/Referer host matches the Host header."""
    for header in ("Origin", "Referer"):
        value = request.headers.get(header)
        if value:
            return urlsplit(value).netloc.lower() == request.host.lower()
    return True  # no header at all: not a browser form, e.g. curl


def _too_many_requests(retry_after: int) -> web.Response:
    return web.Response(
        status=429,
        text="Too many failed login attempts, try again later.",
        headers={"Retry-After": str(retry_after)},
    )


@web.middleware
async def _guard(request: web.Request, handler):
    password = request.app[PASSWORD]
    if not password:
        return web.Response(
            status=503,
            text="The admin dashboard is disabled (DASHBOARD_PASSWORD is not set).",
        )
    limiter: LoginLimiter = request.app[LIMITER]
    ip = limiter.client_ip(request)
    retry = limiter.ip_retry_after(ip)
    if retry is not None:
        return _too_many_requests(
            retry
        )  # locked IP: refused before the password is checked
    global_retry = limiter.global_retry_after()
    if global_retry is not None:
        # Global lockout against distributed guessing: only the correct password gets through,
        # and failures are not counted while it lasts.
        if not _authorized(request, password):
            return _too_many_requests(global_retry)
        limiter.record_success(ip)
    elif not _authorized(request, password):
        if "Authorization" in request.headers:
            limiter.record_failure(
                ip
            )  # wrong or malformed credentials count as failures
        return web.Response(
            status=401,
            text="Unauthorized",
            headers={"WWW-Authenticate": f'Basic realm="{REALM}"'},
        )
    else:
        limiter.record_success(ip)
    if request.method == "POST" and not _same_origin(request):
        return web.Response(status=403, text="Cross-origin POST rejected.")
    return await handler(request)


def make_app(
    bot: object,
    password: str | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> web.Application:
    """Build the dashboard app for this bot; password defaults to DASHBOARD_PASSWORD.

    `clock` is the failed-login limiter's time source (tests pass a fake one).
    """
    if password is None:
        password = os.environ.get("DASHBOARD_PASSWORD") or None
    app = web.Application(middlewares=[_guard])
    app[BOT] = bot
    app[PASSWORD] = password
    app[STARTED] = time.monotonic()
    app[LIMITER] = LoginLimiter(clock=clock, trusted_proxies=_trusted_proxies())

    async def _add_security_headers(
        request: web.Request, response: web.StreamResponse
    ) -> None:
        for name, value in SECURITY_HEADERS.items():
            response.headers[name] = value

    app.on_response_prepare.append(_add_security_headers)  # fires for every response
    app.router.add_get("/", overview, name="overview")
    app.router.add_get("/requests", requests_page, name="requests")
    app.router.add_get("/requests/{id}", request_detail, name="request_detail")
    app.router.add_get("/prompts", prompts_page, name="prompts")
    app.router.add_get("/learning", learning_page, name="learning")
    app.router.add_post("/learning/action", learning_action, name="learning_action")
    app.router.add_get("/memories", memories_page, name="memories")
    app.router.add_post("/memories/add", memory_add, name="memory_add")
    app.router.add_post("/memories/delete", memory_delete, name="memory_delete")
    app.router.add_get("/channels", channels_page, name="channels")
    app.router.add_post("/channels/block", channel_block, name="channel_block")
    app.router.add_post("/channels/unblock", channel_unblock, name="channel_unblock")
    app.router.add_get("/tasks", tasks_page, name="tasks")
    app.router.add_post("/tasks/cancel", task_cancel, name="task_cancel")
    app.router.add_get("/settings", settings_page, name="settings")
    app.router.add_post("/settings", settings_post, name="settings_post")
    return app


async def serve(bot: object) -> None:
    """Serve the admin dashboard next to the bot, until the task is cancelled on shutdown.

    Without DASHBOARD_PASSWORD it only logs once that it is disabled and returns.
    """
    password = os.environ.get("DASHBOARD_PASSWORD") or None
    if not password:
        log.info("DASHBOARD_PASSWORD is not set, the admin dashboard is disabled")
        return
    host = os.environ.get("DASHBOARD_HOST", DEFAULT_HOST)
    port = int(os.environ.get("DASHBOARD_PORT", str(DEFAULT_PORT)))
    app = make_app(bot, password)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    log.info("Admin dashboard listening on http://%s:%d", host, port)
    try:
        await asyncio.Event().wait()  # until the bot's shutdown cancels this task
    finally:
        await runner.cleanup()


# --- Overview ------------------------------------------------------------------------------------


async def overview(request: web.Request) -> web.Response:
    bot = request.app[BOT]
    now = datetime.now(UTC)
    day_ago = now - timedelta(hours=24)
    activity = store.recent_activity(store.ACTIVITY_LIMIT)
    last_day = [a for a in activity if a.at >= day_ago]
    errors = [a for a in last_day if not a.ok]

    last_run: dict[str, store.StyleRun] = {}
    for run in store.style_runs(limit=500):  # newest first
        last_run.setdefault(run.scope, run)

    user = getattr(bot, "user", None)
    user_name = getattr(user, "name", None) or str(user) if user else "(not logged in)"
    latency = getattr(bot, "latency", None)
    uptime = timedelta(seconds=int(time.monotonic() - request.app[STARTED]))
    guilds = getattr(bot, "guilds", [])

    rows = "".join(
        f"<tr><td>{e(scope_label(bot, scope))}</td><td>{e(fmt_time(run.at))}</td>"
        f"<td>{run.messages}</td><td>{e(run_status(run))}</td></tr>"
        for scope, run in sorted(last_run.items())
    )
    error_rows = "".join(
        f"<tr><td>{e(fmt_time(a.at))}</td><td>{e(a.kind)}</td>"
        f'<td><a href="/requests/{a.id}">#{a.id}</a></td>'
        f"<td>{e(scope_label(bot, a.scope))}</td><td>{e(snippet(a.error, 120))}</td></tr>"
        for a in [a for a in activity if not a.ok][:5]
    )
    body = f"""
<section><h2>Bot</h2><table>
<tr><th>User</th><td>{e(user_name)} (id {e(getattr(user, "id", "—"))})</td></tr>
<tr><th>Connected</th><td>{"yes" if bot.is_ready() else "no"}</td></tr>
<tr><th>Latency</th><td>{f"{latency:.3f} s" if latency is not None else "—"}</td></tr>
<tr><th>Uptime</th><td>{e(uptime)}</td></tr>
<tr><th>Guilds</th><td>{len(guilds)}: {
        e(", ".join(g.name for g in guilds) or "—")
    }</td></tr>
</table></section>
<section><h2>Counts</h2><table>
<tr><th>Requests, last 24 h</th><td>{len(last_day)}</td></tr>
<tr><th>Errors, last 24 h</th><td>{len(errors)}</td></tr>
<tr><th>Memories</th><td>{len(store.all_memories())}</td></tr>
<tr><th>Active tasks</th><td>{len(store.all_tasks())}</td></tr>
<tr><th>Open conversations</th><td>{store.conversation_count()}</td></tr>
</table></section>
<section><h2>Last style run per guild</h2>
{
        "<table><tr><th>Guild</th><th>At</th><th>Messages</th><th>Result</th></tr>"
        + rows
        + "</table>"
        if rows
        else "<p class='note'>No style runs yet.</p>"
    }
</section>
<section><h2>Last 5 errors</h2>
{
        "<table><tr><th>At</th><th>Kind</th><th>Request</th><th>Guild</th><th>Error</th></tr>"
        + error_rows
        + "</table>"
        if error_rows
        else "<p class='note'>No errors recorded.</p>"
    }
</section>
"""
    return page("Overview", body, current="/")


def run_status(run: store.StyleRun) -> str:
    if run.error:
        return f"error: {run.error}"
    if run.guide:
        return "learned"
    return "skipped (too few messages)"


# --- Requests ------------------------------------------------------------------------------------


async def requests_page(request: web.Request) -> web.Response:
    bot = request.app[BOT]
    kind = request.query.get("kind", "")
    errors_only = request.query.get("errors") == "1"
    rows = store.recent_activity(300)
    if kind in KINDS:
        rows = [a for a in rows if a.kind == kind]
    if errors_only:
        rows = [a for a in rows if not a.ok]

    filters = " ".join(
        f'<a href="/requests?{urlencode(f)}">{e(label)}</a>'
        for f, label in [
            ({}, "all"),
            ({"kind": "reply"}, "replies"),
            ({"kind": "research"}, "research"),
            ({"kind": "task"}, "tasks"),
            ({"errors": "1"}, "errors only"),
        ]
    )
    table_rows = "".join(
        f"<tr><td>{e(fmt_time(a.at))}</td><td>{e(a.kind)}</td>"
        f"<td>{e(scope_label(bot, a.scope))}</td><td>{e(channel_label(bot, a.channel_id))}</td>"
        f"<td>{e(a.user_name or '—')}</td><td>{e(snippet(a.question))}</td>"
        f"<td class='{'err' if not a.ok else 'ok'}'>"
        f"{'✗ ' + e(snippet(a.error, 60)) if not a.ok else '✓ ok'}</td>"
        f"<td>{a.duration_ms if a.duration_ms is not None else '—'} ms</td>"
        f'<td><a href="/requests/{a.id}">#{a.id}</a></td></tr>'
        for a in rows
    )
    header = (
        f"<table><tr><th>Time ({e(TIMEZONE.key)})</th><th>Kind</th><th>Guild</th>"
        "<th>Channel</th><th>User</th><th>Question</th><th>Result</th><th>Duration</th>"
        "<th></th></tr>"
    )
    table = (
        header + table_rows + "</table>"
        if table_rows
        else "<p class='note'>No requests recorded yet.</p>"
    )
    body = f"<p class='note'>Filter: {filters}</p>{table}"
    return page("Requests", body, current="/requests")


async def request_detail(request: web.Request) -> web.Response:
    bot = request.app[BOT]
    try:
        activity_id = int(request.match_info["id"])
    except ValueError:
        raise web.HTTPNotFound(text="No such request") from None
    a = store.activity(activity_id)
    if a is None:
        raise web.HTTPNotFound(text="No such request")

    def row(label: str, value: str) -> str:
        return f"<tr><th>{e(label)}</th><td>{value}</td></tr>"

    def block(label: str, text: str | None) -> str:
        return row(label, f"<pre>{e(text or '(none)')}</pre>")

    body = f"""
<table>
{row("ID", f'<a href="/requests/{a.id}">#{a.id}</a>')}
{row("Time", e(f"{fmt_time(a.at)} ({TIMEZONE.key})"))}
{row("Kind", e(a.kind))}
{row("Scope", e(scope_label(bot, a.scope)))}
{row("Channel", e(channel_label(bot, a.channel_id)))}
{row("User", e(a.user_name or "—"))}
{row("Duration", f"{a.duration_ms} ms" if a.duration_ms is not None else "—")}
{row("OK", "yes" if a.ok else "no")}
</table>
<h2>Question</h2>
{block("Question", a.question)}
<h2>Prompt sent to the model</h2>
{block("Prompt", a.prompt)}
<h2>Reply</h2>
{block("Reply", a.reply)}
<h2>Error</h2>
{block("Error", a.error)}
"""
    return page(f"Request #{a.id}", body, current="/requests")


# --- Prompts -------------------------------------------------------------------------------------


def prompt_section(title: str, text: str) -> str:
    return f"<section><h2>{e(title)}</h2><pre>{e(text)}</pre></section>"


async def prompts_page(request: web.Request) -> web.Response:
    bot = request.app[BOT]
    scopes = {f"guild:{g.id}" for g in getattr(bot, "guilds", [])}
    scopes.update(scope for scope, _memory in store.all_memories_by_scope())
    scopes.update(scope for scope, _st in store.all_styles())
    parts = [
        prompt_section(
            "Effective system prompt (INSTRUCTIONS + extra)",
            agent.effective_instructions(),
        ),
        prompt_section("Research lead prompt", agent.RESEARCH_INSTRUCTIONS),
        prompt_section("Researcher subagent prompt", agent.RESEARCHER_INSTRUCTIONS),
        prompt_section("Style summarizer prompt", style.SUMMARIZER),
    ]
    for scope in sorted(scopes):
        memory = agent.memory_prompt(scope)
        guide = agent.style_prompt(scope)
        parts.append(
            f"<section><h2>{e(scope_label(bot, scope))}</h2>"
            f"<h3>Memory block, as injected</h3><pre>{e(memory)}</pre>"
            f"<h3>Style guide block, as injected</h3>"
            + (
                f"<pre>{e(guide)}</pre>"
                if guide
                else "<p class='note'>(empty — nothing injected)</p>"
            )
            + "</section>"
        )
    return page("Prompts", "".join(parts), current="/prompts")


# --- Learning ------------------------------------------------------------------------------------


async def learning_page(request: web.Request) -> web.Response:
    bot = request.app[BOT]
    parts = []
    for guild in getattr(bot, "guilds", []):
        scope = f"guild:{guild.id}"
        st = store.style(scope)
        channels = (
            ", ".join(f"#{c}" for c in st.channels)
            if st.channels
            else "all public channels"
        )
        if st.learned_at and st.enabled:
            due = fmt_time(st.learned_at + style.LEARN_EVERY)
        elif st.enabled:
            due = "at the next check (nothing learned yet)"
        else:
            due = "—"
        guide = (
            f"<details><summary>Current guide</summary><pre>{e(st.guide)}</pre></details>"
            if st.guide
            else "<p class='note'>No guide learned.</p>"
        )
        parts.append(
            f"<section><h2>{e(guild.name)} ({e(scope)})</h2><table>"
            f"<tr><th>Enabled</th><td>{'yes' if st.enabled else 'no'}</td></tr>"
            f"<tr><th>Channels</th><td>{e(channels)}</td></tr>"
            f"<tr><th>Learned at</th><td>{e(fmt_time(st.learned_at))}</td></tr>"
            f"<tr><th>Next due</th><td>{e(due)}</td></tr></table>{guide}"
            f"<form method='post' action='/learning/action'>"
            f"<input type='hidden' name='scope' value='{e(scope)}'>"
            f"<input type='hidden' name='next' value='/learning'>"
            f"<label>Channel names to learn from (comma or space separated, empty = unchanged)</label>"
            f"<input type='text' name='channels' value='{e(', '.join(st.channels or []))}'>"
            f"<p><button type='submit' name='action' value='enable'>Enable</button> "
            f"<button type='submit' name='action' value='disable'>Disable</button> "
            f"<button type='submit' name='action' value='relearn'>Relearn now</button> "
            f"<button type='submit' name='action' value='all'>Learn from all public channels</button></p>"
            f"</form></section>"
        )

    runs = store.style_runs(limit=200)
    run_rows = "".join(
        f"<tr><td>{e(fmt_time(r.at))}</td><td>{e(scope_label(bot, r.scope))}</td>"
        f"<td>{r.messages}</td><td>{e(run_status(r))}</td>"
        f"<td><details><summary>guide</summary><pre>{e(r.guide or '(none)')}</pre></details></td></tr>"
        for r in runs
    )
    history = (
        "<table><tr><th>Time</th><th>Guild</th><th>Messages</th><th>Result</th><th>Guide</th></tr>"
        + run_rows
        + "</table>"
        if run_rows
        else "<p class='note'>No style runs yet.</p>"
    )
    parts.append(f"<section><h2>History</h2>{history}</section>")
    return page("Learning", "".join(parts), current="/learning")


async def learning_action(request: web.Request) -> web.Response:
    data = await request.post()
    scope = data.get("scope", "")
    action = data.get("action", "")
    names = [n for n in re.split(r"[,\s]+", data.get("channels", "")) if n] or None
    # The same semantics as the /debeloglav ucenje slash command.
    if scope.startswith("guild:"):
        if action == "enable":
            store.configure_style(scope, enabled=True, channels=names)
        elif action == "disable":
            store.configure_style(scope, enabled=False)
        elif action == "relearn":
            store.configure_style(scope, relearn=True, channels=names)
        elif action == "all":
            store.configure_style(scope, enabled=True, all_channels=True)
    return redirect(data.get("next") or "/learning")


# --- Memories ------------------------------------------------------------------------------------


async def memories_page(request: web.Request) -> web.Response:
    bot = request.app[BOT]
    groups: dict[str, list[store.Memory]] = {}
    for scope, memory in store.all_memories_by_scope():
        groups.setdefault(scope, []).append(memory)

    parts = []
    for scope in sorted(groups):
        rows = "".join(
            f"<tr><td>#{m.id}</td><td><pre>{e(m.text)}</pre></td><td>{e(m.author)}</td>"
            f"<td><form class='inline' method='post' action='/memories/delete'>"
            f"<input type='hidden' name='scope' value='{e(scope)}'>"
            f"<input type='hidden' name='id' value='{m.id}'>"
            f"<input type='hidden' name='next' value='/memories'>"
            f"<button type='submit'>Delete</button></form></td></tr>"
            for m in groups[scope]
        )
        parts.append(
            f"<section><h2>{e(scope_label(bot, scope))}</h2>"
            f"<table><tr><th>ID</th><th>Memory</th><th>Author</th><th></th></tr>{rows}</table></section>"
        )
    if not parts:
        parts.append("<p class='note'>No memories yet.</p>")

    options = "".join(
        f"<option value='guild:{g.id}'>{e(g.name)}</option>"
        for g in getattr(bot, "guilds", [])
    )
    parts.append(
        f"<section><h2>Add a memory</h2>"
        f"<form method='post' action='/memories/add'>"
        f"<input type='hidden' name='next' value='/memories'>"
        f"<label>Scope</label><select name='scope'>{options}</select>"
        f"<label>Text</label><input type='text' name='text' required size='80'>"
        f"<p><button type='submit'>Add</button></p></form></section>"
    )
    return page("Memories", "".join(parts), current="/memories")


async def memory_add(request: web.Request) -> web.Response:
    data = await request.post()
    scope = data.get("scope", "")
    text = (data.get("text") or "").strip()
    if scope and text:
        store.add_memory(scope, text, "dashboard", 0)
    return redirect(data.get("next") or "/memories")


async def memory_delete(request: web.Request) -> web.Response:
    data = await request.post()
    try:
        memory_id = int(data.get("id", ""))
    except ValueError:
        memory_id = -1
    store.delete_memory(data.get("scope", ""), memory_id)
    return redirect(data.get("next") or "/memories")


# --- Channels ------------------------------------------------------------------------------------


async def channels_page(request: web.Request) -> web.Response:
    bot = request.app[BOT]
    env_ids = sorted(blocked.BLOCKED_CHANNELS)
    env_list = (
        ", ".join(f"<code>{e(channel_label(bot, i))}</code>" for i in env_ids)
        if env_ids
        else "<span class='note'>none</span>"
    )
    parts = [
        (
            f"<section><h2>Blocked everywhere (BLOCKED_CHANNELS in .env, read-only)</h2>"
            f"<p>{env_list}</p></section>"
        )
    ]

    per_guild: dict[int, list[int]] = {}
    for guild_id, channel_id in store.all_blocked_channels():
        per_guild.setdefault(guild_id, []).append(channel_id)

    for guild in getattr(bot, "guilds", []):
        blocked_ids = per_guild.get(guild.id, [])
        rows = "".join(
            f"<tr><td>{e(channel_label(bot, cid))}</td><td>"
            f"<form class='inline' method='post' action='/channels/unblock'>"
            f"<input type='hidden' name='guild_id' value='{guild.id}'>"
            f"<input type='hidden' name='channel_id' value='{cid}'>"
            f"<input type='hidden' name='next' value='/channels'>"
            f"<button type='submit'>Unblock</button></form></td></tr>"
            for cid in blocked_ids
        )
        table = (
            "<table><tr><th>Blocked</th><th></th></tr>" + rows + "</table>"
            if rows
            else "<p class='note'>Nothing blocked on this server.</p>"
        )
        places = list(getattr(guild, "text_channels", [])) + list(
            getattr(guild, "categories", [])
        )
        options = "".join(
            f"<option value='{p.id}'>{e(channel_label(bot, p.id))}</option>"
            for p in places
        )
        parts.append(
            f"<section><h2>{e(guild.name)} ({guild.id})</h2>{table}"
            f"<form method='post' action='/channels/block'>"
            f"<input type='hidden' name='guild_id' value='{guild.id}'>"
            f"<input type='hidden' name='next' value='/channels'>"
            f"<label>Block a channel or category</label>"
            f"<select name='channel_id'>{options}</select> "
            f"<button type='submit'>Block</button></form></section>"
        )
    return page("Channels", "".join(parts), current="/channels")


async def channel_block(request: web.Request) -> web.Response:
    data = await request.post()
    try:
        guild_id = int(data.get("guild_id", ""))
        channel_id = int(data.get("channel_id", ""))
    except ValueError:
        return redirect(data.get("next") or "/channels")
    blocked.block(guild_id, channel_id)  # keeps the in-memory cache in sync
    return redirect(data.get("next") or "/channels")


async def channel_unblock(request: web.Request) -> web.Response:
    data = await request.post()
    try:
        guild_id = int(data.get("guild_id", ""))
        channel_id = int(data.get("channel_id", ""))
    except ValueError:
        return redirect(data.get("next") or "/channels")
    blocked.unblock(guild_id, channel_id)
    return redirect(data.get("next") or "/channels")


# --- Tasks ---------------------------------------------------------------------------------------


async def tasks_page(request: web.Request) -> web.Response:
    bot = request.app[BOT]
    tasks = store.all_tasks()
    rows = "".join(
        f"<tr><td>#{t.id}</td><td>{e(scope_label(bot, t.scope))}</td>"
        f"<td>{e(channel_label(bot, t.channel_id))}</td><td>{e(t.user_name)}</td>"
        f"<td><pre>{e(t.prompt)}</pre></td>"
        f"<td>{e(t.cron or 'once')}<br>{e(fmt_time(t.next_run))}</td>"
        f"<td>{'watch' if t.watch else ''}{' mention' if t.mention else ''}</td>"
        f"<td><form class='inline' method='post' action='/tasks/cancel'>"
        f"<input type='hidden' name='scope' value='{e(t.scope)}'>"
        f"<input type='hidden' name='id' value='{t.id}'>"
        f"<input type='hidden' name='next' value='/tasks'>"
        f"<button type='submit'>Cancel</button></form></td></tr>"
        for t in tasks
    )
    body = (
        "<table><tr><th>ID</th><th>Guild</th><th>Channel</th><th>Created by</th><th>Prompt</th>"
        "<th>Schedule</th><th>Flags</th><th></th></tr>" + rows + "</table>"
        if rows
        else "<p class='note'>No active tasks.</p>"
    )
    return page("Tasks", body, current="/tasks")


async def task_cancel(request: web.Request) -> web.Response:
    data = await request.post()
    try:
        task_id = int(data.get("id", ""))
    except ValueError:
        task_id = -1
    store.cancel_task(data.get("scope", ""), task_id)
    return redirect(data.get("next") or "/tasks")


# --- Settings ------------------------------------------------------------------------------------


async def settings_page(request: web.Request) -> web.Response:
    current = agent.current_settings()
    defaults = agent.default_settings()
    error = request.query.get("error")
    parts = [f"<p class='error'>{e(error)}</p>"] if error else []
    for key in sorted(defaults):
        if key == "reasoning_effort":
            options = "".join(
                f"<option value='{e(v)}'"
                + (" selected" if current[key] == v else "")
                + f">{e(v)}</option>"
                for v in agent.REASONING_EFFORTS
            )
            field = f"<select name='value'>{options}</select>"
        elif key == "extra_instructions":
            field = f"<textarea name='value'>{e(current[key])}</textarea>"
        else:
            field = (
                f"<input type='text' name='value' value='{e(current[key])}' size='50'>"
            )
        parts.append(
            f"<section><h2>{e(key)}</h2>"
            f"<p class='note'>Default: <code>{e(defaults[key] or '(empty)')}</code> · "
            f"current: <code>{e(current[key] or '(empty)')}</code></p>"
            f"<form method='post' action='/settings'>"
            f"<input type='hidden' name='key' value='{e(key)}'>"
            f"<input type='hidden' name='next' value='/settings'>"
            f"<label>Value</label>{field}<p>"
            f"<button type='submit'>Save</button> "
            f"<button type='submit' name='reset' value='1'>Reset to default</button></p>"
            f"</form></section>"
        )
    return page("Settings", "".join(parts), current="/settings")


async def settings_post(request: web.Request) -> web.Response:
    bot = request.app[BOT]
    data = await request.post()
    key = data.get("key", "")
    try:
        if data.get("reset"):
            agent.update_setting(key, None)
        else:
            agent.update_setting(key, data.get("value", ""))
    except ValueError as error:
        return redirect("/settings?" + urlencode({"error": str(error)}))
    apply = getattr(bot, "apply_settings", None)
    if callable(apply):
        apply()  # rebuild the agent so new turns use the new settings
    return redirect(data.get("next") or "/settings")
