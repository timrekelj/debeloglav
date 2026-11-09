"""The admin dashboard: a small password-protected web UI served by the bot itself.

Enabled only when DASHBOARD_PASSWORD is set (then it listens on DASHBOARD_HOST:DASHBOARD_PORT,
127.0.0.1:8080 by default). Login is a server-rendered form at /login (password only, no username):
the correct password sets a HttpOnly session cookie. A simple CSRF guard rejects a POST whose
Origin/Referer host is not the Host header. Server-rendered HTML with inline CSS, plain forms
that POST and answer 303, no JavaScript framework. The Geist fonts are served from the package
(/static), so the page loads nothing from other sites. Failed logins are rate-limited per client
IP and globally (429 with Retry-After), and every response carries clickjacking/cache hardening
headers. Requests, style runs and everything else can be exported as JSON or CSV.
"""

import asyncio
import csv
import hmac
import html
import io
import json
import logging
import math
import os
import secrets
import time
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NoReturn
from urllib.parse import urlencode, urlsplit

import discord
from aiohttp import web

from . import agent, blocked, settings, store, style
from .context import timezone

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

STATIC_DIR = Path(__file__).parent / "static"
STATIC_FILES = {  # the only files /static serves, with their content types
    "Geist-Variable.woff2": "font/woff2",
    "GeistMono-Variable.woff2": "font/woff2",
    "OFL.txt": "text/plain",
}

SLOW_MS = 10_000  # a reply slower than this is marked amber, twice as slow red

