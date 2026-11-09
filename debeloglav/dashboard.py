"""The admin dashboard: a small password-protected web UI served by the bot itself.

Enabled only when DASHBOARD_PASSWORD is set (then it listens on DASHBOARD_HOST:DASHBOARD_PORT,
127.0.0.1:8080 by default). Login is a server-rendered form at /login (password only, no username):
the correct password sets a HttpOnly session cookie. A simple CSRF guard rejects a POST whose
Origin/Referer host is not the Host header. Server-rendered HTML with inline CSS, plain forms
that POST and answer 303, no JavaScript framework. Failed logins are rate-limited per client IP
and globally (429 with Retry-After), and every response carries clickjacking/cache hardening
headers.
"""

import asyncio
import hmac
import html
import logging
import math
import os
import re
import secrets
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
KINDS = ("reply", "research", "task")

BOT = web.AppKey("bot", object)
PASSWORD = web.AppKey("password", str | None)
STARTED = web.AppKey("started", float)
SESSIONS = web.AppKey("sessions", dict[str, float])  # token -> monotonic expiry

SESSION_COOKIE = "debeloglav_session"
SESSION_TTL_SECONDS = 7 * 24 * 60 * 60  # one week (in-memory: a restart logs out)

LAYOUT_CSS = """
:root {
  --sans: ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue",
          Arial, sans-serif;
  --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
  --bg: #f3f5f8;
  --surface: #ffffff;
  --surface-2: #edf0f5;
  --border: #e4e8ef;
  --border-strong: #ccd4e0;
  --text: #1b2430;
  --muted: #64707f;
  --hover: #f3f6fa;
  --accent: #4f46e5;
  --accent-strong: #4338ca;
  --accent-soft: #eceafd;
  --on-accent: #ffffff;
  --ring: rgba(79, 70, 229, 0.16);
  --danger-text: #b42318;
  --danger-border: #f0c6c0;
  --danger-bg: #fdecea;
  --ok-text: #177245;
  --ok-bg: #e4f6ec;
  --ok-border: #bfe6cd;
  --warn-text: #8f6400;
  --warn-bg: #fcf2dd;
  --warn-border: #f0dfae;
  --neutral-bg: #eceff4;
  --neutral-text: #5b6675;
  --side-bg: #151a24;
  --side-text: #a6b0c0;
  --side-hover: rgba(255, 255, 255, 0.07);
  --side-active: #4f46e5;
  --shadow: 0 1px 2px rgba(16, 24, 40, 0.06);
  --radius: 12px;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0d1218;
    --surface: #151c27;
    --surface-2: #10161f;
    --border: #242f3e;
    --border-strong: #313d4f;
    --text: #e5eaf2;
    --muted: #939eb0;
    --hover: #1a2331;
    --accent: #818cf8;
    --accent-strong: #a3b0fc;
    --accent-soft: rgba(129, 140, 248, 0.13);
    --on-accent: #10131c;
    --ring: rgba(129, 140, 248, 0.3);
    --danger-text: #ff8f86;
    --danger-border: rgba(255, 143, 134, 0.32);
    --danger-bg: rgba(255, 113, 105, 0.12);
    --ok-text: #5bd692;
    --ok-bg: rgba(46, 204, 113, 0.13);
    --ok-border: rgba(91, 214, 146, 0.3);
    --warn-text: #e6b95c;
    --warn-bg: rgba(230, 185, 92, 0.12);
    --warn-border: rgba(230, 185, 92, 0.3);
    --neutral-bg: rgba(255, 255, 255, 0.08);
    --neutral-text: #9aa5b5;
    --side-bg: #0a0e14;
    --side-active: #6366f1;
    --shadow: 0 1px 2px rgba(0, 0, 0, 0.45);
  }
}
* { box-sizing: border-box; }
body { margin: 0; font-family: var(--sans); font-size: 14.5px; line-height: 1.55;
       background: var(--bg); color: var(--text); }
a { color: var(--accent); }

/* --- Shell: dark sidebar + content ---------------------------------------------------- */
.shell { display: flex; min-height: 100vh; }
.sidebar { width: 238px; flex: 0 0 auto; background: var(--side-bg); display: flex;
           flex-direction: column; padding: 18px 14px; position: sticky; top: 0;
           height: 100vh; }
.brand { display: flex; align-items: center; gap: 10px; color: #fff; text-decoration: none;
         font-weight: 700; font-size: 17px; letter-spacing: -0.01em; padding: 2px 8px 16px; }
.brand svg { flex: 0 0 auto; }
.sidebar nav { display: flex; flex-direction: column; gap: 2px; }
.sidebar nav a { color: var(--side-text); text-decoration: none; padding: 8px 11px;
                 border-radius: 8px; font-size: 14px; font-weight: 550; }
.sidebar nav a:hover { background: var(--side-hover); color: #fff; }
.sidebar nav a.active { background: var(--side-active); color: #fff; }
.logout { margin-top: auto; padding-top: 16px; }
.logout button { width: 100%; background: transparent; border-color: rgba(255, 255, 255, 0.16);
                 color: var(--side-text); }
.logout button:hover { background: var(--side-hover); color: #fff; }
.content { flex: 1; min-width: 0; padding: 30px 38px 64px; }
.page-head { max-width: 1040px; margin: 0 auto 20px; }
.page-head h1 { font-size: 24px; font-weight: 700; letter-spacing: -0.02em; margin: 0; }
main { max-width: 1040px; margin: 0 auto; }
@media (max-width: 760px) {
  .shell { flex-direction: column; }
  .sidebar { width: 100%; height: auto; position: static; flex-direction: row;
             align-items: center; flex-wrap: wrap; gap: 2px 8px; padding: 10px 14px; }
  .brand { padding: 2px 6px 2px 2px; font-size: 16px; }
  .sidebar nav { flex-direction: row; flex-wrap: wrap; }
  .sidebar nav a { padding: 6px 9px; font-size: 13px; }
  .logout { margin: 0 0 0 auto; padding: 0; }
  .logout button { width: auto; padding: 6px 11px; font-size: 13px; }
  .content { padding: 20px 16px 48px; }
  .page-head h1 { font-size: 21px; }
}

/* --- Login page: centered card --------------------------------------------------------- */
.auth { min-height: 100vh; display: flex; align-items: center; justify-content: center;
        padding: 24px 16px;
        background: radial-gradient(720px 380px at 50% -80px, var(--accent-soft), transparent 70%),
                    var(--bg); }
.auth-card { width: 100%; max-width: 380px; background: var(--surface);
             border: 1px solid var(--border); border-radius: 16px;
             box-shadow: 0 10px 34px rgba(16, 24, 40, 0.1); padding: 28px 28px 26px; }
.auth-brand { display: flex; align-items: center; gap: 10px; font-weight: 700; font-size: 18px;
              letter-spacing: -0.01em; margin-bottom: 20px; }
.auth-card h1 { font-size: 19px; font-weight: 650; letter-spacing: -0.01em; margin: 0 0 6px; }
.auth-card .error { margin: 12px 0 0; }
.login-form p { margin: 0; }
.login-form label { margin-top: 16px; }
.login-form button[type='submit'] { width: 100%; margin-top: 18px; padding: 9px 14px;
                                    font-weight: 650; background: var(--accent);
                                    border-color: var(--accent); color: var(--on-accent); }
.login-form button[type='submit']:hover { background: var(--accent-strong);
                                          border-color: var(--accent-strong); }

/* --- Cards, headings, notes ------------------------------------------------------------ */
section { background: var(--surface); border: 1px solid var(--border);
          border-radius: var(--radius); box-shadow: var(--shadow); padding: 18px 22px;
          margin: 0 0 18px; }
h2 { font-size: 15.5px; font-weight: 650; letter-spacing: -0.01em; margin: 0 0 12px; }
h3 { font-size: 13px; font-weight: 600; color: var(--muted); margin: 14px 0 4px; }
.note { color: var(--muted); font-size: 13.5px; margin: 6px 0; }
.error { background: var(--danger-bg); border: 1px solid var(--danger-border);
         color: var(--danger-text); padding: 10px 14px; border-radius: 10px;
         font-size: 13.5px; margin: 0 0 14px; }

/* --- Stat cards (overview counts) ------------------------------------------------------ */
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
         gap: 12px; }
.stat { background: var(--surface-2); border: 1px solid var(--border); border-radius: 10px;
        padding: 12px 16px; }
.stat-value { display: block; font-size: 26px; font-weight: 700; letter-spacing: -0.02em;
              line-height: 1.25; }
.stat-label { display: block; color: var(--muted); font-size: 12.5px; margin-top: 1px; }

/* --- Tables ---------------------------------------------------------------------------- */
.table-wrap { overflow-x: auto; border: 1px solid var(--border); border-radius: var(--radius);
              background: var(--surface); box-shadow: var(--shadow); margin: 0 0 18px; }
section .table-wrap { box-shadow: none; margin-bottom: 0; }
table { border-collapse: collapse; width: 100%; font-size: 13.5px; }
th { text-align: left; padding: 9px 14px; font-size: 11.5px; font-weight: 650;
     text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted);
     background: var(--surface-2); border-bottom: 1px solid var(--border); white-space: nowrap; }
td { padding: 9px 14px; border-bottom: 1px solid var(--border); text-align: left;
     vertical-align: top; }
tbody tr:last-child td { border-bottom: none; }
.table-wrap tbody tr:hover td { background: var(--hover); }
table.kv { font-size: 14px; }
table.kv th, table.kv td { border: none; background: transparent; padding: 8px 0; font-size: 14px;
                          text-transform: none; letter-spacing: normal; white-space: normal; }
table.kv th { color: var(--muted); font-weight: 550; width: 180px; }
table.kv tr + tr th, table.kv tr + tr td { border-top: 1px solid var(--border); }
td pre { margin: 0; }

/* --- Status pills ----------------------------------------------------------------------- */
.pill { display: inline-block; padding: 2px 10px; border-radius: 999px; font-size: 12px;
        font-weight: 600; border: 1px solid transparent; white-space: nowrap; }
.pill.ok { background: var(--ok-bg); color: var(--ok-text); border-color: var(--ok-border); }
.pill.err { background: var(--danger-bg); color: var(--danger-text);
            border-color: var(--danger-border); }
.pill.warn { background: var(--warn-bg); color: var(--warn-text); border-color: var(--warn-border); }
.pill.neutral { background: var(--neutral-bg); color: var(--neutral-text); }

/* --- Buttons ---------------------------------------------------------------------------- */
button { font: inherit; font-size: 13.5px; font-weight: 550; padding: 7px 14px;
         border: 1px solid var(--border-strong); border-radius: 8px; background: var(--surface);
         color: var(--text); cursor: pointer; }
button:hover { background: var(--hover); }
button.primary { background: var(--accent); border-color: var(--accent); color: var(--on-accent); }
button.primary:hover { background: var(--accent-strong); border-color: var(--accent-strong); }
button.danger { background: transparent; color: var(--danger-text);
                border-color: var(--danger-border); }
button.danger:hover { background: var(--danger-bg); }
td button { padding: 4px 10px; font-size: 12.5px; }
.btn-row { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 14px; }
form.inline { display: inline; }

/* --- Forms ------------------------------------------------------------------------------ */
label { display: block; margin: 14px 0 6px; font-weight: 600; font-size: 13px; }
input[type='text'], input[type='password'], select, textarea { font: inherit; font-size: 14px;
  padding: 8px 11px; border: 1px solid var(--border-strong); border-radius: 8px;
  background: var(--surface); color: var(--text); width: 100%; max-width: 560px; }
textarea { min-height: 110px; resize: vertical; }
input[type='text']:focus, input[type='password']:focus, select:focus, textarea:focus {
  outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px var(--ring); }
.field-row { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.field-row select { flex: 1 1 220px; max-width: none; }

/* --- Request filter pills ---------------------------------------------------------------- */
.filters { display: inline-flex; flex-wrap: wrap; gap: 4px; padding: 4px;
           background: var(--surface-2); border: 1px solid var(--border); border-radius: 999px;
           margin: 0 0 16px; }
.filters a { padding: 5px 13px; border-radius: 999px; text-decoration: none; color: var(--muted);
             font-size: 13px; font-weight: 550; }
.filters a:hover { color: var(--text); }
.filters a.active { background: var(--surface); color: var(--text); box-shadow: var(--shadow); }

/* --- Code blocks -------------------------------------------------------------------------- */
pre { background: var(--surface-2); border: 1px solid var(--border); border-radius: 10px;
      padding: 12px 14px; overflow-x: auto; white-space: pre-wrap; word-break: break-word;
      font-family: var(--mono); font-size: 12.5px; line-height: 1.5; margin: 6px 0; }
code { font-family: var(--mono); font-size: 0.92em; background: var(--surface-2);
       border: 1px solid var(--border); border-radius: 6px; padding: 1px 5px; }
pre code { background: none; border: none; padding: 0; }
details { margin: 10px 0 2px; }
summary { cursor: pointer; color: var(--accent); font-size: 13px; font-weight: 600; }
details pre { margin-top: 8px; }

/* --- Focus -------------------------------------------------------------------------------- */
a:focus-visible, button:focus-visible, summary:focus-visible, input:focus-visible,
select:focus-visible, textarea:focus-visible { outline: 2px solid var(--accent);
                                                 outline-offset: 2px; }
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


def safe_next(location: str | None) -> str:
    """A redirect target that stays on this dashboard: a path starting with '/' but not '//'."""
    if location and location.startswith("/") and not location.startswith("//"):
        return location
    return "/"


def redirect(location: str | None) -> NoReturn:
    """303 back to a page of this dashboard (never to another site)."""
    raise web.HTTPSeeOther(location=safe_next(location))


BRAND_SVG = (
    "<svg width='24' height='24' viewBox='0 0 24 24' fill='none' aria-hidden='true'>"
    "<rect x='1' y='1' width='22' height='22' rx='6.5' fill='#6366f1'/>"
    "<path d='M8.2 6.8h4.1a5.2 5.2 0 0 1 0 10.4H8.2V6.8z' fill='#fff'/>"
    "</svg>"
)


def page(
    title: str, body: str, current: str = "/", nav: bool = True, status: int = 200
) -> web.Response:
    """Wrap a page body in the app shell: a dark sidebar with the nav (or, with nav=False,
    the centered login card)."""
    links = []
    for href, label in NAV:
        active = ' class="active"' if href == current else ""
        links.append(f'<a href="{e(href)}"{active}>{e(label)}</a>')
    if nav:
        chrome = (
            "<div class='shell'>"
            "<aside class='sidebar'>"
            f"<a class='brand' href='/'>{BRAND_SVG}<span>Debeloglav</span></a>"
            f"<nav>{''.join(links)}</nav>"
            "<form class='logout' method='post' action='/logout'>"
            "<button type='submit'>Log out</button></form>"
            "</aside>"
            "<div class='content'>"
            f"<header class='page-head'><h1>{e(title)}</h1></header>"
            f"<main>{body}</main>"
            "</div></div>"
        )
    else:
        chrome = (
            "<div class='auth'><div class='auth-card'>"
            f"<div class='auth-brand'>{BRAND_SVG}<span>Debeloglav</span></div>"
            f"<h1>{e(title)}</h1>{body}</div></div>"
        )
    return web.Response(
        text="<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{e(title)} · Debeloglav dashboard</title>"
        f"<style>{LAYOUT_CSS}</style></head><body>{chrome}</body></html>",
        content_type="text/html",
        status=status,
    )


# --- Security ------------------------------------------------------------------------------------

# Brute-force protection for the login form: after PER_IP_FAILURE_LIMIT failed logins from one
# client IP within FAILURE_WINDOW seconds, that IP is locked out for LOCKOUT_SECONDS (429 with
# Retry-After, checked before the password, so a correct guess during the lockout does not help).
# More than GLOBAL_FAILURE_LIMIT failures from all IPs together lock out every login attempt for
# the same time, except ones with the correct password. The client IP is the TCP peer, unless the
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


# --- Login form and session cookies ----------------------------------------------------------------


def _new_session(app: web.Application) -> str:
    """A fresh random session token, stored on the app with its expiry (in-memory, so a restart
    logs everyone out); expired ones are pruned on the way so the dict stays bounded."""
    sessions: dict[str, float] = app[SESSIONS]
    now = time.monotonic()
    for token, expiry in list(sessions.items()):
        if expiry <= now:
            del sessions[token]
    token = secrets.token_urlsafe(32)
    sessions[token] = now + SESSION_TTL_SECONDS
    return token


def _session_valid(request: web.Request) -> bool:
    """Whether the request carries a live session cookie."""
    token = request.cookies.get(SESSION_COOKIE, "")
    sessions: dict[str, float] = request.app[SESSIONS]
    expiry = sessions.get(token)
    if expiry is None:
        return False
    if expiry <= time.monotonic():
        del sessions[token]  # expired
        return False
    return True


def login_form(
    next_url: str, error: str | None = None, status: int = 200
) -> web.Response:
    """The /login page: a centered card with the brand, without the sidebar nav."""
    error_html = f"<p class='error'>{e(error)}</p>" if error else ""
    body = (
        f"{error_html}"
        "<form class='login-form' method='post' action='/login'>"
        f"<input type='hidden' name='next' value='{e(next_url)}'>"
        "<label for='password'>Password</label>"
        "<input type='password' name='password' id='password' required autofocus>"
        "<p><button type='submit'>Log in</button></p>"
        "</form>"
    )
    return page("Log in", body, nav=False, status=status)


async def login_page(request: web.Request) -> web.Response:
    """GET /login: the form, keeping a safe `next` to land on after logging in."""
    return login_form(safe_next(request.query.get("next")))


async def login_post(request: web.Request) -> web.Response:
    """POST /login: check the password in constant time; on success set the session cookie and
    303 to `next`, on failure re-render the form with 401 and count the failure."""
    password = request.app[PASSWORD] or ""
    limiter: LoginLimiter = request.app[LIMITER]
    ip = limiter.client_ip(request)
    retry = limiter.ip_retry_after(ip)
    if retry is not None:
        return _too_many_requests(
            retry
        )  # locked IP: refused before the password is checked
    data = await request.post()
    given = str(data.get("password") or "")
    next_url = safe_next(str(data.get("next") or request.query.get("next")))
    global_retry = limiter.global_retry_after()
    if global_retry is not None:
        # Global lockout against distributed guessing: only the correct password gets through,
        # and failures are not counted while it lasts.
        if not hmac.compare_digest(given.encode(), password.encode()):
            return _too_many_requests(global_retry)
    elif not hmac.compare_digest(given.encode(), password.encode()):
        limiter.record_failure(ip)
        return login_form(next_url, error="Wrong password.", status=401)
    limiter.record_success(ip)
    token = _new_session(request.app)
    response = web.HTTPSeeOther(location=safe_next(next_url))
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="Strict",
        path="/",
    )
    raise response


async def logout(request: web.Request) -> web.Response:
    """POST /logout: drop the session, clear the cookie, 303 back to the login page."""
    request.app[SESSIONS].pop(request.cookies.get(SESSION_COOKIE, ""), None)
    response = web.HTTPSeeOther(location="/login")
    response.del_cookie(SESSION_COOKIE, path="/")
    raise response


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
    if request.method == "POST" and not _same_origin(request):
        return web.Response(status=403, text="Cross-origin POST rejected.")
    if request.path != "/login" and not _session_valid(request):
        # Not logged in: 303 to the login form, remembering where the browser wanted to go
        # (the same for GET and POST — after logging in it lands on that page). The login
        # form itself is public; its own POST is rate-limited in login_post.
        redirect("/login?" + urlencode({"next": request.path_qs}))
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
    app[SESSIONS] = {}

    async def _add_security_headers(
        request: web.Request, response: web.StreamResponse
    ) -> None:
        for name, value in SECURITY_HEADERS.items():
            response.headers[name] = value

    app.on_response_prepare.append(_add_security_headers)  # fires for every response
    app.router.add_get("/login", login_page, name="login")
    app.router.add_post("/login", login_post, name="login_post")
    app.router.add_post("/logout", logout, name="logout")
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
        f"<td>{run.messages}</td><td>{status_pill(run_status(run))}</td></tr>"
        for scope, run in sorted(last_run.items())
    )
    error_rows = "".join(
        f"<tr><td>{e(fmt_time(a.at))}</td><td>{e(a.kind)}</td>"
        f'<td><a href="/requests/{a.id}">#{a.id}</a></td>'
        f"<td>{e(scope_label(bot, a.scope))}</td><td>{e(snippet(a.error, 120))}</td></tr>"
        for a in [a for a in activity if not a.ok][:5]
    )
    connected = (
        "<span class='pill ok'>yes</span>"
        if bot.is_ready()
        else "<span class='pill err'>no</span>"
    )
    body = f"""