LAYOUT_CSS = """
@font-face { font-family: "Geist"; src: url("/static/Geist-Variable.woff2") format("woff2");
             font-weight: 100 900; font-display: swap; }
@font-face { font-family: "Geist Mono"; src: url("/static/GeistMono-Variable.woff2") format("woff2");
             font-weight: 100 900; font-display: swap; }
:root {
  color-scheme: light dark;
  --sans: "Geist", ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  --mono: "Geist Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  --bg: #fafafa;
  --surface: #ffffff;
  --subtle: #f4f4f5;
  --hover: #f7f7f8;
  --border: #ebebee;
  --border-strong: #dcdce1;
  --text: #18181b;
  --muted: #6e6e78;
  --faint: #a1a1aa;
  --accent: #5b5bd6;
  --accent-hover: #4a4ac4;
  --accent-soft: #efeffd;
  --accent-text: #4747c2;
  --on-accent: #ffffff;
  --ring: rgba(91, 91, 214, 0.22);
  --ok: #17794d;
  --ok-bg: #eaf6ef;
  --warn: #8d5a00;
  --warn-bg: #fcf3e2;
  --err: #c4282d;
  --err-bg: #fdeded;
  --s-react: #c4c4cc;
  --s-context: #5ba7f7;
  --s-session: #a78bfa;
  --s-wait: #f59e0b;
  --s-turn: #5b5bd6;
  --s-render: #2dd4bf;
  --s-send: #22c55e;
  --shadow: 0 1px 2px rgba(24, 24, 27, 0.04);
  --radius: 12px;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0c0c0e;
    --surface: #141417;
    --subtle: #1b1b1f;
    --hover: #18181c;
    --border: #25252b;
    --border-strong: #33333a;
    --text: #ededef;
    --muted: #92929c;
    --faint: #62626b;
    --accent: #8282f0;
    --accent-hover: #9a9af5;
    --accent-soft: rgba(130, 130, 240, 0.13);
    --accent-text: #a5a5f7;
    --on-accent: #0c0c14;
    --ring: rgba(130, 130, 240, 0.3);
    --ok: #4cc38a;
    --ok-bg: rgba(76, 195, 138, 0.12);
    --warn: #e5b45a;
    --warn-bg: rgba(229, 180, 90, 0.12);
    --err: #ff7b7f;
    --err-bg: rgba(255, 110, 115, 0.12);
    --s-react: #4a4a52;
    --shadow: none;
  }
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body { margin: 0; font-family: var(--sans); font-size: 14px; line-height: 1.5;
       background: var(--bg); color: var(--text); -webkit-font-smoothing: antialiased;
       font-feature-settings: "ss01", "cv11"; }
a { color: var(--accent-text); text-decoration: none; }
a:hover { text-decoration: underline; text-underline-offset: 2px; }
code, pre, .mono { font-family: var(--mono); }
.num { font-variant-numeric: tabular-nums; white-space: nowrap; }
.muted { color: var(--muted); }
.faint { color: var(--faint); }

/* --- Shell ------------------------------------------------------------------------------- */
.shell { display: flex; min-height: 100vh; }
.sidebar { width: 236px; flex: 0 0 auto; background: var(--surface);
           border-right: 1px solid var(--border); display: flex; flex-direction: column;
           padding: 18px 12px; position: sticky; top: 0; height: 100vh; }
.brand { display: flex; align-items: center; gap: 10px; color: var(--text); padding: 4px 10px 22px;
         font-weight: 600; font-size: 15px; letter-spacing: -0.01em; }
.brand:hover { text-decoration: none; }
.brand small { display: block; color: var(--muted); font-weight: 400; font-size: 12px;
               letter-spacing: 0; margin-top: -2px; }
.nav-label { color: var(--faint); font-size: 11.5px; font-weight: 500; padding: 0 10px 6px; }
.sidebar nav { display: flex; flex-direction: column; gap: 1px; }
.sidebar nav a { display: flex; align-items: center; gap: 10px; color: var(--muted);
                 padding: 7px 10px; border-radius: 8px; font-size: 13.5px; font-weight: 500; }
.sidebar nav a:hover { background: var(--hover); color: var(--text); text-decoration: none; }
.sidebar nav a.active { background: var(--subtle); color: var(--text); }
.sidebar nav a.active svg { color: var(--accent); }
.sidebar nav svg { flex: 0 0 auto; opacity: 0.9; }
.side-foot { margin-top: auto; padding-top: 14px; border-top: 1px solid var(--border); }
.side-foot button { width: 100%; justify-content: center; }
.content { flex: 1; min-width: 0; padding: 34px 44px 72px; }
.page-head { max-width: 1120px; margin: 0 auto 24px; display: flex; align-items: flex-end;
             justify-content: space-between; gap: 16px; flex-wrap: wrap; }
.page-head h1 { font-size: 22px; font-weight: 600; letter-spacing: -0.025em; margin: 0;
                line-height: 1.25; }
.page-head p { margin: 4px 0 0; color: var(--muted); font-size: 13.5px; }
.crumb { color: var(--muted); font-size: 13px; margin-bottom: 4px; display: block; }
.actions { display: flex; gap: 8px; flex-wrap: wrap; }
main { max-width: 1120px; margin: 0 auto; }
@media (max-width: 820px) {
  .shell { flex-direction: column; }
  .sidebar { width: 100%; height: auto; position: static; flex-direction: row; flex-wrap: wrap;
             align-items: center; gap: 6px; padding: 10px 12px; border-right: none;
             border-bottom: 1px solid var(--border); }
  .brand { padding: 2px 8px 2px 4px; }
  .brand small, .nav-label { display: none; }
  .sidebar nav { flex-direction: row; flex-wrap: wrap; }
  .sidebar nav a { padding: 6px 9px; font-size: 13px; }
  .sidebar nav svg { display: none; }
  .side-foot { margin: 0 0 0 auto; padding: 0; border: none; }
  .content { padding: 22px 16px 56px; }
}

/* --- Login --------------------------------------------------------------------------------- */
.auth { min-height: 100vh; display: flex; align-items: center; justify-content: center;
        padding: 24px 16px; }
.auth-card { width: 100%; max-width: 360px; }
.auth-brand { display: flex; align-items: center; gap: 10px; font-weight: 600; font-size: 15px;
              margin-bottom: 28px; }
.auth-card h1 { font-size: 22px; font-weight: 600; letter-spacing: -0.025em; margin: 0 0 4px; }
.auth-card .lead { color: var(--muted); margin: 0 0 20px; font-size: 13.5px; }
.login-form p { margin: 0; }
.login-form input { max-width: none; }
.login-form button { width: 100%; margin-top: 14px; height: 38px; background: var(--accent);
                     border-color: var(--accent); color: var(--on-accent); }
.login-form button:hover { background: var(--accent-hover); border-color: var(--accent-hover); }

/* --- Cards ----------------------------------------------------------------------------------- */
.card { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius);
        box-shadow: var(--shadow); padding: 18px 20px; margin: 0 0 16px; }
.card.flush { padding: 0; overflow: hidden; }
.card-head { display: flex; align-items: center; justify-content: space-between; gap: 12px;
             margin: 0 0 14px; flex-wrap: wrap; }
.card.flush .card-head { padding: 14px 20px 0; margin-bottom: 12px; }
.card h2 { font-size: 14px; font-weight: 600; letter-spacing: -0.01em; margin: 0; }
.card h2 small { color: var(--muted); font-weight: 400; margin-left: 6px; }
.card h3 { font-size: 12.5px; font-weight: 500; color: var(--muted); margin: 18px 0 6px; }
.grid-2 { display: grid; grid-template-columns: repeat(auto-fit, minmax(340px, 1fr)); gap: 16px;
          align-items: start; }
.grid-2 > .card { margin: 0; }
.stack { display: flex; flex-direction: column; gap: 16px; margin-bottom: 16px; }
.note { color: var(--muted); font-size: 13px; margin: 6px 0; }
.empty { color: var(--muted); font-size: 13.5px; text-align: center; padding: 28px 16px; margin: 0; }
.error { background: var(--err-bg); color: var(--err); padding: 10px 14px; border-radius: 8px;
         font-size: 13.5px; margin: 0 0 16px; }

/* --- Stats ----------------------------------------------------------------------------------- */
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
         background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius);
         box-shadow: var(--shadow); margin: 0 0 16px; overflow: hidden; }
.stat { padding: 16px 20px; border-right: 1px solid var(--border); margin-right: -1px; }
.stat-label { display: block; color: var(--muted); font-size: 12.5px; }
.stat-value { display: block; font-size: 24px; font-weight: 600; letter-spacing: -0.03em;
              line-height: 1.3; margin-top: 2px; font-variant-numeric: tabular-nums; }
.stat-hint { display: block; color: var(--faint); font-size: 12px; }

/* --- Tables ---------------------------------------------------------------------------------- */
.table-wrap { overflow-x: auto; }
.card:not(.flush) .table-wrap { border: 1px solid var(--border); border-radius: 10px; }
table { border-collapse: collapse; width: 100%; font-size: 13.5px; }
th { text-align: left; padding: 9px 16px; font-size: 12px; font-weight: 500; color: var(--muted);
     background: var(--subtle); border-bottom: 1px solid var(--border); white-space: nowrap; }
td { padding: 10px 16px; border-bottom: 1px solid var(--border); text-align: left;
     vertical-align: top; }
tbody tr:last-child td { border-bottom: none; }
tbody tr:hover td { background: var(--hover); }
td.right, th.right { text-align: right; }
td.wrap { min-width: 220px; max-width: 420px; }
td pre { margin: 0; }
.sub { display: block; color: var(--muted); font-size: 12px; }
table.kv { font-size: 13.5px; }
table.kv th, table.kv td { background: transparent; border: none; padding: 7px 0;
                          font-size: 13.5px; white-space: normal; }
table.kv th { color: var(--muted); font-weight: 400; width: 190px; }
table.kv tr + tr th, table.kv tr + tr td { border-top: 1px solid var(--border); }
table.kv tbody tr:hover td { background: transparent; }
.row-link { font-family: var(--mono); font-size: 12.5px; }

/* --- Pills ----------------------------------------------------------------------------------- */
.pill { display: inline-flex; align-items: center; gap: 5px; padding: 1px 8px; border-radius: 999px;
        font-size: 12px; font-weight: 500; white-space: nowrap; line-height: 20px;
        background: var(--subtle); color: var(--muted); }
.pill::before { content: ""; width: 6px; height: 6px; border-radius: 50%; background: currentColor;
                opacity: 0.8; }
.pill.plain::before { display: none; }
.pill.ok { background: var(--ok-bg); color: var(--ok); }
.pill.warn { background: var(--warn-bg); color: var(--warn); }
.pill.err { background: var(--err-bg); color: var(--err); }
.pill.accent { background: var(--accent-soft); color: var(--accent-text); }
.chip { display: inline-block; font-family: var(--mono); font-size: 11.5px; padding: 0 6px;
        border: 1px solid var(--border); border-radius: 5px; color: var(--muted); margin: 1px 2px 1px 0;
        line-height: 18px; white-space: nowrap; }
.dur.warn { color: var(--warn); font-weight: 500; }
.dur.err { color: var(--err); font-weight: 500; }

/* --- Buttons --------------------------------------------------------------------------------- */
button, .btn { display: inline-flex; align-items: center; gap: 6px; font: inherit; font-size: 13px;
               font-weight: 500; height: 32px; padding: 0 12px; border: 1px solid var(--border-strong);
               border-radius: 8px; background: var(--surface); color: var(--text); cursor: pointer;
               white-space: nowrap; box-shadow: var(--shadow); }
button:hover, .btn:hover { background: var(--hover); text-decoration: none; }
button.primary, .btn.primary { background: var(--accent); border-color: var(--accent);
                               color: var(--on-accent); }
button.primary:hover, .btn.primary:hover { background: var(--accent-hover);
                                           border-color: var(--accent-hover); }
button.danger { color: var(--err); }
button.danger:hover { background: var(--err-bg); border-color: transparent; }
button.ghost { background: transparent; border-color: transparent; box-shadow: none;
               color: var(--muted); }
button.ghost:hover { background: var(--hover); color: var(--text); }
td button { height: 28px; padding: 0 10px; font-size: 12.5px; }
.btn-row { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 14px; }
form.inline { display: inline; }

/* --- Forms ----------------------------------------------------------------------------------- */
label { display: block; margin: 14px 0 6px; font-weight: 500; font-size: 13px; }
input[type='text'], input[type='password'], input[type='number'], select, textarea {
  font: inherit; font-size: 13.5px; height: 36px; padding: 0 11px; border: 1px solid var(--border-strong);
  border-radius: 8px; background: var(--surface); color: var(--text); width: 100%; max-width: 560px; }
textarea { height: auto; min-height: 120px; padding: 9px 11px; resize: vertical; line-height: 1.5; }
input:focus, select:focus, textarea:focus { outline: none; border-color: var(--accent);
                                            box-shadow: 0 0 0 3px var(--ring); }
.field-row { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.field-row select, .field-row input { flex: 1 1 220px; max-width: none; }

/* --- Settings -------------------------------------------------------------------------------- */
.setting { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1.25fr); gap: 8px 32px;
           padding: 16px 20px; border-top: 1px solid var(--border); }
.card.flush .card-head + .setting { border-top: none; }
.setting h4 { margin: 0; font-size: 13.5px; font-weight: 500; }
.setting h4 code { font-size: 11.5px; color: var(--faint); font-weight: 400; margin-left: 6px; }
.setting p { margin: 3px 0 0; color: var(--muted); font-size: 12.5px; }
.setting form { margin: 0; }
.setting form .field-row { flex-wrap: nowrap; }
.setting form input, .setting form select { max-width: none; }
.setting form textarea { max-width: none; }
.setting .btn-row { margin-top: 8px; }
.source { margin-top: 6px !important; }
@media (max-width: 820px) { .setting { grid-template-columns: 1fr; } }

/* --- Filters ---------------------------------------------------------------------------------- */
.toolbar { display: flex; justify-content: space-between; align-items: center; gap: 12px;
           flex-wrap: wrap; margin: 0 0 14px; }
.filters { display: inline-flex; flex-wrap: wrap; gap: 2px; padding: 3px; background: var(--subtle);
           border: 1px solid var(--border); border-radius: 9px; }
.filters a { padding: 4px 12px; border-radius: 6px; color: var(--muted); font-size: 13px;
             font-weight: 500; }
.filters a:hover { color: var(--text); text-decoration: none; }
.filters a.active { background: var(--surface); color: var(--text);
                    box-shadow: 0 1px 2px rgba(0, 0, 0, 0.08); }

/* --- Timeline --------------------------------------------------------------------------------- */
.bar { display: flex; height: 10px; border-radius: 999px; overflow: hidden; background: var(--subtle);
       margin: 4px 0 12px; }
.bar span { display: block; height: 100%; min-width: 2px; }
.legend { display: flex; flex-wrap: wrap; gap: 6px 18px; font-size: 12.5px; color: var(--muted); }
.legend span { display: inline-flex; align-items: center; gap: 6px; }
.legend i { width: 8px; height: 8px; border-radius: 2px; display: inline-block; }
.legend b { color: var(--text); font-weight: 500; font-variant-numeric: tabular-nums; }
.s-react { background: var(--s-react); } .s-unreact { background: var(--s-react); }
.s-context { background: var(--s-context); } .s-session_start { background: var(--s-session); }
.s-lock_wait { background: var(--s-wait); } .s-turn { background: var(--s-turn); }
.s-render { background: var(--s-render); } .s-send { background: var(--s-send); }
.gap { font-size: 12px; color: var(--faint); display: block; }
.gap.slow { color: var(--warn); }

/* --- Code ------------------------------------------------------------------------------------- */
pre { background: var(--subtle); border: 1px solid var(--border); border-radius: 8px;
      padding: 12px 14px; overflow: auto; white-space: pre-wrap; word-break: break-word;
      font-size: 12.5px; line-height: 1.6; margin: 0; max-height: 520px; }
code { font-size: 0.9em; }
details { margin: 0; }
details + details { margin-top: 6px; }
summary { cursor: pointer; color: var(--accent-text); font-size: 13px; font-weight: 500;
          list-style: none; }
summary::-webkit-details-marker { display: none; }
summary::before { content: "▸"; display: inline-block; width: 14px; color: var(--faint);
                  transition: transform 0.1s; }
details[open] > summary::before { transform: rotate(90deg); }
details pre { margin-top: 8px; }

a:focus-visible, button:focus-visible, summary:focus-visible, input:focus-visible,
select:focus-visible, textarea:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
"""

ICON_PATHS = {
    "/": "<rect x='3' y='3' width='7' height='7' rx='1.5'/><rect x='14' y='3' width='7' height='7' rx='1.5'/>"
    "<rect x='3' y='14' width='7' height='7' rx='1.5'/><rect x='14' y='14' width='7' height='7' rx='1.5'/>",
    "/requests": "<path d='M4 6h16M4 12h16M4 18h10'/>",
    "/prompts": "<path d='M7 3h7l5 5v13H7z'/><path d='M14 3v5h5M10 13h6M10 17h6'/>",
    "/learning": "<path d='M12 3l2.2 5.8L20 11l-5.8 2.2L12 19l-2.2-5.8L4 11l5.8-2.2z'/>",
    "/memories": "<path d='M6 3h12v18l-6-4-6 4z'/>",
    "/channels": "<path d='M9 3L7 21M17 3l-2 18M4 8h17M3 16h17'/>",
    "/tasks": "<circle cx='12' cy='12' r='9'/><path d='M12 7v5l3 2'/>",
    "/settings": "<path d='M4 6h9M19 6h1M4 12h3M13 12h7M4 18h11M21 18h-1'/>"
    "<circle cx='16' cy='6' r='2.2'/><circle cx='10' cy='12' r='2.2'/><circle cx='18' cy='18' r='2.2'/>",
}

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


def icon(href: str) -> str:
    return (
        "<svg width='16' height='16' viewBox='0 0 24 24' fill='none' stroke='currentColor' "
        "stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round' aria-hidden='true'>"
        f"{ICON_PATHS.get(href, '')}</svg>"
    )


def e(text: object) -> str:
    """Escape anything that came from the database or the user, for HTML."""
    return html.escape(str(text) if text is not None else "")


def fmt_time(at: datetime | None) -> str:
    """A UTC timestamp in the bot's local timezone, as the admin sees it in Discord."""
    return at.astimezone(timezone()).strftime("%Y-%m-%d %H:%M:%S") if at else "—"


def fmt_ms(ms: float | None) -> str:
    """A duration for people: 840 ms, 4.2 s or 3 min 12 s."""
    if ms is None:
        return "—"
    if ms < 1000:
        return f"{int(ms)} ms"
    if ms < 60_000:
        return f"{ms / 1000:.1f} s"
    return f"{int(ms // 60_000)} min {int(ms % 60_000 / 1000)} s"


def duration(ms: int | None) -> str:
    """A duration, amber when slow and red when very slow."""
    if ms is None:
        return "<span class='faint'>—</span>"
    level = "err" if ms >= 2 * SLOW_MS else "warn" if ms >= SLOW_MS else ""
    return f"<span class='num dur {level}'>{e(fmt_ms(ms))}</span>"


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


def scope_name(bot: object, scope: str) -> str:
    """The server's name alone, or the scope when the bot doesn't know it."""
    guild_id = guild_id_of(scope)
    guild = bot.get_guild(guild_id) if guild_id is not None else None  # type: ignore[attr-defined]
    return guild.name if guild is not None else scope


def channel_name(bot: object, channel_id: int | None) -> str:
    """#name, or the id when the bot can't see the channel."""
    if channel_id is None:
        return "—"
    channel = bot.get_channel(channel_id)  # type: ignore[attr-defined]
    if channel is None:
        return str(channel_id)
    name = getattr(channel, "name", None) or str(channel_id)
    prefix = "" if isinstance(channel, discord.CategoryChannel) else "#"
    return f"{prefix}{name}"


def channel_label(bot: object, channel_id: int | None) -> str:
    if channel_id is None:
        return "—"
    name = channel_name(bot, channel_id)
    return name if name == str(channel_id) else f"{name} ({channel_id})"


def safe_next(location: str | None) -> str:
    """A redirect target that stays on this dashboard: a path starting with '/' but not '//'."""
    if location and location.startswith("/") and not location.startswith("//"):
        return location
    return "/"


def redirect(location: str | None) -> NoReturn:
    """303 back to a page of this dashboard (never to another site)."""
    raise web.HTTPSeeOther(location=safe_next(location))


BRAND_SVG = (
    "<svg width='28' height='28' viewBox='0 0 28 28' fill='none' aria-hidden='true'>"
    "<rect width='28' height='28' rx='8' fill='#5b5bd6'/>"
    "<path d='M9.5 7.5h4.6a6.5 6.5 0 0 1 0 13H9.5v-13z' fill='#fff'/>"
    "<circle cx='14' cy='14' r='2.2' fill='#5b5bd6'/>"
    "</svg>"
)