<section><h2>Bot</h2><table class='kv'>
<tr><th>User</th><td>{e(user_name)} (id {e(getattr(user, "id", "—"))})</td></tr>
<tr><th>Connected</th><td>{connected}</td></tr>
<tr><th>Latency</th><td>{f"{latency:.3f} s" if latency is not None else "—"}</td></tr>
<tr><th>Uptime</th><td>{e(uptime)}</td></tr>
<tr><th>Guilds</th><td>{len(guilds)}: {
        e(", ".join(g.name for g in guilds) or "—")
    }</td></tr>
</table></section>
<section><h2>Counts</h2><div class='stats'>
<div class='stat'><span class='stat-value'>{
        len(last_day)
    }</span><span class='stat-label'>Requests, last 24 h</span></div>
<div class='stat'><span class='stat-value'>{
        len(errors)
    }</span><span class='stat-label'>Errors, last 24 h</span></div>
<div class='stat'><span class='stat-value'>{
        len(store.all_memories())
    }</span><span class='stat-label'>Memories</span></div>
<div class='stat'><span class='stat-value'>{
        len(store.all_tasks())
    }</span><span class='stat-label'>Active tasks</span></div>
<div class='stat'><span class='stat-value'>{
        store.conversation_count()
    }</span><span class='stat-label'>Open conversations</span></div>