def page(
    title: str,
    body: str,
    current: str = "/",
    nav: bool = True,
    status: int = 200,
    subtitle: str = "",
    actions: str = "",
    crumb: str = "",
) -> web.Response:
    """Wrap a page body in the app shell: a sidebar with the nav (or, with nav=False, the centered
    login card). subtitle, actions and crumb are HTML for the page header."""
    if nav:
        links = "".join(
            f"<a href='{e(href)}'{' class="active"' if href == current else ''}>"
            f"{icon(href)}<span>{e(label)}</span></a>"
            for href, label in NAV
        )
        chrome = (
            "<div class='shell'>"
            "<aside class='sidebar'>"
            f"<a class='brand' href='/'>{BRAND_SVG}<span>Debeloglav<small>Admin</small></span></a>"
            f"<div class='nav-label'>Menu</div><nav>{links}</nav>"
            "<form class='side-foot' method='post' action='/logout'>"
            "<button type='submit' class='ghost'>Log out</button></form>"
            "</aside>"
            "<div class='content'>"
            "<header class='page-head'><div>"
            f"{crumb}<h1>{e(title)}</h1>{f'<p>{subtitle}</p>' if subtitle else ''}</div>"
            f"{f'<div class=actions>{actions}</div>' if actions else ''}</header>"
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
        "<link rel='preload' href='/static/Geist-Variable.woff2' as='font' type='font/woff2' "
        "crossorigin>"
        f"<title>{e(title)} · Debeloglav</title>"
        f"<style>{LAYOUT_CSS}</style></head><body>{chrome}</body></html>",
        content_type="text/html",
        status=status,
    )


def card(title: str, body: str, actions: str = "", flush: bool = False) -> str:
    head = (
        f"<div class='card-head'><h2>{title}</h2>{actions}</div>"
        if title or actions
        else ""
    )
    return f"<section class='card{' flush' if flush else ''}'>{head}{body}</section>"


def table(headers: list[str], rows: list[str], empty: str) -> str:
    """A table with escaped headers (a header ending in '>' is right-aligned), or an empty note."""
    if not rows:
        return f"<p class='empty'>{e(empty)}</p>"
    head = "".join(
        f"<th class='right'>{e(h[:-1])}</th>" if h.endswith(">") else f"<th>{e(h)}</th>"
        for h in headers
    )
    return (
        f"<div class='table-wrap'><table><thead><tr>{head}</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div>"
    )


def stat(value: object, label: str, hint: str = "") -> str:
    return (
        f"<div class='stat'><span class='stat-label'>{e(label)}</span>"
        f"<span class='stat-value'>{e(value)}</span>"
        + (f"<span class='stat-hint'>{e(hint)}</span>" if hint else "")
        + "</div>"
    )


def kv(rows: list[tuple[str, str]]) -> str:
    """A label/value table; the values are HTML."""
    return (
        "<table class='kv'><tbody>"
        + "".join(
            f"<tr><th>{e(label)}</th><td>{value}</td></tr>" for label, value in rows
        )
        + "</tbody></table>"
    )


def download(href: str, label: str) -> str:
    return f"<a class='btn' href='{e(href)}'>{e(label)}</a>"


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
# script) and the fonts from /static working while blocking framing and external loads.
SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
        "font-src 'self'; "
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
        "<p class='lead'>Enter the dashboard password to continue.</p>"
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
    public = request.path == "/login" or request.path.startswith("/static/")
    if not public and not _session_valid(request):
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
        if request.path.startswith("/static/") and response.status == 200:
            response.headers["Cache-Control"] = "public, max-age=604800"  # the fonts

    app.on_response_prepare.append(_add_security_headers)  # fires for every response
    app.router.add_get("/login", login_page, name="login")
    app.router.add_post("/login", login_post, name="login_post")
    app.router.add_post("/logout", logout, name="logout")
    app.router.add_get("/", overview, name="overview")
    app.router.add_get("/requests", requests_page, name="requests")
    app.router.add_get("/requests/{id}", request_detail, name="request_detail")
    app.router.add_get("/requests/{id}/json", export_request, name="export_request")
    app.router.add_get(
        "/export/requests.json", export_requests_json, name="export_requests_json"
    )
    app.router.add_get(
        "/export/requests.csv", export_requests_csv, name="export_requests_csv"
    )
    app.router.add_get("/export/learning.json", export_learning, name="export_learning")
    app.router.add_get("/export/all.json", export_all, name="export_all")
    app.router.add_get("/static/{name}", static_file, name="static")
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


# --- Statistics ----------------------------------------------------------------------------------

STAGES = [  # the stages a request's trace times, in order, with their labels
    ("react", "React"),
    ("context", "Read context"),
    ("session_start", "Start session"),
    ("lock_wait", "Wait for conversation"),
    ("turn", "Agent turn"),
    ("render", "Render"),
    ("unreact", "Unreact"),
    ("send", "Send reply"),
]
STEP_LABELS = {
    "turn_started": "Turn started",
    "model_writing": "Model starts writing",
    "model_message": "Model message",
    "tool_call": "Tool call",
    "tool_result": "Tool result",
    "user_message": "Input",
    "context_message": "Context",
    "notice": "Notice",
    "turn_ended": "Turn ended",
    "busy_wait": "Waited for another turn",
    "session_restarted": "Session restarted",
}
# What the time before a step was spent on.
STEP_GAPS = {
    "model_writing": "model thinking",
    "tool_call": "model thinking",
    "tool_result": "tool running",
    "model_message": "model writing",
}


def percentile(values: list[int], p: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(p * len(ordered)) - 1)]


def tool_name(name: str | None) -> str:
    """A tool's short name: web_search, not its full id."""
    return (name or "?").rsplit(".", 1)[-1]


def tool_timings(trace: dict) -> list[tuple[str, int]]:
    """How long each tool call ran: each call paired with the next result of the same tool."""
    pending: dict[str, list[int]] = {}
    timings: list[tuple[str, int]] = []
    for step in trace.get("steps", []):
        name = tool_name(step.get("name"))
        if step.get("kind") == "tool_call":
            pending.setdefault(name, []).append(step["at_ms"])
        elif step.get("kind") == "tool_result":
            calls = pending.get(name) or next((q for q in pending.values() if q), None)
            if calls:
                timings.append((name, step["at_ms"] - calls.pop(0)))
    return timings


def tools_used(a: store.Activity) -> list[str]:
    calls = a.trace.get("tool_calls")
    if calls:
        return [tool_name(c) for c in calls]
    return [
        tool_name(s.get("name"))
        for s in a.trace.get("steps", [])
        if s.get("kind") == "tool_call"
    ]


def tool_chips(a: store.Activity) -> str:
    names = tools_used(a)
    if not names:
        return "<span class='faint'>—</span>"
    counts: dict[str, int] = {}
    for name in names:
        counts[name] = counts.get(name, 0) + 1
    return "".join(
        f"<span class='chip'>{e(name)}{f' ×{n}' if n > 1 else ''}</span>"
        for name, n in counts.items()
    )


def stage_bar(stages: dict[str, int | float]) -> str:
    """A stacked bar of where the time went, with a legend."""
    known = [
        (key, label, stages.get(key, 0)) for key, label in STAGES if stages.get(key)
    ]
    total = sum(ms for _key, _label, ms in known)
    if not total:
        return "<p class='note'>No timing recorded for this request.</p>"
    bar = "".join(
        f"<span class='s-{key}' style='width:{ms / total * 100:.2f}%' "
        f"title='{e(label)}: {e(fmt_ms(ms))}'></span>"
        for key, label, ms in known
    )
    legend = "".join(
        f"<span><i class='s-{key}'></i>{e(label)} <b>{e(fmt_ms(ms))}</b></span>"
        for key, label, ms in known
    )
    return f"<div class='bar'>{bar}</div><div class='legend'>{legend}</div>"


# --- Overview ------------------------------------------------------------------------------------