</div></section>
<section><h2>Last style run per guild</h2>
{
        "<div class='table-wrap'><table><tr><th>Guild</th><th>At</th><th>Messages</th>"
        "<th>Result</th></tr>" + rows + "</table></div>"
        if rows
        else "<p class='note'>No style runs yet.</p>"
    }
</section>
<section><h2>Last 5 errors</h2>
{
        "<div class='table-wrap'><table><tr><th>At</th><th>Kind</th><th>Request</th><th>Guild</th>"
        "<th>Error</th></tr>" + error_rows + "</table></div>"
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


def status_pill(text: str) -> str:
    """A style-run status as a coloured pill: errors red, learned green, skipped amber."""
    if text.startswith("error:"):
        return f"<span class='pill err'>{e(text)}</span>"
    if text == "learned":
        return "<span class='pill ok'>learned</span>"
    return f"<span class='pill warn'>{e(text)}</span>"


def result_pill(a: store.Activity) -> str:
    """A request's result as a coloured pill: ok green, failure red with the error."""
    if a.ok:
        return "<span class='pill ok'>✓ ok</span>"
    return f"<span class='pill err'>✗ {e(snippet(a.error, 60))}</span>"


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

    active_filter = "errors" if errors_only else (kind if kind in KINDS else "")
    filters = "".join(
        f'<a href="/requests?{urlencode(f)}"'
        + (' class="active"' if key == active_filter else "")
        + f">{e(label)}</a>"
        for f, label, key in [
            ({}, "all", ""),
            ({"kind": "reply"}, "replies", "reply"),
            ({"kind": "research"}, "research", "research"),
            ({"kind": "task"}, "tasks", "task"),
            ({"errors": "1"}, "errors only", "errors"),
        ]
    )
    table_rows = "".join(
        f"<tr><td>{e(fmt_time(a.at))}</td><td>{e(a.kind)}</td>"
        f"<td>{e(scope_label(bot, a.scope))}</td><td>{e(channel_label(bot, a.channel_id))}</td>"
        f"<td>{e(a.user_name or '—')}</td><td>{e(snippet(a.question))}</td>"
        f"<td>{result_pill(a)}</td>"
        f"<td>{a.duration_ms if a.duration_ms is not None else '—'} ms</td>"
        f'<td><a href="/requests/{a.id}">#{a.id}</a></td></tr>'
        for a in rows
    )
    header = (
        "<div class='table-wrap'>"
        f"<table><tr><th>Time ({e(TIMEZONE.key)})</th><th>Kind</th><th>Guild</th>"
        "<th>Channel</th><th>User</th><th>Question</th><th>Result</th><th>Duration</th>"
        "<th></th></tr>"
    )
    table = (
        header + table_rows + "</table></div>"
        if table_rows
        else "<p class='note'>No requests recorded yet.</p>"
    )
    body = f"<div class='filters'>{filters}</div>{table}"
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

    def block(text: str | None) -> str:
        return f"<pre>{e(text or '(none)')}</pre>"

    ok_pill = (
        "<span class='pill ok'>yes</span>"
        if a.ok
        else "<span class='pill err'>no</span>"
    )
    body = f"""
<section><h2>Details</h2><table class='kv'>
{row("ID", f'<a href="/requests/{a.id}">#{a.id}</a>')}
{row("Time", e(f"{fmt_time(a.at)} ({TIMEZONE.key})"))}
{row("Kind", e(a.kind))}
{row("Scope", e(scope_label(bot, a.scope)))}
{row("Channel", e(channel_label(bot, a.channel_id)))}
{row("User", e(a.user_name or "—"))}
{row("Duration", f"{a.duration_ms} ms" if a.duration_ms is not None else "—")}
{row("OK", ok_pill)}
</table></section>
<section><h2>Question</h2>{block(a.question)}</section>
<section><h2>Prompt sent to the model</h2>{block(a.prompt)}</section>
<section><h2>Reply</h2>{block(a.reply)}</section>
<section><h2>Error</h2>{block(a.error)}</section>
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
        enabled_pill = (
            "<span class='pill ok'>enabled</span>"
            if st.enabled
            else "<span class='pill neutral'>disabled</span>"
        )
        parts.append(
            f"<section><h2>{e(guild.name)} ({e(scope)})</h2><table class='kv'>"
            f"<tr><th>Enabled</th><td>{enabled_pill}</td></tr>"
            f"<tr><th>Channels</th><td>{e(channels)}</td></tr>"
            f"<tr><th>Learned at</th><td>{e(fmt_time(st.learned_at))}</td></tr>"
            f"<tr><th>Next due</th><td>{e(due)}</td></tr></table>{guide}"
            f"<form method='post' action='/learning/action'>"
            f"<input type='hidden' name='scope' value='{e(scope)}'>"
            f"<input type='hidden' name='next' value='/learning'>"
            f"<label>Channel names to learn from (comma or space separated, empty = unchanged)</label>"
            f"<input type='text' name='channels' value='{e(', '.join(st.channels or []))}'>"
            f"<div class='btn-row'>"
            f"<button type='submit' name='action' value='enable'>Enable</button> "
            f"<button type='submit' name='action' value='disable' class='danger'>Disable</button> "
            f"<button type='submit' name='action' value='relearn'>Relearn now</button> "
            f"<button type='submit' name='action' value='all'>Learn from all public channels</button>"
            f"</div></form></section>"
        )

    runs = store.style_runs(limit=200)
    run_rows = "".join(
        f"<tr><td>{e(fmt_time(r.at))}</td><td>{e(scope_label(bot, r.scope))}</td>"
        f"<td>{r.messages}</td><td>{status_pill(run_status(r))}</td>"
        f"<td><details><summary>guide</summary><pre>{e(r.guide or '(none)')}</pre></details></td></tr>"
        for r in runs
    )
    history = (
        "<div class='table-wrap'><table><tr><th>Time</th><th>Guild</th><th>Messages</th>"
        "<th>Result</th><th>Guide</th></tr>" + run_rows + "</table></div>"
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
            f"<button type='submit' class='danger'>Delete</button></form></td></tr>"
            for m in groups[scope]
        )
        parts.append(
            f"<section><h2>{e(scope_label(bot, scope))}</h2>"
            f"<div class='table-wrap'><table><tr><th>ID</th><th>Memory</th><th>Author</th>"
            f"<th></th></tr>{rows}</table></div></section>"
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
        f"<div class='btn-row'><button type='submit' class='primary'>Add</button></div>"
        f"</form></section>"
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
        " ".join(f"<code>{e(channel_label(bot, i))}</code>" for i in env_ids)
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
            "<div class='table-wrap'><table><tr><th>Blocked</th><th></th></tr>"
            + rows
            + "</table></div>"
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
            f"<div class='field-row'><select name='channel_id'>{options}</select>"
            f"<button type='submit' class='danger'>Block</button></div></form></section>"
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
        f"<button type='submit' class='danger'>Cancel</button></form></td></tr>"
        for t in tasks
    )
    body = (
        "<div class='table-wrap'><table><tr><th>ID</th><th>Guild</th><th>Channel</th>"
        "<th>Created by</th><th>Prompt</th>"
        "<th>Schedule</th><th>Flags</th><th></th></tr>" + rows + "</table></div>"
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
            f"<label>Value</label>{field}"
            f"<div class='btn-row'><button type='submit' class='primary'>Save</button> "
            f"<button type='submit' name='reset' value='1'>Reset to default</button></div>"
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