async def overview(request: web.Request) -> web.Response:
    bot = request.app[BOT]
    now = datetime.now(UTC)
    day_ago = now - timedelta(hours=24)
    activity = store.recent_activity(store.ACTIVITY_LIMIT)
    last_day = [a for a in activity if a.at >= day_ago]
    errors = [a for a in last_day if not a.ok]
    replies = [a.duration_ms for a in last_day if a.kind == "reply" and a.duration_ms]

    last_run: dict[str, store.StyleRun] = {}
    for run in store.style_runs(limit=500):  # newest first
        last_run.setdefault(run.scope, run)

    user = getattr(bot, "user", None)
    user_name = getattr(user, "name", None) or str(user) if user else "(not logged in)"
    latency = getattr(bot, "latency", None)
    uptime = timedelta(seconds=int(time.monotonic() - request.app[STARTED]))
    guilds = getattr(bot, "guilds", [])

    stats = (
        "<div class='stats'>"
        + stat(len(last_day), "Requests, last 24 h")
        + stat(len(errors), "Errors, last 24 h")
        + stat(
            fmt_ms(percentile(replies, 0.5)), "Median reply", f"{len(replies)} replies"
        )
        + stat(
            fmt_ms(percentile(replies, 0.95)), "Slowest 5% of replies", "p95, last 24 h"
        )
        + stat(len(store.all_memories()), "Memories")
        + stat(len(store.all_tasks()), "Active tasks")
        + stat(store.conversation_count(), "Open conversations")
        + "</div>"
    )

    connected = (
        "<span class='pill ok'>Connected</span>"
        if bot.is_ready()
        else "<span class='pill err'>Disconnected</span>"
    )
    bot_card = card(
        "Bot",
        kv(
            [
                ("Status", connected),
                (
                    "User",
                    f"{e(user_name)} <span class='faint'>{e(getattr(user, 'id', ''))}</span>",
                ),
                (
                    "Gateway latency",
                    e(f"{latency * 1000:.0f} ms" if latency is not None else "—"),
                ),
                ("Uptime", e(uptime)),
                (
                    "Model",
                    (
                        f"<code>{e(settings.get('model'))}</code> · reasoning "
                        f"<code>{e(settings.get('reasoning_effort'))}</code>"
                    ),
                ),
                (
                    "Guilds",
                    f"{len(guilds)}: {e(', '.join(g.name for g in guilds) or '—')}",
                ),
            ]
        ),
    )

    # Where reply time goes: the average of each stage over the traced replies of the last day.
    traced = [a for a in last_day if a.kind == "reply" and a.trace.get("stages")]
    averages = (
        {
            key: sum(a.trace["stages"].get(key, 0) for a in traced) / len(traced)
            for key, _label in STAGES
        }
        if traced
        else {}
    )
    time_card = card(
        f"Where reply time goes <small>average of {len(traced)} replies, last 24 h</small>",
        stage_bar(averages)
        if traced
        else "<p class='empty'>No traced replies yet.</p>",
    )

    per_tool: dict[str, list[int]] = {}
    for a in last_day:
        for name, ms in tool_timings(a.trace):
            per_tool.setdefault(name, []).append(ms)
    tool_rows = [
        f"<tr><td><code>{e(name)}</code></td><td class='right num'>{len(ms)}</td>"
        f"<td class='right'>{duration(int(sum(ms) / len(ms)))}</td>"
        f"<td class='right'>{duration(max(ms))}</td></tr>"
        for name, ms in sorted(per_tool.items(), key=lambda item: -sum(item[1]))
    ]
    tools_card = card(
        "Tools <small>last 24 h</small>",
        table(
            ["Tool", "Calls>", "Average>", "Slowest>"],
            tool_rows,
            "No tool calls recorded.",
        ),
        flush=True,
    )

    run_rows = [
        f"<tr><td>{e(scope_name(bot, scope))}<span class='sub'>{e(scope)}</span></td>"
        f"<td class='num'>{e(fmt_time(run.at))}</td><td class='right num'>{run.messages}</td>"
        f"<td>{status_pill(run_status(run))}</td></tr>"
        for scope, run in sorted(last_run.items())
    ]
    error_rows = [
        f"<tr><td class='num'>{e(fmt_time(a.at))}</td><td>{e(a.kind)}</td>"
        f"<td><a class='row-link' href='/requests/{a.id}'>#{a.id}</a></td>"
        f"<td>{e(scope_name(bot, a.scope))}</td><td class='wrap'>{e(snippet(a.error, 140))}</td></tr>"
        for a in [a for a in activity if not a.ok][:5]
    ]
    body = (
        stats
        + f"<div class='grid-2'>{bot_card}<div>{time_card}{tools_card}</div></div>"
        + "<div class='stack'></div>"
        + card(
            "Last style run per guild",
            table(
                ["Guild", "At", "Messages>", "Result"], run_rows, "No style runs yet."
            ),
            flush=True,
        )
        + card(
            "Last 5 errors",
            table(
                ["At", "Kind", "Request", "Guild", "Error"],
                error_rows,
                "No errors recorded.",
            ),
            flush=True,
        )
    )
    return page(
        "Overview",
        body,
        current="/",
        subtitle="What the bot is doing right now.",
        actions=download("/export/all.json", "Export everything"),
    )


def run_status(run: store.StyleRun) -> str:
    if run.error:
        return f"error: {run.error}"
    if run.guide:
        return "learned"
    return f"skipped (needs at least {style.min_messages()} messages)"


def skipped_channels(run: store.StyleRun) -> str:
    """The channels a style run left out and why, folded away when there are any."""
    if not run.skipped:
        return "<span class='faint'>none</span>"
    return (
        f"<details><summary>{len(run.skipped)} channels</summary>"
        f"<pre>{e(chr(10).join(run.skipped))}</pre></details>"
    )


def status_pill(text: str) -> str:
    """A style-run status as a coloured pill: errors red, learned green, skipped amber."""
    if text.startswith("error:"):
        return f"<span class='pill err' title='{e(text)}'>{e(snippet(text, 60))}</span>"
    if text == "learned":
        return "<span class='pill ok'>learned</span>"
    return f"<span class='pill warn'>{e(text)}</span>"


def result_pill(a: store.Activity) -> str:
    """A request's result as a coloured pill: ok green, failure red with the error."""
    if a.ok:
        return "<span class='pill ok'>ok</span>"
    return (
        f"<span class='pill err' title='{e(a.error)}'>{e(snippet(a.error, 40))}</span>"
    )


# --- Requests ------------------------------------------------------------------------------------


def filtered_activity(query) -> tuple[list[store.Activity], str]:
    """The stored requests the query's kind/errors filter keeps, and the active filter's key."""
    kind = query.get("kind", "")
    errors_only = query.get("errors") == "1"
    rows = store.recent_activity(store.ACTIVITY_LIMIT)
    if kind in KINDS:
        rows = [a for a in rows if a.kind == kind]
    if errors_only:
        rows = [a for a in rows if not a.ok]
    return rows, "errors" if errors_only else (kind if kind in KINDS else "")


async def requests_page(request: web.Request) -> web.Response:
    bot = request.app[BOT]
    rows, active_filter = filtered_activity(request.query)
    query = urlencode(
        {k: v for k, v in request.query.items() if k in ("kind", "errors")}
    )
    suffix = f"?{query}" if query else ""
    filters = "".join(
        f'<a href="/requests?{urlencode(f)}"'
        + (' class="active"' if key == active_filter else "")
        + f">{e(label)}</a>"
        for f, label, key in [
            ({}, "All", ""),
            ({"kind": "reply"}, "Replies", "reply"),
            ({"kind": "research"}, "Research", "research"),
            ({"kind": "task"}, "Tasks", "task"),
            ({"errors": "1"}, "Errors", "errors"),
        ]
    )
    table_rows = [
        f"<tr><td class='num'>{e(fmt_time(a.at))}<span class='sub'>{e(a.kind)}</span></td>"
        f"<td>{e(channel_name(bot, a.channel_id))}<span class='sub'>{e(scope_name(bot, a.scope))}</span></td>"
        f"<td>{e(a.user_name or '—')}</td><td class='wrap'>{e(snippet(a.question, 90))}</td>"
        f"<td>{tool_chips(a)}</td>"
        f"<td>{result_pill(a)}</td>"
        f"<td class='right'>{duration(a.duration_ms)}</td>"
        f'<td class="right"><a class="row-link" href="/requests/{a.id}">#{a.id}</a></td></tr>'
        for a in rows[:300]
    ]
    shown = (
        f"{min(len(rows), 300)} of {len(rows)}" if len(rows) > 300 else str(len(rows))
    )
    body = (
        f"<div class='toolbar'><div class='filters'>{filters}</div>"
        f"<span class='note'>{e(shown)} requests · times in {e(timezone().key)}</span></div>"
        + card(
            "",
            table(
                [
                    "Time",
                    "Where",
                    "User",
                    "Question",
                    "Tools",
                    "Result",
                    "Duration>",
                    ">",
                ],
                table_rows,
                "No requests recorded yet.",
            ),
            flush=True,
        )
    )
    actions = download(f"/export/requests.json{suffix}", "Export JSON") + download(
        f"/export/requests.csv{suffix}", "Export CSV"
    )
    return page(
        "Requests",
        body,
        current="/requests",
        subtitle="Every reply, research job and scheduled task, with where its time went.",
        actions=actions,
    )


async def request_detail(request: web.Request) -> web.Response:
    bot = request.app[BOT]
    a = activity_or_404(request)
    t = a.trace

    def block(text: str | None) -> str:
        return f"<pre>{e(text or '(none)')}</pre>"

    details = [
        ("Time", e(f"{fmt_time(a.at)} ({timezone().key})")),
        ("Kind", e(a.kind)),
        ("Guild", e(scope_label(bot, a.scope))),
        ("Channel", e(channel_label(bot, a.channel_id))),
        ("User", e(a.user_name or "—")),
        ("Duration", duration(a.duration_ms)),
        ("Result", result_pill(a)),
    ]
    if t.get("message_url"):
        details.append(
            ("Message", f"<a href='{e(t['message_url'])}'>Open in Discord</a>")
        )
    if t.get("model"):
        details.append(
            (
                "Model",
                (
                    f"<code>{e(t['model'])}</code> · reasoning "
                    f"<code>{e(t.get('reasoning_effort') or 'none')}</code>"
                ),
            )
        )
    if "continuing" in t:
        details.append(("Conversation", "continued" if t["continuing"] else "new"))
    if t.get("session_id"):
        details.append(("Session", f"<code>{e(t['session_id'])}</code>"))
    if "prompt_chars" in t:
        details.append(
            (
                "Input",
                e(f"{t['prompt_chars']:,} characters · {t.get('images', 0)} images"),
            )
        )
    if t.get("state"):
        end = t["state"] + (f" ({t['stop_reason']})" if t.get("stop_reason") else "")
        details.append(("Turn ended", e(end)))
    if t.get("turn_error"):
        details.append(("Turn error", f"<code>{e(t['turn_error'])}</code>"))

    steps = t.get("steps", [])
    step_rows = []
    previous = None
    for step in steps:
        gap = step["at_ms"] - previous if previous is not None else None
        previous = step["at_ms"]
        gap_html = ""
        if gap is not None:
            why = STEP_GAPS.get(step["kind"], "")
            gap_html = (
                f"<span class='gap{' slow' if gap >= 3000 else ''}'>+{e(fmt_ms(gap))}"
                f"{' · ' + e(why) if why else ''}</span>"
            )
        detail = step.get("detail", "")
        detail_html = (
            f"<details><summary>{e(snippet(detail, 70))}</summary><pre>{e(detail)}</pre></details>"
            if len(detail) > 70
            else e(detail)
        )
        is_tool = step["kind"].startswith("tool")
        pill = "err" if step.get("error") else "accent" if is_tool else ""
        name = step.get("name")
        name_html = (
            f"<code>{e(tool_name(name) if is_tool else name)}</code>" if name else ""
        )
        label = STEP_LABELS.get(step["kind"], step["kind"])
        step_rows.append(
            f"<tr><td class='num'>{e(fmt_ms(step['at_ms']))}{gap_html}</td>"
            f"<td><span class='pill plain {pill}'>{e(label)}</span></td>"
            f"<td>{name_html}</td>"
            f"<td class='wrap'>{detail_html}</td></tr>"
        )
    timeline = (
        stage_bar(t.get("stages", {}))
        + "<h3>Steps of the agent's turn</h3>"
        + table(["At", "Step", "Tool", "Detail"], step_rows, "No steps recorded.")
        if t
        else "<p class='empty'>This request was recorded before requests were traced.</p>"
    )
    body = (
        f"<div class='grid-2'>{card('Details', kv(details))}{card('Timeline', timeline)}</div>"
        f"<div class='stack'></div>"
        + card("Question", block(a.question))
        + card("Prompt sent to the model", block(a.prompt))
        + card("Reply", block(a.reply))
        + (card("Error", block(a.error)) if a.error else "")
    )
    return page(
        f"Request #{a.id}",
        body,
        current="/requests",
        crumb="<a class='crumb' href='/requests'>← Requests</a>",
        actions=download(f"/requests/{a.id}/json", "Export JSON"),
    )


def activity_or_404(request: web.Request) -> store.Activity:
    try:
        activity_id = int(request.match_info["id"])
    except ValueError:
        raise web.HTTPNotFound(text="No such request") from None
    a = store.activity(activity_id)
    if a is None:
        raise web.HTTPNotFound(text="No such request")
    return a


# --- Exports -------------------------------------------------------------------------------------


def activity_json(bot: object, a: store.Activity) -> dict:
    data = store.as_json(a)
    data["guild"] = scope_name(bot, a.scope)
    data["channel"] = channel_name(bot, a.channel_id)
    return data


def attachment(body: str, name: str, content_type: str) -> web.Response:
    stamp = datetime.now(timezone()).strftime("%Y%m%d-%H%M")
    return web.Response(
        text=body,
        content_type=content_type,
        headers={
            "Content-Disposition": f'attachment; filename="debeloglav-{name}-{stamp}"'
        },
    )


def json_attachment(data: object, name: str) -> web.Response:
    body = json.dumps(data, ensure_ascii=False, indent=2, default=str)
    return attachment(body, f"{name}.json", "application/json")


async def export_request(request: web.Request) -> web.Response:
    a = activity_or_404(request)
    return json_attachment(activity_json(request.app[BOT], a), f"request-{a.id}")


async def export_requests_json(request: web.Request) -> web.Response:
    bot = request.app[BOT]
    rows, _active = filtered_activity(request.query)
    return json_attachment([activity_json(bot, a) for a in rows], "requests")


CSV_COLUMNS = [
    "id",
    "at",
    "kind",
    "scope",
    "guild",
    "channel_id",
    "channel",
    "user_name",
    "question",
    "reply",
    "ok",
    "error",
    "duration_ms",
    "model",
    "reasoning_effort",
    "tools",
    *[f"{key}_ms" for key, _label in STAGES],
    "prompt_chars",
    "images",
    "session_id",
]


async def export_requests_csv(request: web.Request) -> web.Response:
    bot = request.app[BOT]
    rows, _active = filtered_activity(request.query)
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=CSV_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for a in rows:
        t = a.trace
        writer.writerow(
            {
                **activity_json(bot, a),
                "model": t.get("model"),
                "reasoning_effort": t.get("reasoning_effort"),
                "tools": " ".join(tools_used(a)),
                **{f"{key}_ms": t.get("stages", {}).get(key) for key, _label in STAGES},
                "prompt_chars": t.get("prompt_chars"),
                "images": t.get("images"),
                "session_id": t.get("session_id"),
            }
        )
    return attachment(out.getvalue(), "requests.csv", "text/csv")


async def export_learning(request: web.Request) -> web.Response:
    return json_attachment(learning_json(request.app[BOT]), "learning")


def learning_json(bot: object) -> dict:
    return {
        "styles": [
            {"scope": scope, "guild": scope_name(bot, scope), **asdict(st)}
            for scope, st in store.all_styles()
        ],
        "runs": [store.as_json(r) for r in store.style_runs(limit=10_000)],
    }


async def export_all(request: web.Request) -> web.Response:
    """Everything the dashboard shows, in one file: for debugging, or to hand to someone."""
    bot = request.app[BOT]
    user = getattr(bot, "user", None)
    data = {
        "exported_at": datetime.now(UTC).isoformat(),
        "timezone": timezone().key,
        "bot": {
            "user": getattr(user, "name", None),
            "id": getattr(user, "id", None),
            "ready": bot.is_ready(),
            "latency_ms": int(bot.latency * 1000)
            if getattr(bot, "latency", None)
            else None,
            "uptime_s": int(time.monotonic() - request.app[STARTED]),
            "guilds": [
                {"id": g.id, "name": g.name} for g in getattr(bot, "guilds", [])
            ],
        },
        "settings": {
            "current": settings.current(),
            "defaults": settings.defaults(),
            "overrides": settings.overrides(),
        },
        "requests": [
            activity_json(bot, a) for a in store.recent_activity(store.ACTIVITY_LIMIT)
        ],
        **learning_json(bot),
        "memories": [
            {"scope": scope, **asdict(m)} for scope, m in store.all_memories_by_scope()
        ],
        "tasks": [asdict(t) for t in store.all_tasks()],
        "blocked_channels": [
            {"guild_id": g, "channel_id": c, "channel": channel_name(bot, c)}
            for g, c in store.all_blocked_channels()
        ],
    }
    return json_attachment(data, "export")


# --- Prompts -------------------------------------------------------------------------------------


def prompt_card(title: str, text: str) -> str:
    return card(
        f"{e(title)} <small>{len(text):,} characters</small>",
        f"<details><summary>Show</summary><pre>{e(text)}</pre></details>",
    )


async def prompts_page(request: web.Request) -> web.Response:
    bot = request.app[BOT]
    scopes = {f"guild:{g.id}" for g in getattr(bot, "guilds", [])}
    scopes.update(scope for scope, _memory in store.all_memories_by_scope())
    scopes.update(scope for scope, _st in store.all_styles())
    parts = [
        card(
            "Effective system prompt (INSTRUCTIONS + extra)",
            f"<pre>{e(agent.effective_instructions())}</pre>",
        ),
        prompt_card("Research lead prompt", agent.RESEARCH_INSTRUCTIONS),
        prompt_card("Researcher subagent prompt", agent.RESEARCHER_INSTRUCTIONS),
        prompt_card("Style summarizer prompt", style.SUMMARIZER),
    ]
    for scope in sorted(scopes):
        memory = agent.memory_prompt(scope)
        guide = agent.style_prompt(scope)
        parts.append(
            card(
                e(scope_label(bot, scope)),
                f"<h3>Memory block, as injected</h3><pre>{e(memory)}</pre>"
                f"<h3>Style guide block, as injected</h3>"
                + (
                    f"<pre>{e(guide)}</pre>"
                    if guide
                    else "<p class='note'>(empty — nothing injected)</p>"
                ),
            )
        )
    return page(
        "Prompts",
        "".join(parts),
        current="/prompts",
        subtitle="The text the models get, as they get it.",
    )


# --- Learning ------------------------------------------------------------------------------------


async def learning_page(request: web.Request) -> web.Response:
    bot = request.app[BOT]
    parts = []
    for guild in getattr(bot, "guilds", []):
        scope = f"guild:{guild.id}"
        st = store.style(scope)
        if st.learned_at and st.enabled:
            due = fmt_time(st.learned_at + style.learn_every())
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
            else "<span class='pill'>disabled</span>"
        )
        buttons = (
            f"<form class='btn-row' method='post' action='/learning/action'>"
            f"<input type='hidden' name='scope' value='{e(scope)}'>"
            f"<input type='hidden' name='next' value='/learning'>"
            f"<button type='submit' name='action' value='enable'>Enable</button>"
            f"<button type='submit' name='action' value='disable' class='danger'>Disable</button>"
            f"<button type='submit' name='action' value='relearn'>Relearn now</button>"
            f"</form>"
        )
        parts.append(
            card(
                f"{e(guild.name)} <small>{e(scope)}</small>",
                kv(
                    [
                        ("Status", enabled_pill),
                        (
                            "Channels",
                            (
                                "every channel the bot can read, except "
                                "<a href='/channels'>blocked ones</a>"
                            ),
                        ),
                        ("Learned at", e(fmt_time(st.learned_at))),
                        ("Next due", e(due)),
                    ]
                )
                + guide
                + buttons,
            )
        )

    runs = store.style_runs(limit=200)
    run_rows = [
        f"<tr><td class='num'>{e(fmt_time(r.at))}</td><td>{e(scope_name(bot, r.scope))}</td>"
        f"<td class='right num'>{r.messages}</td><td>{status_pill(run_status(r))}</td>"
        f"<td>{skipped_channels(r)}</td>"
        f"<td>{f'<details><summary>guide</summary><pre>{e(r.guide)}</pre></details>' if r.guide else '<span class=faint>—</span>'}</td></tr>"
        for r in runs
    ]
    parts.append(
        card(
            "History",
            table(
                ["Time", "Guild", "Messages>", "Result", "Skipped channels", "Guide"],
                run_rows,
                "No style runs yet.",
            ),
            flush=True,
        )
    )
    return page(
        "Learning",
        "".join(parts),
        current="/learning",
        subtitle=f"The style guide is refreshed every {e(settings.get('learn_every_hours'))} h "
        f"from the server's messages; a run needs at least {style.min_messages()} new ones.",
        actions=download("/export/learning.json", "Export JSON"),
    )


async def learning_action(request: web.Request) -> web.Response:
    data = await request.post()
    scope = data.get("scope", "")
    action = data.get("action", "")
    if scope.startswith("guild:"):
        if action == "enable":
            store.configure_style(scope, enabled=True)
        elif action == "disable":
            store.configure_style(scope, enabled=False)
        elif action == "relearn":
            store.configure_style(scope, relearn=True)
    return redirect(data.get("next") or "/learning")


# --- Memories ------------------------------------------------------------------------------------


async def memories_page(request: web.Request) -> web.Response:
    bot = request.app[BOT]
    groups: dict[str, list[store.Memory]] = {}
    for scope, memory in store.all_memories_by_scope():
        groups.setdefault(scope, []).append(memory)

    parts = []
    for scope in sorted(groups):
        rows = [
            f"<tr><td class='num faint'>#{m.id}</td><td class='wrap'>{e(m.text)}</td>"
            f"<td>{e(m.author)}</td>"
            f"<td class='right'><form class='inline' method='post' action='/memories/delete'>"
            f"<input type='hidden' name='scope' value='{e(scope)}'>"
            f"<input type='hidden' name='id' value='{m.id}'>"
            f"<input type='hidden' name='next' value='/memories'>"
            f"<button type='submit' class='danger'>Delete</button></form></td></tr>"
            for m in groups[scope]
        ]
        parts.append(
            card(
                f"{e(scope_label(bot, scope))} <small>{len(rows)}</small>",
                table(["ID", "Memory", "Author", ""], rows, "No memories."),
                flush=True,
            )
        )
    if not parts:
        parts.append(card("", "<p class='empty'>No memories yet.</p>"))

    options = "".join(
        f"<option value='guild:{g.id}'>{e(g.name)}</option>"
        for g in getattr(bot, "guilds", [])
    )
    parts.append(
        card(
            "Add a memory",
            f"<form method='post' action='/memories/add'>"
            f"<input type='hidden' name='next' value='/memories'>"
            f"<label>Server</label><select name='scope'>{options}</select>"
            f"<label>Text</label><input type='text' name='text' required>"
            f"<div class='btn-row'><button type='submit' class='primary'>Add</button></div>"
            f"</form>",
        )
    )
    return page(
        "Memories",
        "".join(parts),
        current="/memories",
        subtitle="Lasting facts the bot knows in every conversation of a server.",
    )


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
    parts = []
    per_guild: dict[int, list[int]] = {}
    for guild_id, channel_id in store.all_blocked_channels():
        per_guild.setdefault(guild_id, []).append(channel_id)

    for guild in getattr(bot, "guilds", []):
        rows = [
            f"<tr><td>{e(channel_name(bot, cid))}<span class='sub'>{cid}</span></td>"
            f"<td class='right'><form class='inline' method='post' action='/channels/unblock'>"
            f"<input type='hidden' name='guild_id' value='{guild.id}'>"
            f"<input type='hidden' name='channel_id' value='{cid}'>"
            f"<input type='hidden' name='next' value='/channels'>"
            f"<button type='submit'>Unblock</button></form></td></tr>"
            for cid in per_guild.get(guild.id, [])
        ]
        places = list(getattr(guild, "text_channels", [])) + list(
            getattr(guild, "categories", [])
        )
        options = "".join(
            f"<option value='{p.id}'>{e(channel_name(bot, p.id))}</option>"
            for p in places
        )
        parts.append(
            card(
                f"{e(guild.name)} <small>{guild.id}</small>",
                (
                    table(["Blocked", ""], rows, "")
                    if rows
                    else "<p class='note'>Nothing blocked on this server.</p>"
                )
                + f"<form method='post' action='/channels/block'>"
                f"<input type='hidden' name='guild_id' value='{guild.id}'>"
                f"<input type='hidden' name='next' value='/channels'>"
                f"<label>Block a channel or category</label>"
                f"<div class='field-row'><select name='channel_id'>{options}</select>"
                f"<button type='submit' class='danger'>Block</button></div></form>",
            )
        )
    return page(
        "Channels",
        "".join(parts),
        current="/channels",
        subtitle="The bot doesn't read blocked channels at all: not for messages, search, "
        "catch-up or style learning.",
    )


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
    rows = [
        f"<tr><td class='num faint'>#{t.id}</td>"
        f"<td>{e(channel_name(bot, t.channel_id))}<span class='sub'>{e(scope_name(bot, t.scope))}</span></td>"
        f"<td>{e(t.user_name)}</td>"
        f"<td class='wrap'>{e(t.prompt)}</td>"
        f"<td><code>{e(t.cron or 'once')}</code><span class='sub num'>next {e(fmt_time(t.next_run))}</span></td>"
        f"<td>{'<span class="pill accent plain">watch</span> ' if t.watch else ''}"
        f"{'<span class="pill plain">mention</span>' if t.mention else ''}</td>"
        f"<td class='right'><form class='inline' method='post' action='/tasks/cancel'>"
        f"<input type='hidden' name='scope' value='{e(t.scope)}'>"
        f"<input type='hidden' name='id' value='{t.id}'>"
        f"<input type='hidden' name='next' value='/tasks'>"
        f"<button type='submit' class='danger'>Cancel</button></form></td></tr>"
        for t in store.all_tasks()
    ]
    body = card(
        "",
        table(
            ["ID", "Where", "Created by", "Prompt", "Schedule", "Flags", ""],
            rows,
            "No active tasks.",
        ),
        flush=True,
    )
    return page(
        "Tasks",
        body,
        current="/tasks",
        subtitle="Reminders, recurring jobs and watches people scheduled.",
    )


async def task_cancel(request: web.Request) -> web.Response:
    data = await request.post()
    try:
        task_id = int(data.get("id", ""))
    except ValueError:
        task_id = -1
    store.cancel_task(data.get("scope", ""), task_id)
    return redirect(data.get("next") or "/tasks")


# --- Settings ------------------------------------------------------------------------------------


def setting_field(setting: settings.Setting, value: str) -> str:
    if setting.kind == "choice":
        options = "".join(
            f"<option value='{e(v)}'"
            + (" selected" if value == v else "")
            + f">{e(v)}</option>"
            for v in setting.choices
        )
        return f"<select name='value'>{options}</select>"
    if setting.kind == "textarea":
        return f"<textarea name='value'>{e(value)}</textarea>"
    if setting.kind == "int":
        limits = "".join(
            f" {attr}='{bound}'"
            for attr, bound in (("min", setting.minimum), ("max", setting.maximum))
            if bound is not None
        )
        return f"<input type='number' name='value' value='{e(value)}'{limits}>"
    return f"<input type='text' name='value' value='{e(value)}'>"


async def settings_page(request: web.Request) -> web.Response:
    current = settings.current()
    defaults = settings.defaults()
    overrides = settings.overrides()
    error = request.query.get("error")
    parts = [f"<p class='error'>{e(error)}</p>"] if error else []
    for group in settings.GROUPS:
        rows = []
        for s in [s for s in settings.SETTINGS if s.group == group]:
            source = (
                f"<span class='pill accent'>changed here</span> "
                f"<span class='faint'>default <code>{e(defaults[s.key] or '(empty)')}</code></span>"
                if s.key in overrides
                else f"<span class='faint'>from {'.env <code>' + e(s.env) + '</code>' if s.env and os.environ.get(s.env) is not None else 'the built-in default'}</span>"
            )
            reset = "<button type='submit' name='reset' value='1' class='ghost'>Reset to default</button>"
            field = setting_field(s, current[s.key])
            buttons = "<button type='submit' class='primary'>Save</button>" + reset
            form_body = (
                f"{field}<div class='btn-row'>{buttons}</div>"
                if s.kind == "textarea"
                else f"<div class='field-row'>{field}{buttons}</div>"
            )
            rows.append(
                f"<div class='setting' id='{e(s.key)}'><div>"
                f"<h4>{e(s.label)}<code>{e(s.key)}</code></h4><p>{e(s.help)}</p>"
                f"<p class='source'>{source}</p></div>"
                f"<form method='post' action='/settings'>"
                f"<input type='hidden' name='key' value='{e(s.key)}'>"
                f"<input type='hidden' name='next' value='/settings#{e(s.key)}'>"
                f"{form_body}</form></div>"
            )
        parts.append(card(e(group), "".join(rows), flush=True))
    return page(
        "Settings",
        "".join(parts),
        current="/settings",
        subtitle="The .env gives the defaults; a value saved here wins until you reset it. "
        "Changes apply to the next message, no restart needed. Keys, tokens and passwords stay "
        "in .env.",
    )


async def settings_post(request: web.Request) -> web.Response:
    bot = request.app[BOT]
    data = await request.post()
    key = data.get("key", "")
    try:
        if data.get("reset"):
            settings.update(key, None)
        else:
            settings.update(key, data.get("value", ""))
    except ValueError as error:
        return redirect("/settings?" + urlencode({"error": str(error)}))
    apply = getattr(bot, "apply_settings", None)
    if callable(apply):
        apply()  # rebuild the agent so new turns use the new settings
    return redirect(data.get("next") or "/settings")


# --- Static files ----------------------------------------------------------------------------------


async def static_file(request: web.Request) -> web.Response:
    """The dashboard's fonts and their license; nothing else is served from disk."""
    name = request.match_info["name"]
    content_type = STATIC_FILES.get(name)
    if content_type is None:
        raise web.HTTPNotFound()
    return web.Response(
        body=(STATIC_DIR / name).read_bytes(), content_type=content_type
    )
